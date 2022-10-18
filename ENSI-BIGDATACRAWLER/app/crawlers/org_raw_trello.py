# REFERENCIA:  ai
#              https://developer.atlassian.com/cloud/trello/guides/rest-api/object-definitions/
# chave da API:  https://trello.com/app-key

#    Boards
# Cards
# Checklists
# Members
# CustomFields
# Actions

# Tokens
# List
# Organizations

import logging
import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from unicodedata import normalize

import pandas as pd
import requests
from azure.storage.filedatalake import FileSystemClient
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry


def key_vault():
    from azure.identity import ClientSecretCredential
    from azure.keyvault.secrets import SecretClient

    credential = ClientSecretCredential(
        tenant_id=os.environ['AZURE_TENANT_ID'],
        client_id=os.environ['AZURE_CLIENT_ID'],
        client_secret=os.environ['AZURE_CLIENT_SECRET'])

    return SecretClient(
        vault_url=os.environ['AZURE_KEY_VAULT_URI'],
        credential=credential)


def authenticate_datalake() -> FileSystemClient:
    from azure.storage.filedatalake import DataLakeServiceClient

    secret_client = key_vault()
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


def __normalize_str(_str: str):
    return re.sub(r'[.,;{}()\n\t=-]', '', normalize('NFKD', _str.strip())
                  .encode('ASCII', 'ignore')
                  .decode('ASCII')
                  .replace(' ', '_')
                  .replace('-', '_')
                  .upper())


def __change_encoding(file):
    p1 = subprocess.Popen(shlex.split('file -bi "%s"' % file), stdout=subprocess.PIPE)
    p2 = subprocess.Popen(shlex.split("awk -F '=' '{print $2}'"), stdin=p1.stdout, stdout=subprocess.PIPE)
    encoding = p2.communicate()[0].decode("utf-8").strip("\n")

    # For dev purposes
    if DEBUG:
        os.system('mv "{file}" "{file}.old"'.format(file=file))
        os.system('head -n 500 "{file}.old" > "{file}"'.format(file=file))

    if encoding not in ['utf-8', 'us-ascii']:
        os.system('mv "{file}" "{file}.old"'.format(file=file))
        subprocess.Popen(shlex.split('iconv -f {f} -t utf-8 "{file}.old" -o "{output}"'
                                     .format(f=encoding, file=file, output=file))).communicate()


def __upload_files(adl, file):
    split = os.path.basename(file).split('.')
    filename = __normalize_str(split[0])

    file_type = list(map(str.lower, [_str for _str in map(str.strip, split[1:]) if len(_str) > 0]))
    if file_type[-1] == 'csv':
        __change_encoding(file)

    filename = file.replace('/tmp/trello/', '')
    # filename = file.replace('/tmp/', '')
    filename = filename.replace('.' + file_type[-1], '')

    adl_write_path = '{lnd}/{file}.{type}'.format(lnd=LND, file=filename, type='.'.join(file_type))

    __upload_bs(adl, file, adl_write_path)
    print('uploading Origem: {0}'.format(file))
    print('uploading Destino: {0}'.format(adl_write_path))
    print('\n')


def __to_csv(df, csv_output):
    csv_output = csv_output + '.csv'
    try:
        df.to_csv(csv_output, index=False, sep=';', encoding='utf-8')
    except Exception as e:
        print('csv_output: {0} \n {1}'.format(csv_output, e))
        if os.path.exists(csv_output):
            os.remove(csv_output)


def __to_parquet(df, parquet_output):
    parquet_output = parquet_output + '.parquet'
    try:
        df.to_parquet(parquet_output, index=False)
    except Exception as e:
        print('parquet_output: {0} \n {1}'.format(parquet_output, e))
        if os.path.exists(parquet_output):
            os.remove(parquet_output)


def __escreve_arquivo(df, output):
    # __to_csv(df, output)
    __to_parquet(df, output)

    # UPLOAD
    try:
        parquet_output = output + '.parquet'

        adl = authenticate_datalake()
        __upload_files(adl, parquet_output)
        #print('SUCESSO: __upload_files: {0}'.format(parquet_output))
        logging.info('SUCESSO: __upload_files: {0}'.format(parquet_output))

    except Exception as e:
        #print('ERROR: __upload_files: {0} - LOG: {1}'.format(parquet_output, e))
        logging.info('ERROR: __upload_files: {0} - LOG: {1}'.format(parquet_output, e))


def limpaUnicode(df, campo):
    df[campo] = df[campo].str.replace("\u2705", "")
    df[campo] = df[campo].str.replace("\u2726", "")
    df[campo] = df[campo].str.replace("\U0001f525", "")
    df[campo] = df[campo].str.replace("\U0001f680", "")
    df[campo] = df[campo].str.replace("\U0001f44d", "")
    df[campo] = df[campo].str.replace("\u201c", "")
    df[campo] = df[campo].str.replace("\u201d", "")
    df[campo] = df[campo].str.replace("\U0001f446", "")
    df[campo] = df[campo].str.replace("\u2013", "")
    df[campo] = df[campo].str.replace("\u2022", "")
    df[campo] = df[campo].str.replace("\uf0a7", "")
    df[campo] = df[campo].str.replace("\u0301", "")
    df[campo] = df[campo].str.replace("\u0327", "")
    df[campo] = df[campo].str.replace("\u0303", "")

    return df


