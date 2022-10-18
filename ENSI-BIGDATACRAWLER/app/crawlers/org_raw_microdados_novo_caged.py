import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import datetime
from threading import Timer
from unicodedata import normalize
import csv
import gc

import bs4
import py7zr
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
                    file_client.upload_data(data=chunk, overwrite=True, timeout=1800)
    except Exception as e:
        file_client.delete_file()
        raise e


def __create_directory(schema=None, table=None, year=None):
    if year:
        return '{lnd}/{schema}__{table}/{year}'.format(lnd=LND, schema=schema, table=table, year=year)
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)


def __drop_directory(adl, schema=None, table=None, year=None):
    adl_drop_path = __create_directory(schema=schema, table=table, year=year)
    print(adl_drop_path)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, year, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    print(adl_write_path)
    __upload_bs(adl, file, adl_write_path)


def __list_ftp_dir(url, order=None):
    cmd = 'wget --progress=bar:force  --timeout=3000 -O- -e use_proxy=yes {url}'.format(url=url)
    stdin, stdout = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE).communicate()
    soup = bs4.BeautifulSoup(stdin, features='html.parser')
    if len(soup) == 0:
        raise Exception('Something bad is occurring')

    tags_for_date = [[tag.find('td', attrs={'class': 'date'}).text, tag.find('td', attrs={'class': 'filename'}).text] \
        for tag in soup.find_all('tr', attrs={'class': 'entry'})]
    order_tag = 1 if order=='filename' else 0
    tags_for_date = sorted(tags_for_date, key=lambda tag: tag[order_tag])

    for filename in tags_for_date:
        yield filename[1]


def __download_file(url, zip_output):
    cmd = 'wget --progress=bar:force --timeout=3000 -e use_proxy=yes {url} -O {output}'.format(url=url, output=zip_output)
    process = subprocess.Popen(shlex.split(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    timer = Timer(1000, process.kill)

    try:
        timer.start()
        process.communicate()
    finally:
        timer.cancel()


def __extract_files(tmp, output):
    with py7zr.SevenZipFile(output, 'r') as z:
        allfiles = z.getnames()
        z.extract(path=tmp, targets=allfiles)

    return allfiles


def __parse_csv(base_path, allfiles):
    df = pd.DataFrame()
    header = None
    for file in allfiles:
        file_path = base_path+file
        with open(file_path, 'r', encoding='utf-8') as infile:
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

        _df = pd.read_csv(  file_path, 
                            sep=';', 
                            header=None, 
                            names=header, 
                            skiprows=skip, 
                            index_col=False, 
                            encoding='ISO-8859-1',
                            dtype=str)

        _df['NOME_ARQUIVO'] = os.path.basename(file)
        
        df = pd.concat([df, _df], axis='index', ignore_index=True)
        
    return df


def __normalize_str(_str):
    return re.sub(r'[,{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('|', '_')
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


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_name = 'org_raw_microdados_novo_caged'
    tmp = '/tmp/org_raw_microdados_novo_caged/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            keys = __call_redis(host, passwd, 'keys', key_name + ":*")
            for key in keys:
                __call_redis(host, passwd, 'delete', key.decode())


        base_ftp_server = 'ftp://ftp.mtps.gov.br/pdet/microdados/NOVO%20CAGED'

        print('Acessando servidor FTP:')
        print(f'{base_ftp_server}')

        years = sorted(filter(lambda _dir: _dir.isdigit() and int(_dir) > 2019, __list_ftp_dir(base_ftp_server)))

        print('Diretorios de anos disponiveis:')
        print(*years, sep=', ')


        if kwargs['reload'] is not None:
            reload_year = str(kwargs['reload'])
            if reload_year not in years:
                return {'exit': 404, 'msg': '%s not found' % reload_year, 'available_years': years}
            years = [reload_year]

        print('Autenticando ao data lake.')
        adl = authenticate_datalake()
        schema, table = 'me', 'novo_caged_'
        for year in years:

            key_name_year = '{key}:{year}'.format(key=key_name, year=year)
            ftp_folder_year = '{base}/{year}'.format(base=base_ftp_server, year=year)

            print(f'Verificando diretorio ANO FTP: {ftp_folder_year}')
            for folder_month in __list_ftp_dir(ftp_folder_year, order='filename'):

                ftp_folder_month = '{base}/{file}'.format(base=ftp_folder_year, file=folder_month)

                last_file_month = -1
                if kwargs['reload'] is None:
                    if __call_redis(host, passwd, 'exists', key_name_year):
                        last_file_month = int(__call_redis(host, passwd, 'get', key_name_year))

                num_month = int(datetime.strptime(folder_month, '%Y%m').timestamp())

                print(f'Verificando diretorio ANOMES FTP: {ftp_folder_month}')
                if last_file_month >= num_month:
                    print('Diretorio ja foi carregado anteriormente.')
                    continue

                for file in __list_ftp_dir(ftp_folder_month, order='filename'):
                    if 'MOV' in file.upper() or \
                        'FOR' in file.upper() or \
                            'EXC' in file.upper():
   
                        file_type = file.replace('CAGED', '')[:3]

                        ftp_file = '{base}/{file}'.format(base=ftp_folder_month, file=file)
                        zip_output = tmp + file

                        print(f'Realizando download do arquivo no servidor FTP: {ftp_file}')
                        __download_file(ftp_file, zip_output)

                        print(f'Realizando extracao do arquivo: {zip_output}')
                        allfiles = __extract_files(tmp, zip_output)

                        print(f'Realizando leitura do arquivo .csv e gerando DataFrame em memoria: {allfiles}')
                        df = __parse_csv(tmp, allfiles)
                        df['ORIGEM_DOWNLOAD'] = '{type}-{folder}'.format(type=file_type, folder=folder_month)

                        parquet_output = tmp + '{file}.parquet'.format(file=file)
                        print(f'Realizando parse do arquivo para {parquet_output}')
                        df.to_parquet(parquet_output, index=False)

                        del df
                        gc.collect()
                        
                        print(f'Dropando diretorio no data lake')
                        __drop_directory(adl, schema, table+file_type.lower(), folder_month)

                        print(f'Realizando upload de arquivo para data lake')
                        __upload_file(adl, schema, table+file_type.lower(), folder_month, parquet_output)

                if kwargs['reload'] is None:
                    __call_redis(host, passwd, 'set', key_name_year, num_month)

                    
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