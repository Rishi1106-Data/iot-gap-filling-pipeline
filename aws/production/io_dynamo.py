"""
io_dynamo.py — All DynamoDB reads, neighbor lookup, and output writing.

Handles the real-world quirks we found in the data:
  * timestamps stored out of order  -> always sort after fetch
  * exact-duplicate timestamps      -> dedup
  * many null columns per device    -> tolerated (only needed cols used)
  * a DeviceId interleaving 2 IMEIs -> optional IMEI filter / auto-pick
  * sensors split across tables      -> topic-driven table resolution
"""

import io
import logging
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
import pandas as pd
from botocore.config import Config
from boto3.dynamodb.conditions import Key

import config

log = logging.getLogger("annam.io")

# ──────────────────────────────────────────────────────────────────────────────
# In-memory neighbour cache (Improvement 1)
#   Many targets share the same neighbour, so its time series is otherwise
#   re-read from DynamoDB once per referencing target. The cache holds each
#   neighbour's loaded frame for the lifetime of one batch run, keyed by
#   (device_id, table_name). A lock makes it safe under the Phase-2.4 thread
#   pool: concurrent workers requesting the same uncached neighbour block on a
#   per-key lock so the DynamoDB read happens exactly once ("no duplicate reads").
# ──────────────────────────────────────────────────────────────────────────────
_neighbor_cache: dict = {}
_neighbor_cache_lock = threading.Lock()
_neighbor_key_locks: dict = {}
_neighbor_cache_stats = {"hits": 0, "misses": 0}


def _key_lock(cache_key):
    """Return a lock unique to this cache key (created once, thread-safely)."""
    with _neighbor_cache_lock:
        lk = _neighbor_key_locks.get(cache_key)
        if lk is None:
            lk = threading.Lock()
            _neighbor_key_locks[cache_key] = lk
        return lk


def reset_neighbor_cache():
    """Clear the neighbour cache and its stats (call at the start of a run)."""
    with _neighbor_cache_lock:
        _neighbor_cache.clear()
        _neighbor_key_locks.clear()
        _neighbor_cache_stats["hits"] = 0
        _neighbor_cache_stats["misses"] = 0


def neighbor_cache_stats() -> dict:
    with _neighbor_cache_lock:
        return dict(_neighbor_cache_stats)

# Adaptive retry mode automatically backs off on throttling (DynamoDB
# ProvisionedThroughputExceeded / S3 SlowDown) and transient network errors,
# so a single blip doesn't fail the whole task. max_attempts covers the
# retries done *inside* a single call; Step Functions still retries the task.
_boto_cfg = Config(
    region_name=config.AWS_REGION,
    retries={"max_attempts": 8, "mode": "adaptive"},
    connect_timeout=10,
    read_timeout=60,
)

_session = boto3.session.Session(region_name=config.AWS_REGION)
_dynamo = _session.resource("dynamodb", config=_boto_cfg)
_s3 = _session.client("s3", config=_boto_cfg)


# ──────────────────────────────────────────────────────────────────────────────
# Table resolution
# ──────────────────────────────────────────────────────────────────────────────
def resolve_table_from_topic(topic: str) -> str:
    """Map an MQTT topic (e.g. 'WS/SSMet_0126/205') to its DynamoDB data table.
    Longest matching prefix wins; falls back to DEFAULT_DATA_TABLE."""
    if not topic:
        return config.DEFAULT_DATA_TABLE
    best, best_len = config.DEFAULT_DATA_TABLE, -1
    for prefix, table in config.TOPIC_TO_TABLE.items():
        if topic.startswith(prefix) and len(prefix) > best_len:
            best, best_len = table, len(prefix)
    return best


def _to_float(v):
    """DynamoDB numbers arrive as Decimal; nulls/blanks/'Null' -> NaN."""
    if v is None:
        return float("nan")
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "" or s.lower() == "null":
            return float("nan")
        try:
            return float(s)
        except ValueError:
            return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


