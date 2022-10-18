import os
import shutil
import time
import redis
import requests
import zipfile
import subprocess
import shlex
from azure.storage.filedatalake import FileSystemClient
from ftplib import FTP
from time import strptime
from datetime import datetime
from unicodedata import normalize
from threading import Timer
import datetime
import re
import pandas as pd
import bs4



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


def __extract_files(zip_output, tmp, prefixes):
    with zipfile.ZipFile(zip_output) as _zip:
        data_files = [file for file in _zip.filelist if
                      all(prefix.lower() in (normalize('NFKD', file.filename.lower())
                                             .encode('ASCII', 'ignore')
                                             .decode('ASCII')) for prefix in prefixes)]
        return [_zip.extract(member=data_file, path=tmp) for data_file in data_files]


def __download_ftp_file(tmp, zip_output):

    year = datetime.date.today().year
    print('download_ftp')
    ftp = FTP()
    ftp.connect(host='ftp.ibge.gov.br', port=2121)
    ftp.login()
    a = "/Trabalho_e_Rendimento/Pesquisa_Nacional_por_Amostra_de_Domicilios_continua/"
    b = "Trimestral/Microdados/Documentacao/"
    ftp.cwd(a)
    ftp.cwd(b)

    listing = []
    ftp.retrlines("LIST")
    ftp.retrlines("LIST", listing.append)
    filename = []
    for i in listing:
        words = i.split(None, 8)
        print(words)
        filename = words[-1].lstrip()
        if filename == zip_output:
            mes = strptime(str(words[-4]), '%b').tm_mon
            data = words[-3]+'/'+str(mes)+'/'+str(year)
            print(data)
            break

    # download the file
    local_filename = os.path.join(tmp, filename)
    lf = open(local_filename, "wb")
    ftp.retrbinary("RETR " + filename, lf.write, 8 * 1024)
    lf.close()


def __get_date_arch(url):
    cmd = 'wget --progress=bar:force -O- -e use_proxy=yes {url}'.format(url=url)
    stdin, stdout = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE).communicate()
    soup = bs4.BeautifulSoup(stdin, features='html.parser')
    print('soup ->')
    print(soup)

    parent = soup.find_all('tr')
    print('parentt')
    for p in parent:
        if p.find('a', attrs={'href': 'Deflatores.zip'}):
            date_arch = p.td.next_sibling.next_sibling.text
            date_arch = date_arch.split(' ')[0]
            print(len(date_arch))
            print(date_arch)
            print('um:')
            yield date_arch


def __download_file(url, zip_output):
    print('__download_file')
    cmd = 'wget --progress=bar:force {url} -nv -O {output} --no-check-certificate'.format(url=url, output=zip_output)
    process = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(3600, process.kill)
    try:
        timer.start()
        process.communicate()
    finally:
        timer.cancel()


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


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '')
                  .upper())


def __upload_file(adl, schema, table, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])

    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __create_directory(schema=None, table=None):
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)


def __drop_directory(adl, schema=None, table=None):
    adl_drop_path = __create_directory(schema=schema, table=table)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


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


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    tmp = '/tmp/org_raw_deflatores/'
    key_name = 'org_raw_deflatores'
    zip_output = 'Deflatores.zip'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        ftp_dir = ('http://ftp.ibge.gov.br/Trabalho_e_Rendimento/'
                   'Pesquisa_Nacional_por_Amostra_de_Domicilios_continua/Trimestral/Microdados/Documentacao/')

        date_arch = __get_date_arch(url=ftp_dir)
        print('ftp_ls_dir')
        date_arch = list(date_arch)[0]
        date_arch = __normalize_str(date_arch)
        date_arch = int(date_arch)
        print('date_arch')
        print(date_arch)

        last_upload_ts = 0
        if kwargs['reload'] is None and __call_redis(host, passwd, 'exists', key_name):
            last_upload_ts = int(__call_redis(host, passwd, 'get', key_name))
            print('last_upload_ts')
            print(last_upload_ts)

        if date_arch > last_upload_ts:

            ftp_dir = ftp_dir + zip_output
            zip_output = tmp + zip_output

            __download_file(ftp_dir, zip_output)

            path_file = __extract_files(zip_output, tmp, prefixes=['.xls'])
            file = path_file[0]
            print(file)

            df = pd.read_excel(path_file[0])
            print(df)
            parquet_output = file + '.parquet'

            df.to_parquet(parquet_output, index=False)
            print(parquet_output)
            print(type(parquet_output))
            adl = authenticate_datalake()
            __drop_directory(adl, schema='ibge', table='deflatores')
            __upload_file(adl, schema='ibge', table='deflatores', file=parquet_output)

            if kwargs['reload'] is None:
                __call_redis(host, passwd, 'set', key_name, date_arch)

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
