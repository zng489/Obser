import os
import re
import shlex
import subprocess
import shutil
import time
import zipfile
from unicodedata import normalize
from threading import Timer

import bs4
import pandas as pd
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


def __create_directory(schema=None, table=None):
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)


def __drop_directory(adl, schema=None, table=None):
    adl_drop_path = __create_directory(schema=schema, table=table)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])

    directory = __create_directory(schema=schema, table=table)
    adl_write_path = '{directory}/{file}.parquet'.format(directory=directory, file=filename)

    __upload_bs(adl, file, adl_write_path)


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII').replace(' ', '_')
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

def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')
    url = 'ftp://geoftp.ibge.gov.br/organizacao_do_territorio/estrutura_territorial/divisao_territorial'
    key_name = 'org_raw_estrutura_territorial'
    tmp = '/tmp/org_raw_estrutura_territorial/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        ls = list(__list_ftp_dir(url))

        processing_year = None
        if kwargs['reload'] is None:
            if __call_redis(host, passwd, 'exists', key_name):
                processing_year = __call_redis(host, passwd, 'get', key_name).decode('utf-8')

            # If redis cached year is the last available year 'ls[-1]'
            if processing_year is None or processing_year != ls[-1]:
                processing_year = ls[-1]
            else:
                return {'exit': 300, 'msg': 'FTP did not updated a new year'}
        else:
            processing_year = kwargs['reload']
            if processing_year not in ls:
                return {'exit': 404, 'msg': '%s not found' % processing_year, 'available_years': ls}

        # List the files in the most recent year
        files_base_url = '/'.join([url, processing_year])
        files_base_ls = __list_ftp_dir(files_base_url)
        _zip = [file for file in files_base_ls if file.endswith('.zip')]
        if len(_zip) > 0:
            _zip = _zip[0]

        zip_output = tmp + _zip
        # Download the zip file
        __download_file('/'.join([files_base_url, _zip]), zip_output)

        xls_output = None
        with zipfile.ZipFile(zip_output) as _zip:
            zip_files = _zip.filelist
            for zip_file in zip_files:
                if zip_file.filename.lower().endswith('municipio.xls'):
                    xls_output = _zip.extract(zip_file, tmp)
                    break

        df = pd.read_excel(xls_output, index_col=False)
        df.columns = [__normalize_str(col) for col in df.columns]

        filename, ext = os.path.basename(xls_output).split('.')
        ext = ext.lower()
        filename = '%s_%s.%s.parquet' % (filename, processing_year, ext)

        parquet_output = tmp + filename
        df.to_parquet(parquet_output, index=False)

        adl = authenticate_datalake()
        schema, table = 'ibge', 'relatorio_dtb_brasil_municipio'
        __drop_directory(adl, schema, table)
        __upload_file(adl, schema, table, parquet_output)

        if kwargs['reload'] is None:
            __call_redis(host, passwd, 'set', key_name, processing_year)

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
