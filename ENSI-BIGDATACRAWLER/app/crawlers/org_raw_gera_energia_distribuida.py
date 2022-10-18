import os
import re
import shutil
import codecs
import zipfile
import time
import csv
from unicodedata import normalize
from datetime import datetime
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


def __create_directory(schema=None, table=None, year=None):
    if year:
        return '{lnd}/{schema}__{table}/{year}'.format(lnd=LND, schema=schema, table=table, year=year)
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)


def __drop_directory(adl, schema=None, table=None, year=None):
    adl_drop_path = __create_directory(schema=schema, table=table, year=year)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, file, year=None):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __find_download_link(last_update):
    
    last_update = None
    result_url_data = None

    url_base = 'https://dadosabertos.aneel.gov.br'
    url_geracao_distribuida = url_base + '/dataset/relacao-de-empreendimentos-de-geracao-distribuida'

    res = requests.get(url_geracao_distribuida, verify=False)
    soup = bs4.BeautifulSoup(res.text, features='html.parser')

    links = soup.findAll(name='a', attrs={'class': 'resource-url-analytics'})
    for i in links:
        if i['href'].endswith('csv'):
            result_url_data = i['href']

    last_update = soup.find(name='span', attrs={'class': 'automatic-local-datetime'})
    last_update = last_update['data-datetime'][0:19]
    last_update = datetime.strptime(last_update, '%Y-%m-%dT%H:%M:%S')
    last_update = int(last_update.strftime('%Y%m%d%H%M%S'))

    return last_update, result_url_data


def __extract_files(zip_output, tmp, prefixes):
    with zipfile.ZipFile(zip_output) as _zip:
        data_files = [file for file in _zip.filelist if
                      all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                             .encode('ASCII', 'ignore')
                                             .decode('ASCII')) for prefix in prefixes)]

        return [_zip.extract(member=data_file, path=tmp) for data_file in data_files
                if '$' not in data_file.filename]


def __download_file(url, zip_output):
    session = requests.Session()
    request = session.get(url, verify=False, timeout=3600)

    with open(zip_output, 'wb') as file:
        file.write(request.content)


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


def __parse_csv(output):
    header = None

    with codecs.open(output, 'rU', 'utf-16') as infile:
        inputs = csv.reader(infile)

        if not header:

            for row in inputs:
                if inputs.line_num == 1:
                    header = __normalize_str(row[0].strip()).split(';')
                    break

            skip = inputs.line_num
    for index, name in enumerate(header):
        for index_ in range(index + 1, len(header)):

            if name == header[index_]:
                header[index_] = header[index_] + '_'

    df = pd.read_csv(output, sep=';', header=None, names=header, skiprows=skip, index_col=False, encoding='utf-16')
    df['NOME_ARQUIVO'] = os.path.basename(output)
    
    for col in df.columns:
        df[col] = df[col].astype(str)
        
    return df


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_names = 'org_raw_aneel_gera_energia_distrib'
    tmp = '/tmp/org_raw_aneel_gera_energia_distrib/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_names)

        last_base = 0
        if __call_redis(host, passwd, 'exists', key_names):
            last_base = int(__call_redis(host, passwd, 'get', key_names).decode('utf-8'))

        adl = authenticate_datalake()
        last_update, url = __find_download_link(last_base)

        if last_base < last_update:
            file_name = 'gera_energia_distribuida_{last_update}'.format(last_update=last_update)
            output = '{tmp}{arq}.{ext}'.format(tmp=tmp, arq=file_name, ext=url.split('.')[-1])

            __download_file(url, output)

            df = __parse_csv(output)

            __drop_directory(adl, schema='aneel', table='gera_energia_distrib')

            parquet_output = tmp + '{file}.parquet'.format(file=file_name)
            df.to_parquet(parquet_output, index=False)
            __upload_file(adl, schema='aneel', table='gera_energia_distrib', file=parquet_output)

            if kwargs['reload'] is None:
                last_base = last_update
                __call_redis(host, passwd, 'set', key_names, str(last_base))

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