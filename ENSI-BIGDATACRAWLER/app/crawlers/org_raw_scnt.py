import os
import re
import shutil
import time
from unicodedata import normalize
import json

import pandas as pd
import requests
from azure.storage.filedatalake import FileSystemClient
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from xlrd import open_workbook


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
    if year:
        return '{lnd}/{schema}__{table}/{year}'.format(lnd=LND, schema=schema, table=table, year=year)
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)


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


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .upper())


def main(**kwargs):
    tmp = '/tmp/org_raw_scnt/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        ulr_base = 'https://apisidra.ibge.gov.br/values'
        end_points = {
            # 'Taxa de variação do índice de volume trimestral':
            'scnt_volume_tx':
                '/t/5932/n1/all/v/all/p/all/c11255/all/d/v6561%201,v6562%201,v6563%201,v6564%201',
            # 'Série encadeada do índice de volume trimestral (Base: média 1995 = 100)':
            'scnt_volume_se':
                '/t/1620/n1/all/v/all/p/all/c11255/all/d/v583%202',
            # 'Série encadeada do índice de volume trimestral com ajuste sazonal (Base: média 1995 = 100)':
            'scnt_volume_saz_se':
                '/t/1621/n1/all/v/all/p/all/c11255/all/d/v584%202',
            # 'Valores a preços correntes':
            'scnt_valor_preco_correntes':
                '/t/1846/n1/all/v/all/p/all/c11255/all/d/v585%200',
            # 'Valores encadeados a preços de 1995':
            'scnt_valor_preco1995':
                '/t/6612/n1/all/v/all/p/all/c11255/all/d/v9318%202',
            # 'Valores encadeados a preços de 1995 com ajuste sazonal':
            'scnt_valor_preco1995_saz':
                '/t/6613/n1/all/v/all/p/all/c11255/all/d/v9319%202',
            # 'Contas econômicas trimestrais':
            'scnt_contas_economicas':
                '/t/2072/n1/all/v/all/p/all',
            # 'Conta financeira trimestral consolidada':
            'scnt_conta_financeira':
                '/t/2205/n1/all/v/all/p/all/c12116/all'
        }
        adl = authenticate_datalake()
        for table, end_point in end_points.items():
            url = ulr_base+end_point

            s = requests.Session()

            retry = Retry(connect=3, backoff_factor=0.5)
            adapter = HTTPAdapter(max_retries=retry)
            s.mount('http://', adapter)
            s.mount('https://', adapter)

            response = s.get(url)

            if response.status_code!=200:
                raise 'status_code not 200'

            response_list = response.json()
            dict_column_name = response_list.pop(0)
            response_json = json.dumps(response_list)

            for key, name_col in dict_column_name.items():
                dict_column_name[key] = __normalize_str(name_col)

            df = pd.read_json(response_json)

            year = df['D3N'].min()[-4::]+'-'+df['D3N'].max()[-4::]

            df.rename(columns=dict_column_name, inplace=True)

            __drop_directory(adl, schema='ibge', table=table)

            parquet_output = tmp + '{file}.parquet'.format(file=table)
            df.to_parquet(parquet_output, index=False)

            __upload_file(adl, schema='ibge', table=table, year=year, file=parquet_output)

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
