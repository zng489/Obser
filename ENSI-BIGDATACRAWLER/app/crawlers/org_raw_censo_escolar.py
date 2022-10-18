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


def __extract_files(zip_output, tmp, prefixes):
    tmp_compress = tmp + 'rar_output'
    with zipfile.ZipFile(zip_output) as _zip:
        data_files = [file for file in _zip.filelist if
                      all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                             .encode('ASCII', 'ignore')
                                             .decode('ASCII')) for prefix in prefixes)]
        file_paths = [_zip.extract(member=data_file, path=tmp) for data_file in data_files
                      if '$' not in data_file.filename]

    for file in file_paths[:]:
        if file.endswith('.rar') or file.endswith('.zip'):
            extractor = rarfile.RarFile(file) if file.endswith('.rar') else zipfile.ZipFile(file)
            extractor.extractall(path=tmp_compress)
            extractor.close()

            file_paths.remove(file)

    if os.path.exists(tmp_compress):
        compress_files = [os.path.join(tmp_compress, path) for path in os.listdir(tmp_compress)]
        file_paths.extend(compress_files)

    return file_paths


def __download_file(url, zip_output):
    request = 'wget --progress=bar:force {url} -O {output}'.format(url=url, output=zip_output)
    process = subprocess.Popen(shlex.split(request), stdin=subprocess.PIPE)
    timer = Timer(3600, process.kill)
    try:
        timer.start()
        process.communicate()
    finally:
        timer.cancel()


def __normalize_str(_str: str):
    return re.sub(r'[.,;{}()\n\t=-]', '', normalize('NFKD', _str.strip())
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .upper())


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


def __upload_school_files(adl, schema, year, zip_output, tmp):
    school_tmp = tmp + 'school/'
    school_files = __extract_files(zip_output, school_tmp, ['dados', 'escolas'])
    assert len(school_files) == 1

    table = 'escola'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=school_files)


def __parse_categories_line_break(categories):
    categories = list(re.split(r'(\d)', c) for c in categories)
    categories = list(item for sublist in categories for item in sublist)

    new_categories = []

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
    return new_categories


def __aggregate_category(categories, year):
    if not any(nan for nan in categories.isnull()):
        agg = {}

        categories = categories.to_dict().values()
        if year > 2016:
            categories = __parse_categories_line_break(categories)
        else:
            categories = list(c.split('-', 1) for c in categories)

        null = -1
        for category in categories:
            category = list(c.strip() for c in category)

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


def __find_skiprows_attachment(content, sheet, header_name):
    wk = open_workbook(file_contents=content)
    wk_sheet = wk.sheet_by_name(sheet)
    if wk_sheet.visibility == 1:
        return -1

    for row_index in range(0, wk_sheet.nrows):
        row = wk_sheet.row(row_index)

        if __normalize_str(row[0].value) == __normalize_str(header_name):
            return row_index


def __upload_parquet_file(df, parquet_output):
    try:
        df.to_parquet(parquet_output, index=False)
        yield parquet_output
    except Exception as e:
        raise e
    finally:
        if os.path.exists(parquet_output):
            os.remove(parquet_output)


