import os
import re
import time
import shutil
import zipfile
import requests
from functools import partial
from unicodedata import normalize

import pandas as pd
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


def __normalize(string: str) -> str:
    return normalize('NFKD', string.strip()).encode('ASCII', 'ignore').decode('ASCII')


def __normalize_replace(string: str) -> str:
    return re.sub(r'[.,;:{}()\n\t=]', '', __normalize(string)
                  .replace(' ', '_')
                  .replace('-', '_')
                  .replace('|', '_')
                  .replace('/', '_')
                  .upper())


def __check_path_exists(adl, path) -> bool:
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


def __create_directory(schema=None, table=None, year=None) -> str:
    if year:
        return f'{LND}/{schema}__{table}/{year}'
    return f'{LND}/{schema}__{table}'


def __drop_directory(adl, schema=None, table=None, year=None) -> None:
    adl_drop_path = __create_directory(schema=schema, table=table, year=year)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __upload_file(adl, schema, table, file, year=None) -> None:
    split = os.path.basename(file).split('.')
    filename = __normalize_replace(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table, year=year)
    file_type = '.'.join(file_type)
    adl_write_path = f'{directory}/{filename}.{file_type}'

    __upload_bs(adl, file, adl_write_path)


def extract_all_zip(output_path: str, remover=True) -> None:
    for file in os.listdir(output_path):
        file = output_path+file
        if file.endswith('.zip'):
            with zipfile.ZipFile(file) as z:
                z.extractall(path=output_path)
            if remover == True:
                os.remove(file)


def download_file(url: str, output_path: str) -> None:
    file = url.split('/')[-1].split('?')[0]+'.zip'
    r = requests.get(url)
    if r.status_code == 200:
        with open(output_path+file,'wb') as f:
            f.write(r.content)
    else:
        raise Exception(f'status_code not 200. Server message: Code {r.status_code} | {r.text}')


def transform_save(dict_df: dict, adl, schema: str, table: str, tmp: str) -> None:
    f, DF = list(dict_df.items())[0]

    _filter = DF.notnull().sum(axis=0)==0
    DF.drop(columns=DF.columns[_filter], inplace=True)

    DF.drop_duplicates(inplace=True, ignore_index=True)
    DF.columns = [__normalize_replace(cl) for cl in DF.columns]

    filename = f.split('/')[-1].replace('.csv','').replace('API_','')
    parquet_output = tmp + f'{filename}.parquet'
    DF.to_parquet(parquet_output, index=False)
    __upload_file(adl, schema=schema, table=table, file=parquet_output)
    os.remove(f)


