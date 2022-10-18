import os
import re
import shutil
import time
import zipfile
from unicodedata import normalize

import requests
from azure.storage.filedatalake import FileSystemClient
from selenium.webdriver.chrome.webdriver import WebDriver, Options
from selenium.webdriver.remote.command import Command


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


def __upload_file(adl, schema, table, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])
    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))

    directory = __create_directory(schema=schema, table=table)
    file_type = '.'.join(file_type)
    adl_write_path = '{directory}/{file}.{type}'.format(directory=directory, file=filename, type=file_type)

    __upload_bs(adl, file, adl_write_path)


def __drop_directory(adl, schema=None, table=None):
    adl_drop_path = __create_directory(schema=schema, table=table)
    if __check_path_exists(adl, adl_drop_path):
        adl.delete_directory(adl_drop_path)


def __normalize_str(_str):
    return re.sub(r'[,;{}()\n\t=-]', '', normalize('NFKD', _str)
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .upper())


def __initialize_driver(tmp):
    opt = Options()
    opt.add_argument("--headless")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--window-size=1920,1080")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)
    driver = WebDriver(options=opt)

    params = {'behavior': 'allow', 'downloadPath': tmp}
    driver.execute_cdp_cmd('Page.setDownloadBehavior', params)

    return driver


def __navigate_download_page(driver: WebDriver):
    url = 'http://www.mtecbo.gov.br/cbosite/pages/downloads.jsf'
    driver.get(url)

    el = driver.find_element_by_id(id_='formDownload:link5')
    el.click()


def __resolve_captcha(driver: WebDriver):
    el = driver.find_element_by_class_name(name='g-recaptcha')
    site_key = el.get_attribute('data-sitekey')

    url = 'https://api.anti-captcha.com/createTask'
    body = {
        'clientKey': SECRET_KEY,
        'task': {
            'type': 'NoCaptchaTaskProxyless',
            'websiteURL': driver.current_url,
            'websiteKey': site_key}
    }

    headers = {'Content-Type': 'application/json'}
    res_task_creation = requests.post(url, json=body, headers=headers)
    task_info = res_task_creation.json()
    if task_info['errorId'] > 0:
        raise Exception('org_raw_estrutura_cbo: __resolve_captcha()', res_task_creation.text)

    url = 'https://api.anti-captcha.com/getTaskResult'
    task_body = {'clientKey': SECRET_KEY, 'taskId': task_info['taskId']}

    while True:
        res_task_status = requests.post(url, json=task_body, headers=headers)
        task_result = res_task_status.json()
        if task_result['errorId'] == 0:
            if task_result['status'] == 'ready':
                break
            time.sleep(5)
        else:
            raise Exception('org_raw_estrutura_cbo: __resolve_captcha()', res_task_status.text)
    return task_result


def __download_file(driver: WebDriver, task_result: dict, tmp: str):
    url = 'http://www.mtecbo.gov.br/cbosite/pages/downloads.jsf'

    token = driver.find_element_by_name(name='DTPINFRA_TOKEN')
    javax = driver.find_element_by_name(name='javax.faces.ViewState')

    body = {
        'formDownload': 'formDownload',
        'formDownload:j_idt67': 'Download',
        'DTPINFRA_TOKEN': int(token.get_attribute('value')),
        'g-recaptcha-response': task_result['solution']['gRecaptchaResponse'],
        'javax.faces.ViewState': javax.get_attribute('value'),
    }

    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:77.0) Gecko/20100101 Firefox/77.0',
        'Cookie': 'JSESSIONID={session}'.format(session=driver.get_cookie('JSESSIONID')['value'])
    }

    res = requests.post(url, data=body, headers=headers)
    if b'xml' in res.content:
        return None

    zip_output = tmp + 'cbo.zip'
    with open(zip_output, mode='wb+') as file:
        file.write(res.content)
    return zip_output


def __upload_files(adl, zip_output, tmp):
    with zipfile.ZipFile(zip_output) as _zip:
        _zip.extractall(tmp)

    for file in os.listdir(tmp):
        filename = file.split('-')[-1]
        filename = __normalize_str(filename.strip())

        table = filename.lower()
        __drop_directory(adl, schema='cbo', table=table)
        __upload_file(adl, schema='cbo', table=table, file=os.path.join(tmp, file))


def __close_driver(driver: WebDriver):
    try:
        driver.execute(Command.STATUS)
        driver.quit()
    except Exception:
        pass


def __download_zip(driver: WebDriver, tmp):
    left_retries = 3
    while left_retries > 0:
        __navigate_download_page(driver)
        task_result = __resolve_captcha(driver)

        zip_output = __download_file(driver, task_result, tmp)
        if zip_output is not None:
            return zip_output, task_result

        left_retries -= 1

    raise Exception('org_raw_estrutura_cbo: __download_zip(): Tried download 3 times and was not success')


def main(**kwargs):
    tmp = '/tmp/org_raw_estrutura_cbo/'
    os.makedirs(tmp, mode=0o777, exist_ok=True)

    adl = authenticate_datalake()

    driver = __initialize_driver(tmp)
    try:
        zip_output, task_result = __download_zip(driver, tmp)
        __close_driver(driver)

        __upload_files(adl, zip_output, tmp)
        return {'anti_captcha_result': task_result}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        __close_driver(driver)


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
SECRET_KEY = 'e76db93537e8a44e72d6c32e4a5a6346'
if __name__ == '__main__':
    import dotenv
    from app import app

    dotenv.load_dotenv(app.ROOT_PATH + '/debug.env')
    exit(execute(host='localhost', passwd=None, reload=None, reset=False, callback=None))
