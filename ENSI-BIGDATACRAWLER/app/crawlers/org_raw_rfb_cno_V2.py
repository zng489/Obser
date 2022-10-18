import os
import time
import redis
from azure.storage.filedatalake import FileSystemClient
import requests, zipfile, io
# import wget
import re
import shlex
import shutil
import subprocess
from zipfile import ZipFile
from threading import Timer
from unicodedata import normalize
import pandas as pd
import glob
from tqdm import tqdm
from urllib.request import urlopen
import concurrent.futures
from concurrent.futures import as_completed



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



#def download_file(url: str, output_path: str, remover=True) -> None:
#    file = url.split('/')[-1].split('.')[0].split('?')[0]+'.zip'
#    r = wget.download(url,output_path)
#    ## print(output_path) 
#    resp = requests.get(url)
#    try:
#        if resp.status_code == 200:
#            with ZipFile(output_path + file, 'r') as zipObj:
#                zipObj.extractall(output_path)
#                ## print('File is unzipped in tmp folder')
#                time.sleep(10)
#            if remover == True:
#                os.remove(output_path + file)  
#        else:
#            raise Exception(f'status_code not 200. Server message: Code {r.status_code} | {r.text}')
#    except:
#        download_file(url, output_path, remover=True)


#def download_file(url: str, output_path: str, remover=True) -> None:
#    file = url.split('/')[-1].split('.')[0].split('?')[0]+'.zip'
#    ## print(output_path)
#    R = requests.get(url, stream=True)
#    try:
#        if R.status_code == 200:
#            with open( tmp + file, 'wb') as r:
#                r.write(R.content)
#            with zipfile.ZipFile(tmp + file,'r') as z:
#                z.extractall(path=tmp)
#                ## print('File is unzipped in tmp folder')
#                time.sleep(10)
#            if remover == True:
#                os.remove(output_path + file)  
#        else:
#            raise Exception(f'status_code not 200. Server message: Code {r.status_code} | {r.text}')
#    except:
#        download_file(url, output_path, remover=True)


#def download_file(url: str, output_path: str, remover=True) -> None:
#    tmp = output_path
#    block_size = 1024 #1 Kibibyte
#    file = url.split('/')[-1].split('.')[0].split('?')[0]+'.zip'
#    ## print(output_path)
#    R = requests.get(url, stream=True)
#    site = urlopen(url)
#    meta = site.info()
#    # Streaming, so we can iterate over the response.
#    #response = requests.get(url, stream=True)
#    total_size_in_bytes = int(meta["Content-Length"])
#    progress_bar = tqdm(total = total_size_in_bytes, unit='iB', unit_scale=True)
#    try:
#        if R.status_code == 200:
#            with open( tmp + file, 'wb') as r:
#                #r.write(R.content)
#                for data in R.iter_content(block_size):
#                    progress_bar.update(len(data))
#                    r.write(data)
#                progress_bar.close()
#
#            with zipfile.ZipFile(tmp + file,'r') as z:
#                z.extractall(path=tmp)
#               ## print('File is unzipped in tmp folder')
#               # time.sleep(10)
#            if remover == True:
#                os.remove(output_path + file)  
#        else:
#            raise Exception(f'status_code not 200. Server message: Code {R.status_code} | {R.text}')
#    except:
#        download_file(url, output_path, remover=True)


# r'C:\Users\acessocni01\Desktop\Test\file_'
def extract_all_zip(path, file_name,remover=True) -> None:
    with zipfile.ZipFile(path + file_name,'r') as z:
        z.extractall(path)
        time.sleep(10)
        if remover == True:
            #os.remove(path)
            shutil.rmtree(path, ignore_errors=True)
            #for file in os.listdir(path):
            #    os.remove(file) 
            print("Deleted '%s' directory successfully" % path)  
        else:
            raise Exception(f'status_code not 200.')


def download_part(path, url_and_headers_and_partfile):
    url, headers, partfile = url_and_headers_and_partfile
    response = requests.get(url, headers=headers)
    
    chunk_size = 1024*1024

    
    size=0 
    with open(path + partfile, 'wb') as f:
        for chunk in response.iter_content(chunk_size):
            if chunk:
                size+=f.write(chunk)
    return size

def make_headers(start, chunk_size):
    end = start + chunk_size - 1
    return {'Range': f'bytes={start}-{end}'}



def __delete_file():
    test = os.listdir('././.')
    for item in test:
        if item.endswith(".tmp"):
            ## print(item)
            os.remove(item)



#def extract_all_zip(output_path: str, remover=True) -> None:
#    for file in os.listdir(output_path):
#        ## print(output_path)
#        file = output_path+file
#        ## print(file)
#        if file.endswith('.zip'):
#            with zipfile.ZipFile(file,'r') as z:
#                z.extractall(path=output_path)
#                ## print(output_path)
#            if remover == True:
#                os.remove(file)

