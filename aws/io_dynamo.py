"""aws/io_dynamo.py — DynamoDB access for sensor metadata, neighbours, results.

Cost-efficiency principles baked in:
  • Single-table-ish design with two tables (metadata vs results) to keep hot
    write traffic off the metadata table.
  • get_sensor_metadata uses GetItem (1 RCU eventually-consistent) — never Scan.
  • list_active_sensors uses a GSI Query on a low-cardinality 'status' key, so
    we read only ACTIVE rows, paginated — never a full-table Scan.
  • get_neighbor_sensors reads the neighbour list straight off the target's own
    metadata item (it is stored as an attribute), so discovering neighbours costs
    ZERO extra reads beyond the single metadata GetItem we already did.
  • write_gap_fill_results batches with BatchWriteItem (≤25 items/call).

----------------------------------------------------------------------------
TABLE 1 — annam-sensor-metadata   (metadata + topology, read-mostly)
----------------------------------------------------------------------------
  PK: PK   = "SENSOR#<device_id>"
  SK: SK   = "META"
  Attributes:
    device_id        (S)  "201"
    status           (S)  "ACTIVE" | "INACTIVE" | "DECOMMISSIONED"
    latitude         (N)
    longitude        (N)
    ref_col          (S)  "CorrectedTemp"
    neighbors        (L)  [ {id:"237", distance_km:16.75}, {id:"249", distance_km:17.5} ]
    s3_raw_key       (S)  "raw/201/201.csv"   (optional override)
    updated_at       (S)  ISO8601

  GSI: gsi_status
    PK: status        (S)   "ACTIVE"
    SK: device_id     (S)   "201"
    → Query status="ACTIVE" returns all active sensors, paginated, cheaply.

  Example item:
    {
      "PK": "SENSOR#201", "SK": "META",
      "device_id": "201", "status": "ACTIVE",
      "latitude": 31.27, "longitude": 74.84, "ref_col": "CorrectedTemp",
      "neighbors": [{"id":"237","distance_km":16.75},{"id":"249","distance_km":17.5}],
      "updated_at": "2026-06-01T00:00:00Z"
    }

----------------------------------------------------------------------------
TABLE 2 — annam-gapfill-results   (run outputs, write-heavy, append-only)
----------------------------------------------------------------------------
  PK: PK   = "SENSOR#<device_id>"
  SK: SK   = "RUN#<run_date>#<run_id>"
  Attributes:
    device_id, run_id, run_date, status ("success"|"failed"),
    rows_total (N), method_counts (M), filled_s3_uri (S),
    reports_s3_prefix (S), train_mode (BOOL), duration_seconds (N),
    error (S, only on failure), ttl (N, epoch seconds for auto-expiry)

  TTL attribute 'ttl' auto-deletes old run records → no storage creep, no
  manual cleanup cost. Set e.g. 180 days out.

  Example item:
    {
      "PK":"SENSOR#201","SK":"RUN#2026-06-01#abc123",
      "device_id":"201","run_id":"abc123","run_date":"2026-06-01",
      "status":"success","rows_total":16666,
      "method_counts":{"original":10260,"model":3747,"neighbor":241},
      "filled_s3_uri":"s3://bucket/filled/2026-06-01/201/filled_dataset.csv",
      "train_mode":false,"duration_seconds":113,"ttl":1780000000
    }
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Dict, Iterator, List, Optional

import boto3
from boto3.dynamodb.conditions import Key
from botocore.config import Config as BotoConfig


_BOTO_CFG = BotoConfig(retries={"max_attempts": 5, "mode": "adaptive"})


def _to_dynamo(obj: Any) -> Any:
    """Recursively convert floats to Decimal (DynamoDB rejects float)."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_dynamo(v) for v in obj]
    return obj


def _from_dynamo(obj: Any) -> Any:
    """Recursively convert Decimal back to int/float for the pipeline/config."""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    if isinstance(obj, dict):
        return {k: _from_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_from_dynamo(v) for v in obj]
    return obj


class DynamoIO:
    """All DynamoDB reads/writes for one pipeline invocation."""

    def __init__(self, sensor_table: str, results_table: str, region: str) -> None:
        self.region = region
        self.ddb = boto3.resource("dynamodb", region_name=region, config=_BOTO_CFG)
        self.sensors = self.ddb.Table(sensor_table)
        self.results = self.ddb.Table(results_table)

    # ── reads ──────────────────────────────────────────────────────────────--
    def get_sensor_metadata(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Single GetItem (cheap). Returns the metadata dict or None."""
        resp = self.sensors.get_item(
            Key={"PK": f"SENSOR#{device_id}", "SK": "META"},
            ConsistentRead=False,  # eventually-consistent = half the RCU cost
        )
        item = resp.get("Item")
        return _from_dynamo(item) if item else None

    def get_neighbor_sensors(self, device_id: str) -> List[Dict[str, Any]]:
        """Return [{id, distance_km}, ...] for a target.

        Cost: ZERO extra reads — neighbours are an attribute of the metadata
        item, so this reuses get_sensor_metadata. We expose it separately for a
        clean call site.
        """
        meta = self.get_sensor_metadata(device_id)
        if not meta:
            return []
        return list(meta.get("neighbors", []))

    def list_active_sensors(
        self, page_limit: int = 1000, max_sensors: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Query the gsi_status GSI for status='ACTIVE'. Paginated, no Scan.

        Returns a list of metadata items (at least device_id + neighbours).
        Supports thousands of sensors via automatic pagination.
        """
        collected: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {
            "IndexName": "gsi_status",
            "KeyConditionExpression": Key("status").eq("ACTIVE"),
            "Limit": page_limit,
        }
        while True:
            resp = self.sensors.query(**kwargs)
            for item in resp.get("Items", []):
                collected.append(_from_dynamo(item))
                if max_sensors and len(collected) >= max_sensors:
                    return collected[:max_sensors]
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return collected

    def iter_active_sensors(self, page_limit: int = 1000) -> Iterator[Dict[str, Any]]:
        """Generator variant for memory-bounded streaming of huge fleets."""
        kwargs: Dict[str, Any] = {
            "IndexName": "gsi_status",
            "KeyConditionExpression": Key("status").eq("ACTIVE"),
            "Limit": page_limit,
        }
        while True:
            resp = self.sensors.query(**kwargs)
            for item in resp.get("Items", []):
                yield _from_dynamo(item)
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

    # ── writes ─────────────────────────────────────────────────────────────--
    def write_gap_fill_results(self, records: List[Dict[str, Any]], ttl_days: int = 180) -> int:
        """BatchWriteItem the per-sensor run results (≤25 items/call).

        Each record must contain at least: device_id, run_id, run_date, status.
        Returns the number of items written.
        """
        if not records:
            return 0
        ttl_epoch = int(time.time()) + ttl_days * 86400
        written = 0
        with self.results.batch_writer(overwrite_by_pkeys=["PK", "SK"]) as batch:
            for rec in records:
                device_id = rec["device_id"]
                run_date = rec.get("run_date", "unknown")
                run_id = rec.get("run_id", "unknown")
                item = {
                    "PK": f"SENSOR#{device_id}",
                    "SK": f"RUN#{run_date}#{run_id}",
                    "ttl": ttl_epoch,
                    **_to_dynamo(rec),
                }
                batch.put_item(Item=item)
                written += 1
        return written
