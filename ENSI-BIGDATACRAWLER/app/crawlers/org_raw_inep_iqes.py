import os
import re
import shlex
import shutil
import subprocess
import time
import csv
from threading import Timer
from unicodedata import normalize

import bs4
import chardet
import py7zr
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
    res = requests.get('https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/indicadores-educacionais/indicadores-de-qualidade-da-educacao-superior')
    soup = bs4.BeautifulSoup(res.text, features='html.parser')
    parent_all_tx = soup.find(name='div', attrs={'class': 'tabs-content'})
    parent_all_tx_divs = parent_all_tx.find_all(name='div', attrs={'class': 'tab-content'})

    for div_all_tx in parent_all_tx_divs[::-1]:
        if not div_all_tx['data-id'].isdigit():
            continue

        year = int(div_all_tx['data-id'])
        url_tx_year = div_all_tx['data-url']

        if year<2007:
            continue

        res = requests.get(url_tx_year)
        soup = bs4.BeautifulSoup(res.text, features='html.parser')
        parent_year_tx = soup.find(name='div', attrs={'id': 'parent-fieldname-text'})

        urls = []
        parent_year_list = [a for a in parent_year_tx.find_all('a')]

        find_params = [('ICG', 'IGC'), ('CPC',)]
        for type_ in ['7Z', 'XLSX', 'XLS']:
            for anchor in parent_year_list:

                if type_ not in anchor.text.upper():
                    continue

                get_table, find_params = find_in_list(find_params, anchor.text.upper())
                if get_table:

                    if kwargs['reload'] is not None and year == int(kwargs['reload']):
                        urls.append((get_table.lower(), __normalize_str(anchor.text).lower()+'.'+type_, anchor.attrs['href']))
                    elif year > base_value:
                        urls.append((get_table.lower(), __normalize_str(anchor.text).lower()+'.'+type_, anchor.attrs['href']))

            if len(urls)==2:
                break

        if len(urls) > 0:
            yield year, urls


def __extract_files(tmp, output ):
    with py7zr.SevenZipFile(output, 'r') as z:
        allfiles = z.getnames()
        allfiles = files_to_parse(allfiles)
        z.extract(path=tmp, targets=allfiles)

    return allfiles


def __download_file(url, output):
    request = 'wget --no-check-certificate --progress=bar:force {url} -O {output}'.format(url=url, output=output)
    process = subprocess.Popen(shlex.split(request), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(3600, process.kill)
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


def __merge_multi_index_header(wk_sheet):
    header = []

    #select rowns from header 
    header_rows = []
    for nrow in range(0, wk_sheet.nrows):
        if not isinstance(wk_sheet.cell_value(nrow,0), float):
            header_rows.append(nrow)
        else:
            break
    skip_header = len(header_rows)

    row_name_num = skip_header - 1
    for ncol in range(0, wk_sheet.ncols):
        header.append(__normalize_str(wk_sheet.cell_value(row_name_num, ncol).strip()))

    return header, skip_header


def __parse_xlsx(file):
    workbook = open_workbook(file)
    sheet = next(iter(workbook.sheet_names()))

    header, skip_header = __merge_multi_index_header(workbook.sheet_by_name(sheet))
    df = pd.read_excel(file, sheet_name=sheet, header=None, names=header, index_col=False, skiprows=skip_header)

    df['NOME_ARQUIVO'] = os.path.basename(file)
    for col in df.columns:
        df[col] = df[col].astype(str)

    df = df[df[df.columns[0]].str.isdigit()]
    
    return df



def __parse_csv(base_path, allfiles):
    df = pd.DataFrame()
    header = None
    
    for file in allfiles:
        file_path = base_path+file

        rawdata = open(file_path, 'rb').readlines()
    
        result = chardet.detect(rawdata[0]+rawdata[1])
        encoding = result['encoding']

        with open(file_path, 'r', encoding=encoding) as infile:
            inputs = csv.reader(infile)

            if not header:

                line_act = []
                for row in inputs:
                    if inputs.line_num==1:
                        header = __normalize_str(row[0].strip()).split(';')
                        continue
                    line_act = row[0].split(';')
                    if line_act[0].isdigit():
                        break
                    else:
                        header = __normalize_str(row[0].strip()).split(';')
                
                skip = inputs.line_num-1

        _df = pd.read_csv(file_path, sep=';', header=None, names=header, skiprows=skip, index_col=False, encoding=encoding)
        _df['NOME_ARQUIVO'] = os.path.basename(file)
        
        df = pd.concat([df, _df], axis='index', ignore_index=True)

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


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_inep_iqes'
    tmp = '/tmp/org_raw_inep_iqes/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        adl = authenticate_datalake()
        for year, urls in __find_download_link(base_value, **kwargs):
            for table, file_name, url in urls:
                output = tmp + file_name

                __download_file(url, output)
                __drop_directory(adl, schema='inep_iqes', table=table, year=year)

                if '7Z' in output:
                    allfiles = __extract_files(tmp, output)
                    assert len(allfiles) > 0

                    df = __parse_csv(tmp, allfiles)

                else:
                    df = __parse_xlsx(output)

                parquet_output = tmp + '{file}.parquet'.format(file=file_name)
                df.to_parquet(parquet_output, index=False)

                __upload_file(adl, schema='inep_iqes', table=table, year=year, file=parquet_output)

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
