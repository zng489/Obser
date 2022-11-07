import os
import re
import json
import time
import shlex
import zipfile
import datetime
import subprocess
from threading import Timer
from xlrd import open_workbook, sheet
from unicodedata import normalize

import redis
import requests
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


def __extract_files(zip_output, tmp, prefixes):
    with zipfile.ZipFile(zip_output) as _zip:
        data_files = [file for file in _zip.filelist if
                      all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                             .encode('ASCII', 'ignore')
                                             .decode('ASCII')) for prefix in prefixes)]

        return [_zip.extract(member=data_file, path=tmp) for data_file in data_files
                if '$' not in data_file.filename]

def __download_file(url, zip_output):
    proxyDict = { 
        "http"  : os.environ.get('ftp_proxy'), 
        "https" : os.environ.get('https_proxy'), 
        "ftp"   : os.environ.get('ftp_proxy')
    }
    r = requests.get(url, proxies=proxyDict)
    with open(zip_output, 'wb') as f:
        assert r.status_code == 200
        f.write(r.content)
        f.close()

def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t\r=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace('  ', '_')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('/', '_')
                  .replace('.', '_')
                  .replace('\n', '_')
                  .upper())

def __filter_footer(df):
    df = df.iloc[:21]
    return df

def __parse_xlsx(file, filter_footer, filter_sheet):
    workbook = open_workbook(file)

    df = pd.DataFrame()
    header_first = None
    sheet_name = filter_sheet if filter_sheet else workbook.sheet_names()[0]
    header, skip_header = __merge_multi_index_header(workbook.sheet_by_name(sheet_name), 1)
    header_first = header_first if header_first else header

    _df = pd.read_excel(file, sheet_name=sheet_name, header=None, names=header_first, index_col=False, skiprows=skip_header)
    
    _df = filter_footer(_df)
    df = pd.concat([df, _df], axis='index', ignore_index=True)

    split = os.path.basename(file).split('.')
    df['NOME_ARQUIVO'] = __normalize_str(split[0])

    for col in df.columns:
        df[col] = df[col].astype(str)

    return df

def __merge_multi_index_header(wk_sheet, column_with_data):
    multi_index_header = []
    columns_name_num = []

    #select rowns from header 
    header_rows = []
    for nrow in range(0, wk_sheet.nrows):
        if not isinstance(wk_sheet.cell_value(nrow,column_with_data), float):
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

def __fix_column_names(df):

    df.columns = [column.replace('_EM_EM_', '_EM_').replace('INOVACOES_INOVACOES_', 'INOVACOES_') for column in df.columns]
    
    return df

def main(**kwargs):
    """
    This function is the main entrypoint execute this bot.
    Generally, the functions above are called here too.
    Many other functions can be created to do whatever in required for the bot to function properly.
    Bots are specific to it's own data sources.
    Take the time to have a look on the other implementations to have ideas on how to develop your bots.

    :param kwargs: dict with all key-value pairs needed as configurations to run the bot.
    :return: None
    """

    # The lines below are just example code.
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    tmp = '/tmp/org_raw_pintec_prod_vend_int/'
    table = 'pintec_prod_vend_int'

    sheets = {
        '2017':'tab2.14',
        '2014':'tab2.14',
        '2011':'tab212',
        '2008':'Tab2_14'
    }
    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        urls = {
            '2017':'https://ftp.ibge.gov.br/Industrias_Extrativas_e_de_Transformacao/Pesquisa_de_Inovacao_Tecnologica/2017/xls/pintec2017_grandes_regioes_e_unidades_da_federacao_selecionadas.xls',
            '2014':'https://ftp.ibge.gov.br/Industrias_Extrativas_e_de_Transformacao/Pesquisa_de_Inovacao_Tecnologica/2014/xls/pintec2014_grandes_regioes_e_unidades_da_federacao_selecionadas.xls',
            '2011':'https://ftp.ibge.gov.br/Industrias_Extrativas_e_de_Transformacao/Pesquisa_de_Inovacao_Tecnologica/2011/xls/gregioesufs_xls.zip',
            '2008':'https://ftp.ibge.gov.br/Industrias_Extrativas_e_de_Transformacao/Pesquisa_de_Inovacao_Tecnologica/2008/cnae2_gran_reg_tab_2_01_a_26.zip'
        }

        adl = authenticate_datalake()
        dfs_list = []

        for year, url in urls.items():
            ext = 'zip' if '.zip' in url else 'xls'
            downloaded_file = f'{year}_{table}.{ext}'

            download_output = tmp + downloaded_file
            
            __download_file(url, download_output)
            
            file = download_output
            filter_sheet = sheets[year]
            
            if year not in ['2017', '2014']:
                files = __extract_files(download_output, tmp, ['.xls'])
                assert len(files) > 0
                file = tmp + sheets[year] + '.xls'
                filter_sheet = None
                        
            __drop_directory(adl, schema='ibge', table=table, year=year)            
            
            df = __parse_xlsx(file, __filter_footer, filter_sheet)
            df = __fix_column_names(df)
            parquet_output = tmp + f'{table}_{year}.parquet'
            df.to_parquet(parquet_output, index=False)
            
            __upload_file(adl, schema='ibge', year=year, table=table, file=parquet_output)
            
        return {'exit':200}     
    except Exception as e:
        raise e

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