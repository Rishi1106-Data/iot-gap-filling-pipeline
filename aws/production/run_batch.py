"""
run_batch.py — Single-task entrypoint that reconstructs ALL sensors in one run.

Replaces the Step Functions Map + per-sensor Fargate fan-out. One Fargate task
(or one local process) lists every sensor and reconstructs them sequentially in
a loop, so library import, the container image pull, and boto3 client/connection
setup are paid ONCE instead of ~143 times.

Resilience: a failure in one sensor is caught and recorded; the remaining
sensors still run. This preserves the behaviour of the old state machine's
Catch -> SensorFailed path. A batch summary is written to S3 at the end.

Usage:
    python run_batch.py                 # process every sensor
    python run_batch.py --limit 5       # smoke test on the first 5 sensors
    python run_batch.py --keys "205#WS/SSMet_0126/205" "...#..."  # explicit subset
"""

import argparse
import logging
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd

# sklearn emits a benign "X does not have valid feature names" warning when a
# model trained on a named DataFrame predicts on a numpy array. It does not
# affect results, but across 143 sensors it would flood CloudWatch logs (and
# cost). Silence just that one category.
warnings.filterwarnings(
    "ignore",
    message="X does not have valid feature names",
    category=UserWarning,
)

import config
import io_dynamo as io
import list_sensors
import reconstruct

# One summary line per sensor keeps CloudWatch ingestion (and cost) low.
# Drop to WARNING for the chattier modules so 143 sensors don't flood logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("annam.io").setLevel(logging.WARNING)
logging.getLogger("annam.reconstruct").setLevel(logging.WARNING)
log = logging.getLogger("annam.batch")


def _split_key(device_key: str):
    if "#" in device_key:
        did, topic = device_key.split("#", 1)
        return did, topic
    return device_key, ""


def _log_timing(device_key: str, t: dict):
    """Emit one consolidated timing line per sensor (Improvement 3).

    A single line keeps CloudWatch ingestion (and cost) low while still exposing
    every phase, so the bottleneck is visible without raising the reconstruct
    module's own (WARNING) log level.
    """
    log.info(
        "TIMING device=%s read=%.2fs neighbors=%.2fs features=%.2fs "
        "training=%.2fs reconstruct=%.2fs write=%.2fs total=%.2fs",
        device_key,
        t.get("read_s", 0.0), t.get("neighbors_s", 0.0),
        t.get("feature_eng_s", 0.0), t.get("model_training_s", 0.0),
        t.get("reconstruction_s", 0.0), t.get("write_s", 0.0),
        t.get("total_s", 0.0),
    )


