import os
import re
import shlex
import shutil
import subprocess
import time
import zipfile_deflate64 as zipfile
from threading import Timer
from unicodedata import normalize

import bs4
import chardet
import unicodecsv
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


def __download_file(url, zip_output):
    cmd = 'wget --no-check-certificate --progress=bar:force -e use_proxy=yes {url} -O {output}'.format(url=url, output=zip_output)
    process = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(1000, process.kill)

    try:
        timer.start()
        process.communicate()
    finally:
        timer.cancel()


def __extract_files(zip_output, tmp, prefixes):

    with zipfile.ZipFile(zip_output) as _zip:
        data_files = [file for file in _zip.filelist if
                    all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                            .encode('ASCII', 'ignore')
                                            .decode('ASCII')) for prefix in prefixes)]

        return [_zip.extract(member=data_file, path=tmp) for data_file in data_files
                if '$' not in data_file.filename]



def __parse_csv(base_path, output):
    df = pd.DataFrame()
    header = None

    rawdata = open(output, 'rb').readlines()

    result = chardet.detect(rawdata[0]+rawdata[1])
    charenc = result['encoding']
    encoding = 'utf8' if 'UTF' in charenc.upper() else ('ISO-8859-1' if 'ISO' in charenc.upper() else 'cp860')

    with open(output, 'rb') as infile:
        inputs = unicodecsv.reader(infile, encoding=encoding)

        if not header:

            for row in inputs:
                if inputs.line_num==1:
                    header = [__normalize_str(v.strip()) for v in row[0].split(';')]
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


def __normalize_str(_str):
    return re.sub(r'[,{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('–', '_')
                  .replace('|', '_')
                  .replace('/', '_')
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


def exists_text(text):

    pnp = {
        'MICRODADOS_MATRICULAS':'pnp_mat',
        'MICRODADOS_SERVIDORES':'pnp_ser',
        'MICRODADOS_EFICIENCIA':'pnp_efi',
        'MICRODADOS_FINANCEIRO':'pnp_fin'
    }

    for key, value in pnp.items():
        if key in text:
            return value
    return None


def __find_download_link():    
    last_update = None

    url_base = 'https://dadosabertos.mec.gov.br/'
    end_point = 'pnp?start=0'

    while True:
        res = requests.get(url_base + end_point)
        soup = bs4.BeautifulSoup(res.text, features='html.parser')

        items = soup.find(name='div', attrs={'class': 'items-leading'})

        results_items = items.findAll(name='div', attrs={'class': 'tileItem'})

        for div in results_items:
            
            results_a = div.findAll('a')

            pnp = exists_text(__normalize_str(results_a[0].text))
            if pnp:
                year = __normalize_str(results_a[0].text).strip().split('_')[0]
                link =  results_a[1].get('href')
                last_update = div.find(name='div', attrs={'class': 'span2 tileInfo'}).find('li').text

                yield year, pnp, link, last_update


        prox_page = soup.find(name='li', attrs={'class': 'pagination-next'})
        if prox_page.find('a'):
            end_point = prox_page.find('a').get('href')
        else:
            break


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_mec_pnp_ept'
    tmp = '/tmp/org_raw_mec_pnp_ept/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            keys = __call_redis(host, passwd, 'keys', key_name + ":*")
            for key in keys:
                __call_redis(host, passwd, 'delete', key.decode())

        if kwargs['reload'] is not None:
            reload_year = str(kwargs['reload'])

        adl = authenticate_datalake()
        schema = 'mec'
        for year, table, link, last_update in __find_download_link():
   
            key_name_year_table = '{key}:{year}:{table}'.format(key=key_name, year=year, table=table)

            
            if kwargs['reload'] is None:
                if __call_redis(host, passwd, 'exists', key_name_year_table):
                    last_file_year = __call_redis(host, passwd, 'get', key_name_year_table).decode()
                    if last_file_year==last_update:
                        continue

            if kwargs['reload'] is not None and year!=reload_year:
                continue

            file_output = tmp + os.path.basename(link)
            __download_file(link, file_output)

            if file_output.endswith('.zip'):
                file_output = __extract_files(file_output, tmp, ['.csv'])[0]
            
            df = __parse_csv(tmp, file_output)

            __drop_directory(adl, schema, table=table, year=year)

            parquet_output = tmp + '{file}.parquet'.format(file=os.path.basename(file_output).replace('.csv',''))
            df.to_parquet(parquet_output, index=False)

            __upload_file(adl, schema, table=table, year=year, file=parquet_output)

            if kwargs['reload'] is None:
                __call_redis(host, passwd, 'set', key_name_year_table, last_update)


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
