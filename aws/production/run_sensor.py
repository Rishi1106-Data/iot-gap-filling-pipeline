"""
run_sensor.py — Entrypoint for the Annam reconstruction pipeline on AWS.

A single Fargate task reconstructs ONE sensor. The orchestration layer
(Step Functions Map state) invokes this once per sensor, in parallel.

Usage (local / container):
    python run_sensor.py --device-key "205#WS/SSMet_0126/205"
    python run_sensor.py --device-id 205 --topic "WS/SSMet_0126/205"

It reads the device-key from the SENSOR_KEY env var if no CLI arg is given,
which is how the Step Functions Map state passes each item in.
"""

import argparse
import logging
import os
import sys
import time

import io_dynamo as io
import reconstruct
import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("annam.run")


def _split_key(device_key: str):
    """'205#WS/SSMet_0126/205' -> ('205', 'WS/SSMet_0126/205')."""
    if "#" in device_key:
        did, topic = device_key.split("#", 1)
        return did, topic
    return device_key, ""


def _log_timing(device_key: str, t: dict):
    """Emit one consolidated timing line per sensor (Improvement 3)."""
    log.info(
        "TIMING device=%s read=%.2fs neighbors=%.2fs features=%.2fs "
        "training=%.2fs reconstruct=%.2fs write=%.2fs total=%.2fs",
        device_key,
        t.get("read_s", 0.0), t.get("neighbors_s", 0.0),
        t.get("feature_eng_s", 0.0), t.get("model_training_s", 0.0),
        t.get("reconstruction_s", 0.0), t.get("write_s", 0.0),
        t.get("total_s", 0.0),
    )


def run_sensor(device_key: str) -> dict:
    """Reconstruct one sensor end-to-end. Returns a small status dict."""
    device_id, topic = _split_key(device_key)
    log.info("=== Reconstructing device %s (topic %s) ===", device_id, topic)

    # Effective read window: explicit START_DATE, else last LOOKBACK_DAYS
    # (Phase 2, Improvement 3).
    start_ts = io.effective_start_ts()
    t = {}
    t0 = time.perf_counter()

    # 1. Resolve target table from topic and load the target series
    target_table = io.resolve_table_from_topic(topic)
    log.info("Target table: %s", target_table)
    _t = time.perf_counter()
    target_df = io.load_series(device_id, target_table, start_ts)
    t["read_s"] = round(time.perf_counter() - _t, 2)
    if target_df.empty:
        log.error("No target data for %s — aborting.", device_key)
        return {"device": device_key, "status": "no_data"}

    # 2. Fast gap detection (Improvement 1). Correction 1: no missing timestamps
    #    and no missing reconstructable values -> skip immediately. Do NOT load
    #    neighbours, generate features, train models, reconstruct, write back to
    #    DynamoDB, or generate reports.
    gap_info = reconstruct.analyze_target_gaps(target_df)
    if not gap_info["has_work"]:
        log.info("SKIPPED_NO_GAPS device=%s", device_key)
        return {"device": device_key, "status": "skipped_no_gaps"}

    # 3. Look up neighbors and load each from its own table
    _t = time.perf_counter()
    neighbor_meta = io.get_neighbors(device_key)
    log.info("Found %d neighbors", len(neighbor_meta))
    neighbor_frames = []
    for nb in neighbor_meta:
        nb_table = io.resolve_table_from_topic(nb["topic"])
        nb_df = io.load_series_cached(nb["id"], nb_table, start_ts)
        neighbor_frames.append({
            "id": nb["id"], "dist_km": nb["dist_km"], "df": nb_df,
        })
        log.info("  neighbor %s from %s: %d rows (%.1f km)",
                 nb["id"], nb_table, len(nb_df), nb["dist_km"])
    t["neighbors_s"] = round(time.perf_counter() - _t, 2)

    # 4. Run the reconstruction. device_id is passed only as the persisted-model
    #    key (Optimisation 5); it is ignored unless PERSIST_MODELS_ENABLED.
    result = reconstruct.run_reconstruction(target_df, neighbor_frames,
                                            model_key=device_id)

    # 5. Write reconstructed rows back to the source DynamoDB table
    #    (+ optional S3 debug report, disabled by default).
    _t = time.perf_counter()
    io.write_output(device_id, result["final_df"], result["reports"],
                    target_table)
    t["write_s"] = round(time.perf_counter() - _t, 2)

    t.update(result.get("timings", {}))
    t["total_s"] = round(time.perf_counter() - t0, 2)
    _log_timing(device_key, t)

    rows = len(result["final_df"])
    filled = int(result["final_df"]["filled_flag"].sum())
    log.info("Done: %d rows, %d filled (interval %g min)",
             rows, filled, result["interval_min"])
    return {"device": device_key, "status": "ok",
            "rows": rows, "filled": filled}


def main():
    ap = argparse.ArgumentParser(description="Reconstruct one Annam sensor.")
    ap.add_argument("--device-key", help="'id#topic', e.g. 205#WS/SSMet_0126/205")
    ap.add_argument("--device-id", help="device id (with --topic)")
    ap.add_argument("--topic", help="mqtt topic (with --device-id)")
    args = ap.parse_args()

    if args.device_key:
        key = args.device_key
    elif args.device_id and args.topic:
        key = f"{args.device_id}#{args.topic}"
    else:
        key = os.environ.get("SENSOR_KEY")

    if not key:
        log.error("No sensor specified. Use --device-key or set SENSOR_KEY.")
        sys.exit(2)

    result = run_sensor(key)
    log.info("Result: %s", result)
    # Non-zero exit on failure so Step Functions can retry the task.
    # 