#def extract_all_zip(output_path: str, remover=True) -> None:
#    for file in os.listdir(output_path):
#        ## print(output_path)
#        file = output_path+file
#        ## print(file)
#        if file.endswith('.zip'):
#            with zipfile.ZipFile(file,'r') as z:
#                z.extractall(path=output_path)
#                ## print(output_path)
#            if remover == True:
#                os.remove(file)



def __check_path_exists(adl, path):
    try:
        next(adl.get_paths(path, recursive=False, max_results=1))
        return True
    except:
        return False
def __create_directory(schema=None, table=None, year=None):
    if year:
        return '{lnd}/{schema}__{table}/{year}'.format(lnd=LND, schema=schema, table=table, year=year)
    return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)
    # {lnd}/{schema}{table}

def __drop_directory(adl, schema=None, table=None, year=None):
    adl_drop_path = __create_directory(schema=schema, table=table, year=year)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)



def __upload_bs(adl, lpath, rpath) -> None:
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
                  .replace('-', '_')
                  .upper())



def __upload_file(adl, schema, table, year, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)



def main(**kwargs):    
    url = 'https://cdn-103.anonfiles.com/E8a3e3Bay2/c66a4493-1666010919/cno.zip'
    #URL = 'http://200.152.38.155/CNO/cno.zip'
    #URL = 'https://github.com/zng489/pentaho/blob/main/CNO.zip?raw=true'

    adl = authenticate_datalake()
    schema = 'rfb_cno'
    table = 'cadastro_nacional_de_obras'
    #year = None
    #tmp = '/tmp/org_raw_rfb_cno/'
    tmp = 'C:/Users/acessocni01/Desktop/Test/file_/'
    os.makedirs(tmp, mode=0o777, exist_ok=True)
    #start = time.time()
    try:
        print('Starting....')


        #path = 'C:/Users/acessocni01/Desktop/Test/file_/'
        file_name = 'cno.zip'
        response = requests.get(url, stream=True)
        file_size = int(response.headers.get('content-length', 0))
        chunk_size = 1024*1024
        #os.makedirs(path, mode=0o777, exist_ok=True) 

        chunks = range(0, file_size, chunk_size)
        my_iter = [[url, make_headers(chunk, chunk_size), f'{file_name}.part{i}'] for i, chunk in enumerate(chunks)] 

        with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
            jobs = [executor.submit(download_part,tmp,i) for i in my_iter]

        with tqdm(total=file_size, unit='iB', unit_scale=True, unit_divisor=chunk_size, leave=True, colour='cyan') as bar:
            for job in as_completed(jobs):
                size = job.result()
                bar.update(size)

        with open(tmp + file_name, 'wb') as outfile:
            for i in range(len(chunks)):
                chunk_path = tmp + f'{file_name}.part{i}'
                with open(chunk_path, 'rb') as s:
                    outfile.write(s.read())
                os.remove(chunk_path)

        extract_all_zip(tmp,file_name,remover=True)
        #shutil.rmtree('path', ignore_errors=True)
        
        #download_file(URL,tmp,remover=True)
        folder_path = tmp
        file_list = glob.glob(folder_path + "/*.csv")
        #print(file_list)
        file_list_url = [i for i in range(0, len(file_list))]
        #print(file_list_url)


        for n,i in zip(file_list, file_list_url):
            nome_inserido_durante_save = n.split('/')[-1].split('\\')[-1].split('.')[0]
            ## print(nome_inserido_durante_save)

            __drop_directory(adl, schema, table=table, year=nome_inserido_durante_save)
            ## print(file_list[i])
            data = pd.read_csv(file_list[i] , encoding='ISO-8859-1')
            data['date'] = pd.to_datetime('today').strftime("%d/%m/%Y") # .strftime("%m/%d/%Y")  
            all_columns = list(data.columns) # Creates list of all column headers
            data[all_columns] = data[all_columns].astype(str)


            #######################################################data.to_parquet('my.parquet', index=False)
            parquet_output = file_list[i].replace('.csv','.parquet') #/tmp/org_raw_rfb_cno/cno.csv
            ## print(parquet_output)
            ########################################################data.to_csv(parquet_output, index=False)
            data.to_parquet(parquet_output, index=False)
            __upload_file(adl, schema, table=table, year=nome_inserido_durante_save, file=parquet_output)

        return {'exit': 200}
    except Exception as e:
        raise e
    finally:
        shutil.rmtree(tmp)
        #tempo = time.time() - start
        #print(tempo)




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
        __delete_file()
        metadata['execution_time'] = time.time() - start
    if kwargs['callback'] is not None:
        requests.post(kwargs['callback'], json=metadata)
    return metadata


DEBUG, LND = None, None

if __name__ == '__main__':
    #import dotenv
    ## from dotenv import load_dotenv, find_dotenv
    #from app import app
    #dotenv.load_dotenv(app.ROOT_PATH + '/debug.env')
    exit(execute(host='localhost', passwd=None, reload=None, reset=False, callback=None))
