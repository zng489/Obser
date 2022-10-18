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


def __find_download_link(base_value, **kwargs):
    res = requests.get('https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/indicadores-educacionais/adequacao-da-formacao-docente')
    soup = bs4.BeautifulSoup(res.text, features='html.parser')
    parent = soup.find(name='div', attrs={'class': 'tabs-content'})
    parent_divs = parent.find_all(name='div', attrs={'class': 'tab-content'})

    for div in parent_divs[::-1]:
        url = []
        if div.attrs['data-id'] != 'Sobre':
            year, url_ano = div.attrs['data-id'], div.attrs['data-url']
            url.append((year, url_ano))
        print('year')
        print(year)
        print('urls')
        print(url)
        urls = []

        for year, res in url:
            res = requests.get(res)
            soup = bs4.BeautifulSoup(res.text, features='html.parser')
            parent = soup.find(name='div', attrs={'data-nav': 'adequacao-da-formacao-docente'})

            for anchor in parent.find_all('a'):
                txt = anchor.text
                txt = txt.split('(')[0].strip()

                if kwargs['reload'] is not None and year == int(kwargs['reload']):
                    urls.append((__normalize_str(txt).lower(), anchor.attrs['href']))
                elif int(year) > base_value:
                    urls.append((__normalize_str(txt).lower(), anchor.attrs['href']))
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


def __download_file(url, zip_output):
    request = 'wget --progress=bar:force {url} -O {output} --no-check-certificate'.format(url=url, output=zip_output)
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


def __merge_multi_index_header(wk_sheet, header_rows):
    multi_index_header = []
    for header_row in header_rows:
        row = wk_sheet.row(header_row - 1)
        multi_index_header.append(list(cell.value for cell in row))

    header, forward_fill = [], []
    for i in range(0, len(multi_index_header[-1])):
        col_names = []
        for k in range(0, len(multi_index_header)):
            col = multi_index_header[k][i]
            if isinstance(col, float):
                col = str(int(col))

            if len(col) > 50:
                col = col.split(' ')[0]

            col = __normalize_str(col)
            if k == 0 and len(col) > 0:
                forward_fill = ['' for _ in range(0, len(multi_index_header))]

            if len(col) == 0:
                col = forward_fill[k]
            else:
                forward_fill[k] = col

            if len(col) > 0:
                col_names.append(col)
        header.append('_'.join(col_names))

    return header


def __parse_xlsx(files, header_rows):
    for file in files:
        workbook = open_workbook(file)
        sheet = next(iter(workbook.sheet_names()))

        header = __merge_multi_index_header(workbook.sheet_by_name(sheet), header_rows)
        df = pd.read_excel(file, sheet_name=sheet, header=None, names=header, index_col=False, skiprows=11)

        df['NOME_ARQUIVO'] = file.split('/')[-1]
        for col in df.columns:
            df[col] = df[col].astype(str)

        df = df[df['ANO'].str.isdigit()]
        yield df


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_inse_adf'
    tmp = '/tmp/org_raw_inse_adf/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        adl = authenticate_datalake()
        for year, urls in __find_download_link(base_value, **kwargs):
            for table, url in urls:
                zip_output = tmp + '%s_%s.zip' % (year, table)
                __download_file(url, zip_output)

                files = __extract_files(zip_output, tmp, ['.xlsx'])
                assert len(files) > 0

                __drop_directory(adl, schema='inep_afd', table=table, year=year)
                df = pd.concat(__parse_xlsx(files, header_rows=[7, 8, 9, 10]), axis='index', ignore_index=True)

                parquet_output = tmp + '{file}.parquet'.format(file=table)
                df.to_parquet(parquet_output, index=False)
                __upload_file(adl, schema='inep_afd', table=table, year=year, file=parquet_output)

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
