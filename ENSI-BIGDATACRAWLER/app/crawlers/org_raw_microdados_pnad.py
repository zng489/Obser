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
        client_secret=os.environ['AZURE_CLIENT_SECRET'],
        proxies={'http': os.environ['http_proxy']})

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


def __read_in_chunks(file_object, chunk_size=4 * 1024 * 1024):
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


def __create_directory(schema=None, table=None, year=None, year_month=None):
    path = '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)
    if year is not None and year_month is not None:
        path = '{adl}/{year}/{month}'.format(adl=path, year=year, month=year_month)
    return path


def __drop_directory(adl, schema=None, table=None, year=None, year_month=None):
    adl_drop_path = __create_directory(schema=schema, table=table, year=year, year_month=year_month)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, file, year=None, year_month=None):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])

    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))
    if file_type[-1] in ['csv', 'txt']:
        __change_encoding(file)

    directory = __create_directory(schema=schema, table=table, year=year, year_month=year_month)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __parse_str_to_timestamp(_str, regex, date_format):
    match = re.search(regex, _str)
    upload_ts = 0
    if match is not None:
        upload_ts = int(datetime.strptime(match.group(), date_format).timestamp())
    return upload_ts


def __list_ftp_dir(url):

    res = requests.get(url)
    soup = bs4.BeautifulSoup(res.text, features='html.parser')

    if len(soup) == 0:
        raise Exception('Something bad is occurring')

    keys = {}

    for i in soup.find_all("tr"):

        if i.find('a') and str(i.a['href']).split('.')[-1] == 'zip':

            key = str(i.a['href'])
            dt = str(i.td.next_sibling.next_sibling.text).strip()
            dt = datetime.strptime(dt, '%Y-%m-%d %H:%M')
            dt = int(dt.strftime('%Y%m%d%H%M%S'))
            update = {key: dt}
            keys.update(update)

        elif i.find('a') and str(i.a['href'])[:4].isnumeric():

            key = (i.a['href'])
            update = {key: '0'}
            keys.update(update)

    return keys


def __download_file(url, zip_output):
    cmd = 'wget --progress=bar:force {url} -nv -O {output}'.format(url=url, output=zip_output)
    process = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(3600, process.kill)
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
        file_paths = [_zip.extract(member=data_file, path=tmp)
                      for data_file in data_files if data_file.file_size > 1000]

    return file_paths


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


def __normalize_cols(cols):
    return [__normalize_str(col) for col in cols]


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


def __download_txt(adl, host, passwd, tmp, key_name, ftp_dir, keys):
    processed_data = False

    for k in keys:

        ftp_date = int(keys[k])

        i = k.split('.')[0].split('_')[1]
        trim_year = i[:6]
        year = i[-4:]

        ftp_dir_year = '{base_dir}/{year}/'.format(base_dir=ftp_dir, year=year)

        key_name_year_month = '{key}:{year_month}'.format(key=key_name, year_month=trim_year)
        last_upload_ts = -1

        if __call_redis(host, passwd, 'exists', key_name_year_month):
            last_upload_ts = int(__call_redis(host, passwd, 'get', key_name_year_month))

        if ftp_date > last_upload_ts:

            tmp_year_month = '{tmp}/{year_month}/'.format(tmp=tmp, year_month=trim_year)

            os.makedirs(tmp_year_month, mode=0o777, exist_ok=True)

            zip_output = tmp_year_month + k
            url = '{base_dir}/{filename}'.format(base_dir=ftp_dir_year, filename=k)

            __download_file(url, zip_output)

            __drop_directory(adl, schema='ibge', table='pnad', year=year, year_month=trim_year)
            for pnad in __extract_files(zip_output, tmp_year_month, ['PNAD']):
                #pnad = pnad.replace("\\", "/") for windows SO
                __upload_file(adl, schema='ibge', table='pnad', file=pnad, year=year, year_month=trim_year)

            __call_redis(host, passwd, 'set', key_name_year_month, ftp_date)

            shutil.rmtree(tmp_year_month)
            processed_data = True

    return processed_data


