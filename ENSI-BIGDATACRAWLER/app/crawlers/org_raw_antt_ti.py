import os
import redis
import time
import requests
from azure.storage.filedatalake import FileSystemClient
import pandas as pd
from bs4 import BeautifulSoup
import urllib.request




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

def __check_path_exists(adl, path):
    try:
        next(adl.get_paths(path, recursive=False, max_results=1))
        return True
    except:
        return False


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


def __upload_file(adl, schema, table, file):
    split = os.path.basename(file).split('.')
    filename = 'antt_ti'

    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)

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
    tmp = '/tmp/org_raw_antt-ti/'
    key_name = 'org_raw_raw_antt-ti'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)

        host, passwd = kwargs['host'], kwargs['passwd']
        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_name)

        base_value = 0
        if kwargs['reload'] is None and __call_redis(host, passwd, 'exists', key_name):
            base_value = int(__call_redis(host, passwd, 'get', key_name))

        last_upload = __transform()
        if last_upload > base_value:
            adl = authenticate_datalake()

            file_output, filename = __download_file(tmp)
            print(file_output)

            if file_output is None:
                return {'exit': 500}

            df = pd.read_csv(file_output, sep=";")


            df[df.columns] = df[df.columns].ffill()



            parquet_output = tmp + filename + '.parquet'
            df.to_parquet(parquet_output, index=False)
            print(parquet_output)
            print("Data da ultima atualização:",__transform())


            __drop_directory(adl, schema='antt', table='ti')
            __upload_file(adl, schema='antt', table='ti', file=parquet_output)

            if kwargs['reload'] is None:
                __call_redis(host, passwd, 'set', key_name, __transform())

            return {'exit': 200}
        else:
            return {'exit': 300, 'msg': "They didn't upload a new file"}
    except Exception as e:
        raise e
    finally:
        print("")



def __download_file(tmp):
    url = 'https://dados.antt.gov.br/dataset/6977061e-0086-4818-808b-c6db68ef4638/resource/f9a956c7-ec27-4fbc-9c99-32561c91bd50/download/solucoes_de_ti_gerenciadas_pela_antt.csv' \

    res = requests.get(url)
    print(res)
    if res.ok:
        filename = 'antt.csv'
        file_output = tmp + filename
        with open(file_output, mode='wb+') as file:
            file.write(res.content)
        return file_output, filename
    return None, None


def __lastupload():
    url = "https://dados.antt.gov.br/dataset/solucoes-ti-gerenciadas/resource/f9a956c7-ec27-4fbc-9c99-32561c91bd50"
    page = urllib.request.urlopen(url)
    html = page.read().decode("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    return soup.body.td.text


def __convert_to_num():
    lp = __lastupload()
    lst = lp.split('/')
    mth = lst[-2]
    dic = {"Janeiro": "01", "Fevereiro": "02", "Março": "03", "Abril": "04", "Maio": "05", "Junho": "06", "Julho": "07",
           "Agosto": "08", "Setembro": "09", "Outubro": "10", "Novembro": "11", "Dezembro": "12"}
    for key in dic:
        if key == mth:
            return dic[key]

def __transform():
    lp = __lastupload()
    lst = lp.split('/')
    day = lst[0]
    mth = __convert_to_num()
    yr = lst[-1]
    string = "{}{}".format(yr,mth)
    number = int(string)
    return number



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
        metadata['msg'] = '{class_type}: {error}'.format(class_type=type(e), error=str(e))
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
