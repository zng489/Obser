import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from unicodedata import normalize

import libarchive.public
import redis
import requests
from azure.storage.filedatalake import FileSystemClient


def authenticate_datalake() -> FileSystemClient:
    from azure.identity import ClientSecretCredential
    from azure.keyvault.secrets import SecretClient
    from azure.storage.filedatalake import DataLakeServiceClient

    credential = ClientSecretCredential(
        tenant_id=os.environ['AZURE_TENANT_ID'],
        client_id=os.environ['AZURE_CLIENT_ID'],
        client_secret=os.environ['AZURE_CLIENT_SECRET'])

    secret_client = SecretClient(
        vault_url=os.environ['AZURE_KEY_VAULT_URI'],
        credential=credential)

    blob_secret = secret_client.get_secret(os.environ['AZURE_KEY_VAULT_SECRET'])
    url = f'https://{os.environ["AZURE_ADL_STORE_NAME"]}.dfs.core.windows.net'

    adl = DataLakeServiceClient(account_url=url, credential=blob_secret.value)
    return adl.get_file_system_client(file_system=os.environ['AZURE_ADL_FILE_SYSTEM'])


def __check_path_exists(adl, path):
    try:
        next(adl.get_paths(path, recursive=False, max_results=1))
        return True
    except:
        return False


def __read_in_chunks(file_object, chunk_size=100 * 1024 * 1024):
    """Lazy function (generator) to read a file piece by piece.
    Default chunk size: 100Mb."""
    offset, length = 0, 0
    while True:
        data = file_object.read(chunk_size)
        if not data:
            break

        length += len(data)
        yield data, offset, length
        offset += chunk_size


def __upload_bs(adl, lpath, rpath):
    file_client = adl.get_file_client(rpath)
    try:
        with open(lpath, mode='rb') as file:
            for chunk, offset, length in __read_in_chunks(file):
                if offset > 0:
                    file_client.append_data(data=chunk, offset=offset)
                    file_client.flush_data(length)
                else:
                    file_client.upload_data(data=chunk, overwrite=True)
    except Exception as e:
        file_client.delete_file()
        raise e


def __upload_file(adl, schema, table, year, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])

    directory = __create_directory(schema, table, year)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=split[-1])
    __upload_bs(adl, file, adl_write_path)


def __download_file(adl: FileSystemClient, tmp, file):
    try:
        file_client = adl.get_file_client(file.name)
        download = file_client.download_file()

        local_file = tmp + file.filename
        with open(local_file, mode='wb') as local:
            for chunk in download.chunks():
                local.write(chunk)
        return local_file
    except Exception as e:
        logging.error(e)
        raise e


def __create_directory(schema=None, table=None, year=None):
    path = '{lnd}/{schema}/{table}'.format(lnd=LND, schema=schema, table=table)
    if year is not None:
        path = '{path}/{year}'.format(path=path, year=year)
    return path


def __drop_file(adl, path):
    try:
        file = adl.get_file_client(path)
        file.get_file_properties()
        file.delete_file()
    except:
        pass


def __list_recursive(adl: FileSystemClient, path: str, last_upload: int):
    if not __check_path_exists(adl, path):
        return []

    paths = [path for path in adl.get_paths(path) if not path.is_directory and path.name.endswith('.7z')]
    for path in paths:
        if isinstance(path.last_modified, str):
            path.last_modified = datetime.strptime(path.last_modified, '%a, %d %b %Y %H:%M:%S %Z')

        path.last_modified = int(path.last_modified.timestamp())
        if path.last_modified > last_upload:
            split = path.name.split('/')
            path.year, path.filename = split[-2:]
            yield path


def __list_7z_files(adl: FileSystemClient, schema: str, table: str, last_upload: int):
    path = __create_directory(schema=schema, table=table)
    generator = __list_recursive(adl, path, last_upload)
    files = sorted(generator, key=lambda key: key.last_modified)
    return files


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .upper())


