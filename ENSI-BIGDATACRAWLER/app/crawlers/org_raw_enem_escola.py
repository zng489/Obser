import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from threading import Timer
from unicodedata import normalize

import bs4
import numpy as np
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


def __check_path_exists(adl: FileSystemClient, path):
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


def __upload_bs(adl: FileSystemClient, lpath, rpath):
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


def __create_directory(schema=None, table=None):
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)


def __drop_directory(adl, schema=None, table=None):
    adl_drop_path = __create_directory(schema=schema, table=table)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, l_path, r_path):
    adl_write_path = '{directory}/{r_path}'.format(directory=__create_directory(schema, table),
                                                   r_path=r_path)
    logging.info('uploading adl', adl_write_path)
    __upload_bs(adl, l_path, adl_write_path)


def __find_download_link():
    res = requests.get('https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/microdados/enem-por-escola')
    soup = bs4.BeautifulSoup(res.text, features='html.parser')
    parent = soup.find(name='div', attrs={'id': 'content-core'})

    anchor = parent.find(name='a')
    match = re.search(r'\d{1,2}/\d{1,2}/\d{4}', anchor.text)
    if match is not None:
        upload_date = datetime.strptime(match.group(), '%d/%m/%Y')
    else:
        upload_date = datetime.now()

    return anchor.attrs['href'], int(upload_date.timestamp())


def __extract_files(zip_output, tmp, prefixes):
    with zipfile.ZipFile(zip_output) as _zip:
        data_files = [file for file in _zip.filelist if
                      all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                             .encode('ASCII', 'ignore')
                                             .decode('ASCII')) for prefix in prefixes)]
        return [_zip.extract(member=data_file, path=tmp) for data_file in data_files]


def __change_encoding(file):
    p1 = subprocess.Popen(shlex.split('file -bi "%s"' % file), stdout=subprocess.PIPE)
    p2 = subprocess.Popen(shlex.split("awk -F '=' '{print $2}'"), stdin=p1.stdout, stdout=subprocess.PIPE)
    encoding = p2.communicate()[0].decode("utf-8").strip("\n")

    # For dev purposes
    if DEBUG:
        os.system('mv "{file}" "{file}.old"'.format(file=file))
        os.system('head -n 500 "{file}.old" > "{file}"'.format(file=file))

    if encoding not in ['utf-8', 'us-ascii']:
        os.system('mv "{file}" "{file}.old"'.format(file=file))
        subprocess.Popen(shlex.split('iconv -f {f} -t utf-8 "{file}.old" -o "{output}"'
                                     .format(f=encoding, file=file, output=file))).communicate()


def __parse_categories(categories):
    categories = list(c.split('-') for c in categories)
    categories = list(item.strip() for sublist in categories for item in sublist)

    new_categories = []
    if any(c.isdigit() for c in categories):
        number, desc = None, ''
        for category in categories:
            category = category.replace('-', '').replace('\n', ' ').strip()
            if len(category) > 0:
                if category.isdigit():
                    if number is not None:
                        new_categories.append((number, desc.strip()))
                        number, desc = None, ''
                    number = category
                else:
                    desc = category

        if number is not None:
            new_categories.append([number, desc.strip()])
        else:
            new_categories.append([desc.strip()])
    else:
        new_categories.append([','.join(categories)])
    return new_categories


def __aggregate_category(categories):
    if not any(nan for nan in categories.isnull()):
        agg = {}

        categories = categories.values[0].split('\n')
        categories = __parse_categories(categories)

        null = -1
        for category in categories:
            key, value = None, None
            if len(category) > 1:
                key, value = category
            else:
                value = category[0]

            if key is None or len(key) == 0:
                key = str(null)
                null -= 1
            agg.update({key: value})
        return json.dumps(agg, ensure_ascii=False)

    return np.nan


def __clean_dictionary(abs_path):
    excel = pd.ExcelFile(abs_path)
    assert len(excel.sheet_names) == 1

    df = excel.parse(sheet_name=excel.sheet_names[0], skiprows=2, ignore_index=True)
    cols = ['ID', 'NOME_VARIAVEL', 'DESCRICAO', 'CATEGORIA', 'INFO_EDICAO']
    df.columns = cols

    df['ASSUNTO'] = np.where(df['NOME_VARIAVEL'].isnull(), df['ID'], np.nan)

    fill_cols = ['ASSUNTO', 'NOME_VARIAVEL', 'DESCRICAO']
    df[fill_cols] = df[fill_cols].fillna(method='ffill')

    df['ID'] = df['ID'].astype(str)
    df = df[df['ID'].notnull() & df['ID'].str.isdigit()]
    df = df.set_index('ID')

    df_categories = df.groupby(by='ID')['CATEGORIA'] \
        .apply(__aggregate_category) \
        .to_frame()

    df = df.drop(columns='CATEGORIA')
    df = df.merge(df_categories, on='ID')
    df = df.rename(columns={'CATEGORIA': 'DOMINIO'})
    df['NOME_ARQUIVO'] = excel.sheet_names[0]

    reorder_cols = ['NOME_ARQUIVO', 'ASSUNTO', 'NOME_VARIAVEL', 'DESCRICAO', 'DOMINIO', 'INFO_EDICAO']
    df = df.reindex(reorder_cols, axis='columns')

    df = df.where(df.notnull(), None)
    for col in df.columns:
        df[col] = df[col].astype(str)

    return df


def __normalize_cols(cols):
    return [__normalize_str(col) for col in cols]


def __download_file(url, zip_output):
    request = 'wget --progress=bar:force {url} -O {output}'.format(url=url, output=zip_output)
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


def __find_files(zip_output, tmp):
    files = __extract_files(zip_output, tmp, ['microdados_enem_escola.csv'])
    files.extend(__extract_files(zip_output, tmp, ['dicionario_microdados_enem_escola.xlsx']))
    assert len(files) == 2

    return files


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_enem_escola'
    tmp = '/tmp/org_raw_enem_escola/'
    schema = 'inep_enem_escola'

    adl = authenticate_datalake()
    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if kwargs['reload'] is None and __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        zip_output = tmp + 'microdados_enem_escola.zip'
        url, upload_ts = __find_download_link()
        if upload_ts > base_value:
            __download_file(url, zip_output)

            __drop_directory(adl, schema, table='enem_escola')
            __drop_directory(adl, schema, table='enem_escola_dicionario')

            for file_path in __find_files(zip_output, tmp):
                filename, ext = os.path.basename(file_path).split('.')
                filename = __normalize_str(filename)
                ext = ext.lower()

                if ext == 'csv':
                    __change_encoding(file_path)
                    __upload_file(adl, schema, table='enem_escola', l_path=file_path, r_path='%s.%s' % (filename, ext))
                elif ext == 'xlsx':
                    tmp_parquet = '/%s/%s.parquet' % (tmp, filename)
                    try:
                        df = __clean_dictionary(file_path)
                        df.to_parquet(tmp_parquet, index=False)
                        __upload_file(adl, schema, table='enem_escola_dicionario',
                                      l_path=tmp_parquet, r_path='%s.%s.parquet' % (filename, ext))
                    except Exception as e:
                        raise e
                    finally:
                        if os.path.exists(tmp_parquet):
                            os.remove(tmp_parquet)

            if kwargs['reload'] is None:
                __call_redis(host, passwd, 'set', key_name, upload_ts)

            return {'exit': 200}
        else:
            return {'exit': 300, 'msg': "They didn't upload a new file"}
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
