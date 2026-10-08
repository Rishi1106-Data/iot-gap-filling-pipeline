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
from botocore.exceptions import ClientError
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
# Reconstructed vs original records
#
# Original sensor observations are the source of truth. Rows this pipeline wrote
# earlier carry filled_flag = 1 and an imputation_method. They are excluded when a
# series is loaded (so a later run never trains on, or reconstructs from, its own
# earlier output), and only rows that were reconstructed successfully are written.
# ──────────────────────────────────────────────────────────────────────────────
# imputation_method values that denote a successful reconstruction. "unresolved"
# and "original" are deliberately absent.
RECONSTRUCTED_METHODS = ("interpolation", "model", "neighbor")


def _drop_reconstructed(df: pd.DataFrame) -> pd.DataFrame:
    """Drop rows previously written by this pipeline (reconstructed or placeholder).

    A row counts as non-original when filled_flag == 1 or imputation_method is one
    of the reconstruction methods / "unresolved". Raw device readings carry neither
    attribute and are always kept.
    """
    drop = pd.Series(False, index=df.index)
    if "filled_flag" in df.columns:
        drop |= df["filled_flag"].map(_to_float).eq(1)
    if "imputation_method" in df.columns:
        drop |= df["imputation_method"].isin(RECONSTRUCTED_METHODS + ("unresolved",))
    n = int(drop.sum())
    if n:
        log.debug("Excluded %d previously reconstructed row(s) from the input series", n)
    return df[~drop]


def _is_reconstructed_item(item: dict) -> bool:
    """True for an item this pipeline wrote earlier (reconstructed or placeholder)."""
    return (_to_float(item.get("filled_flag")) == 1
            or item.get("imputation_method") in RECONSTRUCTED_METHODS + ("unresolved",))


def _newest_original_item(table_name: str, device_id: str, projection: str | None = None,
                          names: dict | None = None, page_size: int = 25,
                          max_pages: int = 8) -> dict | None:
    """Newest item for the device that is an ORIGINAL observation.

    Reconstructed rows are skipped, so they can never stand in for the latest real
    reading (watermark probe) or supply identity attributes. Reads at most
    page_size * max_pages items from the newest end; returns None if there is no
    original in that range (callers treat that as "process the sensor").
    """
    table = _dynamo.Table(table_name)
    kwargs = {"KeyConditionExpression": Key(config.DATA_PK).eq(str(device_id)),
              "ScanIndexForward": False, "Limit": page_size}
    if projection:
        kwargs["ProjectionExpression"] = projection + ", #ff, #im"
        kwargs["ExpressionAttributeNames"] = {**(names or {}),
                                              "#ff": "filled_flag", "#im": "imputation_method"}
    for _ in range(max_pages):
        resp = table.query(**kwargs)
        for item in resp.get("Items", []):
            if not _is_reconstructed_item(item):
                return item
        if "LastEvaluatedKey" not in resp:
            return None
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return None


def reconstruction_masks(final_df: pd.DataFrame):
    """Split a reconstruction result into (persist_mask, unresolved_mask).

    persist_mask    : rows reconstructed successfully — imputation_method is one of
                      RECONSTRUCTED_METHODS AND the reference value is not null.
                      Only these rows are written to DynamoDB.
    unresolved_mask : every other non-original row (gap slots that could not be
                      reconstructed). These are counted, never written.
    filled_flag keeps its existing meaning (0 = original, 1 = non-original) and is
    not used here.
    """
    empty = pd.Series(dtype=bool)
    if (final_df is None or final_df.empty
            or "imputation_method" not in final_df.columns):
        return empty, empty
    method = final_df["imputation_method"]
    if config.REF_COL in final_df.columns:
        ref_ok = final_df[config.REF_COL].notna()
    else:
        ref_ok = pd.Series(False, index=final_df.index)
    persist = method.isin(RECONSTRUCTED_METHODS) & ref_ok
    unresolved = (method != "original") & ~persist
    return persist, unresolved


