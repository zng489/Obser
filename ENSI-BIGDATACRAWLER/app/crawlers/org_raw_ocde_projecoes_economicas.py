import os
import re
import time
import shutil
import requests
from functools import partial
from unicodedata import normalize

import pandas as pd
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


def __normalize(string: str) -> str:
    return normalize('NFKD', string.strip()).encode('ASCII', 'ignore').decode('ASCII')


def __normalize_replace(string: str) -> str:
    return re.sub(r'[.,;:{}()\n\t=]', '', __normalize(string)
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('|', '_')
                  .replace('/', '_')
                  .upper())


def __check_path_exists(adl, path) -> bool:
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


def __upload_bs(adl, lpath, rpath) -> None:
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


def __create_directory(schema=None, table=None, year=None) -> str:
    if year:
        return f'{LND}/{schema}__{table}/{year}'
    return f'{LND}/{schema}__{table}'


def __drop_directory(adl, schema=None, table=None, year=None) -> None:
    adl_drop_path = __create_directory(schema=schema, table=table, year=year)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, file, year=None) -> None:
    split = os.path.basename(file).split('.')
    filename = __normalize_replace(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = f'{directory}/{filename}.{file_type}'

    __upload_bs(adl, file, adl_write_path)


def download_file(url: str, output_path: str, filename: str) -> None:
    r = requests.get(url)
    if r.status_code == 200:
        with open(output_path+filename,'wb') as f:
            f.write(r.content)
    else:
        raise Exception(f'status_code not 200. Server message: Code {r.status_code} | {r.text}')


def main(**kwargs):
    """
    This function is the main entrypoint execute this bot.

    :param kwargs: dict with all key-value pairs needed as configurations to run the bot.
    """

    adl = authenticate_datalake()

    schema = 'ocde'
    table = 'projecoes_economicas'

    key_name = f'org_raw_{schema}_{table}'
    tmp = f'/tmp/{key_name}/'
    os.makedirs(tmp, mode=0o777, exist_ok=True)

    url_part1 = 'https://stats.oecd.org/sdmx-json/data/DP_LIVE/.'
    url_part2 = '.../OECD?contentType=csv&detail=code&separator=comma&csv-lang=en'
    cod_indicators = {
        'Real GDP Forecast': 'REALGDPFORECAST',
        'Inflation forecast': 'CPIFORECAST',
        'Investiment forecast': 'GFCFFORECAST',
        'Current acount balance forecast': 'BOPFORECAST',
        'Unemployment rate forecast': 'UNEMPFORECAST',
        'Trade in goods and services forecast': 'TRADEGOODSERVFORECAST'
    }

    try:
        for cod in list(cod_indicators.values()):
            url = url_part1+cod+url_part2
            filename = cod+'.csv'
            download_file(url=url, output_path=tmp, filename=filename)

        __drop_directory(adl, schema=schema, table=table)

        list_files = sorted([tmp+f for f in os.listdir(tmp) if f.endswith('.csv')])

        dict_dfs = {f:pd.read_csv(f) for f in list_files}

        for f in dict_dfs.keys():
            filter_ = dict_dfs[f].notnull().sum(axis=0)==0
            #dict_dfs[f].drop(columns=dict_dfs[f].columns[filter_], inplace=True)

            dict_dfs[f].columns = [__normalize_replace(cl) for cl in dict_dfs[f].columns]

            parquet_output = f.replace('.csv','.parquet')
            dict_dfs[f].to_parquet(parquet_output, index=False)
            __upload_file(adl, schema=schema, table=table, file=parquet_output)
            os.remove(f)

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
