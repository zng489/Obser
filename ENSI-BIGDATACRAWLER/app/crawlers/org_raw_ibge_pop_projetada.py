import os
import re
import shutil
import time
import subprocess

from unicodedata import normalize
import json

import bs4
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


def get_cod_ano_proj(cod_dict):

    ano_list = []

    for k, v in cod_dict.items():
        if 'ANO' in v.upper():
            ano_list.append(k)

    return ano_list[-1]


def main(**kwargs):
    tmp = '/tmp/org_raw_ibge_pop_projetada/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        url_base = 'https://apisidra.ibge.gov.br/values'

        cod_anos_proj = [4336,12037,13242,49029,49030,49031,49032,49033,49034,49035,49036,49037,
            49038,49039,49040,49041,49042,49043,49044,49045,49046,116327,116329,116330,116331,116332,
            116334,116335,116336,116338,119270,49047,49048,49049,49050,49051,49052,49053,49054,49055,
            49056,49057,49058,49059,49060,49061,49062,49063,49064,49065,49066,49067,49068,49069,49070,
            49071,49072,49073,49074,49075,49076]

        end_points = {
            # Tabela 7358 - População, por sexo e idade
            'pop_projetada_pop_sex_idade':
                '/t/7358/n3/all/v/all/p/all/c2/all/c287/all/c1933/{}',
            # Tabela 7360 - Indicadores implícitos na projeção da população
            'pop_projetada_indicadores_implicitos':
                '/t/7360/n1/all/n2/all/n3/all/v/all/p/all/c1933/{}/d/v2493%202,v10605%202,v10606%202,v10607%202,v10608%202,v10609%202,v10610%202,v10611%202,v10612%202,v10613%202',
            # Tabela 7362 - Esperança de vida ao nascer e Taxa de mortalidade infantil, por sexo
            'pop_projetada_expectativa_txmortal':
                '/t/7362/n1/all/n2/all/n3/all/v/all/p/all/c2/6794/c1933/{}/d/v1940%202,v2503%202',
            # Tabela 7363 - Taxa específica de fecundidade, por grupo de idade da mãe
            'pop_projetada_txfecund':
                '/t/7363/n1/all/n2/all/n3/all/v/all/p/all/c950/all/c1933/{}/d/v10614%204',
            # Tabela 7365 - Proporção de pessoas, por grupo de idade
            'pop_projetada_pessoas_prop':
                '/t/7365/n1/all/n2/all/n3/all/v/all/p/all/c58/all/c1933/{}/d/v10615%202',
        }

        adl = authenticate_datalake()
        for table, end_point in end_points.items():
            for cod_ano_proj in cod_anos_proj:
                url = url_base+end_point.format(cod_ano_proj)

                proxyDict = { 
                            "http"  : os.environ.get('ftp_proxy'), 
                            "https" : os.environ.get('https_proxy'), 
                            "ftp"   : os.environ.get('ftp_proxy')
                            }
                retry = Retry(connect=3, backoff_factor=0.5)
                adapter = HTTPAdapter(max_retries=retry)
                s.mount('http://', adapter)
                s.mount('https://', adapter)

                response = requests.get(url, proxies=proxyDict)

                if response.status_code!=200:
                    raise 'status_code not 200'

                response_list = response.json()
                cod_ano = get_cod_ano_proj(response_list[0])
                del(response_list[0])

                response_json = json.dumps(response_list)

                df = pd.read_json(response_json)
                year_proj = str(df[cod_ano].max().item())

                __drop_directory(adl, schema='ibge', table=table, year=year_proj)

                parquet_output = tmp + '{file}.parquet'.format(file=table+year_proj)
                df.to_parquet(parquet_output, index=False)

                __upload_file(adl, schema='ibge', table=table, year=year_proj, file=parquet_output)

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
