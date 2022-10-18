import os
import re
import shutil
import time
from unicodedata import normalize

import pandas as pd
import redis
import requests
from azure.storage.filedatalake import FileSystemClient
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry


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

    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __normalize_cols(cols):
    return [__normalize_str(col) for col in cols]


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .upper())


def __get_last_upload():
    s = requests.Session()
    retry = Retry(connect=3, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount('http://', adapter)
    s.mount('https://', adapter)

    url = 'https://sidra.ibge.gov.br/Ajax/JSon/Tabela/1/1737?versao=-1'

    res = requests.get(url)
    if res.ok:
        content = res.json()
        if 'Periodos' in content:
            periods = content['Periodos']
            if 'Periodos' in periods:
                return max(map(lambda v: v['Codigo'], periods['Periodos']))

    raise Exception('__get_last_upload(): API error', res.text)


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
    tmp = '/tmp/org_raw_ipca/'
    key_name = 'org_raw_ipca'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        host, passwd = kwargs['host'], kwargs['passwd']
        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if kwargs['reload'] is None and __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        last_upload = __get_last_upload()
        if last_upload > base_value:
            adl = authenticate_datalake()

            file_output, filename = __download_file(tmp)
            print(file_output)
            if file_output is None:
                return {'exit': 500}

            df = pd.read_excel(file_output, skiprows=1, skipfooter=1, usecols=[0, 1, 2, 3, 5],
                               names=['NIVEL', 'CODIGO_PAIS', 'PAIS', 'MES_ANO', 'VALOR_IPCA'])


            df[['NIVEL', 'CODIGO_PAIS', 'PAIS']] = df[['NIVEL', 'CODIGO_PAIS', 'PAIS']].ffill()
            df['CODIGO_PAIS'] = df['CODIGO_PAIS'].astype(int)
            df[df.columns] = df[df.columns].astype(str)

            parquet_output = tmp + filename + '.parquet'
            df.to_parquet(parquet_output, index=False)
            print(parquet_output)

            __drop_directory(adl, schema='ibge', table='ipca')
            __upload_file(adl, schema='ibge', table='ipca', file=parquet_output)

            if kwargs['reload'] is None:
                __call_redis(host, passwd, 'set', key_name, last_upload)

            return {'exit': 200}
        else:
            return {'exit': 300, 'msg': 'without new files'}
    except Exception as e:
        raise e
    finally:
        shutil.rmtree(tmp)


def __download_file(tmp):
    url = 'https://sidra.ibge.gov.br/geratabela?format=xlsx&name=tabela1737.xlsx' \
          '&terr=NCS&rank=-&query=t/1737/n1/all/v/2266/p/all/d/v2266%2013/l/,,t%2Bp%2Bv'
    res = requests.get(url)
    if res.ok:
        filename = res.headers['content-disposition'].split('=')[-1]
        file_output = tmp + filename
        with open(file_output, mode='wb+') as file:
            file.write(res.content)
        return file_output, filename
    return None, None


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