def get_member_token(key, token, area_):
    url_api_board = "https://api.trello.com/1/members/me?key={0}&token={1}";

    t = requests.Session()
    response = t.get(url_api_board.format(key, token))
    df1 = pd.DataFrame()

    try:
        dados = response.json()
        df = pd.json_normalize(dados)
        df['area'] = area_
        df1 = df[['id', 'username', 'fullName', 'email']]

        df1 = df1.rename(columns={'id': 'idMember'})
    except Exception as e:
        #print('get_member_token: area:{0} - ERROR: {1}'.format(area_, e))
        logging.info('get_member_token: area:{0} - ERROR: {1}'.format(area_, e))

    return df1


def detalhe_organizations(key, token, idBoard, idOrganizations, area_):
    url_api_board = "https://api.trello.com/1/organizations/{0}?key={1}&token={2}&cards=all";

    t = requests.Session()
    response = t.get(url_api_board.format(idOrganizations, key, token))
    df1 = pd.DataFrame()
    try:
        dados = response.json()
        df = pd.json_normalize(dados)
        df['area'] = area_
        df['idBoard'] = idBoard
        df1 = df[['area', 'idBoard', 'id', 'name', 'displayName', 'website', 'teamType', 'desc', 'url', 'logoHash',
                  'logoUrl']]
        df1 = df1.rename(columns={'id': 'idOrganizations'})
    except Exception as e:
        #print('detalhe_organizations: idOrganizations:{0} - ERROR: {1}'.format(idOrganizations, e))
        logging.info('detalhe_organizations: idOrganizations:{0} - ERROR: {1}'.format(idOrganizations, e))

    return df1


def detalhe_lista_boards_por_members(key, token, idMembers, area_):
    url_api_board = "https://api.trello.com/1/members/{0}/boards?key={1}&token={2}&cards=all";
    t = requests.Session()
    df1 = pd.DataFrame()

    try:
        response = t.get(url_api_board.format(idMembers, key, token))
        # response.encoding = 'ISO-8859-1'
        dados = response.json()
        df = pd.json_normalize(dados)
        df = limpaUnicode(df, 'name')
        df = limpaUnicode(df, 'desc')
        df['area'] = area_

        df1 = df[
            ['area', 'id', 'name', 'desc', 'dateLastActivity', 'starred', 'shortLink', 'idOrganization', 'idEnterprise',
             'closed']]

        df1 = df1.rename(columns={'id': 'idBoard'})
    except Exception as e:
        #print('detalhe_lista_boards_por_members: ERROR: {1}'.format(e))
        logging.info('detalhe_lista_boards_por_members:  ERROR: {1}'.format(e))

    return df1


def detalhe_lista_boards(key, token, idOrganizations):
    url_api_board = "https://api.trello.com/1/organizations/{0}/boards?key={1}&token={2}&cards=all";

    t = requests.Session()

    response = t.get(url_api_board.format(idOrganizations, key, token))
    # response.encoding = 'latin-1'
    dados = response.json()
    df = pd.json_normalize(dados)
    df = limpaUnicode(df, 'name')
    df = limpaUnicode(df, 'desc')

    df1 = df[
        ['id', 'name', 'desc', 'dateLastActivity', 'starred', 'shortLink', 'idOrganization', 'idEnterprise', 'closed']]

    df1 = df1.rename(columns={'id': 'idBoard'})

    return df1


def detalhe_board_cards(key, token, shortLink_board, idBoard, area_):
    url_api_board = "https://api.trello.com/1/boards/{0}/Cards?key={1}&token={2}&cards=all";

    t = requests.Session()
    response = t.get(url_api_board.format(shortLink_board, key, token))

    df1 = pd.DataFrame()
    try:
        dados = response.json()
        if len(dados) > 0:
            df = pd.json_normalize(dados)
            df = limpaUnicode(df, 'name')
            df = limpaUnicode(df, 'desc')
            df['area'] = area_

            df1 = df[['area',
                      'idBoard',
                      'id',
                      'idMembers',
                      'idList',
                      'idChecklists',
                      'idLabels',
                      'name',
                      'desc',
                      'closed',
                      'dateLastActivity',
                      'due',
                      'labels',
                      'pos'
                      ]].astype(str)
            df1 = df1.rename(columns={'id': 'idCard'})


        else:
            '''print(
                'detalhe_board_cards: idBoard:{0} - shortLink_board:{1} -  ERROR: {2}'.format(idBoard, shortLink_board,
                                                                                              'BOARD SEM CARDS'))'''

            logging.info(
                'detalhe_board_cards: idBoard:{0} - shortLink_board:{1} -  ERROR: {2}'.format(idBoard, shortLink_board,
                                                                                              'BOARD SEM CARDS'))
    except Exception as e:
        '''print(
            'detalhe_board_cards: idBoard:{0} - shortLink_board:{1} -  ERROR: {2}'.format(idBoard, shortLink_board, e))'''
        logging.info(
            'detalhe_board_cards: idBoard:{0} - shortLink_board:{1} -  ERROR: {2}'.format(idBoard, shortLink_board, e))
    return df1


