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
            for chunk, offset, length in __read_in_chunks(file, chunk_size=50 * 1024 * 1024):
                if offset > 0:
                    file_client.append_data(data=chunk, offset=offset)
                    file_client.flush_data(length)
                else:
                    file_client.upload_data(data=chunk, overwrite=True, timeout=1800)
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
    res = requests.get('https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/microdados/saeb')
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
    import csv
    
    old_file = file + 'old.csv'
    if os.path.exists(old_file):
        os.remove(old_file)

    os.rename(file, old_file)

    with open(old_file, 'r', encoding='utf-8', errors='ignore') as infile, open(file, 'w') as outfile:
        inputs = csv.reader(infile)
        output = csv.writer(outfile)

        for row in inputs:
            output.writerow(row)


def __normalize_cols(cols):
    return [__normalize_str(col) for col in cols]


def __download_file(url, zip_output):
    request = 'wget --no-check-certificate --progress=bar:force {url} -O {output}'.format(url=url, output=zip_output)
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


def __upload_students(adl, schema, year, zip_output, tmp):
    tmp_students = tmp + 'students/'
    files = __extract_files(zip_output, tmp_students, ['ts_quest_aluno.csv'])
    files.extend(__extract_files(zip_output, tmp_students, ['ts_resultado_aluno.csv']))
    files.extend(__extract_files(zip_output, tmp_students, ['ts_resposta_aluno.csv']))
    files.extend(__extract_files(zip_output, tmp_students, ['ts_aluno_', '.csv']))
    assert len(files) > 0

    table = 'aluno'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=files)


def __upload_school(adl, schema, year, zip_output, tmp):
    tmp_students = tmp + 'school/'
    files = __extract_files(zip_output, tmp_students, ['ts_quest_escola.csv'])
    files.extend(__extract_files(zip_output, tmp_students, ['ts_escola.csv']))
    assert len(files) > 0

    table = 'escola'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=files)


def __upload_principal(adl, schema, year, zip_output, tmp):
    tmp_students = tmp + 'principal/'
    files = __extract_files(zip_output, tmp_students, ['ts_quest_diretor.csv'])
    files.extend(__extract_files(zip_output, tmp_students, ['ts_diretor.csv']))
    assert len(files) > 0

    table = 'diretor'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=files)


def __upload_item(adl, schema, year, zip_output, tmp):
    tmp_students = tmp + 'item/'
    files = __extract_files(zip_output, tmp_students, ['ts_item.csv'])
    assert len(files) > 0

    table = 'item'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=files)


def __upload_teacher(adl, schema, year, zip_output, tmp):
    tmp_students = tmp + 'teacher/'
    files = __extract_files(zip_output, tmp_students, ['ts_quest_professor.csv'])
    files.extend(__extract_files(zip_output, tmp_students, ['ts_professor.csv']))
    assert len(files) > 0

    table = 'professor'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=files)


def __upload_result(adl, schema, year, zip_output, tmp):
    if year>2011:
        tmp_result = tmp + 'result/'
        files = __extract_files(zip_output, tmp_result, ['ts_uf.xlsx'])
        files.extend(__extract_files(zip_output, tmp_result, ['ts_brasil.xlsx']))
        files.extend(__extract_files(zip_output, tmp_result, ['ts_regiao.xlsx']))
        files.extend(__extract_files(zip_output, tmp_result, ['ts_municipio.xlsx']))
        assert len(files) > 0

        table = 'resultado'

        files_parquet = []
        for file in files:
            split = os.path.basename(file).split('.')
            filename = __normalize_str(split[0])

            df = __parse_xlsx(file)
            df['ANO'] = year

            parquet_output = tmp_result + '{file}.parquet'.format(file=filename)
            files_parquet.append(parquet_output)
            df.to_parquet(parquet_output, index=False)

        __drop_directory(adl, schema, table=table, year=year)
        __upload_files(adl, schema, table=table, year=year, files=files_parquet)


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


def __transform_dictionary(year, files):
    parse_sheets = (['TS_QUEST_ALUNO', 'TS_QUEST_ESCOLA', 'TS_QUEST_DIRETOR', 'TS_ITEM', 'TS_QUEST_PROFESSOR']
                    if year == 2011 else
                    ['TS_ALUNO_5EF', 'TS_ALUNO_9EF', 'TS_ALUNO_3EM', 'TS_ALUNO_3EM_AG', 'TS_ALUNO_3EM_ESC',
                     'TS_ESCOLA', 'TS_DIRETOR', 'TS_ITEM', 'TS_PROFESSOR'])

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
            yield year, __to_parquet(df, parquet_output)


def __upload_dictionary(adl, schema, year, zip_output, tmp):
    tmp_students = tmp + 'dictionary/'
    files = __extract_files(zip_output, tmp_students, ['dicionario', '.xlsx'])
    assert len(files) == 1

    table = 'dicionario'
    __drop_directory(adl, schema, table=table, year=year)
    for year, files in __transform_dictionary(year, files):
        __upload_files(adl, schema, table=table, year=year, files=files)


def pipeline(functions, **kwargs):
    for func in functions:
        func(**kwargs)


def __parse_xlsx(file):
    workbook = open_workbook(file)

    sheet = next(iter(workbook.sheet_names()))
    header, skip_header = __merge_multi_index_header(workbook.sheet_by_name(sheet))

    df = pd.read_excel(file, sheet_name=sheet, header=None, names=header, index_col=False, skiprows=skip_header)


    split = os.path.basename(file).split('.')
    df['NOME_ARQUIVO'] = __normalize_str(split[0])

    for col in df.columns:
        df[col] = df[col].astype(str)

    return df


def __merge_multi_index_header(wk_sheet):
    multi_index_header = []
    columns_name_num = []

    #select rowns from header 
    header_rows = []
    is_float = False
    for nrow in range(0, wk_sheet.nrows):
        for ncol in range(0, wk_sheet.ncols):
            if isinstance(wk_sheet.cell_value(nrow, ncol), float):
                is_float = True

        if not is_float:                
            header_rows.append(nrow)
        else:
            break

    skip_header = len(header_rows)

    #select column names
    for nrow in header_rows[::-1]:
        col_value_year = wk_sheet.cell_value(nrow,0)
        if len(col_value_year)!=0 \
            and nrow==max(header_rows) \
                and len(header_rows)>1:
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


def __name_column(column, list_terms):
    for term in list_terms:
        if term.upper() in column.upper():
            return True
    
    return False


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_saeb'
    tmp = '/tmp/org_raw_saeb/'
    schema = 'inep_saeb'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 2010
        if __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        adl = authenticate_datalake()
        for year, url in __find_download_link(base_value, **kwargs):
            zip_output = tmp + 'microdados_saeb_%s.zip' % year
            __download_file(url, zip_output)

            functions = [__upload_students, __upload_school, __upload_principal,
                            __upload_item, __upload_teacher, __upload_dictionary, __upload_result]

            for func in functions:
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
