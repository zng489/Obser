import os
import re
import shlex
import shutil
import subprocess
import zipfile
import time
import csv
from threading import Timer
from unicodedata import normalize
import json

import bs4
import chardet
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


def __find_download_link(base_names, **kwargs):
    
    url = 'https://dadosabertos.dataprev.gov.br/dataset/inss-comunicacao-de-acidente-de-trabalho-cat'

    cmd = 'wget --no-check-certificate --progress=bar:force -O- -e use_proxy=yes {url}'.format(url=url)
    stdin, stdout = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE).communicate()
    soup = bs4.BeautifulSoup(stdin, features='html.parser')
    
    if len(soup) == 0:
        raise Exception('Something bad is occurring')

    parent_all_tx = soup.find(name='div', attrs={'class': 'module-content'})

    for anchor in parent_all_tx.find_all(name='li', attrs={'class': 'resource-item'}):
        urls = []

        title = __normalize_str(anchor.find(name='a', attrs={'class': 'heading'}).get('title')).lower()
        href = anchor.find(name='a', attrs={'class': 'resource-url-analytics'}).get('href')

        if 'dicionario' in href:
            continue

        year = int(re.findall(re.compile("(?:(?:19|20)[0-9]{2})"), title)[0])
        if kwargs['reload'] is not None and year == int(kwargs['reload']):
            urls.append((title, href))

        elif href not in  base_names:
            urls.append((title, href))

        if len(urls) > 0:
            yield year, urls


def __extract_files(zip_output, tmp, prefixes):
    with zipfile.ZipFile(zip_output) as _zip:
        data_files = [file for file in _zip.filelist if
                      all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                             .encode('ASCII', 'ignore')
                                             .decode('ASCII')) for prefix in prefixes)]

        return [_zip.extract(member=data_file, path=tmp) for data_file in data_files
                if '$' not in data_file.filename]


def __download_file(url, output):
    request = 'wget --no-check-certificate --progress=bar:force -e use_proxy=yes {url} -O {output}'.format(url=url, output=output)
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


def __parse_csv(base_path, output):
    df = pd.DataFrame()
    header = None

    rawdata = open(output, 'rb').readlines()
    
    result = chardet.detect(rawdata[0]+rawdata[1])
    encoding = result['encoding']


    with open(output, 'r', encoding=encoding) as infile:
        inputs = csv.reader(infile)

        if not header:

            for row in inputs:
                if inputs.line_num==1:
                    header = __normalize_str(row[0].strip()).split(';')
                    break

            skip = inputs.line_num
    for index, name in enumerate(header):
        for index_ in range(index+1, len(header)):

            if name==header[index_]:
                header[index_]=header[index_]+'_'

    df = pd.read_csv(output, sep=';', header=None, names=header, skiprows=skip, index_col=False, encoding=encoding)
    df['NOME_ARQUIVO'] = os.path.basename(output)
    
    for col in df.columns:
        df[col] = df[col].astype(str)
        
    return df


def files_to_parse(list_file: list):
    list_return = []
    files_not_parse = ('CPC', 'CAPES', 'ATUALI')
    for file in list_file:
        not_contain = 0
        for not_file in files_not_parse:

            if not_file not in file.upper():
                not_contain+=1
                
        if not_contain==len(files_not_parse):
            list_return.append(file)

    return list_return


def find_in_list(list_params, text):
    for index, params in enumerate(list_params):
        for param in params:
            if param in text:
                find = list_params.pop(index)[0]

                return find, list_params

    return False, list_params


def rename_file(name, year):

    trimestre = {
        '.1': ('jan', 'mar'),
        '.2': ('abr', 'jun'),
        '.3': ('jul', 'set'),
        '.4': ('out', 'dez')
    }
    for trim, months in trimestre.items():
        if months[0] in name and months[1] in name:
            return year+trim


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_names = 'org_raw_inss_cat'
    tmp = '/tmp/org_raw_inss_cat/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_names)

        base_names = []
        if __call_redis(host, passwd, 'exists', key_names):
            base_names = json.loads(__call_redis(host, passwd, 'get', key_names))

        adl = authenticate_datalake()
        for year, urls in __find_download_link(base_names, **kwargs):
            for file_name, url in urls:
                output = tmp + file_name + '.' + url.split('.')[-1]

                __download_file(url, output)
                __drop_directory(adl, schema='cat', table='inss', year=year)

                if '.csv' in output:

                    df = __parse_csv(tmp, output)

                elif '.zip' in output:
                    
                    file = __extract_files(output, tmp, ['.csv'])
                    assert len(file) == 1
                    
                    df = __parse_csv(tmp, file[0])

                parquet_output = tmp + '{file}.parquet'.format(file=rename_file(file_name, str(year)))
                df.to_parquet(parquet_output, index=False)
                __upload_file(adl, schema='inss', table='cat', year=year, file=parquet_output)


                if kwargs['reload'] is None:
                    base_names.append(url)
                    __call_redis(host, passwd, 'set', key_names, json.dumps(base_names))

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
