"""tests/test_aws_production.py — tests for aws/production (DynamoDB batch pipeline).

aws/production uses flat top-level imports (``import config``, ``import io_dynamo``)
that collide with the module names in the legacy ``aws/`` layer, so each test imports
the production modules in isolation and restores ``sys.modules`` afterwards.
DynamoDB is mocked with moto; nothing here touches a real AWS account.
"""
import ast
import logging
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal

import boto3
import pandas as pd
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD = os.path.join(REPO, "aws", "production")
MODULES = ("config", "io_dynamo", "list_sensors", "reconstruct", "run_batch", "run_sensor")

TOPIC = "WS/SSMet_0126/{}"
TABLE = "WS_SSMet_0126_Data"


@pytest.fixture
def prod(monkeypatch):
    """Import aws/production modules fresh, under a mocked AWS, then restore state."""
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_REGION": "us-east-1",
                 "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "LOOKBACK_DAYS": "0", "INCREMENTAL_ENABLED": "true",
                 "MAX_WORKERS": "2", "S3_REPORT_ENABLED": "false"}.items():
        monkeypatch.setenv(k, v)
    saved_mods = {m: sys.modules.pop(m) for m in MODULES if m in sys.modules}
    saved_path = list(sys.path)
    sys.path.insert(0, PROD)
    try:
        with mock_aws():
            ns = {m: __import__(m) for m in ("config", "io_dynamo", "reconstruct",
                                              "list_sensors", "run_batch")}
            yield type("Prod", (), ns)
    finally:
        sys.path[:] = saved_path
        for m in MODULES:
            sys.modules.pop(m, None)
        sys.modules.update(saved_mods)


# ── static / entrypoint checks ────────────────────────────────────────────────
@pytest.mark.parametrize("fname", ["run_batch.py", "run_sensor.py"])
def test_entrypoints_have_main_guard(fname):
    """Regression: these files were once truncated, so `python run_batch.py` did nothing."""
    tree = ast.parse(open(os.path.join(PROD, fname), encoding="utf-8").read())
    guards = [n for n in tree.body if isinstance(n, ast.If)
              and "__name__" in ast.dump(n.test)]
    assert guards, f"{fname} has no `if __name__ == '__main__'` guard"


