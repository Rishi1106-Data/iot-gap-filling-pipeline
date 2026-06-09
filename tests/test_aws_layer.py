"""tests/test_aws_layer.py — unit tests for the AWS integration layer.

Covers config resolution, batching/clustering, DynamoDB read/write, S3 neighbour
caching, and run_batch fault isolation. Uses moto to mock AWS. These tests sit
alongside the existing pipeline tests and do NOT touch src/.
"""
import os
import sys
import json
import importlib
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(REPO, "sample_data")

for p in (os.path.join(REPO, "aws"), REPO, os.path.join(REPO, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-south-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    monkeypatch.setenv("DATA_BUCKET", "annam-test")
    monkeypatch.setenv("METRICS_MODE", "emf")


def _make_tables(region="ap-south-1"):
    ddb = boto3.client("dynamodb", region_name=region)
    ddb.create_table(
        TableName="annam-sensor-metadata",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                   {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "device_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[{
            "IndexName": "gsi_status",
            "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"},
                          {"AttributeName": "device_id", "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"},
        }],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="annam-gapfill-results",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                   {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _put_sensor(did, neighbors, status="ACTIVE", region="ap-south-1"):
    res = boto3.resource("dynamodb", region_name=region)
    res.Table("annam-sensor-metadata").put_item(Item={
        "PK": f"SENSOR#{did}", "SK": "META", "device_id": did, "status": status,
        "neighbors": [{"id": n, "distance_km": Decimal(str(d))} for n, d in neighbors],
        "ref_col": "CorrectedTemp", "updated_at": "test",
    })


# ── config ─────────────────────────────────────────────────────────────────-
def test_config_resolves_from_env():
    import config
    importlib.reload(config)
    c = config.load()
    assert c.data_bucket == "annam-test"
    assert c.sensor_table == "annam-sensor-metadata"
    assert c.raw_key("201") == "raw/201/201.csv"
    assert c.model_key("201") == "models/201/model_selection.joblib"


def test_config_requires_bucket(monkeypatch):
    monkeypatch.delenv("DATA_BUCKET", raising=False)
    import config
    importlib.reload(config)
    with pytest.raises(RuntimeError):
        config.load()


# ── batching ──────────────────────────────────────────────────────────────--
def test_create_batches_respects_size():
    import list_sensors
    sensors = [{"device_id": str(i), "neighbors": []} for i in range(57)]
    batches = list_sensors.create_sensor_batches(sensors, batch_size=25)
    assert len(batches) == 3
    assert [b["size"] for b in batches] == [25, 25, 7]


def test_create_batches_clusters_shared_neighbours():
    import list_sensors
    # Two sensors share neighbour set {A,B}; one has {C}. Clustering should keep
    # the shared-neighbour pair adjacent (same/contiguous batch).
    sensors = [
        {"device_id": "1", "neighbors": [{"id": "A"}, {"id": "B"}]},
        {"device_id": "2", "neighbors": [{"id": "C"}]},
        {"device_id": "3", "neighbors": [{"id": "A"}, {"id": "B"}]},
    ]
    batches = list_sensors.create_sensor_batches(sensors, batch_size=2)
    # Devices 1 and 3 (same neighbour key) must be adjacent in the flattened order
    order = [d for b in batches for d in b["device_ids"]]
    assert abs(order.index("1") - order.index("3")) == 1


def test_create_batches_rejects_bad_size():
    import list_sensors
    with pytest.raises(ValueError):
        list_sensors.create_sensor_batches([], batch_size=0)


# ── DynamoDB ──────────────────────────────────────────────────────────────--
@mock_aws
def test_dynamo_metadata_and_neighbors():
    _make_tables()
    _put_sensor("201", [("237", 16.75), ("249", 17.5)])
    import io_dynamo
    dy = io_dynamo.DynamoIO("annam-sensor-metadata", "annam-gapfill-results", "ap-south-1")
    meta = dy.get_sensor_metadata("201")
    assert meta["device_id"] == "201"
    nbrs = dy.get_neighbor_sensors("201")
    assert {n["id"] for n in nbrs} == {"237", "249"}
    assert dy.get_sensor_metadata("999") is None


@mock_aws
def test_dynamo_list_active_only():
    _make_tables()
    _put_sensor("1", [("2", 5)], status="ACTIVE")
    _put_sensor("2", [("1", 5)], status="ACTIVE")
    _put_sensor("3", [("1", 5)], status="INACTIVE")
    import io_dynamo
    dy = io_dynamo.DynamoIO("annam-sensor-metadata", "annam-gapfill-results", "ap-south-1")
    actives = dy.list_active_sensors()
    ids = {s["device_id"] for s in actives}
    assert ids == {"1", "2"}  # INACTIVE excluded


@mock_aws
def test_dynamo_results_batch_write():
    _make_tables()
    import io_dynamo
    dy = io_dynamo.DynamoIO("annam-sensor-metadata", "annam-gapfill-results", "ap-south-1")
    recs = [
        {"device_id": "201", "run_id": "r1", "run_date": "2026-06-01",
         "status": "success", "rows_total": 100, "method_counts": {"model": 10}},
        {"device_id": "202", "run_id": "r1", "run_date": "2026-06-01",
         "status": "failed", "error": "boom"},
    ]
    n = dy.write_gap_fill_results(recs)
    assert n == 2
    res = boto3.resource("dynamodb", region_name="ap-south-1")
    got = res.Table("annam-gapfill-results").get_item(
        Key={"PK": "SENSOR#201", "SK": "RUN#2026-06-01#r1"})["Item"]
    assert got["status"] == "success"
    assert "ttl" in got  # auto-expiry set


# ── S3 neighbour cache ───────────────────────────────────────────────────────
@mock_aws
def test_s3_neighbour_cache_downloads_once(tmp_path):
    region = "ap-south-1"
    s3c = boto3.client("s3", region_name=region)
    s3c.create_bucket(Bucket="annam-test",
                      CreateBucketConfiguration={"LocationConstraint": region})
    s3c.put_object(Bucket="annam-test", Key="raw/237/237.csv", Body=b"x,y\n1,2\n")

    import io_s3
    s3 = io_s3.S3IO("annam-test", region, work_dir=str(tmp_path))
    p1 = s3.load_neighbor_cached("237", "raw/237/237.csv")
    mtime1 = os.path.getmtime(p1)
    # Second call must reuse the cached file (no re-download → mtime unchanged)
    p2 = s3.load_neighbor_cached("237", "raw/237/237.csv")
    assert p1 == p2
    assert os.path.getmtime(p2) == mtime1


@mock_aws
def test_s3_missing_model_returns_none(tmp_path):
    region = "ap-south-1"
    s3c = boto3.client("s3", region_name=region)
    s3c.create_bucket(Bucket="annam-test",
                      CreateBucketConfiguration={"LocationConstraint": region})
    import io_s3
    s3 = io_s3.S3IO("annam-test", region, work_dir=str(tmp_path))
    got = s3.load_models_from_s3("models/999/model_selection.joblib",
                                 str(tmp_path / "m.joblib"))
    assert got is None  # missing model is not an exception


# ── run_batch fault isolation ────────────────────────────────────────────────
@mock_aws
def test_run_batch_isolates_failures(tmp_path, monkeypatch):
    region = "ap-south-1"
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_ID", "r1")
    monkeypatch.setenv("RUN_DATE", "2026-06-01")
    monkeypatch.setenv("TRAIN_MODE", "false")

    s3c = boto3.client("s3", region_name=region)
    s3c.create_bucket(Bucket="annam-test",
                      CreateBucketConfiguration={"LocationConstraint": region})
    _make_tables()
    # Sensor "bad" exists but has NO raw file in S3 and NO neighbours → fails.
    _put_sensor("bad", [])

    import config, run_batch
    importlib.reload(config)
    importlib.reload(run_batch)

    # By design, an all-fail batch RAISES (so Step Functions retry is meaningful),
    # but the per-sensor failure is recorded in DynamoDB BEFORE the raise.
    with pytest.raises(RuntimeError, match="All 1 sensors in batch failed"):
        run_batch.run_batch(["bad"], cfg=config.load())

    # Failure must still be recorded in DynamoDB results.
    res = boto3.resource("dynamodb", region_name=region)
    item = res.Table("annam-gapfill-results").get_item(
        Key={"PK": "SENSOR#bad", "SK": "RUN#2026-06-01#r1"})["Item"]
    assert item["status"] == "failed"
    assert "error" in item


@mock_aws
def test_run_batch_partial_failure_does_not_raise(tmp_path, monkeypatch):
    """A batch with at least one success must NOT raise; failures are isolated."""
    region = "ap-south-1"
    monkeypatch.setenv("WORK_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_ID", "r2")
    monkeypatch.setenv("RUN_DATE", "2026-06-01")
    monkeypatch.setenv("TRAIN_MODE", "true")

    s3c = boto3.client("s3", region_name=region)
    s3c.create_bucket(Bucket="annam-test",
                      CreateBucketConfiguration={"LocationConstraint": region})
    _make_tables()

    # "good" target + two DISTINCT neighbours uploaded; "bad" has no data.
    s3c.upload_file(os.path.join(SAMPLE_DIR, "Annam_216_new.csv"),
                    "annam-test", "raw/good/good.csv")
    s3c.upload_file(os.path.join(SAMPLE_DIR, "Annam_255_new.csv"),
                    "annam-test", "raw/nbrA/nbrA.csv")
    s3c.upload_file(os.path.join(SAMPLE_DIR, "Annam_255_new.csv"),
                    "annam-test", "raw/nbrB/nbrB.csv")
    _put_sensor("good", [("nbrA", 16.0), ("nbrB", 17.0)])
    _put_sensor("bad", [])

    monkeypatch.setenv("N_SPLITS", "3")
    monkeypatch.setenv("VAL_N_GAPS", "20")

    import config, run_batch
    importlib.reload(config)
    importlib.reload(run_batch)

    # Should NOT raise: one success isolates the one failure.
    summary = run_batch.run_batch(["good", "bad"], cfg=config.load())
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["failed_device_ids"] == ["bad"]