# ──────────────────────────────────────────────────────────────────────────────
# Neighbor lookup
# ──────────────────────────────────────────────────────────────────────────────
def get_neighbors(device_topic_key: str):
    """Read one row from WS_Spatial_Neighbors.

    device_topic_key is the partition key value, e.g. '205#WS/SSMet_0126/205'.
    Returns a list of dicts: [{id, raw_id, topic, dist_km}, ...] (up to MAX_NEIGHBORS).
    """
    table = _dynamo.Table(config.NEIGHBORS_TABLE)
    try:
        resp = table.get_item(Key={config.NEIGHBORS_KEY_NAME: device_topic_key})
    except Exception as exc:
        # After adaptive retries are exhausted, treat as "no neighbors" rather
        # than crashing — the sensor can still be partially reconstructed.
        log.error("Neighbor lookup failed for %s: %s", device_topic_key, exc)
        return []
    item = resp.get("Item")
    if not item:
        log.warning("No neighbor row for key %s", device_topic_key)
        return []

    neighbors = []
    for i in range(1, config.MAX_NEIGHBORS + 1):
        nid = item.get(f"neighbor_{i}_id")
        if not nid:
            continue
        neighbors.append({
            "id": str(item.get(f"neighbor_{i}_raw_id", nid)),
            "key": str(nid),  # full 'id#topic' form
            "topic": item.get(f"neighbor_{i}_topic", ""),
            "dist_km": _to_float(item.get(f"neighbor_{i}_dist")),
        })
    return neighbors


# ──────────────────────────────────────────────────────────────────────────────
# Effective read window (Improvement 3 — configurable historical window)
# ──────────────────────────────────────────────────────────────────────────────
def effective_start_ts() -> str | None:
    """Resolve the sort-key lower bound for reads.

    Precedence: an explicit config.START_DATE always wins (unchanged Phase-1
    behaviour). Otherwise, if LOOKBACK_DAYS > 0, read only the latest window:
    now - LOOKBACK_DAYS. If neither is set, return None (full history).

    The returned string is formatted like the TimeStamp sort key
    ("YYYY-MM-DD HH:MM:SS") so it composes with Key(DATA_SK).gte(...).
    """
    if config.START_DATE:
        return config.START_DATE
    days = getattr(config, "LOOKBACK_DAYS", 0) or 0
    if days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return cutoff.strftime("%Y-%m-%d %H:%M:%S")
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Neighbour loading with cache (Improvement 1)
# ──────────────────────────────────────────────────────────────────────────────
def load_series_cached(device_id: str, table_name: str,
                       start_ts: str | None = None) -> pd.DataFrame:
    """load_series, but memoised per (device_id, table_name) for the batch run.

    Used for neighbours, which are shared across many targets. Returns the SAME
    frame object load_series would return; reconstruction treats it read-only
    (it copies before mutating), so sharing one frame across targets does not
    change any output. Falls back to a plain read when the cache is disabled.
    """
    if not getattr(config, "NEIGHBOR_CACHE_ENABLED", True):
        return load_series(device_id, table_name, start_ts)

    cache_key = (str(device_id), str(table_name), start_ts)

    # Fast path: already cached.
    with _neighbor_cache_lock:
        if cache_key in _neighbor_cache:
            _neighbor_cache_stats["hits"] += 1
            return _neighbor_cache[cache_key]

    # Slow path: hold a per-key lock so exactly one worker reads this neighbour
    # while others wait, then all reuse the cached frame (no duplicate reads).
    with _key_lock(cache_key):
        with _neighbor_cache_lock:
            if cache_key in _neighbor_cache:
                _neighbor_cache_stats["hits"] += 1
                return _neighbor_cache[cache_key]
        df = load_series(device_id, table_name, start_ts)
        with _neighbor_cache_lock:
            _neighbor_cache[cache_key] = df
            _neighbor_cache_stats["misses"] += 1
        return df


# ──────────────────────────────────────────────────────────────────────────────
# Reading a sensor's time series
# ──────────────────────────────────────────────────────────────────────────────
def _query_all(table_name: str, device_id: str, start_ts: str | None,
               max_pages: int = 10000):
    """Page through a full Query for one DeviceId. Returns list of item dicts.

    Raises on hard failure so the caller (and Step Functions) can retry the
    whole sensor — a partial read would silently corrupt the reconstruction,
    so failing loudly is safer than returning incomplete data.
    """
    table = _dynamo.Table(table_name)
    cond = Key(config.DATA_PK).eq(device_id)
    if start_ts:
        cond = cond & Key(config.DATA_SK).gte(start_ts)

    items, kwargs, pages = [], {"KeyConditionExpression": cond}, 0
    while True:
        try:
            resp = table.query(**kwargs)
        except Exception as exc:
            log.error("Query failed on %s for device %s (page %d): %s",
                      table_name, device_id, pages, exc)
            raise
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        pages += 1
        if not lek:
            break
        if pages >= max_pages:
            # Guard against an unbounded loop on pathological data; very unlikely
            # but better than spinning forever and racking up read cost.
            log.warning("Hit max_pages (%d) for device %s in %s; returning partial.",
                        max_pages, device_id, table_name)
            break
        kwargs["ExclusiveStartKey"] = lek
    return items