def detalhe_board_cards_lists(key, token, idBoard, idCard, area_):
    url_api_board = "https://api.trello.com/1/cards/{0}/list?key={1}&token={2}";
    t = requests.Session()
    response = t.get(url_api_board.format(idCard, key, token))

    df1 = pd.DataFrame()
    try:
        dados = response.json()
        if len(dados) > 0:
            df = pd.json_normalize(dados)
            df['idCard'] = idCard
            df['area'] = area_

            df1 = df[['area',
                      'idBoard',
                      'pos',
                      'id',
                      'closed',
                      'name',
                      'idCard'
                      ]].astype(str)
            df1 = df1.rename(columns={'id': 'idList'})
        else:
            #print('detalhe_board_cards_lists: idBoard:{0}  -  ERROR: {1}'.format(idBoard, 'LISTA LIMPA'))
            logging.info('detalhe_board_cards_lists: idBoard:{0}  -  ERROR: {1}'.format(idBoard, 'LISTA LIMPA'))

    except Exception as e:
        #print('detalhe_board_cards_lists: idBoard:{0} - ERROR: {1}'.format(idBoard, e))
        logging.info('detalhe_board_cards_lists: idBoard:{0} - ERROR: {1}'.format(idBoard, e))

    return df1


def detalhe_card_customfields(key, token, idCard, area_):
    url_api_board = "https://api.trello.com/1/cards/{0}/customFieldItems?key={1}&token={2}";

    t = requests.Session()
    response = t.get(url_api_board.format(idCard, key, token))
    df1 = pd.DataFrame()

    try:
        dados = response.json()
        df = pd.json_normalize(dados)
        df['idCard'] = idCard
        df['area'] = area_

        df1 = df
        df1 = df1.rename(columns={'value.text': 'value_text'})
        df1 = df1.rename(columns={'value.date': 'value_date'})
    except Exception as e:
        #print('detalhe_card_customfields: Card: {0} - ERRO:{1}'.format(idCard, e))
        logging.info('detalhe_card_customfields: Card: {0} - ERRO:{1}'.format(idCard, e))

    return df1


def detalhe_customfields(key, token, idCard, idCustomField, area_):
    url_api_board = "https://api.trello.com/1/customFields/{0}?key={1}&token={2}";

    retry = Retry(connect=3, backoff_factor=0.5)
    adapter = HTTPAdapter(max_retries=retry)

    t = requests.Session()

    t.mount('http://', adapter)
    t.mount('https://', adapter)

    response = t.get(url_api_board.format(idCustomField, key, token))
    df1 = pd.DataFrame()

    try:
        dados = response.json()
        df = pd.json_normalize(dados)
        df['idCard'] = idCard
        df['area'] = area_

        df1 = df
    except Exception as e:
        #print('detalhe_customfields: Card: {0} - CustomField: {1}  - ERRO:{2}'.format(idCard, idCustomField, e))
        logging.info('detalhe_customfields: Card: {0} - CustomField: {1}  - ERRO:{2}'.format(idCard, idCustomField, e))

    return df1


def detalhe_board_cards_checklist(key, token, idChecklist, idBoard, idCard, area_):
    url_api = "https://api.trello.com/1/checklists/{2}?key={0}&token={1}";
    t = requests.Session()
    response = t.get(url_api.format(key, token, idChecklist))

    try:
        dados = response.json()
        df1 = pd.DataFrame()
        if len(dados) > 0:
            # NOME DA LISTA
            dfLista = pd.json_normalize(dados)
            nomeLista = ''

            try:
                nomeLista = str(dfLista['name'][0])
            except Exception as e:
                nomeLista = ''

            # DADOS DO CHECKLIST
            df = pd.json_normalize(dados, 'checkItems')
            if df.empty:
                print('sem resultados')
            else:
                df = limpaUnicode(df, 'name')
                df['idBoard'] = idBoard
                df['idCard'] = idCard
                df['nameList'] = nomeLista
                df['area'] = area_
                df1 = df[
                    ['area','idChecklist', 'id', 'state', 'name', 'nameData', 'due', 'pos', 'idMember', 'idBoard', 'idCard',
                     'nameList']].astype(str)

                # AJUSTA COLUNAS
                CHECKLIST_ = pd.DataFrame(columns=[
                    "area",
                    "idBoard",
                    "idCard",
                    "idMember",
                    "idChecklist",
                    "nameList",
                    "id",
                    "state",
                    "name",
                    "nameData",
                    "due",
                    "pos"
                ])

                for check in df1.itertuples():
                    CHECKLIST_ = CHECKLIST_.append(
                        {
                            "area": check.area,
                            "idBoard": check.idBoard,
                            "idCard": check.idCard,
                            "idMember": check.idMember,
                            "idChecklist": check.idChecklist,
                            "nameList": nomeLista,
                            "id": check.id,
                            "state": check.state,
                            "name": check.name,
                            "nameData": check.nameData,
                            "due": check.due,
                            "pos": check.pos
                        }, ignore_index=True)

                df1 = CHECKLIST_

    except Exception as e:
        '''print('detalhe_board_cards_checklist: idBoard:{0} - idChecklist :{1} -ERROR: {2} -OBJ: {3}\n'.format(idBoard,
                                                                                                             idChecklist,
                                                                                                             e, df))'''
        logging.info(
            'detalhe_board_cards_checklist: idBoard:{0} - idChecklist :{1} -ERROR: {2}\n'.format(idBoard, idChecklist,
                                                                                                 e))

    return df1


