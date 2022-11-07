import json
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
import numpy as np
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


def __upload_files(adl, schema, table, year, files):
    for file in files:
        split = os.path.basename(file).split('.')
        filename = __normalize_str(split[0])

        file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))
        if file_type[-1] == 'csv':
            __change_encoding(file)

        directory = __create_directory(schema=schema, table=table, year=year)
        file_type = '.'.join(file_type)
        adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

        __upload_bs(adl, file, adl_write_path)


def __find_download_link(base_value, **kwargs):
    res = requests.get('https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/microdados/prova-brasil')
    soup = bs4.BeautifulSoup(res.text, features='html.parser')
    parent = soup.find(name='div', attrs={'id': 'content-core'})

    links = zip(parent.find_all(name='strong'), parent.find_all(name='a'))
    for strong, anchor in sorted(links, key=lambda v: int(v[0].text)):
        year = int(strong.text)

        if kwargs['reload'] is not None and year == int(kwargs['reload']):
            yield year, anchor.attrs['href']
        elif year > base_value:
            yield year, anchor.attrs['href']


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


def __upload_student(adl, schema, year, zip_output, tmp):
    tmp_students = tmp + 'student/'

    files = __extract_files(zip_output, tmp_students, ['ts_quest_aluno.csv'])
    files.extend(__extract_files(zip_output, tmp_students, ['ts_resultado_aluno.csv']))
    files.extend(__extract_files(zip_output, tmp_students, ['ts_resposta_aluno.csv']))
    assert len(files) == 3

    table = 'aluno'
    __drop_directory(adl, schema=schema, table=table, year=year)
    __upload_files(adl, schema=schema, table=table, year=year, files=files)


def __upload_principal(adl, schema, year, zip_output, tmp):
    tmp_principal = tmp + 'principal/'

    files = __extract_files(zip_output, tmp_principal, ['ts_quest_diretor.csv'])
    assert len(files) == 1

    table = 'diretor'
    __drop_directory(adl, schema=schema, table=table, year=year)
    __upload_files(adl, schema=schema, table=table, year=year, files=files)


def __upload_teacher(adl, schema, year, zip_output, tmp):
    tmp_principal = tmp + 'principal/'

    files = __extract_files(zip_output, tmp_principal, ['dados', 'professor.csv'])
    assert len(files) == 1

    table = 'professor'
    __drop_directory(adl, schema=schema, table=table, year=year)
    __upload_files(adl, schema=schema, table=table, year=year, files=files)


def __upload_school(adl, schema, year, zip_output, tmp):
    tmp_school = tmp + 'school/'

    files = __extract_files(zip_output, tmp_school, ['dados', 'escola.csv'])
    assert len(files) == 2

    table = 'escola'
    __drop_directory(adl, schema=schema, table=table, year=year)
    __upload_files(adl, schema=schema, table=table, year=year, files=files)


def __to_parquet(df, parquet_output):
    try:
        df.to_parquet(parquet_output, index=False)
        yield parquet_output
    except Exception as e:
        raise e
    finally:
        if os.path.exists(parquet_output):
            os.remove(parquet_output)


def __parse_header(xls):
    header = []
    idx = []

    for i in range(0, xls.nrows):
        is_header = False
        for cell in xls.row(i):
            if isinstance(cell.value, str) and len(cell.value) > 0:
                nrm_value = __normalize_str(cell.value)
                if nrm_value in ['VARIAVEL', 'ENUNCIADO']:
                    is_header = True

                if is_header and nrm_value not in header:
                    header.append(nrm_value)

        if is_header:
            idx.append(i)

    return header, idx


def __aggregate_questions(cols):
    _dict = cols.dropna(axis='columns').to_dict('records')

    if len(_dict) == 0 or len(_dict) > 1:
        return np.nan

    return json.dumps(_dict[0], ensure_ascii=False)


def __aggregate_codes(codes):
    codes = codes.dropna()
    if len(codes) == 0:
        return np.nan

    codes_dict = {}
    for codes_list in codes.tolist():
        key, value = None, None
        for i, code in enumerate(codes_list):
            code = code.strip()
            if i % 2 == 0:
                key = code
            else:
                value = code

        if value is None:
            codes_dict['-1'] = key
        else:
            codes_dict[key] = value

    return json.dumps(codes_dict, ensure_ascii=False)


