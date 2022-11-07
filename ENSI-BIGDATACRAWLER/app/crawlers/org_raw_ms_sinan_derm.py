import os
import re
import shlex
import shutil
import subprocess
import time
from threading import Timer
from unicodedata import normalize
from datetime import date
import gc

from simpledbf import Dbf5
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


def __create_directory(schema=None, table=None, year=None):
    return '{lnd}/{schema}__{table}/{year}'.format(lnd=LND, schema=schema, table=table, year=year)


def __drop_directory(adl, schema=None, table=None, year=None):
    adl_drop_path = __create_directory(schema=schema, table=table, year=year)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, year, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __download_file(url, output):
    request = 'wget --no-check-certificate --progress=bar:force {url} -O {output}'.format(url=url, output=output)
    process = subprocess.Popen(shlex.split(request), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(300, process.kill)
    try:
        timer.start()
        process.communicate()
    finally:
        timer.cancel()


def __normalize_str(_str):
    return re.sub(r'[,{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('|', '_')
                  .replace('/', '_')
                  .replace('.', '_')
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


def main(**kwargs):

    '''
    Esta funcao utiliza-se do algoritmo blast-dbf, que esta escrito na linguagem C
    e encontra-se no repositorio: https://github.com/eaglebh/blast-dbf

    os arquivos utilizados sao:
        blast.c
        blast.h
        blast-dbf.c
    Os mesmos foram movidos para o repositorio: app/bin/blast-dbf do bot

    '''

    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_ms_sinan_derm'
    tmp = '/tmp/org_raw_ms_sinan_derm/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        if kwargs['reload'] is not None:
            reload_year = [int(kwargs['reload'])]

        base_value = 0
        if __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        adl = authenticate_datalake()
        url = 'https://datasus.saude.gov.br/wp-content/ftp.php'

        year_today = date.today().year
        years = [*range(2010, year_today+1)] if kwargs['reload'] is None else reload_year

        set_workdir = False
        for  year in years:
            if year<=base_value and kwargs['reload'] is None:
                continue

            s = requests.Session()

            payload = {
                'tipo_arquivo[]': 'DERM',
                'modalidade[]': 1,
                'fonte[]': 'SINAN',
                'ano[]': year,
                'uf[]': 'BR'
            }

            response = s.post(url, data=payload)

            if len(response.json())>0:
                file_name = response.json()[0]['arquivo']
                url_file_ftp = response.json()[0]['endereco']

                output = tmp + file_name

                __download_file(url_file_ftp, output)

                if not set_workdir:
                    os.chdir('bin/blast-dbf')
                    set_workdir = True

                    os.system(f'gcc -o {tmp}blast-dbc-dbf blast.c blast-dbf.c')
                
                output_dbf = output.replace('dbc','dbf')
                os.system(f'{tmp}blast-dbc-dbf {output} {output_dbf}')

                
                dbf = Dbf5(output_dbf, codec='ISO-8859-1')
                df = dbf.to_dataframe()

                del dbf
                gc.collect()

                __drop_directory(adl, schema='ms_sinan', table='derm', year=year)

                parquet_output = tmp + '{file}.parquet'.format(file=os.path.basename(output))
                df.to_parquet(parquet_output, index=False)

                __upload_file(adl, schema='ms_sinan', table='derm', year=year, file=parquet_output)


            if kwargs['reload'] is None:
                __call_redis(host, passwd, 'set', key_name, year)

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
