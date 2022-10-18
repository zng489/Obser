import logging
import argparse
import os
import json

def validate(crawler: str, content: str, error_report: dict):
    logging.info("Analyzing crawler '{crawler}'".format(crawler=crawler))
    patterns = [
        'def authenticate_datalake() -> FileSystemClient:',
        'def main(**kwargs):',
        'def execute(**kwargs):',
        'DEBUG, LND = None, None',
        "if __name__ == '__main__':",
        'import dotenv',
        'from app import app',
        "dotenv.load_dotenv(app.ROOT_PATH + '/debug.env')",
        "exit(execute(host='localhost', passwd=None, reload=None, reset=False, callback=None))",
        'global DEBUG, LND',
        "DEBUG = bool(int(os.environ.get('DEBUG', 1)))",
        "LND = '/tmp/dev/lnd/crw' if DEBUG else '/lnd/crw'",
    ]
    errors = []
    for p in patterns:
        if content.find(p) == -1:
            errors.append("Mandatory code pattern '{p}' not found".format(p=p))
    if len(errors) > 0:
        error_report[crawler] = errors

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(module)s - %(funcName)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(conflict_handler='resolve')
    parser.add_argument('-d', '--dir', help='Base dir of your git report for ADF', required=True)
    args = parser.parse_args()

    crawlers_base_path = '{repo}/app/crawlers'.format(repo=args.dir)

    crawlers = os.listdir(crawlers_base_path)
    error_report = {}
    for c in crawlers:
        if c == '__init__.py':
            # Skip __init__.py files
            continue
        else:
            with open ('{path}/{c}'.format(path=crawlers_base_path, c=c), 'r') as f:
                content = f.read()
                validate(
                    crawler=c,
                    content=content,
                    error_report=error_report
                )
    if len(error_report.keys()) > 0:
        logging.error(json.dumps(error_report, indent=4))
        raise Exception('Errors were found. Check log.')