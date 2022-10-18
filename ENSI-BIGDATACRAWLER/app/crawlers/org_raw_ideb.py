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


def __create_directory(schema=None, table=None):
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)


def __drop_directory(adl, schema=None, table=None):
    adl_drop_path = __create_directory(schema=schema, table=table)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_files(adl, schema, table, files):
    for file in files:
        split = os.path.basename(file).split('.')
        filename = __normalize_str(split[0])

        file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))
        if file_type[-1] == 'csv':
            __change_encoding(file)

        directory = __create_directory(schema=schema, table=table)
        file_type = '.'.join(file_type)
        adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

        __upload_bs(adl, file, adl_write_path)


def __parse_upload_ts(soup):
    upload_ts = soup.find(name='span', attrs={'class': 'documentModified'})
    upload_ts = upload_ts.find(name='span', attrs={'class': 'value'}).text
    upload_ts = upload_ts.split(' ')
    upload_ts = datetime.strptime(upload_ts[0], '%d/%m/%Y')
    upload_ts = int(upload_ts.timestamp())

    return upload_ts

def __find_download_links(soup):
    parents = soup.find_all(name='div', attrs={'id': 'p_p_id_56_INSTANCE_6G5n5Ax2xN2I_'})

    for parent in parents:

        titles = parent.find_all(name='h3')
        sub_titles = parent.find_all(name='div', attrs={'class': 'list-download'})

        if len(titles) != len(sub_titles):
            break

        for i in range(len(titles)):
            title = titles[i].text
            anchors = sub_titles[i].find_all(name='a')
            for anchor in anchors:
                subs = anchor.text
                link_download = anchor.attrs['href']
                yield title, subs, link_download


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
    request = 'wget --progress=bar:force {url} -O {output} --no-check-certificate'.format(url=url, output=zip_output)
    process = subprocess.Popen(shlex.split(request), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(3600, process.kill)
    try:
        timer.start()
        process.communicate()
    finally:
        timer.cancel()


def __normalize_str(_str):
    return re.sub(r'[,;{}()\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('\n', '_')
                  .replace('/', '_')
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


def __read_xlsx(path, header_rows):
    wk = open_workbook(path)
    for sheet in wk.sheet_names():
        wk_sheet = wk.sheet_by_name(sheet)
        if wk_sheet.visibility == 1:
            continue

        header = __merge_multi_index_header(wk_sheet, header_rows)
        columns_list = list(range(0, len(header)))
        skiprows = header_rows[-1] + 1

        df = pd.read_excel(path, sheet_name=sheet, header=None, names=header, skiprows=skiprows, usecols=columns_list)
        df = df[~df[df.columns[-1]].isna()]
        df['NOME_ARQUIVO'] = sheet

        columns = df.columns.tolist()
        columns = columns[-1:] + columns[:-1]
        df = df.reindex(columns, axis=1)

        for col in df.columns:
            df[col] = df[col].astype(str)

        yield df, sheet


def __get_upper_chars(_str):
    return ''.join(filter(lambda x: x.isupper(), _str))


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_ideb'
    tmp = '/tmp/org_raw_ideb/'

    adl = authenticate_datalake()
    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if kwargs['reload'] is None and __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        res = requests.get('https://www.gov.br/inep/pt-br/areas-de-atuacao/pesquisas-estatisticas-e-indicadores/ideb/resultados')
        soup = bs4.BeautifulSoup(res.text, features='html.parser')
        upload_ts = __parse_upload_ts(soup)
        print(upload_ts)
        if upload_ts > base_value:
            zip_output = tmp + 'ideb.zip'

            for title, subtitle, url in __find_download_links(soup):
                print('title =' + title)

                print('url = ' + url)

                __download_file(url, zip_output)

                for file in __extract_files(zip_output, tmp, prefixes=['.xlsx']):
                    for df, sheet in __read_xlsx(file, header_rows=[7, 8, 9]):
                        if __normalize_str(title) in ['MUNICIPIOS', 'ESCOLAS']:
                            ts = subtitle.split('(')
                            ts2 = ts[0]
                            ts = ts2.split('-')
                            suffix = __get_upper_chars(ts[-1] if len(ts) > 1 else ts2)
                        else:
                            suffix = __get_upper_chars(re.search(r'\((.*)\)', sheet).group())

                        prefix = '_'.join(filter(lambda _chr: len(_chr) > 2, __normalize_str(title).split('_')))
                        table = '{prefix}_{suffix}'.format(prefix=prefix.lower(), suffix=suffix.lower())

                        tmp_parquet = tmp + __normalize_str(sheet) + '.xlsx.parquet'
                        try:
                            df.to_parquet(tmp_parquet, index=False)

                            __drop_directory(adl, schema='inep_ideb', table=table)
                            __upload_files(adl, schema='inep_ideb', table=table, files=[tmp_parquet])
                        finally:
                            print(tmp_parquet)
                            #os.remove(tmp_parquet)

            if kwargs['reload'] is None:
                __call_redis(host, passwd, 'set', key_name, upload_ts)

            return {'exit': 200}
        else:
            return {'exit': 300, 'msg': "They didn't upload a new file"}
    except Exception as e:
        raise e
    finally:
        print(tmp)
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