def test_run_batch_cli_prints_usage():
    env = dict(os.environ, AWS_DEFAULT_REGION="us-east-1")
    r = subprocess.run([sys.executable, "run_batch.py", "--help"], cwd=PROD,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "usage: run_batch.py" in r.stdout


def test_production_files_contain_no_account_ids():
    import re
    for fname in os.listdir(PROD):
        path = os.path.join(PROD, fname)
        if os.path.isfile(path):
            text = open(path, encoding="utf-8", errors="ignore").read()
            assert not re.search(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:", text), fname


# ── pure-logic checks ─────────────────────────────────────────────────────────
def test_resolve_table_longest_prefix_and_fallback(prod):
    assert prod.io_dynamo.resolve_table_from_topic("WS/SSMet_0126/205") == "WS_SSMet_0126_Data"
    assert prod.io_dynamo.resolve_table_from_topic("WS/Campus/3") == "WS_Campus_Data"
    assert prod.io_dynamo.resolve_table_from_topic("unknown/x") == prod.config.DEFAULT_DATA_TABLE
    assert prod.io_dynamo.resolve_table_from_topic("") == prod.config.DEFAULT_DATA_TABLE


def test_has_new_data_logic(prod):
    f = prod.io_dynamo.has_new_data
    assert f(None, "2026-01-01 00:00:00")
    assert f({"LastDataTimestamp": "2026-01-01 00:00:00"}, None)
    assert f({"LastDataTimestamp": "2026-01-01 00:00:00"}, "2026-01-01 00:05:00")
    assert not f({"LastDataTimestamp": "2026-01-01 00:05:00"}, "2026-01-01 00:05:00")


def _frame(prod, drop):
    import pandas as pd
    idx = pd.date_range("2026-09-01", periods=300, freq="5min")
    df = pd.DataFrame({"TimeStamp": idx, "CurrentTemperature": 25.0})
    return df.drop(index=drop).reset_index(drop=True)


def test_analyze_target_gaps(prod):
    assert prod.reconstruct.analyze_target_gaps(_frame(prod, []))["has_work"] is False
    assert prod.reconstruct.analyze_target_gaps(_frame(prod, [50, 51, 52]))["has_work"] is True


# ── helpers for end-to-end tests against mocked DynamoDB ──────────────────────
N = 864                                   # 3 days of 5-minute slots
START = datetime(2026, 9, 1)
KEY = f"216#{TOPIC.format(216)}"
D = lambda x: Decimal(str(round(x, 3)))
TS = lambda i: (START + timedelta(minutes=5 * i)).strftime("%Y-%m-%d %H:%M:%S")

DEFAULT_GAPS = {100} | {200, 201, 202} | set(range(400, 412))   # 1 + 3 + 12 slots


def _weather(dev, i, phase):
    s = math.sin(2 * math.pi * i / 288 + phase)
    return {"CurrentTemperature": D(28 + 6 * s), "CorrectedTemp": D(28.5 + 6 * s),
            "CurrentHumidity": D(60 - 20 * s), "CorrectedHumidity": D(61 - 20 * s),
            "AtmPressure": D(1000 + 3 * s), "WindSpeed": D(5 + 2 * s),
            "RainfallHourly": D(0), "Topic": TOPIC.format(dev)}


def _seed(prod, tgt_missing=DEFAULT_GAPS, nbr_missing=()):
    """Create the three tables and seed sensor 216 (+ neighbours 217, 218)."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(TableName=TABLE, BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "DeviceId", "KeyType": "HASH"},
                                {"AttributeName": "TimeStamp", "KeyType": "RANGE"}],
                     AttributeDefinitions=[{"AttributeName": "DeviceId", "AttributeType": "S"},
                                           {"AttributeName": "TimeStamp", "AttributeType": "S"}])
    ddb.create_table(TableName="WS_Spatial_Neighbors", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "deviceId_topic", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "deviceId_topic", "AttributeType": "S"}])
    ddb.create_table(TableName="WS_Reconstruction_Metadata", BillingMode="PAY_PER_REQUEST",
                     KeySchema=[{"AttributeName": "DeviceId", "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": "DeviceId", "AttributeType": "S"}])
    data = ddb.Table(TABLE)
    with data.batch_writer() as bw:
        for dev, phase in (("216", 0.0), ("217", 0.1), ("218", 0.2)):
            for i in range(N):
                if dev == "216" and i in tgt_missing:
                    continue
                if dev != "216" and i in nbr_missing:
                    continue
                bw.put_item(Item={"DeviceId": dev, "TimeStamp": TS(i), **_weather(dev, i, phase)})
    ddb.Table("WS_Spatial_Neighbors").put_item(Item={
        "deviceId_topic": KEY,
        "neighbor_1_id": f"217#{TOPIC.format(217)}", "neighbor_1_raw_id": "217",
        "neighbor_1_topic": TOPIC.format(217), "neighbor_1_dist": Decimal("5.0"),
        "neighbor_2_id": f"218#{TOPIC.format(218)}", "neighbor_2_raw_id": "218",
        "neighbor_2_topic": TOPIC.format(218), "neighbor_2_dist": Decimal("8.0")})
    return data


def _items(data, dev="216"):
    out, kw = [], {"KeyConditionExpression": Key("DeviceId").eq(dev)}
    while True:
        r = data.query(**kw)
        out += r["Items"]
        if "LastEvaluatedKey" not in r:
            return out
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]


def _metadata(prod):
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "WS_Reconstruction_Metadata").get_item(Key={"DeviceId": "216"}).get("Item")


def _spy(prod, monkeypatch):
    """Record what run_reconstruction receives and returns."""
    calls, orig = [], prod.reconstruct.run_reconstruction

    def spy(target_df, neighbor_frames, **kw):
        res = orig(target_df, neighbor_frames, **kw)
        calls.append({"target": target_df.copy(), "result": res,
                      "neighbors": {n["id"]: n["df"].copy() for n in neighbor_frames}})
        return res

    monkeypatch.setattr(prod.reconstruct, "run_reconstruction", spy)
    return calls


# ── A/B/C: successful reconstructions are written ─────────────────────────────
def test_batch_reconstructs_and_writes_back(prod):
    data = _seed(prod)
    assert prod.list_sensors.list_sensor_keys() == [KEY]
    prod.io_dynamo.reset_neighbor_cache()
    res = prod.run_batch.reconstruct_one(KEY)
    n_missing = len(DEFAULT_GAPS)
    assert res["status"] == "ok" and res["filled"] == n_missing and res["unresolved"] == 0

    items = _items(data)
    assert len(items) == N                              # every slot present again
    filled = [i for i in items if i.get("filled_flag") == 1]
    originals = [i for i in items if "filled_flag" not in i]
    assert len(filled) == n_missing and len(originals) == N - n_missing
    methods = [i["imputation_method"] for i in filled]
    assert methods.count("interpolation") == 1          # A: isolated gap
    assert methods.count("model") == 3                  # B: medium gap -> ML
    assert methods.count("neighbor") == 12              # C: long gap -> neighbours
    assert all(5 <= float(i["CurrentTemperature"]) <= 55 for i in filled)
    assert all(i.get("Topic") == TOPIC.format(216) for i in filled)

    md = _metadata(prod)                                # watermark with separate counts
    assert (md["GapCount"], md["FilledCount"], md["UnresolvedCount"]) == (16, 16, 0)
    assert prod.run_batch.reconstruct_one(KEY)["status"] == "skipped_no_new_data"


# ── D/E/J: unresolved slots are tracked, not written, and get no rainfall ─────
UNRES = set(range(400, 412))
NBR_DARK = set(range(395, 415))           # neighbours also have no data around the gap


def test_unresolved_gap_not_written_and_counted(prod, monkeypatch):
    gaps = UNRES | set(range(600, 612))   # 12 unresolved + 12 reconstructed from neighbours
    data = _seed(prod, tgt_missing=gaps, nbr_missing=NBR_DARK)
    calls = _spy(prod, monkeypatch)
    res = prod.run_batch.reconstruct_one(KEY)

    assert res["status"] == "ok" and res["filled"] == 12 and res["unresolved"] == 12
    keys = {i["TimeStamp"] for i in _items(data)}
    assert not any(TS(i) in keys for i in UNRES)                       # D: nothing stored
    assert all(TS(i) in keys for i in range(600, 612))                 # reconstructed ones are
    assert len(keys) == N - len(UNRES)
    assert not any(i.get("imputation_method") == "unresolved" for i in _items(data))

    md = _metadata(prod)                                               # E: counted separately
    assert (md["GapCount"], md["FilledCount"], md["UnresolvedCount"]) == (24, 12, 12)

    final = calls[0]["result"]["final_df"]                             # J: no fabricated rain
    unres_rows = final[final["imputation_method"] == "unresolved"]
    assert len(unres_rows) == 12
    assert unres_rows["RainfallHourly"].isna().all()
    assert unres_rows["CurrentTemperature"].isna().all()


def test_sensor_with_only_unresolved_gaps_is_not_counted_reconstructed(prod, monkeypatch, caplog):
    data = _seed(prod, tgt_missing=UNRES, nbr_missing=NBR_DARK)
    monkeypatch.setattr(sys, "argv", ["run_batch.py", "--keys", KEY])
    caplog.set_level(logging.INFO)
    prod.run_batch.main()

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "RECONSTRUCTED device=" in text and "filled=0 unresolved=12" in text
    assert "UNRESOLVED device=" in text                                # warning for CloudWatch
    batch = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Batch done")]
    assert batch and " 0 reconstructed, 1 ok," in batch[0]             # F
    assert "filled slots=0 unresolved slots=12" in batch[0]
    assert not [i for i in _items(data) if i.get("filled_flag") == 1]
    md = _metadata(prod)
    assert (md["FilledCount"], md["UnresolvedCount"]) == (0, 12)


# ── G/H/I: previously reconstructed rows never feed back in ──────────────────
def test_second_run_excludes_previous_reconstruction(prod, monkeypatch):
    data = _seed(prod)
    assert prod.run_batch.reconstruct_one(KEY)["filled"] == 16
    written = {i["TimeStamp"] for i in _items(data) if i.get("filled_flag") == 1}
    original_ts = {i["TimeStamp"] for i in _items(data) if "filled_flag" not in i}
    assert len(written) == 16

    monkeypatch.setattr(prod.config, "INCREMENTAL_ENABLED", False)   # force a full re-run
    prod.io_dynamo.reset_neighbor_cache()
    calls = _spy(prod, monkeypatch)
    res = prod.run_batch.reconstruct_one(KEY)

    target_ts = set(calls[0]["target"]["TimeStamp"].dt.strftime("%Y-%m-%d %H:%M:%S"))
    assert not (target_ts & written)                                 # G: reconstructed rows excluded
    assert target_ts == original_ts                                  # H: every original row kept
    final = calls[0]["result"]["final_df"]
    assert dict(final["imputation_method"].value_counts()) == {      # gaps found again, not "original"
        "original": N - 16, "neighbor": 12, "model": 3, "interpolation": 1}
    assert res["filled"] == 16 and res["skipped_existing"] == 0      # own rows may be refreshed
    assert len(_items(data)) == N                                    # idempotent: same keys


def test_load_series_keeps_originals_and_drops_reconstructed(prod):
    data = _seed(prod)
    data.put_item(Item={"DeviceId": "217", "TimeStamp": "2026-09-01 00:02:30",
                        "CurrentTemperature": D(54.0), "filled_flag": 1,
                        "imputation_method": "neighbor", "confidence_level": "Medium"})
    data.put_item(Item={"DeviceId": "217", "TimeStamp": "2026-09-01 00:07:30",   # old placeholder
                        "RainfallHourly": D(0), "filled_flag": 1,
                        "imputation_method": "unresolved", "confidence_level": "unknown"})
    df = prod.io_dynamo.load_series("217", TABLE, None)
    assert len(df) == N                                              # H: only the originals
    assert 54.0 not in set(df["CurrentTemperature"].round(1))


def test_neighbour_frames_exclude_reconstructed_rows(prod, monkeypatch):
    data = _seed(prod)
    for k in range(5):                                               # I: neighbour already holds fills
        data.put_item(Item={"DeviceId": "217", "TimeStamp": f"2026-09-01 00:0{k}:30",
                            "CurrentTemperature": D(54.0), "filled_flag": 1,
                            "imputation_method": "model", "confidence_level": "Medium"})
    prod.io_dynamo.reset_neighbor_cache()
    calls = _spy(prod, monkeypatch)
    assert prod.run_batch.reconstruct_one(KEY)["status"] == "ok"
    nb = calls[0]["neighbors"]["217"]
    assert len(nb) == N and 54.0 not in set(nb["CurrentTemperature"].round(1))


def test_original_rows_are_untouched_by_a_run(prod):
    data = _seed(prod)
    snap = lambda: {i["TimeStamp"]: dict(i) for i in _items(data) if "filled_flag" not in i}
    before = snap()
    assert prod.run_batch.reconstruct_one(KEY)["status"] == "ok"
    after = snap()
    assert after == before                                  # same keys, same attributes, same values


def test_newest_reconstructed_row_does_not_count_as_new_data(prod):
    data = _seed(prod)
    assert prod.run_batch.reconstruct_one(KEY)["status"] == "ok"      # run 1 stores a watermark
    last_original = prod.io_dynamo.probe_latest_source_ts(TABLE, "216")
    assert last_original == TS(N - 1)
    # A reconstructed row that is newer than every original reading must not look like new data.
    data.put_item(Item={"DeviceId": "216", "TimeStamp": TS(N + 10), "CurrentTemperature": D(30.0),
                        "filled_flag": 1, "imputation_method": "neighbor", "Topic": TOPIC.format(216)})
    assert prod.io_dynamo.probe_latest_source_ts(TABLE, "216") == last_original
    assert prod.run_batch.reconstruct_one(KEY)["status"] == "skipped_no_new_data"


def test_identity_attributes_come_from_an_original_row(prod):
    data = _seed(prod)
    data.put_item(Item={"DeviceId": "216", "TimeStamp": TS(N + 10), "filled_flag": 1,
                        "imputation_method": "unresolved", "Topic": "WRONG/placeholder"})
    rep = prod.io_dynamo._representative_source_item(TABLE, "216")
    assert rep.get("Topic") == TOPIC.format(216) and "filled_flag" not in rep


# ── K: a real reading is never overwritten ────────────────────────────────────
def _final_df(prod, rows):
    idx = pd.DatetimeIndex([r["ts"] for r in rows], name="TimeStamp")
    df = pd.DataFrame(index=idx)
    for c in list(prod.config.CONT_VARS) + list(prod.config.RAIN_VARS):
        df[c] = [r.get(c, float("nan")) for r in rows]
    df["imputation_method"] = [r["method"] for r in rows]
    df["confidence_level"] = "Medium"
    df["filled_flag"] = [0 if r["method"] == "original" else 1 for r in rows]
    return df


def test_writer_filters_rows_and_does_not_overwrite_real_items(prod):
    data = _seed(prod)
    t = lambda m: datetime(2026, 10, 1, 0, m)
    key = lambda m: {"DeviceId": "216", "TimeStamp": t(m).strftime("%Y-%m-%d %H:%M:%S")}
    data.put_item(Item={**key(10), "CurrentTemperature": D(40.0)})                  # real reading
    data.put_item(Item={**key(15), "CurrentTemperature": D(1.0), "filled_flag": 1,  # old reconstruction
                        "imputation_method": "model"})
    ref = prod.config.REF_COL
    df = _final_df(prod, [
        {"ts": t(5), "method": "model", ref: 20.0},                   # free key        -> written
        {"ts": t(10), "method": "interpolation", ref: 21.0},          # real item there -> skipped
        {"ts": t(15), "method": "neighbor", ref: 22.0},               # own old fill    -> refreshed
        {"ts": t(20), "method": "unresolved"},                        # unresolved      -> not written
        {"ts": t(25), "method": "neighbor"},                          # no reference value -> not written
        {"ts": t(30), "method": "original", ref: 23.0},               # original        -> not written
    ])
    stats = prod.io_dynamo.write_filled_to_dynamo("216", df, TABLE)
    assert stats == {"written": 2, "skipped_existing": 1}
    get = lambda m: data.get_item(Key=key(m)).get("Item")
    assert float(get(10)["CurrentTemperature"]) == 40.0 and "filled_flag" not in get(10)   # K
    assert float(get(5)["CurrentTemperature"]) == 20.0 and get(5)["filled_flag"] == 1
    assert float(get(15)["CurrentTemperature"]) == 22.0
    assert get(20) is None and get(25) is None and get(30) is None

    persist, unres = prod.io_dynamo.reconstruction_masks(df)
    assert int(persist.sum()) == 3 and int(unres.sum()) == 2
    assert prod.io_dynamo.summarize_reconstruction(df, stats) == {
        "filled": 2, "unresolved": 2, "skipped_existing": 1, "gap_count": 5}


def test_real_reading_arriving_mid_run_is_not_overwritten(prod, monkeypatch):
    data = _seed(prod)
    orig, slot = prod.reconstruct.run_reconstruction, 401

    def late_arrival(target_df, neighbor_frames, **kw):
        res = orig(target_df, neighbor_frames, **kw)        # read + compute done ...
        data.put_item(Item={"DeviceId": "216", "TimeStamp": TS(slot),   # ... then a real reading lands
                            "CurrentTemperature": D(99.0)})
        return res

    monkeypatch.setattr(prod.reconstruct, "run_reconstruction", late_arrival)
    res = prod.run_batch.reconstruct_one(KEY)
    assert res["filled"] == 15 and res["skipped_existing"] == 1
    item = data.get_item(Key={"DeviceId": "216", "TimeStamp": TS(slot)})["Item"]
    assert float(item["CurrentTemperature"]) == 99.0 and "filled_flag" not in item
    md = _metadata(prod)
    assert (md["GapCount"], md["FilledCount"], md["UnresolvedCount"]) == (16, 15, 0)


# ── J: rainfall zero-fill needs real evidence ────────────────────────────────
def _frame_with_rain(prod, drop, rain_col=True, rain=0.0):
    idx = pd.date_range("2026-09-01", periods=300, freq="5min")
    i = pd.Series(range(300), dtype=float)
    df = pd.DataFrame({"TimeStamp": idx})
    for k, c in enumerate(prod.config.CONT_VARS):
        df[c] = 20 + 5 * (i / 50).map(math.sin) + k
    for c in prod.config.RAIN_VARS:
        df[c] = rain
    if not rain_col:
        df = df.drop(columns=list(prod.config.RAIN_VARS))
    return df.drop(index=list(drop)).reset_index(drop=True)


GAP = list(range(100, 112))      # 12 slots -> long gap


def _recon(prod, target, neighbors):
    return prod.reconstruct.run_reconstruction(target, neighbors, model_key="t")["final_df"]


def test_rain_not_fabricated_without_any_reconstruction(prod):
    final = _recon(prod, _frame_with_rain(prod, GAP), [])            # no neighbours -> unresolved
    rows = final[final["imputation_method"] == "unresolved"]
    assert len(rows) == 12
    assert rows[list(prod.config.RAIN_VARS)].isna().all().all()


def test_rain_zero_when_neighbours_observe_dry_weather(prod):
    nb = [{"id": "n1", "dist_km": 5.0, "df": _frame_with_rain(prod, [])}]
    final = _recon(prod, _frame_with_rain(prod, GAP), nb)
    rows = final[final["imputation_method"] == "neighbor"]
    assert len(rows) == 12 and (rows["RainfallHourly"] == 0).all()   # existing rule preserved


def test_rain_not_zero_when_neighbour_observes_rain(prod):
    wet = _frame_with_rain(prod, [])
    wet.loc[105, "RainfallHourly"] = 2.5
    final = _recon(prod, _frame_with_rain(prod, GAP), [{"id": "n1", "dist_km": 5.0, "df": wet}])
    slot = final.index[105]
    assert pd.isna(final.loc[slot, "RainfallHourly"])


def test_rain_zero_does_not_spread_across_long_gap_without_evidence(prod):
    # Neighbour has no rain column, so only the target's own observations count.
    nb = [{"id": "n1", "dist_km": 5.0, "df": _frame_with_rain(prod, [], rain_col=False)}]
    final = _recon(prod, _frame_with_rain(prod, GAP), nb)
    rain = final["RainfallHourly"]
    assert (rain.iloc[100:103] == 0).all() and (rain.iloc[109:112] == 0).all()   # within 3 slots
    assert rain.iloc[103:109].isna().all()                                       # no evidence