def summarize_reconstruction(final_df: pd.DataFrame, write_stats: dict) -> dict:
    """Counts for one sensor, kept separate so unresolved slots never look filled.

    filled           : reconstructed rows actually written to the source table
    unresolved       : gap slots that could not be reconstructed (not written)
    skipped_existing : reconstructed rows not written because a real item exists
    gap_count        : all gap slots found = filled + skipped_existing + unresolved
    """
    persist, unresolved = reconstruction_masks(final_df)
    n_unres = int(unresolved.sum()) if len(unresolved) else 0
    n_ok = int(persist.sum()) if len(persist) else 0
    return {"filled": int(write_stats.get("written", 0)),
            "unresolved": n_unres,
            "skipped_existing": int(write_stats.get("skipped_existing", 0)),
            "gap_count": n_ok + n_unres}


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

    # Original observations only: never feed previously reconstructed rows (or
    # unresolved placeholders) back in as if they were real readings.
    df = _drop_reconstructed(df)
    if df.empty:
        log.warning("No original rows for device %s in %s", device_id, table_name)
        return pd.DataFrame()

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
    stats = write_filled_to_dynamo(device_id, final_df, target_table)

    # Optional, debug-only S3 dump of the full dataset + reports. Off by default
    # so normal execution never writes a reconstructed dataset to S3.
    if getattr(config, "S3_REPORT_ENABLED", False):
        write_s3_debug_report(device_id, final_df, reports)
    return stats


def _representative_source_item(target_table: str, device_id: str) -> dict:
    """Fetch the newest ORIGINAL row for the device (all attributes) so its
    static identity attributes can be carried onto reconstructed rows.

    Best-effort: returns {} on empty/failure, in which case reconstructed rows
    simply omit the preserved attributes (no worse than before, never fatal).
    Only used when config.PRESERVE_SOURCE_ATTRS is non-empty.
    """
    if not (getattr(config, "PRESERVE_SOURCE_ATTRS", None) or []):
        return {}
    try:
        return _newest_original_item(target_table, device_id) or {}
    except Exception as exc:
        log.warning("Representative-row read failed for %s in %s: %s "
                    "(reconstructed rows will omit preserved attrs)",
                    device_id, target_table, exc)
        return {}


# A reconstructed item may be created if the key is free, or may replace an item
# that is itself a previous reconstruction (filled_flag = 1). It can never replace
# a real device reading, which has no filled_flag attribute.
_WRITE_CONDITION = "attribute_not_exists(#pk) OR #ff = :one"