def detalhe_board_members(key, token, idBoard, area_):
    url_api = "https://api.trello.com/1/boards/{2}/members?fields=id,username,activityBlocked,avatarHash,fullName,initials,nonPublicAvailable&key={0}&token={1}";

    t = requests.Session()
    response = t.get(url_api.format(key, token, idBoard))
    df1 = pd.DataFrame()

    try:
        dados = response.json()
        df = pd.json_normalize(dados)
        df['area'] = area_
        df['idBoard'] = idBoard
        df1 = df
        df1 = df1.rename(columns={'id': 'idMember'})
    except Exception as e:
        #print('detalhe_board_members: {0}'.format(e))
        logging.info('detalhe_board_members: {0}'.format(e))
    return df1


def detalhe_board_cards_members(key, token, idBoard, idCard, area_):
    url_api = "https://api.trello.com/1/cards/{2}/members?key={0}&token={1}";

    t = requests.Session()
    response = t.get(url_api.format(key, token, idCard))
    df1 = pd.DataFrame()

    try:
        dados = response.json()
        df = pd.json_normalize(dados)
        df['idBoard'] = idBoard
        df['idCard'] = idCard
        df['area'] = area_
        df1 = df
        df1 = df1.rename(columns={'id': 'idMember'})
    except Exception as e:
        #print('detalhe_board_cards_members: {0}'.format(e))
        logging.info('detalhe_board_cards_members: {0}'.format(e))

    return df1


def detalhe_board_actions(key, token, idBoard, area_, dt_inicio, dt_final):
    url_api = "https://api.trello.com/1/boards/{2}/actions?filter={3}&fields=id,idMemberCreator,type,date,data&key={0}&token={1}&limit=1000&since=" + dt_inicio + "&before=" + dt_final;

    # COMMENTCARD
    df1 = pd.DataFrame()
    t = requests.Session()
    response = t.get(url_api.format(key, token, idBoard, 'commentCard'))

    try:
        dados = response.json()
        df = pd.json_normalize(dados)

        df = df.rename(columns={'data.text': 'data_text'})
        df = df.rename(columns={'data.card.id': 'idCard'})
        df = df.rename(columns={'data.board.id': 'idBoard'})
        df = df.rename(columns={'data.dateLastEdited': 'data_dateLastEdited'})
        df = df.rename(columns={'id': 'idAction'})

        df['area'] = area_
        df['card_old_due'] = ''
        df['card_due'] = ''

        df1 = df

        df1 = limpaUnicode(df1, 'data_text')

        df1 = df1[
            ['area', 'idBoard', 'idCard', 'idAction', 'idMemberCreator', 'type', 'date', 'data_text', 'card_old_due',
             'card_due']]

    except Exception as e:
        #print('detalhe_board_cards_actions:commentCard: {0}'.format(e))
        logging.info('detalhe_board_cards_actions:commentCard: {0}'.format(e))

    # UPDATECARD:DUE
    df2 = pd.DataFrame()
    t2 = requests.Session()
    response2 = t2.get(url_api.format(key, token, idBoard, 'updateCard:due'))

    try:
        dados2 = response2.json()
        df_2 = pd.json_normalize(dados2)

        df_2 = df_2.rename(columns={'data.text': 'data_text'})
        df_2 = df_2.rename(columns={'data.card.id': 'idCard'})
        df_2 = df_2.rename(columns={'data.board.id': 'idBoard'})
        df_2 = df_2.rename(columns={'data.dateLastEdited': 'data_dateLastEdited'})
        df_2 = df_2.rename(columns={'id': 'idAction'})

        # OLD
        df_2 = df_2.rename(columns={'data.old.due': 'card_old_due'})
        df_2 = df_2.rename(columns={'data.card.due': 'card_due'})

        df_2['area'] = area_
        df_2['data_text'] = '-'
        df_2['data_dateLastEdited'] = ''

        df2 = df_2

        df2 = df2[
            ['area', 'idBoard', 'idCard', 'idAction', 'idMemberCreator', 'type', 'date', 'data_text', 'card_old_due',
             'card_due']]

    except Exception as e:
        #print('detalhe_board_cards_actions:updateCard:due: {0}'.format(e))
        logging.info('detalhe_board_cards_actions:updateCard:due: {0}'.format(e))

    frames = [df1, df2]
    result = pd.concat(frames)

    return result


