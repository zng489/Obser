import os
import re
import shutil
import time
from unicodedata import normalize
from datetime import date
import json

import bs4
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
                    file_client.upload_data(data=chunk, overwrite=True)
    except Exception as e:
        file_client.delete_file()
        raise e


def __create_directory(schema=None, table=None, year=None):
    if year:
        return '{lnd}/{schema}__{table}/{year}'.format(lnd=LND, schema=schema, table=table, year=year)
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)


def __adl_file_path(adl, schema, table, year, file):

    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_file_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    return adl_file_path


def __drop_file(adl, schema, table, year, file):

    adl_file_path = __adl_file_path(adl, schema, table, year, file)

    try:
        file = adl.get_file_client(adl_file_path)
        file.get_file_properties()
        file.delete_file()
    except:
        pass


def __upload_file(adl, schema, table, file, year=None):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __find_download_link(base_names, **kwargs):
    year_today = date.today().year
    years = [*range(2010, year_today+1)] if kwargs['reload'] is None else [int(kwargs['reload'])]

    url_base = 'https://www.imf.org'
    url_entire = '/en/Publications/WEO/weo-database/{}/{}'

    #adicionar novos meses caso sejam criados no site de origem
    months = (
        # 'january',
        # 'february',
        # 'march', 
        ('april','ABR'), 
        # 'may', 
        # 'june',
        # 'july', 
        # 'august',
        ('september', 'SET'), 
        ('october', 'OUT')
        # 'novermber', 
        # 'december'
        )

    dict_base = {
            'by_countries': 'pais',
            'by_country_groups': 'grupo_paises'
    }

    for year in years:
        for month in months:

            urls = []

            res = requests.get(url_base+url_entire.format(year, month[0]))

            if 'error' not in res.url:
                soup = bs4.BeautifulSoup(res.text, features='html.parser')

                entire_tag = soup.find(lambda tag:tag.name=='a' and 'Entire Dataset' == tag.text.strip())
                assert entire_tag!=None
                entire_href = entire_tag['href']

                res_second = requests.get(url_base+entire_href)
                soup_second = bs4.BeautifulSoup(res_second.text, features='html.parser')

                countries_tag = soup_second.find(lambda tag:tag.name=='a' and 'By Countries' == tag.text.strip())
                assert countries_tag!=None

                url_download = url_base+countries_tag.attrs['href']
                if kwargs['reload'] is not None and year == int(kwargs['reload']):
                    urls.append((dict_base.get(__normalize_str(countries_tag.text).lower()), url_download))
                elif url_download not in base_names:
                    urls.append((dict_base.get(__normalize_str(countries_tag.text).lower()), url_download))

                countries_group_tag = soup_second.find(lambda tag:tag.name=='a' and 'By Country Groups' == tag.text.strip())
                assert countries_group_tag!=None

                url_download = url_base+countries_group_tag.attrs['href']
                if kwargs['reload'] is not None and year == int(kwargs['reload']):
                    urls.append((dict_base.get(__normalize_str(countries_group_tag.text).lower()), url_download))
                elif url_download not in base_names:
                    urls.append((dict_base.get(__normalize_str(countries_group_tag.text).lower()), url_download))
        
            if len(urls) > 0:
                yield year, month[1], urls


def __download_file(url, tmp):
    session =  requests.Session()
    request = session.get(url, timeout=3600)

    file_path = tmp+request.headers['content-disposition'].split('=')[-1].replace('"','')
    with open(file_path, 'wb') as file:
        file.write(request.content)

    return file_path


def __normalize_str(_str):
    return re.sub(r'[,{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('|', '_')
                  .replace('/', '_')
                  .replace('.', '_')
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


def __parse_csv(output_file):

    df = pd.read_csv(output_file, sep='\t')
    if pd.isnull(df.iloc[0][0]):
        df = pd.read_csv(output_file, sep='\t', encoding='utf-16-le')
    return df



def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_names = 'org_raw_fmi_proj'
    tmp = '/tmp/org_raw_fmi_proj/'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_names)

        base_names = []
        if __call_redis(host, passwd, 'exists', key_names):
            base_names = json.loads(__call_redis(host, passwd, 'get', key_names))

        adl = authenticate_datalake()
        for year, month, urls in __find_download_link(base_names, **kwargs):
            for table_name, url in urls:
                
                output_file = __download_file(url, tmp)
                df = __parse_csv(output_file)

                parquet_output = tmp + '{month}-{file}.parquet'.format(month=month, file=os.path.basename(output_file))

                __drop_file(adl, schema='fmi_proj', table=table_name, year=str(year), file=parquet_output)

                df.to_parquet(parquet_output, index=False)
                __upload_file(adl, schema='fmi_proj', table=table_name, year=str(year), file=parquet_output)

                if kwargs['reload'] is None:
                    base_names.append(url)
                    __call_redis(host, passwd, 'set', key_names, json.dumps(base_names))

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