def __clean_dictionary(abs_path):
    cols = ['POSICAO_INICIAL', 'TAMANHO', 'CODIGO_VARIAVEL', 'QUESITO_N', 'QUESITO_DESCRICAO',
            'CATEGORIA_TIPO', 'CATEGORIA_DESCRICAO', 'PERIODO']

    excel = pd.ExcelFile(abs_path)
    df = excel.parse(sheet_name=excel.sheet_names[0], header=None, names=cols, skiprows=3)
    df['TAMANHO'] = df['TAMANHO'].astype('Int64')

    is_nan = df[df.columns[1:]].isna().all(axis=1)
    df['ASSUNTO'] = np.where(is_nan, df['POSICAO_INICIAL'], np.nan)

    fill_cols = ['ASSUNTO', 'POSICAO_INICIAL', 'TAMANHO', 'CODIGO_VARIAVEL', 'PERIODO']
    df[fill_cols] = df[fill_cols].fillna(method='ffill')
    df = df[~is_nan]

    grp = ['POSICAO_INICIAL', 'TAMANHO', 'CODIGO_VARIAVEL']
    grp_fill = ['QUESITO_N', 'QUESITO_DESCRICAO']
    df_agg = (df
              .groupby(by=grp)[grp + grp_fill]
              .transform(lambda x: x.ffill()))

    df = df.drop(columns=grp_fill).merge(df_agg, how='inner', on=grp)
    df = df.drop_duplicates().reset_index(drop=True)
    df = df.where(df.notnull(), None)
    df[df.columns] = df[df.columns].astype(str)

    reordered_cols = ['ASSUNTO', 'POSICAO_INICIAL', 'TAMANHO', 'CODIGO_VARIAVEL',
                      'QUESITO_N', 'QUESITO_DESCRICAO', 'CATEGORIA_TIPO', 'CATEGORIA_DESCRICAO', 'PERIODO']
    df = df.reindex(reordered_cols, axis='columns')

    return df


def __download_dictionary(adl, tmp, ftp_dir, doc):

    ftp_doc_dir = '{base_dir}/{doc}/'.format(base_dir=ftp_dir, doc=doc)
    ftp_ls_doc_dir = __list_ftp_dir(ftp_doc_dir)

    file_doc = next((file_doc for file_doc in ftp_ls_doc_dir if 'dicionario' in file_doc.lower()))
    url = '{base_dir}/{file}'.format(base_dir=ftp_doc_dir, file=file_doc)
    zip_output = '{tmp}/{file}'.format(tmp=tmp, file=file_doc)

    __download_file(url, zip_output)
    file = next(iter(__extract_files(zip_output, tmp, prefixes=['dicionario'])))

    df = __clean_dictionary(file)
    parquet_output = '{file}.parquet'.format(file=file)
    df.to_parquet(parquet_output, index=False)

    __drop_directory(adl, schema='ibge', table='pnad_dicionario')
    __upload_file(adl, schema='ibge', table='pnad_dicionario', file=parquet_output)


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_microdados_pnad'
    tmp = '/tmp/org_raw_microdados_pnad'

    adl = authenticate_datalake()
    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)
        if kwargs['reset']:
            keys = __call_redis(host, passwd, 'keys', key_name + ":*")
            for key in keys:
                __call_redis(host, passwd, 'delete', key.decode())

        ftp_dir = ('http://ftp.ibge.gov.br/Trabalho_e_Rendimento/'
                   'Pesquisa_Nacional_por_Amostra_de_Domicilios_continua/Trimestral/Microdados/')
        ftp_years = __list_ftp_dir(url=ftp_dir)

        keys = {}
        for i in ftp_years.keys():
            keys.update(__list_ftp_dir(ftp_dir+i))

        processed_data = __download_txt(adl, host, passwd, tmp, key_name, ftp_dir, keys)

        if processed_data:
            __download_dictionary(adl, tmp, ftp_dir, 'Documentacao')
            return {'exit': 200}

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