def load_series(device_id: str, table_name: str,
                start_ts: str | None = None) -> pd.DataFrame:
    """Load one sensor's history into a clean DataFrame.

    Returns a frame indexed by parsed TimeStamp with numeric weather columns,
    sorted, de-duplicated, and (optionally) filtered to a single IMEI.
    Tolerates missing columns — only what's present is returned.
    """
    raw = _query_all(table_name, device_id, start_ts)
    if not raw:
        log.warning("No rows for device %s in %s", device_id, table_name)
        return pd.DataFrame()

    df = pd.DataFrame(raw)

    # Optional IMEI handling (the two-units-per-DeviceId problem)
    if config.IMEI_ATTR in df.columns:
        if config.IMEI_FILTER:
            df = df[df[config.IMEI_ATTR].astype(str) == str(config.IMEI_FILTER)]
        elif config.IMEI_PICK == "most_temperature" and "CurrentTemperature" in df.columns:
            tmp = df.copy()
            tmp["_t"] = pd.to_numeric(tmp["CurrentTemperature"], errors="coerce")
            counts = tmp.dropna(subset=["_t"]).groupby(config.IMEI_ATTR).size()
            if len(counts):
                keep = counts.idxmax()
                df = df[df[config.IMEI_ATTR].astype(str) == str(keep)]

    if df.empty:
        return pd.DataFrame()

    # Parse + clean timestamps (handles ISO and day-first)
    df["TimeStamp"] = parse_timestamps(df[config.DATA_SK])
    df = df[df["TimeStamp"].notna()]
    df = df[df["TimeStamp"].dt.year <= 2030]

    # Coerce all weather columns to float
    wanted = set(config.CONT_VARS + config.RAIN_VARS)
    for col in wanted:
        if col in df.columns:
            df[col] = df[col].map(_to_float)

    # Keep only useful columns
    keep_cols = ["TimeStamp"] + [c for c in wanted if c in df.columns]
    df = df[keep_cols]

    return df.sort_values("TimeStamp").reset_index(drop=True)


def parse_timestamps(series: pd.Series) -> pd.Series:
    """Auto-detect ISO vs day-first; pick whichever yields fewer NaT."""
    iso = pd.to_datetime(series, errors="coerce")
    day = pd.to_datetime(series, dayfirst=True, errors="coerce")
    return iso if iso.isna().sum() <= day.isna().sum() else day


# ──────────────────────────────────────────────────────────────────────────────
# Output
#
# Primary (and, by default, ONLY) output destination is DynamoDB: the
# reconstructed / gap-filled rows are inserted back into the SAME source data
# table the sensor was read from. The caller resolves that table with
# resolve_table_from_topic() and passes it in as `target_table`, so the write
# always lands in the correct source table (existing table-resolution logic is
# reused unchanged). No reconstructed dataset is written to S3 during normal
# execution.
#
# An optional S3 dump of the full filled dataset + report CSVs remains available
# behind config.S3_REPORT_ENABLED (default False) for offline debugging only; it
# is skipped entirely in normal runs.
# ──────────────────────────────────────────────────────────────────────────────
def write_output(device_id: str, final_df: pd.DataFrame, reports: dict,
                 target_table: str):
    """Insert reconstructed (filled) rows back into the source DynamoDB table.

    `target_table` is the same table the sensor's data was read from (resolved
    by the caller via resolve_table_from_topic). DynamoDB is the primary output;
    the optional S3 report is written only when config.S3_REPORT_ENABLED is set.
    """
    # Primary output: reconstructed rows -> source DynamoDB table.
    write_filled_to_dynamo(device_id, final_df, target_table)

    # Optional, debug-only S3 dump of the full dataset + reports. Off by default
    # so normal execution never writes a reconstructed dataset to S3.
    if getattr(config, "S3_REPORT_ENABLED", False):
        write_s3_debug_report(device_id, final_df, reports)


def _representative_source_item(target_table: str, device_id: str) -> dict:
    """Fetch one recent original row for the device (all attributes) so its
    static identity attributes can be carried onto reconstructed rows.

    Best-effort: returns {} on empty/failure, in which case reconstructed rows
    simply omit the preserved attributes (no worse than before, never fatal).
    Only used when config.PRESERVE_SOURCE_ATTRS is non-empty.
    """
    if not (getattr(config, "PRESERVE_SOURCE_ATTRS", None) or []):
        return {}
    try:
        table = _dynamo.Table(target_table)
        resp = table.query(
            KeyConditionExpression=Key(config.DATA_PK).eq(str(device_id)),
            ScanIndexForward=False,
            Limit=1,
        )
    except Exception as exc:
        log.warning("Representative-row read failed for %s in %s: %s "
                    "(reconstructed rows will omit preserved attrs)",
                    device_id, target_table, exc)
        return {}
    items = resp.get("Items", [])
    return items[0] if items else {}


