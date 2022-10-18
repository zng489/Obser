import os
import re
import shutil
import time
from datetime import date
from unicodedata import normalize
from datetime import datetime

import bs4
import pandas as pd
from pandas.api.types import infer_dtype
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


def __call_redis(host, password, function_name, *args):
    db = redis.Redis(host=host, password=password, port=6379, db=0, socket_keepalive=True, socket_timeout=2)
    try:
        method_fn = getattr(db, function_name)
        return method_fn(*args)
    except Exception as _:
        raise _
    finally:
        db.close()


def __check_path_exists(adl, path):
    try:
        next(adl.get_paths(path, recursive=False, max_results=1))
        return True
    except:
        return False


def list_directory_contents(adl, path):
    try:
        paths = adl.get_paths(path=path, recursive=False)
        directories_path = []
        for path in paths:
            directories_path.append(path.name)

        return directories_path

    except :
        return directories_path


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


def __create_directory(schema=None, table=None, date=None):
    if date:
        return '{lnd}/{schema}__{table}/{date}'.format(lnd=LND, schema=schema, table=table, date=date)
    else:
        return '{lnd}/{schema}__{table}'.format(lnd=LND, schema=schema, table=table)      


def __drop_directory(adl, schema=None, table=None, date=None):
    adl_drop_path = __create_directory(schema=schema, table=table, date=date)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, date, file):

    adl_write_path = __adl_file_path(adl, schema, table, date, file)

    __upload_bs(adl, file, adl_write_path)


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ','-')
                  .lower())


def get_jobs_page(session, page, data_acesso):

    url_page = 'https://www.vagas.com.br/vagas-de-*?ordenar_por=mais_recentes&p%5B%5D=Brasil&pagina={}&_={}'\
                    .format(page, data_acesso)
    retry = 10
    get_count = 0  
    while True:
        try:
            response = session.get(url_page, timeout=300)
            if response.status_code==200:

                if 'Não encontramos vagas' in response.text:
                    return False

                return response

            elif get_count>retry:
                raise Exception('Status code: {}'.format(response.status_code))
            
        except Exception as e:
            if get_count>retry:
                raise Exception('Erro: {}'.format(e))
        
        time.sleep(60)
        get_count+=1


def get_job(session, url_job):

    retry = 10
    get_count = 0  
    while True:
        try:
            response = session.get(url_job, timeout=300)
            if response.status_code==200:

                soup_second = bs4.BeautifulSoup(response.text, 'html.parser')

                return soup_second

            elif get_count>retry:
                raise Exception('Status code: {}'.format(response.status_code))
            
        except Exception as e:
            if get_count>retry:
                raise Exception('Erro: {}'.format(e))
        
        time.sleep(60)
        get_count+=1


def get_jobs(session, page):

    data_acesso = str(datetime.today().timestamp())
    data_acesso = data_acesso.replace('.', '')[0:13]

    lista_vagas = []

    response = get_jobs_page(session, page, data_acesso)

    if not response:
        return response

    soup_main = bs4.BeautifulSoup(response.text, "html.parser" )
    html_vagas = soup_main.find('section',{'class':"grupoDeVagas"})

    vagas = html_vagas.findAll('li')

    for vaga in vagas:

        href = vaga.find('a',{'class':"link-detalhes-vaga"}).get('href')
        link_vaga = 'https://www.vagas.com.br' + href

        soup_second = get_job(session, link_vaga)
        
        try:
            profissao = soup_second.find('h1',{'class':"job-shortdescription__title"}).getText().replace('\n','')
        except:
            profissao = soup_second.find('span',{'class':"job-hierarchylist__item job-hierarchylist__item--level"}).getText().replace('\n','')

        empresa = soup_second.find('h2',{'class':"job-shortdescription__company"}).getText().replace('\n','').replace('  ','')

        try:
            qtd_vagas = soup_second.find('span',{'class':"job-hierarchylist__item job-hierarchylist__item--quantity"}).getText().replace('\n','').replace('  ','')
        except:
            qtd_vagas = "None"

        codigo = soup_second.find('li',{'class':"job-breadcrumb__item job-breadcrumb__item--id"}).getText().replace('\n','').replace('  ','')

        conteudo = soup_second.find('div',{'class':"job-tab-content job-description__text texto"}).getText().replace('\n','').replace('  ','').replace('Descrição','').replace(':','')

        data_publicada = soup_second.find('li',{'class':"job-breadcrumb__item job-breadcrumb__item--published job-breadcrumb__item--nostyle"}).getText().replace('\n','').replace('  ','')

        localidade = soup_second.find('span',{'class':"info-localizacao"}).getText().replace('\n','').replace('  ','')

        salario = soup_second.find('div',{'class':"infoVaga"}).div.getText().replace('\R$','').replace('\n','').replace('   ','')         

        beneficios = []
        try:
            soup_beneficios = soup_second.find('ul',{'class':"job-benefits__list"})
            soup_beneficios = soup_beneficios.findAll('li')
            for beneficio in soup_beneficios:
                beneficios.append(beneficio.getText().replace('\n',' ').replace('   ','').strip())

        except:
            pass

        dicionario_vagas = {
                "titulo":profissao,
                "url_vaga":link_vaga,
                'dadosEmpresa':empresa,
                'id':codigo,
                "qtdVagas":qtd_vagas,
                'conteudo':conteudo,
                'dataPublicacao':data_publicada,
                'localidade':localidade,
                'salario':salario,
                'beneficios':beneficios
                }

        lista_vagas.append(dicionario_vagas)

    return lista_vagas