def detalhe_board_cards_actions(key, token, idCards, area_):
    url_api = "https://api.trello.com/1/cards/{2}/actions?key={0}&token={1}&filter=commentCard";

    t = requests.Session()
    response = t.get(url_api.format(key, token, idCards))
    df1 = pd.DataFrame()

    try:
        dados = response.json()
        df = pd.json_normalize(dados)

        df = df.rename(columns={'data.text': 'data_text'})
        df = df.rename(columns={'data.card.id': 'idCard'})
        df = df.rename(columns={'data.board.id': 'idBoard'})
        df = df.rename(columns={'data.dateLastEdited': 'data_dateLastEdited'})
        df = df.rename(columns={'id': 'idAction'})
        df['area'] = area_

        df1 = df

        df1 = limpaUnicode(df1, 'data_text')

        df1 = df1[['area', 'idBoard', 'idCard', 'idAction', 'idMemberCreator', 'type', 'date', 'data_text']]

    except Exception as e:
        #print('detalhe_board_cards_actions: {0}'.format(e))
        logging.info('detalhe_board_cards_actions: {0}'.format(e))

    return df1


def main(**kwargs):
    #print('AREA:{0} - TOKEN:{1} - CHAVE:{2} - USUARIO:{3}'.format(kwargs.get('AREA'), kwargs.get('TOKEN'), kwargs.get('CHAVE'), kwargs.get('USUARIO')))
    key = kwargs.get('CHAVE') 
    token = kwargs.get('TOKEN')  
    idMember = kwargs.get('USUARIO')  
    area = kwargs.get('AREA')

    # busca idMember para consultas
    membro = get_member_token(key, token, area)
    idMember = membro['idMember'][0]

    tmp = '/tmp/trello/'

    # cria os diretorio para armazenar os arquivos
    tmp_organizations = tmp + 'trello__organizations/'
    tmp_board = tmp + 'trello__board/'
    tmp_card = tmp + 'trello__card/'
    tmp_member = tmp + 'trello__member/'
    tmp_actions = tmp + 'trello__actions/'
    tmp_list = tmp + 'trello__list/'
    tmp_customfields = tmp + 'trello__customfields/'
    tmp_checklist = tmp + 'trello__checklist/'
    tmp_log = tmp + 'trello/'

    # os.makedirs(tmp, mode=0o777, exist_ok=True)
    os.makedirs(tmp_board, mode=0o777, exist_ok=True)
    os.makedirs(tmp_card, mode=0o777, exist_ok=True)
    os.makedirs(tmp_member, mode=0o777, exist_ok=True)
    os.makedirs(tmp_checklist, mode=0o777, exist_ok=True)
    os.makedirs(tmp_actions, mode=0o777, exist_ok=True)
    os.makedirs(tmp_customfields, mode=0o777, exist_ok=True)
    os.makedirs(tmp_organizations, mode=0o777, exist_ok=True)
    os.makedirs(tmp_list, mode=0o777, exist_ok=True)
    os.makedirs(tmp_log, mode=0o777, exist_ok=True)

    logging.basicConfig(filename='/tmp/trello/trello/log_trello.log', level=logging.INFO, filemode='w')
    logging.info('Iniciando Processo: ' + str(datetime.today()))

    # remove subdiretorio
    # tmp_organizations = tmp
    # tmp_board = tmp
    # tmp_card = tmp
    # tmp_member = tmp
    # tmp_actions = tmp
    # tmp_list = tmp
    # tmp_customfields = tmp
    # tmp_checklist = tmp

    output = "{file}"

    # OBJETOS DE APOIO
    LISTA_CUSTOMFIELDS = pd.DataFrame(columns=[
        "area",
        "idBoard",
        "idCards",
        "idCustomField",
        "name",
        "type",
        "value"
    ])

    LISTA_LIST = pd.DataFrame(columns=[
        "area",
        "idBoard",
        "idCard",
        "idList",
        "pos",
        "closed",
        "name"
    ])

    LISTA_CHECKLIST_IN_CARDS = pd.DataFrame(columns=[
        "area",
        "idBoard",
        "idCard",
        "idMember",
        "idChecklist",
        "nameList",
        "id",
        "state",
        "name",
        "nameData",
        "due",
        "pos"
    ])

    # LISTA DE BOARDS
    dados = detalhe_lista_boards_por_members(key, token, idMember, area)
    if len(dados) > 0:
        filename = 'lista_boards_' + area
        __escreve_arquivo(dados, tmp_board + output.format(file=filename))

    quadros = buscaArquivoDeQuadros()
    dados = dados.merge(quadros, left_on='idBoard', right_on='id', how='left')
    dados = dados[dados['dateLastActivity'] != dados['data']]
    # PARA CADA BOARD BUSCA DOS CARDS
    for row in dados.itertuples():
        idBoard = row.idBoard
        nome_board = row.name
        shortLink_board = row.shortLink
        idOrganizations = row.idOrganization
        print(nome_board)

        LISTA_MEMBERS = pd.DataFrame(columns=[
            "area",
            "idBoard",
            "idCard",
            "idMember",
            "idMemberReferrer",
            "username",
            "activityBlocked",
            "avatarHash",
            "avatarUrl",
            "fullName",
            "initials",
            "nonPublicAvailable"
        ])

        LISTA_ACTIONS = pd.DataFrame(columns=[
            "area",
            "idBoard",
            "idCard",
            "idAction",
            "idMemberCreator",
            "type",
            "date",
            "data_text",
            "card_old_due",
            "card_due"
        ])

        if shortLink_board != '-1':
            #print('BOARD: {0}'.format(nome_board))
            logging.info('BOARD: {0}'.format(nome_board))

            # BUSCA ORGANIZACAO
            if idOrganizations:
                dados_organizations = detalhe_organizations(key, token, idBoard, idOrganizations, area)
                if len(dados_organizations) > 0:
                    filename = 'boards_organizations_' + shortLink_board
                    __escreve_arquivo(dados_organizations, tmp_organizations + output.format(file=filename))

            # MEMBERS OF BOARDS
            dados_members = detalhe_board_members(key, token, idBoard, area)
            if len(dados_members) > 0:
                #print("MEMBERS: {0} IN BOARD {1}".format(len(dados_members), idBoard))
                logging.info("MEMBERS: {0} IN BOARD {1}".format(len(dados_members), idBoard))
                for member in dados_members.itertuples():
                    LISTA_MEMBERS = LISTA_MEMBERS.append(
                        {
                            "area": member.area,
                            "idBoard": member.idBoard,
                            "idCard": "",
                            "idMember": member.idMember,
                            "idMemberReferrer": "",
                            "username": member.username,
                            "activityBlocked": member.activityBlocked,
                            "avatarHash": member.avatarHash,
                            "fullName": member.fullName,
                            "initials": member.initials,
                            "nonPublicAvailable": member.nonPublicAvailable
                        }, ignore_index=True)

                filename_member = "boards_members_" + shortLink_board
                __escreve_arquivo(LISTA_MEMBERS, tmp_member + output.format(file=filename_member))

            # ACTIONS
            data_carga = [
                ['2019-01-01', '2019-01-31', 'T1'],
                ['2019-02-01', '2019-02-29', 'T2'],
                ['2019-03-01', '2019-03-31', 'T3'],
                ['2019-04-01', '2019-04-30', 'T4'],
                ['2019-05-01', '2019-05-31', 'T5'],
                ['2019-06-01', '2019-06-30', 'T6'],
                ['2019-07-01', '2019-07-31', 'T7'],
                ['2019-08-01', '2019-08-31', 'T8'],
                ['2019-09-01', '2019-09-30', 'T9'],
                ['2019-10-01', '2019-10-31', 'T10'],
                ['2019-11-01', '2019-11-30', 'T11'],
                ['2019-12-01', '2019-12-31', 'T12'],
                ['2020-01-01', '2020-01-31', 'T1'],
                ['2020-02-01', '2020-02-29', 'T2'],
                ['2020-03-01', '2020-03-31', 'T3'],
                ['2020-04-01', '2020-04-30', 'T4'],
                ['2020-05-01', '2020-05-31', 'T5'],
                ['2020-06-01', '2020-06-30', 'T6'],
                ['2020-07-01', '2020-07-31', 'T7'],
                ['2020-08-01', '2020-08-31', 'T8'],
                ['2020-09-01', '2020-09-30', 'T9'],
                ['2020-10-01', '2020-10-31', 'T10'],
                ['2020-11-01', '2020-11-30', 'T11'],
                ['2020-12-01', '2020-12-31', 'T12'],
                ['2021-01-01', '2021-01-31', 'T1'],
                ['2021-02-01', '2021-02-28', 'T2'],
                ['2021-03-01', '2021-03-31', 'T3'],
                ['2021-04-01', '2021-04-30', 'T4'],
                ['2021-05-01', '2021-05-31', 'T5'],
                ['2021-06-01', '2021-06-30', 'T6'],
                ['2021-07-01', '2021-07-31', 'T7'],
                ['2021-08-01', '2021-08-31', 'T8'],
                ['2021-09-01', '2021-09-30', 'T9'],
                ['2021-10-01', '2021-10-31', 'T10'],
                ['2021-11-01', '2021-11-30', 'T11'],
                ['2021-12-01', '2021-12-31', 'T12']
            ]

            for posicao in data_carga:
                dt_inicio = posicao[0]
                dt_final = posicao[1]
                name_file_aux = str(dt_inicio) + '_' + str(posicao[2])

                dados_actions = detalhe_board_actions(key, token, idBoard, area, dt_inicio, dt_final)
                if len(dados_actions) > 0:
                    #print("ACTIONS: {0} IN BOARD {1}".format(len(dados_actions), idBoard))
                    logging.info("ACTIONS: {0} IN BOARD {1}".format(len(dados_actions), idBoard))
                    for item_action in dados_actions.itertuples():
                        try:
                            LISTA_ACTIONS = LISTA_ACTIONS.append(
                                {
                                    "area": item_action.area,
                                    "idBoard": item_action.idBoard,
                                    "idCard": item_action.idCard,
                                    "idAction": item_action.idAction,
                                    "idMemberCreator": item_action.idMemberCreator,
                                    "type": item_action.type,
                                    "date": item_action.date,
                                    "data_text": item_action.data_text,
                                    "card_old_due": item_action.card_old_due,
                                    "card_due": item_action.card_due
                                }, ignore_index=True)
                        except Exception as e:
                            LISTA_ACTIONS = LISTA_ACTIONS.append(
                                {
                                    "area": item_action.area,
                                    "idBoard": item_action.idBoard,
                                    "idCard": item_action.idCard,
                                    "idAction": item_action.idAction,
                                    "idMemberCreator": item_action.idMemberCreator,
                                    "type": item_action.type,
                                    "date": item_action.date,
                                    "data_text": "",
                                    "card_old_due": item_action.card_old_due,
                                    "card_due": item_action.card_due
                                }, ignore_index=True)

                    filename_action = "boards_cards_actions_" + shortLink_board + "_" + name_file_aux
                    __escreve_arquivo(LISTA_ACTIONS, tmp_actions + output.format(file=filename_action))

                    LISTA_ACTIONS = pd.DataFrame(columns=[
                        "area",
                        "idBoard",
                        "idCard",
                        "idAction",
                        "idMemberCreator",
                        "type",
                        "date",
                        "data_text",
                        "card_old_due",
                        "card_due"
                    ])

            # BUSCA CARDS PARA O BOARD
            board_card = detalhe_board_cards(key, token, shortLink_board, idBoard, area)
            if len(board_card) > 0:
                #print('CARDS: {0} IN BOARD {1}'.format(len(board_card), nome_board))
                logging.info('CARDS: {0} IN BOARD {1}'.format(len(board_card), nome_board))
                filename = 'boards_cards_' + shortLink_board
                __escreve_arquivo(board_card, tmp_card + output.format(file=filename))

            # PARA CADA CARD BUSCA : LIST ,CUSTOMFIELDS ,CHECKLISTS
            for cards in board_card.itertuples():
                idCard = cards.idCard
                idChecklist = cards.idChecklists

                # LIST OF CARDS
                dados_lista_in_cards = detalhe_board_cards_lists(key, token, idBoard, idCard, area)
                if len(dados_lista_in_cards) > 0:
                    #print("LIST: {0} IN CARD {1}".format(len(dados_lista_in_cards), idCard))
                    logging.info("LIST: {0} IN CARD {1}".format(len(dados_lista_in_cards), idCard))
                    for item in dados_lista_in_cards.itertuples():
                        LISTA_LIST = LISTA_LIST.append(
                            {
                                "area": item.area,
                                "idBoard": item.idBoard,
                                "idCard": item.idCard,
                                "idList": item.idList,
                                "pos": item.pos,
                                "closed": item.closed,
                                "name": item.name
                            }, ignore_index=True).astype(str)

                # CUSTOMFIELDS
                dados_customfields_in_card = detalhe_card_customfields(key, token, idCard, area)
                if len(dados_customfields_in_card) > 0:
                    #print("CUSTOM: {0} IN CARD {1}".format(len(dados_customfields_in_card), idCard))
                    logging.info("CUSTOM: {0} IN CARD {1}".format(len(dados_customfields_in_card), idCard))
                    # ITEM DENTRO DO CUSTOM FIELD
                    for custom in dados_customfields_in_card.itertuples():
                        dados_customfields_detalhes = detalhe_customfields(key, token, custom.idCard,
                                                                           custom.idCustomField, area)

                        if len(dados_customfields_detalhes) > 0:
                            #print("CUSTOM ITEM: {0} IN CARD {1}".format(len(dados_customfields_detalhes), idCard))
                            logging.info(
                                "CUSTOM ITEM: {0} IN CARD {1}".format(len(dados_customfields_detalhes), idCard))
                            # TRATA RESULTADO
                            custom_idboard_ = idBoard
                            custom_idcard_ = dados_customfields_detalhes['idCard'][0]
                            custom_idCustomField_ = dados_customfields_detalhes['id'][0]
                            custom_name_ = dados_customfields_detalhes['name'][0]
                            custom_type_ = dados_customfields_detalhes['type'][0]
                            custom_area = dados_customfields_detalhes['area'][0]

                            custom_options_ = ''
                            custom_value = ''
                            custom_idValue = ''

                            if custom_type_ == 'list':
                                custom_options_ = dados_customfields_detalhes['options'][0]
                                custom_idValue = custom.idValue

                                # SELECIONA APENAS ITEM ESCOLHIDO
                                df_options = pd.json_normalize(custom_options_)
                                df_options = df_options.rename(columns={'value.text': 'value_text'})
                                for option_item in df_options.itertuples():
                                    if option_item.id == custom_idValue:
                                        custom_value = option_item.value_text

                            if custom_type_ == 'text':
                                custom_value = custom.value_text
                            if custom_type_ == 'date':
                                custom_value = custom.value_date

                            '''print('idCustomField: {0} name:{1} type:{2} value:{3}'.format(custom_idCustomField_,
                                                                                          custom_name_,
                                                                                          custom_type_,
                                                                                          custom_value))'''

                            logging.info('idCustomField: {0} name:{1} type:{2} value:{3}'.format(custom_idCustomField_,
                                                                                                 custom_name_,
                                                                                                 custom_type_,
                                                                                                 custom_value))
                            LISTA_CUSTOMFIELDS = LISTA_CUSTOMFIELDS.append({
                                "area": custom_area,
                                "idBoard": custom_idboard_,
                                "idCards": custom_idcard_,
                                "idCustomField": custom_idCustomField_,
                                "name": custom_name_,
                                "type": custom_type_,
                                "value": custom_value
                            }, ignore_index=True)

                # CHECKLISTS
                if len(eval(idChecklist)) > 0:
                    #print('CHECKLISTS: {0} IN CARD: {1} '.format(eval(idChecklist), idCard))
                    logging.info('CHECKLISTS: {0} IN CARD: {1} '.format(eval(idChecklist), idCard))
                    try:
                        for check_items in eval(idChecklist):
                            #print("check_items: idChecklist: {0}".format(check_items))
                            logging.info("check_items: idChecklist: {0}".format(check_items))
                            dados_checklist = detalhe_board_cards_checklist(key, token, check_items, idBoard, idCard,
                                                                            area)
                            if len(dados_checklist) > 0:
                                for check in dados_checklist.itertuples():
                                    LISTA_CHECKLIST_IN_CARDS = LISTA_CHECKLIST_IN_CARDS.append(
                                        {
                                            "area": check.area,
                                            "idBoard": check.idBoard,
                                            "idCard": check.idCard,
                                            "idMember": check.idMember,
                                            "idChecklist": check.idChecklist,
                                            "nameList": check.nameList,
                                            "id": check.id,
                                            "state": check.state,
                                            "name": check.name,
                                            "nameData": check.nameData,
                                            "due": check.due,
                                            "pos": check.pos
                                        }, ignore_index=True)
                    except Exception as e:
                        #print("Checklist em  branco")
                        logging.info("Checklist em  branco")

            # GERA ARQUIVO FINAL POR BOARD
            if len(LISTA_LIST) > 0:
                filename_list = "boards_cards_list_" + shortLink_board
                __escreve_arquivo(LISTA_LIST, tmp_list + output.format(file=filename_list))

            if len(LISTA_CUSTOMFIELDS) > 0:
                filename_customfield = "boards_cards_customfields_" + shortLink_board
                __escreve_arquivo(LISTA_CUSTOMFIELDS, tmp_customfields + output.format(file=filename_customfield))

            if len(LISTA_CHECKLIST_IN_CARDS) > 0:
                filename_check = "boards_cards_checklists_" + shortLink_board
                __escreve_arquivo(LISTA_CHECKLIST_IN_CARDS, tmp_checklist + output.format(file=filename_check))

    return {'exit': 200}