def write_filled_to_dynamo(device_id: str, final_df: pd.DataFrame,
                           target_table: str):
    """Write ONLY reconstructed (filled_flag == 1) rows back to `target_table`.

    Uses batch_writer() for efficient batched inserts (it auto-batches into
    groups of 25 and retries unprocessed items). Original rows (filled_flag == 0)
    are never written, so genuine source readings are never overwritten.

    Each written record carries:
      * the primary key (DeviceId + TimeStamp);
      * every reconstructed weather value present on the row — CONT_VARS *and*
        RAIN_VARS (rainfall is reconstructed too, so it must be persisted now
        that DynamoDB is the sole output);
      * the reconstruction metadata (imputation_method, confidence_level,
        filled_flag) that marks the row as a fill; and
      * the static device-identity attributes in config.PRESERVE_SOURCE_ATTRS
        (e.g. Topic, Latitude, Longitude, IMEINumber), copied from a
        representative source row so downstream consumers and the pipeline's own
        IMEI re-read keep working. Time-varying telemetry is intentionally not
        fabricated for a synthetic gap timestamp.
    """
    if final_df is None or final_df.empty or "filled_flag" not in final_df.columns:
        log.info("No reconstructed rows to write for device %s", device_id)
        return

    filled = final_df[final_df["filled_flag"] == 1]
    if filled.empty:
        log.info("No filled rows for device %s; nothing to insert into %s",
                 device_id, target_table)
        return

    # Static identity attributes to carry onto reconstructed rows (real values
    # from a representative source row; never the PK/SK, which we set below).
    preserve_attrs = getattr(config, "PRESERVE_SOURCE_ATTRS", None) or []
    rep = _representative_source_item(target_table, device_id) if preserve_attrs else {}
    preserved = {k: rep[k] for k in preserve_attrs
                 if k in rep and k not in (config.DATA_PK, config.DATA_SK)}
    if preserve_attrs and not preserved:
        log.warning("No preservable identity attrs found for device %s in %s "
                    "(rows will still carry keys + reconstructed values)",
                    device_id, target_table)

    value_cols = list(config.CONT_VARS) + list(config.RAIN_VARS)
    table = _dynamo.Table(target_table)
    written = 0
    try:
        with table.batch_writer() as bw:
            for ts, row in filled.iterrows():
                # Seed with static identity attrs, then set the authoritative
                # key / reconstructed values / metadata (these always win).
                item = dict(preserved)
                item[config.DATA_PK] = str(device_id)
                item[config.DATA_SK] = ts.strftime("%Y-%m-%d %H:%M:%S")
                for col in value_cols:
                    if col in row and pd.notna(row[col]):
                        item[col] = Decimal(str(round(float(row[col]), 4)))
                # Preserve reconstruction metadata on the written record.
                item["imputation_method"] = str(row.get("imputation_method", ""))
                item["confidence_level"] = str(row.get("confidence_level", ""))
                item["filled_flag"] = Decimal(str(int(row.get("filled_flag", 1))))
                bw.put_item(Item=item)
                written += 1
    except Exception as exc:
        # Losing reconstructed output silently is the worst outcome — raise so
        # the task fails and Step Functions / the batch runner records it.
        log.error("DynamoDB write-back failed for device %s to %s: %s",
                  device_id, target_table, exc)
        raise

    log.info("Wrote %d reconstructed rows for device %s back to %s",
             written, device_id, target_table)


# ──────────────────────────────────────────────────────────────────────────────
# Optional S3 debug output (disabled by default)
#   Runs only when config.S3_REPORT_ENABLED is true — e.g. for offline
#   debugging. Never runs during normal execution, so no reconstructed dataset
#   lands in S3 in production.
# ──────────────────────────────────────────────────────────────────────────────
def write_s3_debug_report(device_id: str, final_df: pd.DataFrame, reports: dict):
    """Optionally dump the full filled dataset + report CSVs to S3 for debugging."""
    base = f"{config.OUTPUT_PREFIX}/device={device_id}"

    # Main dataset
    if config.OUTPUT_FORMAT == "parquet":
        buf = io.BytesIO()
        final_df.to_parquet(buf, index=True)
        _put(f"{base}/filled_dataset.parquet", buf.getvalue())
    else:
        _put(f"{base}/filled_dataset.csv", final_df.to_csv().encode())

    # Reports
    for name, rdf in reports.items():
        if rdf is not None and not rdf.empty:
            _put(f"{base}/{name}.csv", rdf.to_csv(index=False).encode())

    log.info("[debug] Wrote S3 report for device %s to s3://%s/%s/",
             device_id, config.OUTPUT_BUCKET, base)