def __call_redis(host, password, function_name, *args):
    db = redis.Redis(host=host, password=password, port=6379, db=0, socket_keepalive=True, socket_timeout=2)
    try:
        method_fn = getattr(db, function_name)
        return method_fn(*args)
    except Exception as _:
        raise _
    finally:
        db.close()


def __change_encoding(file):
    p1 = subprocess.Popen(shlex.split('file -bi "%s"' % file), stdout=subprocess.PIPE)
    p2 = subprocess.Popen(shlex.split("awk -F '=' '{print $2}'"), stdin=p1.stdout, stdout=subprocess.PIPE)
    encoding = p2.communicate()[0].decode("utf-8").strip("\n")

    # For dev purposes
    if DEBUG:
        os.system('mv "{file}" "{file}.old"'.format(file=file))
        os.system('head -n 500 "{file}.old" > "{file}"'.format(file=file))

    if encoding not in ['utf-8', 'us-ascii']:
        os.system('mv "{file}" "{file}.old"'.format(file=file))
        subprocess.Popen(shlex.split('iconv -f {f} -t utf-8 "{file}.old" -o "{output}"'
                                     .format(f=encoding, file=file, output=file))).communicate()


def __extract_files(tmp, file):
    with libarchive.public.file_reader(file) as entries:
        for entry in entries:
            txt_output = tmp + entry.pathname
            with open(txt_output, 'wb') as f:
                for block in entry.get_blocks():
                    f.write(block)

            yield txt_output


def main(**kwargs):
    tmp = '/tmp/org_raw_rais_vinculo'
    key_name = 'org_raw_rais_vinculo'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        host, passwd = kwargs['host'], kwargs['passwd']
        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if kwargs['reload'] is None and __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        adl = authenticate_datalake()
        files = __list_7z_files(adl, schema='me', table='rais_vinculo', last_upload=base_value)
        if len(files) == 0:
            return {'exit': 300, 'msg': 'without new files'}

        for file in files:
            tmp_file = '{tmp}/{ts}/'.format(tmp=tmp, ts=file.last_modified)
            os.makedirs(tmp_file, exist_ok=True)

            try:
                zip_output = __download_file(adl, tmp_file, file)
                for txt in __extract_files(tmp_file, zip_output):
                    __change_encoding(txt)
                    __upload_file(adl, schema='me', table='rais_vinculo', year=file.year, file=txt)

                    __drop_file(adl, file.name)
                    if kwargs['reload'] is None:
                        __call_redis(host, passwd, 'set', key_name, file.last_modified)
            except Exception as e:
                raise Exception(file.name, e)
            finally:
                shutil.rmtree(tmp_file)

        return {'exit': 200}
    except Exception as e:
        raise e
    finally:
        shutil.rmtree(tmp)


def execute(**kwargs):
    global DEBUG, LND

    DEBUG = bool(int(os.environ.get('DEBUG', 1)))
    LND = '/tmp/dev/lnd/crw' if DEBUG else '/lnd/crw'
    LND = '/tmp/dev/uld' if DEBUG else '/uld'

    start = time.time()
    metadata = {'finished_with_errors': False}
    try:
        log = main(**kwargs)
        if log is not None:
            metadata.update(log)
    except Exception as e:
        metadata['exit'] = 500
        metadata['finished_with_errors'] = True
        metadata['msg'] = '{class_type}: {error}'.format(class_type=type(e), error=str(e))
    finally:
        metadata['execution_time'] = time.time() - start

    if kwargs['callback'] is not None:
        requests.post(kwargs['callback'], json=metadata)

    return metadata


DEBUG, LND = None, None
if __name__ == '__main__':
    import dotenv
    from app import app

    dotenv.load_dotenv(app.ROOT_PATH + '/debug.env')
    exit(execute(host='localhost', passwd=None, reload=None, reset=False, callback=None))
