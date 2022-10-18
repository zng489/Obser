import requests
import concurrent.futures
from concurrent.futures import as_completed
from tqdm import tqdm
import os
import zipfile
import shutil
import time

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


url = 'http://200.152.38.155/CNO/cno.zip'
#url = 'https://cdn-107.anonfiles.com/E8a3e3Bay2/e57886b9-1665929785/cno.zip'
path = 'C:/Users/acessocni01/Desktop/Test/file_/'
file_name = 'cno.zip'
response = requests.get(url, stream=True)
file_size = int(response.headers.get('content-length', 0))
chunk_size = 1024*1024
os.makedirs(path, mode=0o777, exist_ok=True) 

chunks = range(0, file_size, chunk_size)
my_iter = [[url, make_headers(chunk, chunk_size), f'{file_name}.part{i}'] for i, chunk in enumerate(chunks)] 

with concurrent.futures.ThreadPoolExecutor(max_workers=15) as executor:
    jobs = [executor.submit(download_part,path,i) for i in my_iter]

    with tqdm(total=file_size, unit='iB', unit_scale=True, unit_divisor=chunk_size, leave=True, colour='cyan') as bar:
        for job in as_completed(jobs):
            size = job.result()
            bar.update(size)

with open(path + file_name, 'wb') as outfile:
    for i in range(len(chunks)):
        chunk_path = path + f'{file_name}.part{i}'
        with open(chunk_path, 'rb') as s:
            outfile.write(s.read())
        os.remove(chunk_path)

extract_all_zip(path,file_name,remover=True)
shutil.rmtree('path', ignore_errors=True)



