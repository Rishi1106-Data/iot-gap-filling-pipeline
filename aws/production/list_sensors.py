"""
list_sensors.py — Produce the list of sensor keys to reconstruct.

Step Functions' Map state needs an array of items to fan out over. This scans
WS_Spatial_Neighbors and emits every deviceId_topic key. Run it as a small
Lambda (or the first Fargate task) at the start of the daily run.

Output: JSON list of {"SENSOR_KEY": "<id#topic>"} to stdout, which Step
Functions can consume directly as the Map ItemsPath.
"""

import json
import logging

import boto3

import config

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("annam.list")


def list_sensor_keys():
    dynamo = boto3.resource("dynamodb", region_name=config.AWS_REGION)
    table = dynamo.Table(config.NEIGHBORS_TABLE)
    keys, kwargs = [], {"ProjectionExpression": config.NEIGHBORS_KEY_NAME}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            k = item.get(config.NEIGHBORS_KEY_NAME)
            if k:
                keys.append(k)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return keys


def lambda_handler(event, context):
    keys = list_sensor_keys()
    log.info("Found %d sensors", len(keys))
    return [{"SENSOR_KEY": k} for k in keys]


if __name__ == "__main__":
    print(json.dumps(lambda_handler(None, None)))