def __transform_attachments(attachments, year):
    for attachment in attachments:
        with open(attachment, mode='rb') as file:
            content = file.read()
            pd_excel = pd.ExcelFile(content)

        filename, ext = os.path.basename(attachment).split('.')

        if year == 2015:
            cols = ['N', 'NOME_VARIAVEL', 'NOME_VARIAVEL_ANTIGO', 'DESCRICAO', 'TIPO', 'TAMANHO', 'CATEGORIA']
        else:
            cols = ['N', 'NOME_VARIAVEL', 'DESCRICAO', 'TIPO', 'TAMANHO', 'CATEGORIA']

        sheet_names = pd_excel.sheet_names if year not in [2017, 2018] else pd_excel.sheet_names[:-1]
        for sheet in sheet_names:
            skiprows = __find_skiprows_attachment(content, sheet, header_name='N')
            df = pd_excel.parse(sheet_name=sheet, index_col=False, skiprows=skiprows)
            if year > 2016:
                df = pd.DataFrame(df.values[2:], columns=df.columns)
                if len(df.columns) > 6:
                    df = df.drop(columns=df.columns[6:], axis='index')

            df.columns = cols
            if year == 2015:
                df = df.drop(columns='NOME_VARIAVEL_ANTIGO', axis='index')

            # Creates column ASSUNTO and fill for every row
            topic_col = 'DESCRICAO' if 2016 < year < 2019 else 'N'
            df['ASSUNTO'] = np.where(df['NOME_VARIAVEL'].isnull() & df['CATEGORIA'].isnull(), df[topic_col], np.nan)

            # Forward fill columns
            for col in df.columns:
                if col not in ['CATEGORIA']:
                    df[col] = df[col].ffill()

            # Remove comments inside DF
            df = df[df['DESCRICAO'] != df['ASSUNTO']]

            df['N'] = df['N'].astype(str)
            df = df[df['N'].str.replace('.', '').str.isdigit()]
            df = df.reset_index(drop=True)

            # Concat CATEGORIA rows into one
            df_categories = df.groupby(by='N')['CATEGORIA'] \
                .apply(__aggregate_category, *[year]) \
                .to_frame() \
                .reset_index()

            # Merge DF with concatenated categories
            df = df.drop(columns='CATEGORIA', axis='index')
            df = df.merge(df_categories, on='N')

            # Drop duplicated values (because has multi index column)
            df = df.drop_duplicates(subset='N')
            df = df.drop(columns='N', axis='index')
            df = df.reset_index(drop=True)

            # Create filename column
            df['NOME_ARQUIVO'] = '%s.%s' % (filename, ext)

            # Reorder columns position
            reorder_cols = ['NOME_ARQUIVO', 'ASSUNTO', 'NOME_VARIAVEL', 'DESCRICAO',
                            'CATEGORIA', 'TAMANHO', 'TIPO']
            df = df.reindex(reorder_cols, axis='columns')

            # Change NaN to None
            df = df.where(df.notnull(), None)

            # Cast all columns to str before transform into parquet
            for col in df.columns:
                df[col] = df[col].astype(str)

            parquet_output = '/tmp/{file}_{sheet}.{ext}.parquet'.format(file=filename, sheet=sheet, ext=ext)
            yield from __upload_parquet_file(df, parquet_output)


def __upload_school_metadata(adl, schema, year, zip_output, tmp):
    attachment_tmp = tmp + 'attachment/'
    if year > 2016:
        prefixes = ['dicionario', 'educa', 'basica.xlsx'] if year > 2018 else ['dicionario', 'educa', 'basica', '.xlsx']
        attachments = __extract_files(zip_output, attachment_tmp, prefixes)
    else:
        extension = '.xlsx' if year > 2015 else '.xls'
        attachments = __extract_files(zip_output, attachment_tmp, ['anexo i -', extension])
    assert len(attachments) == 1

    table = 'metadados_escola'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=__transform_attachments(attachments, year))


def __upload_class_files(adl, schema, year, zip_output, tmp):
    class_tmp = tmp + 'class/'
    class_files = __extract_files(zip_output, class_tmp, ['turmas'])
    assert len(class_files) == 1

    table = 'turmas'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=class_files)


def __upload_registration(adl, schema, year, zip_output, tmp):
    reg_tmp = tmp + 'registration/'
    reg_files = __extract_files(zip_output, reg_tmp, ['matricula'])
    assert len(reg_files) > 0

    table = 'matriculas'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=reg_files)


def __upload_teacher(adl, schema, year, zip_output, tmp):
    tea_tmp = tmp + 'teacher/'
    tea_files = __extract_files(zip_output, tea_tmp, ['docentes_'])
    assert len(tea_files) > 0

    table = 'docente'
    __drop_directory(adl, schema, table=table, year=year)
    __upload_files(adl, schema, table=table, year=year, files=tea_files)


def __upload_manager(adl, schema, year, zip_output, tmp):
    mng_tmp = tmp + 'manager/'
    mng_files = __extract_files(zip_output, mng_tmp, ['gestor', 'csv'])
    # assert len(mng_files) > 0

    if len(mng_files) > 0:
        table = 'gestor'
        __drop_directory(adl, schema, table=table, year=year)
        __upload_files(adl, schema, table=table, year=year, files=mng_files)