def parse_list(jobs):

    df = pd.DataFrame(jobs)

    for col in df.columns:
        if infer_dtype(df[col])=='mixed':
            df[col] = df[col].astype('str')

    return df


def __adl_file_path(adl, schema, table, date, file):

    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, date=date)
    file_type = '.'.join(file_type)
    adl_file_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    return adl_file_path


def __drop_file(adl, schema, table, date, file):

    adl_file_path = __adl_file_path(adl, schema, table, date, file)

    try:
        file = adl.get_file_client(adl_file_path)
        file.get_file_properties()
        file.delete_file()
    except:
        pass


def main(**kwargs):
    host, passwd = kwargs.pop('host'), kwargs.pop('passwd')

    key_page = 'org_raw_vagascom_vagas_page'
    tmp = '/tmp/org_raw_vagascom_vagas/'

    table = 'vagas'

    try:
        os.makedirs(tmp, mode=0o777, exist_ok=True)
        adl = authenticate_datalake()
        dt_today = str(date.today())

        adl_directories_path = __create_directory(schema='vagascom', table=table)
        directories = list_directory_contents(adl, adl_directories_path)
        while len(directories)>7:
            directory = min(directories)
            __drop_directory(adl, schema='vagascom', table=table, date=os.path.basename(directory))   
            directories = list_directory_contents(adl, adl_directories_path)

        if kwargs['reset']:
            __call_redis(host, passwd, 'delete', key_page)
            __drop_directory(adl, schema='vagascom', table=table, date=dt_today)

        adl_directory_path = __create_directory(schema='vagascom', table=table, date=dt_today)
        if not __check_path_exists(adl, adl_directory_path):
            __call_redis(host, passwd, 'delete', key_page)

        page_init = 1
        if __call_redis(host, passwd, 'exists', key_page):
            page_init = int(__call_redis(host, passwd, 'get', key_page)) + 1

        s = requests.Session()

        jobs = []
        actual_page = page_init
        while True:
            jobs_page = get_jobs(s, actual_page)
            if jobs_page:
                jobs += jobs_page

            set_key_page = False
            if actual_page%20==0 or (not jobs_page and jobs):

                df = parse_list(jobs)

                jobs = []

                page_rage = 'vagascom_{}_page_{}_a_{}'.format(dt_today, page_init, actual_page)
                parquet_output = tmp + '{file}.parquet'.format(file=page_rage)
                __drop_file(adl, schema='vagascom', table=table, date=dt_today, file=parquet_output)
                df.to_parquet(parquet_output, index=False)

                __upload_file(adl, schema='vagascom', table=table, date=dt_today, file=parquet_output)

                set_key_page = True
                page_init = actual_page

            actual_page += 1
            
            if kwargs['reload'] is None and set_key_page:
                __call_redis(host, passwd, 'set', key_page, page_init)

            if not jobs_page:
                break

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