def __transform_dictionary(files):
    parse_sheets = ['TS_RESPOSTA_ALUNO', 'TS_QUEST_ESCOLA', 'TS_QUEST_DIRETOR', 'TS_QUEST_PROFESSOR',
                    'TS_QUEST_ALUNO', 'TS_RESULTADO_ALUNO', 'TS_RESULTADO_ESCOLA']
    for file in files:
        filename, ext = os.path.basename(file).split('.')

        workbook = open_workbook(file)

        sheets = [sheet for sheet in workbook.sheet_names() if sheet in parse_sheets]
        for sheet in sheets:
            header, idx = __parse_header(workbook.sheet_by_name(sheet))

            df = pd.read_excel(file, sheet_name=sheet, header=None, names=header, index_col=False)
            df = df.drop(df.index[idx])
            df = df.dropna(how='all')

            df['NOME_ARQUIVO'] = sheet

            df['ASSUNTO'] = np.where(df['TIPO'].isna(), df['VARIAVEL'], np.nan)
            df['ASSUNTO'] = df['ASSUNTO'].fillna(method='ffill')

            drop_comment_cols = ['TIPO', 'CODIGO_DE_PREENCHIMENTO']
            if 'ENUNCIADO' in df.columns:
                drop_comment_cols = drop_comment_cols + ['ENUNCIADO']
            df = df[~df[drop_comment_cols].isna().all(axis=1)]

            ffill_cols = ['VARIAVEL', 'TIPO', 'TAMANHO', 'DESCRICAO']
            df[ffill_cols] = df[ffill_cols].fillna(method='ffill')

            columns = df.columns.tolist()
            df = df[columns[-2:] + columns[:-2]]
            df = df.reset_index(drop=True)

            grp_cols = ['NOME_ARQUIVO', 'ASSUNTO', 'VARIAVEL', 'TIPO', 'TAMANHO', 'DESCRICAO']
            df['CODIGO_DE_PREENCHIMENTO'] = (df['CODIGO_DE_PREENCHIMENTO']
                                             .str.split('-', 1))
            df_codes = (df
                        .groupby(by=grp_cols)['CODIGO_DE_PREENCHIMENTO']
                        .apply(__aggregate_codes)
                        .reset_index())
            df = df.drop(columns='CODIGO_DE_PREENCHIMENTO')
            df = df.merge(df_codes, on=grp_cols)

            if 'ENUNCIADO' in df.columns:
                df['DESCRICAO'] = np.where(df['ENUNCIADO'].isna(), df['DESCRICAO'], df['ENUNCIADO'])
                df = df.drop(columns='ENUNCIADO')

                qts_cols = df.columns[6:-1]
                df_qt = (df
                         .groupby(by=grp_cols)[qts_cols]
                         .apply(__aggregate_questions)
                         .reset_index())

                df = df.drop(columns=qts_cols)
                df = df.merge(df_qt, on=grp_cols)
                df['CODIGO_DE_PREENCHIMENTO'] = np.where(df['CODIGO_DE_PREENCHIMENTO'].isna(),
                                                         df[0],
                                                         df['CODIGO_DE_PREENCHIMENTO'])
                df = df.drop(columns=0)

            df = df.drop_duplicates()
            df = df.reset_index(drop=True)

            for col in df.columns:
                df[col] = df[col].astype(str)

            normalized_sheet_name = __normalize_str(sheet)
            parquet_output = '/tmp/{file}_{sheet}.{ext}.parquet'.format(file=filename,
                                                                        sheet=normalized_sheet_name, ext=ext)

            yield __to_parquet(df, parquet_output)


def __upload_dictionary(adl, schema, year, zip_output, tmp):
    tmp_students = tmp + 'dictionary/'
    files = __extract_files(zip_output, tmp_students, ['dicionario', '.xlsx'])
    assert len(files) == 1

    table = 'dicionario'
    __drop_directory(adl, schema=schema, table=table, year=year)

    for files in __transform_dictionary(files):
        __upload_files(adl, schema=schema, table=table, year=year, files=files)


def pipeline(functions, **kwargs):
    for func in functions:
        func(**kwargs)


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    schema = 'inep_prova_brasil'
    key_name = 'org_raw_prova_brasil'
    tmp = '/tmp/org_raw_prova_brasil/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 2010
        if __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        adl = authenticate_datalake()
        for year, url in __find_download_link(base_value, **kwargs):
            zip_output = tmp + 'prova_brasil_%s.zip' % year
            __download_file(url, zip_output)

            functions = [__upload_school, __upload_principal, __upload_student,
                         __upload_teacher, __upload_dictionary]
            for func in functions[::-1]:
                func(adl, schema, year, zip_output, tmp)

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
