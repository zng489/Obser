import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from threading import Timer
from unicodedata import normalize

import bs4
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


def __create_directory(schema=None, table=None, year=None):
    return '{lnd}/{schema}__{table}/{year}'.format(lnd=LND, schema=schema, table=table, year=year)


def __upload_file(adl, schema, table, year, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])

    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))
    if file_type[-1] == 'txt':
        __change_encoding(file)

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __list_ftp_dir(url):
    cmd = 'wget --progress=bar:force -O- -e use_proxy=yes {url}'.format(url=url)
    stdin, stdout = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE).communicate()
    soup = bs4.BeautifulSoup(stdin, features='html.parser')
    if len(soup) == 0:
        raise Exception('Something bad is occurring')

    for filename in soup.find_all('td', attrs={'class': 'filename'}):
        yield filename.text


def __download_file(url, zip_output):
    cmd = 'wget --progress=bar:force -e use_proxy=yes {url} -O {output}'.format(url=url, output=zip_output)
    process = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(3600, process.kill)
    try:
        timer.start()
        process.communicate()
    finally:
        timer.cancel()


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII').replace(' ', '_')
                  .upper())


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


def __call_redis(host, password, function_name, *args):
    db = redis.Redis(host=host, password=password, port=6379, db=0, socket_keepalive=True, socket_timeout=2)
    try:
        method_fn = getattr(db, function_name)
        return method_fn(*args)
    except Exception as _:
        raise _
    finally:
        db.close()


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_microdados_caged'
    tmp = '/tmp/org_raw_microdados_caged/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            keys = __call_redis(host, passwd, 'keys', key_name + ":*")
            for key in keys:
                __call_redis(host, passwd, 'delete', key.decode())

        base_ftp_server = 'ftp://ftp.mtps.gov.br/pdet/microdados/CAGED/'
        years = sorted(filter(lambda _dir: _dir.isdigit() and int(_dir) > 2014, __list_ftp_dir(base_ftp_server)))

        if kwargs['reload'] is not None:
            reload_year = str(kwargs['reload'])
            if reload_year not in years:
                return {'exit': 404, 'msg': '%s not found' % reload_year, 'available_years': years}
            years = [reload_year]

        adl = authenticate_datalake()
        schema, table = 'me', 'caged'
        for year in years:
            key_name_year = '{key}:{year}'.format(key=key_name, year=year)

            last_file_month = -1
            if kwargs['reload'] is None:
                if __call_redis(host, passwd, 'exists', key_name_year):
                    last_file_month = int(__call_redis(host, passwd, 'get', key_name_year))

            ftp_folder = '{base}/{year}'.format(base=base_ftp_server, year=year)
            for file in __list_ftp_dir(ftp_folder):
                file_month = file.split('_')[-1]
                file_month = file_month.split('.')[0]
                file_month = int(datetime.strptime(file_month, '%m%Y').timestamp())
                if last_file_month >= file_month:
                    continue

                ftp_file = '{base}/{file}'.format(base=ftp_folder, file=file)
                zip_output = tmp + file
                __download_file(ftp_file, zip_output)

                with libarchive.public.file_reader(zip_output) as entries:
                    for entry in entries:
                        txt_output = tmp + entry.pathname
                        with open(txt_output, 'wb') as f:
                            for block in entry.get_blocks():
                                f.write(block)

                __upload_file(adl, schema, table, year, txt_output)
                if kwargs['reload'] is None:
                    __call_redis(host, passwd, 'set', key_name_year, file_month)

                if os.path.exists(zip_output):
                    os.remove(zip_output)

                if os.path.exists(txt_output):
                    os.remove(txt_output)

        return {'exit': 200}
    except Exception as e:
        raise e
    finally:
        shutil.rmtree(tmp)


def execute(**kwargs):
    global DEBUG, LND

    DEBUG = bool(int(os.environ.get('DEBUG', 1)))
    LND = '/tmp/dev/lnd/crw' if DEBUG else '/lnd/crw'

    start = time.time()
    metadata = {'finished_with_errors': False}
    try:
        log = main(**kwargs)
        if log is not None:
            metadata.update(log)
    except Exception as e:
        metadata['exit'] = 500
        metadata['finished_with_errors'] = True
        metadata['msg'] = str(e)
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