def buscaArquivoDeConfiguracao():
    area_ = 'false'
    try:
        url = f'https://{os.environ["AZURE_ADL_STORE_NAME"]}.blob.core.windows.net/{os.environ["AZURE_ADL_FILE_SYSTEM"]}/lnd/crw/trello/config/API_TOKEN.csv?sp=r&st=2021-03-16T05:09:53Z&se=2222-03-16T13:09:53Z&spr=https&sv=2020-02-10&sr=b&sig=hpdGAU0wNFKm8BdFfDDvwbjY3i3Nqs1gbpPWtqfFzXE%3D'
        area_ = pd.read_csv(url, delimiter=";")
        #print(area_)

    except Exception as e:
        print('ERROR: {0}'.format(e))

    return area_

def buscaArquivoDeQuadros():
    area_ = 'false'
    try:
        url = f'https://{os.environ["AZURE_ADL_STORE_NAME"]}.blob.core.windows.net/{os.environ["AZURE_ADL_FILE_SYSTEM"]}/lnd/crw/trello/config/quadros.csv?sp=r&st=2021-03-16T05:11:04Z&se=2222-03-16T13:11:04Z&spr=https&sv=2020-02-10&sr=b&sig=m2WDpwqZ6zD3BZWnErZ7JFIG%2BAjfVImhzDAKqPA5RBk%3D'
        area_ = pd.read_csv(url, delimiter=",", header=None, usecols=[1,4])
        area_.columns = ['id', 'data']
        #print(area_)

    except Exception as e:
        print('ERROR: {0}'.format(e))

    return area_