def __transform_aux_files(aux_files):
    for aux_file in aux_files:
        with open(aux_file, mode='rb') as file:
            content = file.read()
            pd_excel = pd.ExcelFile(content)

        filename, ext = os.path.basename(aux_file).split('.')
        for sheet in pd_excel.sheet_names:
            sheet_name = sheet.lower()

            df, table, reorder_cols = None, None, []
            if sheet_name.startswith('anexo2'):
                skiprows = __find_skiprows_attachment(content, sheet, 'Eixo')
                df = pd_excel.parse(sheet, index_col=False, skiprows=skiprows)

                df = df.drop(columns=df.columns[3:], axis='index')
                df.columns = ['EIXO', 'COD_CURSO', 'NOME_CURSO']

                df['EIXO'] = df['EIXO'].ffill()
                df = df.dropna()
                df['COD_CURSO'] = df['COD_CURSO'].astype(int)

                table = 'curso_educacao_profissional'
                reorder_cols = ['NOME_ARQUIVO'] + df.columns.tolist()

            if sheet_name.startswith('anexo4'):
                skiprows = __find_skiprows_attachment(content, sheet, 'COD')
                df = pd_excel.parse(sheet, index_col=False, skiprows=skiprows)

                df = df.drop(columns=df.columns[3:], axis='index')
                df.columns = ['COD', 'CODIGO_ISO_ALPHA_3', 'NOME_PAIS_OU_AREA']

                df = df.dropna(subset=['NOME_PAIS_OU_AREA'])
                df['COD'] = df['COD'].astype(int)

                table = 'paises'
                reorder_cols = ['NOME_ARQUIVO'] + df.columns.tolist()

            if sheet_name.startswith('anexo5'):
                skiprows = __find_skiprows_attachment(content, sheet, 'CODIGO_AREA')
                df = pd_excel.parse(sheet, index_col=False, skiprows=skiprows)

                df = df.drop(columns=df.columns[5:], axis='index')
                df.columns = ['CODIGO_AREA', 'NOME_AREA', 'CODIGO_OCDE', 'NOME_GRAU', 'GRAU_ACADEMICO']

                ffill = ['CODIGO_AREA', 'NOME_AREA']
                df[ffill] = df[ffill].ffill()
                df = df.dropna(subset=['NOME_GRAU'])

                df[df.columns] = df[df.columns].astype(str)

                table = 'cursos_form_sup'
                reorder_cols = ['NOME_ARQUIVO'] + df.columns.tolist()

            if sheet_name.startswith('anexo6'):
                skiprows = __find_skiprows_attachment(content, sheet, 'CODIGO_IES')
                df = pd_excel.parse(sheet, index_col=False, skiprows=skiprows)
                df = df.drop(columns=df.columns[8:], axis='index')

                df.columns = ['CODIGO_IES', 'NOME_IES_INSTITUICAO_ENSINO_SUPERIOR',
                              'CODIGO_UF', 'CODIGO_MUNICIPIO',
                              'CODIGO_DEPENDENCIA_ADMINISTRATIVA', 'NOME_DEPENDENCIA_ADMINISTRATIVA',
                              'TIPO_INSTITUICAO', 'CONDICAO_FUNCIONAMENTO']
                df = df.dropna(subset=['NOME_IES_INSTITUICAO_ENSINO_SUPERIOR'])

                int_cols = ['CODIGO_IES', 'CODIGO_UF', 'CODIGO_MUNICIPIO', 'CODIGO_DEPENDENCIA_ADMINISTRATIVA']
                df[int_cols] = df[int_cols].astype('Int64')

                table = 'ies'
                reorder_cols = ['NOME_ARQUIVO'] + df.columns.tolist()

            if sheet_name.startswith('anexo7'):
                skiprows = __find_skiprows_attachment(content, sheet, 'CODIGO')
                df = pd_excel.parse(sheet, index_col=False, skiprows=skiprows)

                df = df.drop(columns=df.columns[2:], axis='index')
                df.columns = ['CODIGO', 'NOME_ETAPA']

                df = df.dropna()
                df['CODIGO'] = df['CODIGO'].astype(int)

                table = 'etapa_ensino'
                reorder_cols = ['NOME_ARQUIVO'] + df.columns.tolist()

            if sheet_name.startswith('anexo8'):
                skiprows = __find_skiprows_attachment(content, sheet, 'CODIGO')
                df = pd_excel.parse(sheet, index_col=False, skiprows=skiprows)
                df.columns = ['CODIGO', 'AREA_CONHECIMENTO_COMPONENTES_CURRICULARES']

                df = df.dropna(subset=['AREA_CONHECIMENTO_COMPONENTES_CURRICULARES'])
                df['CODIGO'] = df['CODIGO'].astype(int)

                table = 'area_conhecimento'
                reorder_cols = ['NOME_ARQUIVO'] + df.columns.tolist()

            if df is not None:
                normalized_sheet_name = __normalize_str(sheet_name)

                # Create filename column
                df['NOME_ARQUIVO'] = '%s__%s.%s' % (filename, normalized_sheet_name, ext)

                # Reorder columns position
                df = df.reindex(reorder_cols, axis='columns')

                parquet_output = '/tmp/{file}_{sheet}.{ext}.parquet'.format(file=filename,
                                                                            sheet=normalized_sheet_name, ext=ext)
                yield table, __upload_parquet_file(df, parquet_output)