def reconstruct_one(device_key: str) -> dict:
    """Reconstruct a single sensor. Returns a small status dict.
    Mirrors the old run_sensor.run_sensor() but never calls sys.exit()."""
    device_id, topic = _split_key(device_key)
    # Effective read window: explicit START_DATE, else the last LOOKBACK_DAYS
    # (Improvement 3). Computed once here so target + neighbours share it.
    start_ts = io.effective_start_ts()

    t = {}
    t0 = time.perf_counter()
    target_table = io.resolve_table_from_topic(topic)

    # 0. Incremental pre-check (Improvement 2). If the sensor's newest source
    #    timestamp has not advanced since the last successful run, skip it
    #    without reading full history or loading neighbours. A cheap 1-item
    #    probe supplies the newest timestamp.
    if getattr(config, "INCREMENTAL_ENABLED", False):
        meta = io.get_reconstruction_metadata(device_id)
        latest_ts = io.probe_latest_source_ts(target_table, device_id)
        if not io.has_new_data(meta, latest_ts):
            log.info("SKIPPED_NO_NEW_DATA device=%s (watermark %s)",
                     device_key, latest_ts)
            t["total_s"] = round(time.perf_counter() - t0, 2)
            return {"device": device_key, "status": "skipped_no_new_data",
                    "last_data_ts": latest_ts, **t}

    # 1. Load target (needed for its values regardless; also feeds gap detection,
    #    so no extra read is incurred).
    _t = time.perf_counter()
    target_df = io.load_series(device_id, target_table, start_ts)
    t["read_s"] = round(time.perf_counter() - _t, 2)
    if target_df.empty:
        return {"device": device_key, "status": "no_data"}
    latest_data_ts = io.latest_data_timestamp(target_df)

    # 2. Fast gap detection (Phase 1). Correction 1: if the sensor has no
    #    missing timestamps AND no missing reconstructable values, skip it
    #    immediately — do NOT load neighbours, generate features, train models,
    #    reconstruct, write back to DynamoDB, or generate reports. Only the
    #    incremental watermark is recorded (unchanged metadata optimization) so
    #    the next run can fast-skip this unchanged sensor. Then move on.
    gap_info = reconstruct.analyze_target_gaps(target_df)
    if not gap_info["has_work"]:
        log.info("SKIPPED_NO_GAPS device=%s", device_key)
        if getattr(config, "INCREMENTAL_ENABLED", False):
            io.put_reconstruction_metadata(device_id, latest_data_ts, 0)
        t["total_s"] = round(time.perf_counter() - t0, 2)
        return {"device": device_key, "status": "skipped_no_gaps", **t}

    # 3. Full path: load neighbors (via cache) and run the full reconstruction.
    _t = time.perf_counter()
    neighbor_meta = io.get_neighbors(device_key)
    neighbor_frames = []
    for nb in neighbor_meta:
        nb_table = io.resolve_table_from_topic(nb["topic"])
        # Cached load (Improvement 1): a neighbour shared by many targets is
        # read from DynamoDB only once per batch run.
        nb_df = io.load_series_cached(nb["id"], nb_table, start_ts)
        neighbor_frames.append({"id": nb["id"], "dist_km": nb["dist_km"], "df": nb_df})
    t["neighbors_s"] = round(time.perf_counter() - _t, 2)

    # device_id is passed only as the persisted-model key (Optimisation 5);
    # ignored unless PERSIST_MODELS_ENABLED.
    result = reconstruct.run_reconstruction(target_df, neighbor_frames,
                                            model_key=device_id)

    _t = time.perf_counter()
    io.write_output(device_id, result["final_df"], result["reports"],
                    target_table)
    t["write_s"] = round(time.perf_counter() - _t, 2)

    # Record watermark for incremental processing (Improvement 2).
    if getattr(config, "INCREMENTAL_ENABLED", False):
        gap_count = int(result["final_df"]["filled_flag"].sum())
        io.put_reconstruction_metadata(device_id, latest_data_ts, gap_count)

    t.update(result.get("timings", {}))
    t["total_s"] = round(time.perf_counter() - t0, 2)
    _log_timing(device_key, t)

    rows = len(result["final_df"])
    filled = int(result["final_df"]["filled_flag"].sum())
    return {"device": device_key, "status": "ok", "rows": rows,
            "filled": filled, "interval_min": result["interval_min"], **t}


def write_batch_summary(results: list[dict], started_at: float):
    """Optionally write one CSV summarising the whole run to S3.

    This is an operational run summary (per-sensor status), not a reconstructed
    dataset. It is gated on config.S3_REPORT_ENABLED and disabled by default so
    normal execution writes nothing to S3 — the same per-sensor and batch-level
    status is always emitted to CloudWatch logs regardless.
    """
    if not getattr(config, "S3_REPORT_ENABLED", False):
        log.info("Batch summary S3 write skipped (S3_REPORT_ENABLED is false); "
                 "per-sensor status is in the CloudWatch logs above.")
        return
    df = pd.DataFrame(results)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    key = f"{config.OUTPUT_PREFIX}/_batch_runs/run_{stamp}.csv"
    try:
        io._s3.put_object(
            Bucket=config.OUTPUT_BUCKET,
            Key=key,
            Body=df.to_csv(index=False).encode(),
        )
        log.info("Wrote batch summary to s3://%s/%s", config.OUTPUT_BUCKET, key)
    except Exception as exc:
        log.error("Could not write batch summary: %s", exc)