def execute(**kwargs):
    global DEBUG, LND

    DEBUG = bool(int(os.environ.get('DEBUG', 1)))
    LND = '/tmp/dev/lnd/crw' if DEBUG else '/lnd/crw'

    start = time.time()
    metadata = {'finished_with_errors': False}

    try:
        configs = buscaArquivoDeConfiguracao()
        for index, config in configs.iterrows():
            print(config["AREA"])
            log = main(AREA=config['AREA'], CHAVE=config['CHAVE']
                       , TOKEN=config['TOKEN'], USUARIO=config['USUARIO'], **kwargs)
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

    # upload do log
    try:
        import zipfile

        #print('creating archive')
        zf = zipfile.ZipFile('/tmp/trello/trello/log_trello.zip', mode='w')
        try:
            #print('adding log')
            zf.write('/tmp/trello/trello/log_trello.log')
        finally:
            #print('closing')
            zf.close()
        adl = authenticate_datalake()
        __upload_files(adl, '/tmp/trello/trello/log_trello.zip')
    except Exception as e:
        print('ERRO AO CARREGAR LOG')

    return metadata


DEBUG, LND = None, None
if __name__ == '__main__':
    import dotenv
    from app import app

    dotenv.load_dotenv(app.ROOT_PATH + '/debug.env')
    exit(execute(host='localhost', passwd=None, reload=None, reset=False, callback=None))
