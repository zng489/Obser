# ALTERADO

import os
import re
import shlex
import shutil
import subprocess
import time
import zipfile
from threading import Timer
from unicodedata import normalize

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


def __find_download_link(base_value, context, **kwargs):
    for i in context:

        soup_url = i['href']

        res = requests.get(soup_url)
        soup = bs4.BeautifulSoup(res.text, features='html.parser')
        parent = soup.find(name='div', attrs={'class': 'tabs'})
        parent = parent.find_all(name='a')

        for j in parent:
            print('j = ' + str(j))
            if j.text == 'Sobre':
                continue

            new_url = str(soup_url + "/" + j.text)

            res = requests.get(new_url)
            soup = bs4.BeautifulSoup(res.text, features='html.parser')
            parent = soup.find(name='base')

            get_year = soup.find(name='meta', property="og:title")
            year = get_year.get('content')

            res = requests.get(parent.attrs['href'])
            soup = bs4.BeautifulSoup(res.text, features='html.parser')
            parent = soup.find(name='div', id='parent-fieldname-text')
            parent = parent.find(name='a', text='Escolas')
            anchor = parent.get('href')

            #year = anchor.split('/')[-3] get_year pelo link

            yield year, anchor



def __find_pages(context) -> list:
    hold_results = []

    soup_url = 'https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/indicadores-educacionais'
    res = requests.get(soup_url)
    soup = bs4.BeautifulSoup(res.text, features='html.parser')
    parent = soup.find_all(name='a', attrs={'class': 'govbr-card-content'})

    for div in parent:
        pages_struct = {}
        ct = div.find(name="span", attrs={"class": "titulo"}).text
        if ct in context:
            anchor = div.attrs["href"]
            pages_struct.update({"name": ct, "href": anchor})
            hold_results.append(pages_struct)

    return hold_results


def __extract_files(zip_output, tmp, prefixes):
    with zipfile.ZipFile(zip_output) as _zip:
        data_files = [file for file in _zip.filelist if
                      all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                             .encode('ASCII', 'ignore')
                                             .decode('ASCII')) for prefix in prefixes)]
        return [_zip.extract(member=data_file, path=tmp) for data_file in data_files
                if '$' not in data_file.filename]


def __download_file(url, zip_output):
    request = 'wget --progress=bar:force {url} -O {output} --no-check-certificate'.format(url=url, output=zip_output)
    print(request)
    process = subprocess.Popen(shlex.split(request), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(3600, process.kill)
    try:
        timer.start()
        process.communicate()
    finally:
        timer.cancel()


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
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


def __parse_xlsx(tmp, file):
    filename, ext = os.path.basename(file).split('.')

    excel = pd.ExcelFile(file)
    for sheet in excel.sheet_names:
        if sheet.lower().startswith('dic'):
            df = excel.parse(sheet_name=sheet, index_col=False, dtype=str)
            table = 'socioeconomico_dicionario'
        elif sheet.lower().startswith('ban') or sheet.lower().endswith('2015'):
            df = excel.parse(sheet_name=sheet, index_col=False, skiprows=9, dtype=str)
            table = 'socioeconomico'
        else:
            df = excel.parse(sheet_name=sheet, index_col=False, dtype=str)
            table = 'socioeconomico'

        df.columns = [__normalize_str(col) for col in df.columns]

        df['NOME_ARQUIVO'] = sheet
        normalized_sheet_name = __normalize_str(sheet)
        parquet_output = tmp + '{file}_{sheet}.{ext}.parquet'.format(file=filename, sheet=normalized_sheet_name,
                                                                     ext=ext)
        print(parquet_output)
        yield df, table, parquet_output


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_inse_nivel_socioeconomico'
    tmp = '/tmp/org_raw_inse_nivel_socioeconomico/'

    context_pages = ["Nível Socioeconômico (Inse)"]

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if __call_redis(host, passwd, 'exists', key_name):
            base_value = __call_redis(host, passwd, 'get', key_name)

        inside_context = __find_pages(context=context_pages)

        iterable = list(__find_download_link(base_value, context=inside_context, **kwargs))
        if len(iterable) == 0:
            return {'exit': 300, 'msg': 'without new files'}

        adl = authenticate_datalake()
        for year, url in iterable:
            extension = url.split('.')[-1]
            if extension == 'xlsx':
                arq = url.split('/')[-1]
                zip_output = tmp + arq
                __download_file(url, zip_output)
                files = [tmp+arq]

            else:
                zip_output = tmp + 'inse_socio_economic.zip'
                __download_file(url, zip_output)
                files = __extract_files(zip_output, tmp, ['.xlsx'])

            for file in files:
                print('files in file: '+file)

            assert len(files) == 1

            for df, table, parquet_output in __parse_xlsx(tmp, files[0]):
                df.to_parquet(parquet_output, index=False)
                print(df)
                __drop_directory(adl, schema='inep_inse', table=table, year=year)
                __upload_file(adl, schema='inep_inse', table=table, year=year, file=parquet_output)

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