def main():
    ap = argparse.ArgumentParser(description="Reconstruct ALL Annam sensors in one task.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N sensors (smoke test).")
    ap.add_argument("--keys", nargs="*", default=None,
                    help="Explicit list of device keys; skips the neighbor-table scan.")
    args = ap.parse_args()

    started_at = time.time()

    if args.keys:
        keys = args.keys
    else:
        keys = list_sensors.list_sensor_keys()
    if args.limit:
        keys = keys[: args.limit]

    # De-duplicate keys defensively so a repeated key can never be processed
    # twice under the thread pool ("no duplicate processing").
    seen, deduped = set(), []
    for k in keys:
        if k not in seen:
            seen.add(k)
            deduped.append(k)
    if len(deduped) != len(keys):
        log.warning("Dropped %d duplicate key(s) from the work list",
                    len(keys) - len(deduped))
    keys = deduped

    # Fresh neighbour cache per run so stale frames never leak between runs.
    io.reset_neighbor_cache()

    max_workers = max(1, int(getattr(config, "MAX_WORKERS", 1)))
    log.info("Batch start: %d sensors to reconstruct (%d worker%s)",
             len(keys), max_workers, "" if max_workers == 1 else "s")

    # Results are stored by input index so the summary preserves input order
    # regardless of completion order ("preserve current output order").
    results_by_idx = [None] * len(keys)
    _progress = {"done": 0}
    _progress_lock = threading.Lock()

    def _run(idx: int, key: str) -> dict:
        t0 = time.time()
        try:
            res = reconstruct_one(key)
        except Exception as exc:  # noqa: BLE001 — one bad sensor must not kill the batch
            log.error("%s FAILED: %s", key, exc)
            res = {"device": key, "status": "error", "error": str(exc)}
        res["seconds"] = round(time.time() - t0, 1)
        results_by_idx[idx] = res
        # logging is thread-safe; the lock only serialises the progress counter.
        with _progress_lock:
            _progress["done"] += 1
            done = _progress["done"]
        log.info("[%d/%d] %s -> %s (%.1fs)",
                 done, len(keys), key, res["status"], res["seconds"])
        return res

    if max_workers == 1:
        # Fully sequential path — byte-for-byte the Phase-1 behaviour.
        for idx, key in enumerate(keys):
            _run(idx, key)
    else:
        # Correction 3: submit all sensors, then let the ThreadPoolExecutor
        # context-manager exit (shutdown(wait=True)) block until the last sensor
        # finishes. Results are already captured in results_by_idx and exceptions
        # are handled inside _run, so no separate drain/idle loop is needed —
        # execution proceeds straight to the summary and returns normally after
        # the final sensor.
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for idx, key in enumerate(keys):
                pool.submit(_run, idx, key)

    results = [r for r in results_by_idx if r is not None]

    ok = sum(r["status"] == "ok" for r in results)
    failed = sum(r["status"] == "error" for r in results)
    no_data = sum(r["status"] == "no_data" for r in results)
    skipped = sum(r["status"] == "skipped_no_gaps" for r in results)
    skipped_new = sum(r["status"] == "skipped_no_new_data" for r in results)

    # Correction 2: "sensors successfully reconstructed" — count a sensor ONLY
    # when it completed AND at least one gap-filled record was written back to
    # DynamoDB (filled >= 1). Sensors with no gaps, no data, or that filled
    # nothing do not count. This is distinct from the visited-progress [i/N]
    # indicator, which still tracks how many sensors have been processed.
    reconstructed = sum(1 for r in results
                        if r["status"] == "ok" and r.get("filled", 0) >= 1)

    write_batch_summary(results, started_at)

    cache = io.neighbor_cache_stats()
    elapsed = time.time() - started_at
    log.info("Batch done in %.1f min: %d reconstructed, %d ok, %d skipped_no_gaps, "
             "%d skipped_no_new_data, %d no_data, %d failed (of %d) | "
             "neighbour cache hits=%d misses=%d",
             elapsed / 60, reconstructed, ok, skipped, skipped_new, no_data,
             failed, len(keys), cache.get("hits", 0), cache.get("misses", 0))

    # Exit non-zero only if EVERY sensor failed — a few bad sensors are normal
    # and should not flag the whole scheduled task as failed (avoids noisy alarms).
    