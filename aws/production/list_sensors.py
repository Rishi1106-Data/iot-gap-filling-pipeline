"""
list_sensors.py — Produce the list of sensor keys to reconstruct.

Scans WS_Spatial_Neighbors and returns every deviceId_topic key. It is imported
by run_batch.py (sensor discovery step of the scheduled batch).

Running this file directly prints the keys as JSON ({"SENSOR_KEY": ...} items).
lambda_handler is a leftover from an earlier design and is NOT deployed: this
package contains no Lambda function and no Step Functions state machine.
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
