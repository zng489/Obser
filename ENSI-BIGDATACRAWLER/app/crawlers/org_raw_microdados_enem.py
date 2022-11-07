import json
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
import rarfile
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
    if file_type[-1] in ['csv', 'txt']:
        __change_encoding(file)

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __find_download_link(base_value, **kwargs):
    res = requests.get('https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/microdados/enem')
    soup = bs4.BeautifulSoup(res.text, features='html.parser')
    parent = soup.find(name='div', attrs={'id': 'content-core'})

    links = zip(parent.find_all(name='strong'), parent.find_all(name='a'))
    for strong, anchor in sorted(links, key=lambda v: int(v[0].text)):
        year = int(re.search(r'\d{4}', strong.text).group())
        if (kwargs['reload'] is None and year > base_value) or (kwargs['reload'] == year):
            match = re.search(r'\d{1,2}/\d{1,2}/\d{4}', anchor.text)

            updated_ts = 0
            if match is not None:
                updated_ts = int(datetime.strptime(match.group(), '%d/%m/%Y').timestamp())
            yield year, updated_ts, anchor.attrs['href']


def __extract_files(zip_output, tmp, prefixes):
    with zipfile.ZipFile(zip_output) as _zip:
        if len(_zip.filelist) == 1:
            for file in _zip.filelist:
                unzip_rar_path = os.path.join(tmp, file.filename)
                if not os.path.exists(unzip_rar_path):
                    _zip.extract(file, path=tmp)
                file_paths = [unzip_rar_path]
        else:
            data_files = [file for file in _zip.filelist if
                          all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                                 .encode('ASCII', 'ignore')
                                                 .decode('ASCII')) for prefix in prefixes)]
            file_paths = [_zip.extract(member=data_file, path=tmp)
                          for data_file in data_files if data_file.file_size > 1000]

    tmp_compress = tmp + 'rar_output'
    for file in file_paths[:]:
        if file.endswith('.rar'):
            extractor = rarfile.RarFile(file)
            data_files = [file for file in extractor.infolist() if
                          all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                                 .encode('ASCII', 'ignore')
                                                 .decode('ASCII')) for prefix in prefixes)]
            for data_file in data_files:
                extractor.extract(member=data_file, path=tmp_compress)
                file_paths.append(os.path.join(tmp_compress, data_file.filename))
            file_paths.remove(file)

    return file_paths


def __uncompress_files(zip_output, tmp_year, year):
    files = []
    if year not in [2011, 2012]:
        files.extend(__extract_files(zip_output, tmp_year,
                                     prefixes=['microdados_enem_{year}.csv'.format(year=year)]))
    else:
        files.extend(__extract_files(zip_output, tmp_year,
                                     prefixes=['dados_enem_{year}.csv'.format(year=year)]))
        files.extend(__extract_files(zip_output, tmp_year,
                                     prefixes=['dados_enem_{year}.txt'.format(year=year)]))
        files.extend(__extract_files(zip_output, tmp_year,
                                     prefixes=['questionario_', 'csv']))
        files.extend(__extract_files(zip_output, tmp_year,
                                     prefixes=['questionario_', 'txt']))
    files.extend(__extract_files(zip_output, tmp_year,
                                 prefixes=['dicionario_microdados_enem_{year}.xlsx'.format(year=year)]))
    return files


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


