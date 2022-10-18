import json
import os
import argparse
import logging


def validate_activities(activities:list, rules: list, errors: list):
    for r in rules:
        has_activity = False
        for a in activities:
            if a['type'] == r['type']:  # There's just one key in the config dict and that's the activity type
                # Verify: a contains r
                if r['typeProperties'].items() <= a['typeProperties'].items():
                    # Breaks "for a in activities" loop
                    has_activity = True
                    break
        # Iterated through all activities and still haven't found
        if has_activity is False:
            errors.append("Mandatory activity {r} is not declared.".format(r=r))


def validate_name(name: str, prefix: list, errors: list):
    name_valid = False
    for p in prefix:
        if name.startswith(p):
            name_valid = True
    if name_valid is not True:
        message = "Name '{name}' has invalid prefix. Allowed prefixes are {prefix}".format(
            name=name,
            prefix=prefix,
        )
        logging.info(message)
        errors.append(message)


def get_layer(pipeline: dict, layer_configs: dict):
    folder = pipeline['properties']['folder']['name']
    layer = None

    for l in layer_configs.keys():
        if l.endswith('/*'):
            # If the config ends with '/*', check the prefix
            value_to_compare = l.replace('/*', '')
            if folder.startswith(value_to_compare):
                layer = l
                break
        else:
            if folder == l:
                layer = l
                break
    if layer is None:
        logging.info("Pipeline '{name}' is in folder '{folder}' but this folder has no layer rules related. Won't be evaluated".format(
            name=pipeline['name'],
            folder=folder
        ))
    return layer


def validate_parameters(parameters, rules: dict, errors: list):
    for r in rules.keys():
        try:
            param = parameters[r]
            type_ = param['type'].lower()
            type_expected = rules[r]['type']
            if type_ != type_expected:
                errors.append(
                    "Mandatory parameter '{p}' is not of expected type '{type}'.".format(
                        p=r,
                        type=type_expected,
                    )
                )
        except KeyError:
            errors.append("Mandatory parameter '{p}' not declared.".format(p=r))


def validate(pipeline: dict, error_report: dict):
    errors = []
    layer_configs = {
        'raw/bdo/*': {
            'name': {
                'prefix': ['org_raw_']
            },
            'parameters':{
                'tables':{'type': 'array'},
                'env': {'type': 'object'},
            },
            'activities': [
                {
                    "type": "ExecutePipeline",
                    "typeProperties": {
                        "pipeline": {
                            "referenceName": "raw_load_dbo_unified__0__switch_env",
                            "type": "PipelineReference"
                        },
                    },
                },
            ],
        },
        'raw/crw/*': {
            'name': {
                'prefix': ['org_raw_']
            },
            'parameters': {
                'tables': {'type': 'object'},
                'databricks': {'type': 'object'},
                'env': {'type': 'object'},
            },
            'activities': [
                {
                    "type": "ExecutePipeline",
                    "typeProperties": {
                        "pipeline": {
                            "referenceName": "import_crw__0__switch_env",
                            "type": "PipelineReference"
                        },
                    },
                },
            ],
        },
        'raw/usr/*': {
            'name': {
                'prefix': ['org_raw_']
            },
            'parameters': {
                'files': {'type': 'array'},
                'databricks': {'type': 'object'},
                'env': {'type': 'object'},
            },
        },
        'trs/*': {
            'name': {
                'prefix': ['raw_trs_'],
            },
            'parameters': {
                'tables': {'type': 'object'},
                'databricks': {'type': 'object'},
                'env': {'type': 'object'},
            },
            'activities': [
                {
                    "type": "ExecutePipeline",
                    "typeProperties": {
                        "pipeline": {
                            "referenceName": "trusted__0__switch_env",
                            "type": "PipelineReference"
                        },
                    },
                },
            ],
        },
        'biz/*': {
            'name': {
                'prefix': ['trs_biz_', 'biz_biz_']
            },
            'parameters': {
                'tables': {'type': 'object'},
                'user_parameters': {'type': 'object'},
                'env': {'type': 'object'},
            },
            'activities': [
                {
                    "type": "ExecutePipeline",
                    "typeProperties": {
                        "pipeline": {
                            "referenceName": "business__0__switch_env",
                            "type": "PipelineReference"
                        },
                    },
                },
            ],
        },
        'workflow/*': {
            'parameters': {
                'env': {'type': 'object'},
            },
        }
    }

    layer = get_layer(pipeline=pipeline, layer_configs=layer_configs)
    if layer is None:
        return

    name = pipeline['name']
    logging.info("Pipeline '{pipeline}' is of layer '{layer}'".format(
        pipeline=name,
        layer=layer,
    ))
    if layer not in layer_configs.keys():
        # Undeclared layers won't be evaluated
        return

    # ======== Name [START]
    try:
        layer_configs[layer]['name']
        validate_name(name=name,
                      prefix=layer_configs[layer]['name']['prefix'],
                      errors=errors)
    except KeyError:
        logging.info("Pipeline '{name}' is of layer '{layer}' and there are no 'name' configs to evaluate".format(
            name=name,
            layer=layer,
        ))
    # ======== Name [END]

    # ======== Parameters [START]
    has_parameters_validation = False
    try:
        layer_configs[layer]['parameters']
        has_parameters_validation = True
    except KeyError:
        logging.info("Pipeline '{name}' is of layer '{layer}' and there are no 'parameter' configs to evaluate".format(
            name=name,
            layer=layer,
        ))
    if has_parameters_validation is True:
        try:
            pipeline['properties']['parameters']
            validate_parameters(
                parameters=pipeline['properties']['parameters'],
                rules=layer_configs[layer]['parameters'],
                errors=errors
            )
        except KeyError:
            message = "Pipeline '{name}' has parameters validation for layer '{layer}', but implementation has no parameters".format(
                name=name,
                layer=layer,
            )
            errors.append(message)
            logging.info(message)
    # ======== Parameters [END]

    # ======== Activities [START]
    try:
        layer_configs[layer]['activities']
        validate_activities(activities=pipeline['properties']['activities'],
                            rules=layer_configs[layer]['activities'],
                            errors=errors,
                            )
    except KeyError:
        logging.info("Pipeline '{name}' is of layer '{layer}' and there are no 'activities' configs to evaluate".format(
            name=name,
            layer=layer,
        ))
    # ======== Activities [END]
    if len(errors) > 0:
        error_report[name] = errors


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(module)s - %(funcName)s - %(levelname)s - %(message)s'
    )

    parser = argparse.ArgumentParser(conflict_handler='resolve')
    parser.add_argument('-d', '--diff-file', help='Full absolute path of your diff file', required=True)
    args = parser.parse_args()

    error_report = {}

    with open(args.diff_file, 'r') as f:
        pipelines = [i.replace('\n', '') for i in f.readlines()]

    for p in pipelines:
        with open(p, 'r') as x:
            pipeline = json.load(x)
            if 'folder' in pipeline['properties']:
                folder = pipeline['properties']['folder']['name']
                validate(pipeline=pipeline,
                         error_report=error_report
                         )
            else:
                name = pipeline['name']
                message = "Pipeline '{name}' is in root level. All items in this project should be placed in folders.".format(
                    name=name,
                )
                logging.warning(message)
                error_report[name] = [message]

    if len(error_report.keys()) > 0:
        logging.error(json.dumps(error_report, indent=4))
        raise Exception('Errors were found. Check log.')
    else:
        exit(0)