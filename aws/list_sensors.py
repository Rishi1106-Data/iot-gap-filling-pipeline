"""aws/list_sensors.py — sensor discovery + batch creation.

Runs as the first compute step of the Step Functions state machine (either as a
tiny Fargate task or a Lambda — it has no heavy deps beyond boto3). It:

  1. list_active_sensors()  → reads ACTIVE sensors from DynamoDB (GSI Query).
  2. create_sensor_batches() → groups them into fixed-size batches.

It prints a JSON array of batches to stdout. Step Functions captures that as the
Map state's input, so EACH batch becomes ONE Fargate task that processes many
sensors — never one task per sensor.

Batching strategy (cost-aware):
  • Sensors that share neighbours are co-located in the same batch when possible,
    so the per-task local-disk neighbour cache (io_s3.load_neighbor_cached) gets
    maximum hit rate and minimum duplicate S3 GETs.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import Any, Dict, List

from config import load as load_cfg          # noqa: E402  (aws/ on sys.path)
from io_dynamo import DynamoIO                # noqa: E402
from cloudwatch_logger import CloudWatchLogger  # noqa: E402


def list_active_sensors(dynamo: DynamoIO, max_sensors: int | None = None) -> List[Dict[str, Any]]:
    """Thin wrapper so callers/tests have a stable name. Returns metadata items."""
    return dynamo.list_active_sensors(max_sensors=max_sensors)


def _neighbor_ids(sensor: Dict[str, Any]) -> List[str]:
    return [str(n["id"]) for n in sensor.get("neighbors", [])]


def create_sensor_batches(
    sensors: List[Dict[str, Any]], batch_size: int
) -> List[Dict[str, Any]]:
    """Group sensors into batches, clustering by shared neighbours.

    Returns a list of batch descriptors:
        {"batch_id": int, "device_ids": [...], "size": int}

    Clustering heuristic: sort sensors by their first neighbour id so devices
    that lean on the same neighbour fall adjacent, then chunk. This is a cheap
    O(n log n) approximation — not a graph partition — but it materially raises
    neighbour-cache hit rate without adding infrastructure.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    # Cluster key: the set of neighbour ids (sorted, joined). Sensors with the
    # same neighbours share a key and sort together.
    def cluster_key(s: Dict[str, Any]) -> str:
        nids = sorted(_neighbor_ids(s))
        return "|".join(nids) if nids else "~none~"

    ordered = sorted(sensors, key=lambda s: (cluster_key(s), str(s.get("device_id"))))

    batches: List[Dict[str, Any]] = []
    for i in range(0, len(ordered), batch_size):
        chunk = ordered[i : i + batch_size]
        device_ids = [str(s["device_id"]) for s in chunk]
        batches.append(
            {
                "batch_id": len(batches),
                "device_ids": device_ids,
                "size": len(device_ids),
            }
        )
    return batches


def main() -> int:
    cfg = load_cfg()
    log = CloudWatchLogger(cfg.cw_namespace, cfg.log_level, dimensions={"stage": "list_sensors"})
    dynamo = DynamoIO(cfg.sensor_table, cfg.results_table, cfg.region)

    sensors = list_active_sensors(dynamo)
    log.info("Discovered active sensors", count=len(sensors))

    batches = create_sensor_batches(sensors, cfg.batch_size)
    log.info(
        "Created batches",
        batch_count=len(batches),
        batch_size=cfg.batch_size,
        total_sensors=len(sensors),
    )
    log.metric("ActiveSensors", len(sensors))
    log.metric("BatchCount", len(batches))

    payload = {"batches": batches, "run_id": cfg.run_id, "run_date": cfg.run_date}

    # Write to S3 at runs/<run_id>/batches.json so the Step Functions LoadBatches
    # state can read it deterministically (stdout from an ECS task is not
    # directly addressable by the state machine).
    if cfg.run_id:
        import boto3
        s3 = boto3.client("s3", region_name=cfg.region)
        key = f"runs/{cfg.run_id}/batches.json"
        s3.put_object(
            Bucket=cfg.data_bucket,
            Key=key,
            Body=json.dumps(payload).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        log.info("Wrote batches to S3", key=key, batch_count=len(batches))

    # Also emit to stdout for local/manual runs and debugging.
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