def __upload_aux_tables(adl, schema, year, zip_output, tmp):
    aux_tmp = tmp + 'aux/'
    aux_files = __extract_files(zip_output, aux_tmp, ['auxiliares.xlsx'])
    assert len(aux_files) > 0

    for table, files in __transform_aux_files(aux_files):
        __drop_directory(adl, schema, table=table, year=year)
        __upload_files(adl, schema, table=table, year=year, files=files)


def __call_redis(host, password, function_name, *args):
    db = redis.Redis(host=host, password=password, port=6379, db=0, socket_keepalive=True, socket_timeout=2)
    try:
        method_fn = getattr(db, function_name)
        return method_fn(*args)
    except Exception as _:
        raise _
    finally:
        db.close()


def __find_download_links(**kwargs):
    base_year = 2008
    reload = kwargs.get('reload', None)

    res = requests.get('https://www.gov.br/inep/pt-br/acesso-a-informacao/dados-abertos/microdados/censo-escolar')
    soup = bs4.BeautifulSoup(res.text, features='html.parser')
    parent = soup.find(name='div', attrs={'id': 'content-core'})

    links = zip(parent.find_all(name='strong'), parent.find_all(name='a'))
    for strong, anchor in sorted(links, key=lambda v: int(v[0].text)):
        year = int(re.search(r'\d{4}', strong.text).group())
        if (reload is None and year > base_year) or (reload == year):
            match = re.search(r'\d{1,2}/\d{1,2}/\d{4}', anchor.text)

            updated_ts = 0
            if match is not None:
                updated_ts = int(datetime.strptime(match.group(), '%d/%m/%Y').timestamp())
            yield year, updated_ts, anchor.attrs['href']


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_censo_escolar'
    tmp = '/tmp/org_raw_censo_escolar/'

    adl = authenticate_datalake()
    try:
        if kwargs['reset']:
            keys = __call_redis(host, passwd, 'keys', key_name + ":*")
            for key in keys:
                __call_redis(host, passwd, 'delete', key.decode())

        schema = 'inep_censo_escolar'
        for year, updated_ts, url in __find_download_links(**kwargs):
            key_year = '{key}:{year}'.format(key=key_name, year=year)

            last_updated_ts = None
            if __call_redis(host, passwd, 'exists', key_year):
                last_updated_ts = int(__call_redis(host, passwd, 'get', key_year))

            if last_updated_ts is None or updated_ts > last_updated_ts:
                os.makedirs(tmp, mode=0o777, exist_ok=True)

                zip_output = tmp + 'micro_censo_escolar_%s.zip' % year
                __download_file(url, zip_output)

                __upload_school_files(adl, schema, year, zip_output, tmp)
                __upload_school_metadata(adl, schema, year, zip_output, tmp)
                __upload_class_files(adl, schema, year, zip_output, tmp)
                __upload_registration(adl, schema, year, zip_output, tmp)
                __upload_manager(adl, schema, year, zip_output, tmp)
                __upload_teacher(adl, schema, year, zip_output, tmp)
                if year > 2016:
                    __upload_aux_tables(adl, schema, year, zip_output, tmp)

                if kwargs['reload'] is None:
                    if updated_ts == 0:
                        updated_ts = int(datetime.now().timestamp())
                    __call_redis(host, passwd, 'set', key_year, updated_ts)

                shutil.rmtree(tmp)
        return {'exit': 200}
    except Exception as e:
        raise e
    finally:
        if os.path.exists(tmp):
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