def main(**kwargs):
    """
    This function is the main entrypoint execute this bot.

    :param kwargs: dict with all key-value pairs needed as configurations to run the bot.
    """

    adl = authenticate_datalake()

    schema = 'banco_mundial'
    table = 'indicadores_selecionados'

    key_name = f'org_raw_{schema}_{table}'
    tmp = f'/tmp/{key_name}/'
    os.makedirs(tmp, mode=0o777, exist_ok=True)

    try:
        cod_indicators = ['ENF.CONT.COEN.QUJP.DB16.DFRN', 'ENF.CONT.COEN.QUJP.XD', 'ENF.CONT.COEN.RK.DB19',
                          'FP.CPI.TOTL.ZG', 'GB.XPD.RSDV.GD.ZS', 'GC.DOD.TOTL.GD.ZS', 'GC.XPN.TOTL.GD.ZS',
                          'IC.BUS.DFRN.XQ', 'IC.BUS.EASE.DFRN.XQ.DB1719', 'IC.BUS.EASE.XQ',
                          'IC.CNST.PRMT.DFRN.DB1619', 'IC.CNST.PRMT.RK', 'IC.CRED.ACC.ACES.DB1519',
                          'IC.CRED.ACC.CRD.DB1519.DFRN', 'IC.CRED.ACC.CRD.RK',
                          'IC.CRED.ACC.LGL.RGHT.012.XD.DB1519.DFRN', 'IC.CRED.ACC.LGL.RGHT.XD.012.DB1519',
                          'IC.ELC.ACES.RK.DB19', 'IC.REG.PRRT.REG.RK.DB19', 'IC.REG.STRT.BUS.DFRN',
                          'LP.LPI.CUST.XQ', 'LP.LPI.INFR.XQ', 'LP.LPI.ITRN.XQ', 'LP.LPI.LOGS.XQ',
                          'LP.LPI.OVRL.XQ', 'LP.LPI.TIME.XQ', 'LP.LPI.TRAC.XQ', 'NE.EXP.GNFS.ZS',
                          'NE.GDI.TOTL.ZS', 'NE.IMP.GNFS.ZS', 'NV.AGR.TOTL.ZS', 'NV.IND.TOTL.ZS', 
                          'NY.GDP.DEFL.KD.ZG', 'NY.GDP.MKTP.CD', 'NY.GDP.MKTP.KD.ZG', 'NY.GDP.PCAP.CD',
                          'NY.GDP.PCAP.KD.ZG', 'NY.GDP.PCAP.PP.CD', 'NY.GNS.ICTR.ZS', 'NYGDPMKTPKDZ',
                          'PAY.TAX.DB1719.DRFN', 'PAY.TAX.RK.DB19', 'PROT.MINOR.INV.DFRN.DB1519',
                          'PROT.MINOR.INV.IC.PRIN.MINOR.RK', 'RESLV.ISV.COPR.03.XD.DB1519',
                          'RESLV.ISV.CPI.04.XD.DB1519', 'RESLV.ISV.DB1519.DFRN', 'RESLV.ISV.DURS.YR',
                          'RESLV.ISV.RCOV.RT', 'RESLV.ISV.RK.DB19', 'SL.TLF.TOTL.IN', 'SL.UEM.1524.FE.ZS',
                          'SL.UEM.1524.MA.ZS', 'SL.UEM.TOTL.FE.ZS', 'SL.UEM.TOTL.MA.ZS', 'SL.UEM.TOTL.ZS',
                          'SP.POP.GROW', 'SP.POP.TOTL', 'TG.VAL.TOTL.GD.ZS', 'TRD.ACRS.BRDR.RK.DB19']

        url_part1 = 'https://api.worldbank.org/v2/en/indicator/'
        url_part2 = '?downloadformat=csv'
        for cod in cod_indicators:
            url = url_part1+cod+url_part2
            download_file(url,tmp)

        extract_all_zip(tmp)

        __drop_directory(adl, schema=schema, table=table)

        list_files = sorted([tmp+f for f in os.listdir(tmp) if 'Metadata_' not in f and f.endswith('.csv')])

        dict_dfs = {f:pd.read_csv(f, skiprows=4) for f in list_files}
        updated_date = {f:pd.read_csv(f, skiprows=2, nrows=1, header=None).values[0][1] for f in list_files}

        for f in dict_dfs.keys():
            _filter = dict_dfs[f].notnull().sum(axis=0)==0
            dict_dfs[f].drop(columns=dict_dfs[f].columns[_filter], inplace=True)

            dict_dfs[f]['Last Updated Date'] = updated_date[f]
            dict_dfs[f].columns = [__normalize_replace(cl) for cl in dict_dfs[f].columns]

            filename = f.split('/')[-1].replace('.csv','').replace('API_','')
            parquet_output = tmp + f'{filename}.parquet'
            dict_dfs[f].to_parquet(parquet_output, index=False)
            __upload_file(adl, schema=schema, table=table, file=parquet_output)
            os.remove(f)

        metadatas = [{'name':'Metadata_Indicator', 'table':'documentacao_indica'},
                     {'name':'Metadata_Country', 'table':'documentacao_paises'}]

        for metadata in metadatas:
            list_files = sorted([tmp+f for f in os.listdir(tmp) if metadata['name'] in f])
            list_dict_df = [{f:pd.read_csv(f)} for f in list_files]
            list(map(partial(transform_save, adl=adl, schema=schema,
                                             table=metadata['table'], tmp=tmp), list_dict_df))

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
