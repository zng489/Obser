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


def __call_redis(host, password, function_name, *args):
    db = redis.Redis(host=host, password=password, port=6379, db=0, socket_keepalive=True, socket_timeout=2)
    try:
        method_fn = getattr(db, function_name)
        return method_fn(*args)
    except Exception as _:
        raise _
    finally:
        db.close()


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
    res = requests.get('https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/indicadores-educacionais/taxas-de-rendimento')
    soup = bs4.BeautifulSoup(res.text, features='html.parser')
    parent_all_tx = soup.find(name='div', attrs={'class': 'tabs-content'})
    parent_all_tx_divs = parent_all_tx.find_all(name='div', attrs={'class': 'tab-content'})

    for div_all_tx in parent_all_tx_divs[::-1]:
        if not div_all_tx['data-id'].isdigit():
            continue

        year = int(div_all_tx['data-id'])
        url_tx_year = div_all_tx['data-url']

        res = requests.get(url_tx_year)
        soup = bs4.BeautifulSoup(res.text, features='html.parser')
        parent_year_tx = soup.find(name='div', attrs={'data-nav': 'taxas-de-rendimento'})

        urls = []

        for anchor in parent_year_tx.find_all('a'):
            if kwargs['reload'] is not None and year == int(kwargs['reload']):
                urls.append((__normalize_str(anchor.text).lower(), anchor.attrs['href']))
            elif year > base_value:
                urls.append((__normalize_str(anchor.text).lower(), anchor.attrs['href']))
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
    session =  requests.Session()
    request = session.get(url, timeout=3600)

    with open(zip_output, 'wb') as file:
        file.write(request.content)


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('/', '_')
                  .replace('.', '_')
                  .upper())


def __merge_multi_index_header(wk_sheet):
    multi_index_header = []
    columns_name_num = []

    #select rowns from header 
    header_rows = []
    for nrow in range(0, wk_sheet.nrows):
        if not isinstance(wk_sheet.cell_value(nrow,0), float):
            header_rows.append(nrow)
        else:
            break
    skip_header = len(header_rows)

    #select column names
    for nrow in header_rows[::-1]:
        col_value_year = wk_sheet.cell_value(nrow,0)
        if len(col_value_year)!=0 and nrow==max(header_rows):
            continue
        if len(col_value_year)==0:
            columns_name_num.append(nrow)
            continue
        columns_name_num.append(nrow)
        break

    for column_name_num in columns_name_num[::-1]:
        row = wk_sheet.row(column_name_num)
        multi_index_header.append(list(cell.value for cell in row))

    header, forward_fill = [], []
    if multi_index_header:
        for i in range(1, len(multi_index_header[-1])):
            for k in range(0, len(multi_index_header)):
                if not multi_index_header[k][i]:
                    multi_index_header[k][i] = multi_index_header[k][i-1]

        for i in range(0, len(multi_index_header[-1])):
            col_names = []
            for k in range(0, len(multi_index_header)):
                col = multi_index_header[k][i]
                if isinstance(col, float):
                    col = str(int(col))

                col = __normalize_str(col.strip())
                if k == 0 and len(col) >= 0:
                    forward_fill = ['' for _ in range(0, len(multi_index_header))]

                if len(col) == 0:
                    col = forward_fill[k]
                else:
                    forward_fill[k] = col

                if len(col) > 0:
                    col_names.append(col)
            header.append('_'.join(col_names))

    return header, skip_header

    
def __parse_xlsx(file):
    workbook = open_workbook(file)

    df = pd.DataFrame()
    for sheet in workbook.sheet_names():
        header, skip_header = __merge_multi_index_header(workbook.sheet_by_name(sheet))

        _df = pd.read_excel(file, sheet_name=sheet, header=None, names=header, index_col=False, skiprows=skip_header)
        if not "ANO" in _df.columns:
            continue
        
        df = pd.concat([df, _df], axis='index', ignore_index=True)

    split = os.path.basename(file).split('.')
    df['NOME_ARQUIVO'] = __normalize_str(split[0])

    for col in df.columns:
        df[col] = df[col].astype(str)

    df = df[df['ANO'].str.isdigit()]
    return df


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_inse_taxa_rendimento'
    tmp = '/tmp/org_raw_inse_taxa_rendimento/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        adl = authenticate_datalake()
        for year, urls in __find_download_link(base_value, **kwargs):
            for table_all, url in urls:
                zip_output = tmp + '%s_%s.zip' % (year, table_all)
                __download_file(url, zip_output)

                files = __extract_files(zip_output, tmp, ['.xls'])
                assert len(files) > 0

                __drop_directory(adl, schema='inep_taxa_rendimento', table=table_all, year=year)
                for file in files:

                    df = __parse_xlsx(file)
                    table = df['NOME_ARQUIVO'][1].lower()
                    parquet_output = tmp + '{file}.parquet'.format(file=table)
                    df.to_parquet(parquet_output, index=False)
                    __upload_file(adl, schema='inep_taxa_rendimento', table=table_all, year=year, file=parquet_output)
            
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