def _put(key: str, body: bytes):
    try:
        _s3.put_object(Bucket=config.OUTPUT_BUCKET, Key=key, Body=body)
    except Exception as exc:
        # Losing output silently is the worst outcome — raise so the task fails
        # and Step Functions retries the whole sensor.
        log.error("S3 put failed for s3://%s/%s: %s", config.OUTPUT_BUCKET, key, exc)
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Incremental-processing metadata (Improvement 2)
#   WS_Reconstruction_Metadata is keyed by DeviceId and stores a per-sensor
#   watermark so a run can skip sensors whose data has not advanced.
#   Attributes: DeviceId, LastProcessed (run time, ISO-8601 UTC),
#               LastDataTimestamp (newest source TimeStamp seen), GapCount.
#   Reads/writes here are best-effort: any failure logs and degrades to
#   "process the sensor" so a metadata problem never blocks reconstruction.
# ──────────────────────────────────────────────────────────────────────────────
def probe_latest_source_ts(table_name: str, device_id: str) -> str | None:
    """Cheaply fetch just the newest TimeStamp for a device (1 item read).

    Queries the data table with ScanIndexForward=False and Limit=1 so the
    incremental check can decide "has new data?" without reading full history.
    Returns the timestamp string, or None on empty/failure (caller treats None
    as "process the sensor").
    """
    try:
        table = _dynamo.Table(table_name)
        resp = table.query(
            KeyConditionExpression=Key(config.DATA_PK).eq(str(device_id)),
            ScanIndexForward=False,
            Limit=1,
            ProjectionExpression="#ts",
            ExpressionAttributeNames={"#ts": config.DATA_SK},
        )
    except Exception as exc:
        log.warning("Latest-ts probe failed for %s in %s: %s (will process)",
                    device_id, table_name, exc)
        return None
    items = resp.get("Items", [])
    if not items:
        return None
    raw = items[0].get(config.DATA_SK)
    if raw is None:
        return None
    parsed = parse_timestamps(pd.Series([raw]))
    if parsed.isna().all():
        return None
    return parsed.iloc[0].strftime("%Y-%m-%d %H:%M:%S")


def get_reconstruction_metadata(device_id: str) -> dict | None:
    """Return the stored watermark row for a device, or None if absent/failed."""
    try:
        table = _dynamo.Table(config.METADATA_TABLE)
        resp = table.get_item(Key={config.METADATA_KEY_NAME: str(device_id)})
    except Exception as exc:
        log.warning("Metadata read failed for %s: %s (will process sensor)",
                    device_id, exc)
        return None
    return resp.get("Item")


def latest_data_timestamp(target_df: pd.DataFrame) -> str | None:
    """Newest source TimeStamp in the loaded target frame, as an ISO string."""
    if target_df is None or target_df.empty or "TimeStamp" not in target_df.columns:
        return None
    ts = pd.to_datetime(target_df["TimeStamp"], errors="coerce").dropna()
    if ts.empty:
        return None
    return ts.max().strftime("%Y-%m-%d %H:%M:%S")


def has_new_data(metadata: dict | None, latest_ts: str | None) -> bool:
    """Decide whether a sensor has new data since the last successful run.

    New data iff there is no prior watermark, or the newest source timestamp is
    strictly greater than the stored LastDataTimestamp. On any ambiguity we
    return True (process the sensor) so we never skip real work.
    """
    if metadata is None or latest_ts is None:
        return True
    prev = metadata.get("LastDataTimestamp")
    if not prev:
        return True
    try:
        return pd.to_datetime(latest_ts) > pd.to_datetime(str(prev))
    except Exception:
        return True


def put_reconstruction_metadata(device_id: str, last_data_ts: str | None,
                                gap_count: int) -> None:
    """Upsert the per-sensor watermark after a successful reconstruction."""
    try:
        table = _dynamo.Table(config.METADATA_TABLE)
        item = {
            config.METADATA_KEY_NAME: str(device_id),
            "LastProcessed": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "GapCount": int(gap_count),
        }
        if last_data_ts:
            item["LastDataTimestamp"] = str(last_data_ts)
        table.put_item(Item=item)
    except Exception as exc:
        # A metadata write failure must not fail the sensor — the reconstruction
        # already succeeded and its output is written. Worst case: the sensor is
        # reprocessed next run (correct, just not saved effort).
        log.warning("Metadata write failed for %s: %s", device_id, exc)