def __clean_dictionary(file_path, tmp, year):
    excel = pd.ExcelFile(file_path)
    matches = ['_ENEM_{year}'.format(year=year),
               'DICIONARIO_VARIAVEIS',
               'QUESTIONARIO']
    sheets = [sheet for sheet in excel.sheet_names if any(match in __normalize_str(sheet) for match in matches)]
    assert len(sheets) > 0

    skiprows = 2
    usecols = "A:G" if year in [2011] else "A:F"
    for sheet in sheets:
        df = pd.read_excel(file_path, skiprows=skiprows, usecols=usecols, sheet_name=sheet)
        cols = ['NOME_VARIAVEL', 'DESCRICAO', 'CATEGORIA', 'DESCRICAO_CATEGORIA', 'TAMANHO', 'TIPO'] \
            if len(df.columns) == 6 \
            else ['NOME_VARIAVEL', 'DESCRICAO', 'CATEGORIA', 'DESCRICAO_CATEGORIA', 'INICIO', 'TAMANHO', 'TIPO']

        df.columns = cols
        df = pd.DataFrame(df.values[1:], columns=df.columns)

        df['ASSUNTO'] = np.where(df['DESCRICAO'].isnull(), df['NOME_VARIAVEL'], np.nan)
        df = df.fillna(method='ffill')
        df = df.dropna(axis='index', subset=['DESCRICAO'])

        columns = df.columns.tolist()

        df_desc_concat = df \
            .groupby(by=['ASSUNTO', 'NOME_VARIAVEL'])['DESCRICAO'] \
            .apply(set) \
            .apply(', '.join) \
            .to_frame() \
            .reset_index()

        df = df.drop(columns=['DESCRICAO'], axis='columns')

        df = df.merge(df_desc_concat, how='left', left_on=['ASSUNTO', 'NOME_VARIAVEL'],
                      right_on=['ASSUNTO', 'NOME_VARIAVEL'])

        df = df[df['ASSUNTO'] != df['NOME_VARIAVEL']]
        df = df[columns[-1:] + columns[:-1]]
        df = df.reset_index(drop=True)

        df_cat_desc_dict = df \
            .dropna(axis='index', subset=['CATEGORIA', 'DESCRICAO_CATEGORIA']) \
            .groupby(by=['ASSUNTO', 'NOME_VARIAVEL']) \
            .apply(lambda _df: _df[['CATEGORIA', 'DESCRICAO_CATEGORIA']].to_dict('split')) \
            .apply(lambda _dict: {str(k).strip(): str(v).strip() for k, v in _dict['data']}) \
            .apply(json.dumps, ensure_ascii=False) \
            .to_frame() \
            .reset_index()

        df = df.merge(df_cat_desc_dict, how='left', left_on=['ASSUNTO', 'NOME_VARIAVEL'],
                      right_on=['ASSUNTO', 'NOME_VARIAVEL'])
        df = df.drop(columns=['CATEGORIA', 'DESCRICAO_CATEGORIA'], axis='index')
        df = df.drop_duplicates(subset=['ASSUNTO', 'NOME_VARIAVEL'])
        df = df.reset_index(drop=True)

        cols = ['ASSUNTO', 'NOME_VARIAVEL', 'DESCRICAO', 'TAMANHO', 'TIPO', 'DOMINIO'] \
            if len(df.columns) == 6 \
            else ['ASSUNTO', 'NOME_VARIAVEL', 'DESCRICAO', 'INICIO', 'TAMANHO', 'TIPO',
                  'DOMINIO']

        filename = os.path.basename(file_path)
        if sheet.startswith('QUEST'):
            split = filename.split('.')
            filename = '{file}_QUESTIONARIO.{ext}'.format(file=split[0], ext=split[1])

        df.columns = cols
        df['NOME_ARQUIVO'] = filename
        reorder_cols = ['NOME_ARQUIVO', 'ASSUNTO', 'NOME_VARIAVEL', 'DESCRICAO', 'DOMINIO', 'TAMANHO', 'TIPO'] \
            if len(df.columns) == 7 \
            else ['NOME_ARQUIVO', 'ASSUNTO', 'NOME_VARIAVEL', 'DESCRICAO', 'DOMINIO', 'INICIO', 'TAMANHO', 'TIPO']
        df = df.reindex(reorder_cols, axis='columns')

        tmp_parquet = '{tmp}/{file}.parquet'.format(tmp=tmp, file=filename)
        df.to_parquet(tmp_parquet, index=False)

        yield tmp_parquet


def __normalize_cols(cols):
    return [__normalize_str(col) for col in cols]


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


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_microdados_enem'
    tmp = '/tmp/org_raw_microdados_enem'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)
        if kwargs['reset']:
            keys = __call_redis(host, passwd, 'keys', key_name + ":*")
            for key in keys:
                __call_redis(host, passwd, 'delete', key.decode())

        adl = authenticate_datalake()
        base_value, processed_data = 2008, False
        for year, upload_ts, url in __find_download_link(base_value, **kwargs):
            key_name_year = '{key}:{year}'.format(key=key_name, year=year)

            last_upload_ts = -1
            if __call_redis(host, passwd, 'exists', key_name_year):
                last_upload_ts = int(__call_redis(host, passwd, 'get', key_name_year))

            if upload_ts > last_upload_ts:
                tmp_year = '{tmp}/{year}/'.format(tmp=tmp, year=year)
                os.makedirs(tmp_year, mode=0o777, exist_ok=True)

                zip_output = tmp_year + 'microdados_enem_%s.zip' % year
                __download_file(url, zip_output)

                __drop_directory(adl, schema='inep_enem', table='dados', year=year)
                __drop_directory(adl, schema='inep_enem', table='dados_complementares', year=year)
                if year != 2010:
                    __drop_directory(adl, schema='inep_enem', table='dicionario', year=year)

                files = __uncompress_files(zip_output, tmp_year, year)
                for file in files:
                    if any(file.lower().endswith(ext) for ext in ['csv', 'txt']):
                        if year in [2011, 2012] and 'QUEST' in __normalize_str(file):
                            __upload_file(adl, schema='inep_enem', table='dados_complementares', year=year, file=file)
                        else:
                            __upload_file(adl, schema='inep_enem', table='dados', year=year, file=file)
                    else:
                        if year not in [2010]:
                            for tmp_parquet in __clean_dictionary(file, tmp=tmp_year, year=year):
                                __upload_file(adl, schema='inep_enem', table='dicionario', year=year, file=tmp_parquet)

                processed_data = True
                if kwargs['reload'] is None:
                    __call_redis(host, passwd, 'set', key_name_year, upload_ts)

                shutil.rmtree(tmp_year)

        if not processed_data:
            return {'exit': 300, 'msg': "They didn't upload a new file"}
        else:
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