def write_filled_to_dynamo(device_id: str, final_df: pd.DataFrame,
                           target_table: str) -> dict:
    """Write ONLY successfully reconstructed rows back to `target_table`.

    A row is written iff reconstruction_masks() marks it as persistable: its
    imputation_method is interpolation / model / neighbor AND the reference value
    is not null. Unresolved slots are never written (they are counted by the
    caller and recorded in the metadata table instead), and original rows are
    never written.

    Each item is written with a conditional PutItem so a real reading that
    arrived after this run read the table (same DeviceId + TimeStamp key) is
    never overwritten; such rows are skipped and counted. Batch writes do not
    support conditions, so items are put one at a time. Items carry:
      * the primary key (DeviceId + TimeStamp);
      * every reconstructed weather value present on the row (CONT_VARS and
        RAIN_VARS, non-null only);
      * imputation_method, confidence_level and filled_flag = 1, which also lets
        load_series() exclude them on later runs;
      * the static device-identity attributes in config.PRESERVE_SOURCE_ATTRS,
        copied from a representative source row. Time-varying telemetry is
        intentionally not fabricated.

    Returns {"written": int, "skipped_existing": int}.
    """
    stats = {"written": 0, "skipped_existing": 0}
    persist, _unresolved = reconstruction_masks(final_df)
    if persist.empty or not persist.any():
        log.info("No reconstructed rows to write for device %s", device_id)
        return stats
    filled = final_df[persist]

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
    names = {"#pk": config.DATA_PK, "#ff": "filled_flag"}
    values = {":one": Decimal("1")}
    try:
        for ts, row in filled.iterrows():
            item = dict(preserved)
            item[config.DATA_PK] = str(device_id)
            item[config.DATA_SK] = ts.strftime("%Y-%m-%d %H:%M:%S")
            for col in value_cols:
                if col in row and pd.notna(row[col]):
                    item[col] = Decimal(str(round(float(row[col]), 4)))
            item["imputation_method"] = str(row.get("imputation_method", ""))
            item["confidence_level"] = str(row.get("confidence_level", ""))
            item["filled_flag"] = Decimal("1")
            try:
                table.put_item(Item=item, ConditionExpression=_WRITE_CONDITION,
                               ExpressionAttributeNames=names,
                               ExpressionAttributeValues=values)
                stats["written"] += 1
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    stats["skipped_existing"] += 1
                else:
                    raise
    except Exception as exc:
        # Losing reconstructed output silently is the worst outcome — raise so
        # the batch runner records this sensor as failed.
        log.error("DynamoDB write-back failed for device %s to %s: %s",
                  device_id, target_table, exc)
        raise

    if stats["skipped_existing"]:
        log.warning("Skipped %d slot(s) for device %s in %s: a real item already "
                    "exists at that key (not overwritten)",
                    stats["skipped_existing"], device_id, target_table)
    log.info("Wrote %d reconstructed rows for device %s back to %s",
             stats["written"], device_id, target_table)
    return stats


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
#               LastDataTimestamp (newest ORIGINAL source TimeStamp seen),
#               GapCount, FilledCount, UnresolvedCount.
#   Reads/writes here are best-effort: any failure logs and degrades to
#   "process the sensor" so a metadata problem never blocks reconstruction.
# ──────────────────────────────────────────────────────────────────────────────
def probe_latest_source_ts(table_name: str, device_id: str) -> str | None:
    """Cheaply fetch the newest ORIGINAL TimeStamp for a device.

    Queries the data table newest-first (a small page, normally one read) and
    skips rows this pipeline wrote earlier, so reconstructed rows never count as
    "new data". Returns the timestamp string, or None on empty/failure (caller
    treats None as "process the sensor").
    """
    try:
        item = _newest_original_item(table_name, device_id, projection="#ts",
                                     names={"#ts": config.DATA_SK})
    except Exception as exc:
        log.warning("Latest-ts probe failed for %s in %s: %s (will process)",
                    device_id, table_name, exc)
        return None
    if not item:
        return None
    raw = item.get(config.DATA_SK)
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
                                gap_count: int, filled_count: int | None = None,
                                unresolved_count: int | None = None) -> None:
    """Upsert the per-sensor watermark after a successful reconstruction.

    GapCount        : all non-original (gap) slots found = filled + unresolved
                      (+ any reconstructed slot skipped because a real item existed).
    FilledCount     : reconstructed slots actually written to the source table.
    UnresolvedCount : gap slots that could not be reconstructed (not written).
    """
    try:
        table = _dynamo.Table(config.METADATA_TABLE)
        item = {
            config.METADATA_KEY_NAME: str(device_id),
            "LastProcessed": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "GapCount": int(gap_count),
        }
        if filled_count is not None:
            item["FilledCount"] = int(filled_count)
        if unresolved_count is not None:
            item["UnresolvedCount"] = int(unresolved_count)
        if last_data_ts:
            item["LastDataTimestamp"] = str(last_data_ts)
        table.put_item(Item=item)
    except Exception as exc:
        # A metadata write failure must not fail the sensor — the reconstruction
        # already succeeded and its output is written. Worst case: the sensor is
        # reprocessed next run (correct, just not saved effort).
        log.warning("Metadata write failed for %s: %s", device_id, exc)
