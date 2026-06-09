"""aws/run_batch.py — Fargate batch entry point. Processes MANY sensors per task.

This is the only place the AWS layer touches the existing pipeline, and it does
so through the verified public contract ONLY:

    main.run_pipeline(config: dict) -> (filled_df, eval_df, exec_summary: dict)

It does NOT import or re-implement any reconstruction / model / validation logic.
For each sensor in the batch it:

  1. Reads sensor metadata + neighbours from DynamoDB (1 cheap GetItem each).
  2. Downloads the target CSV from S3; downloads neighbour CSVs via the per-task
     local-disk cache (shared neighbours fetched once).
  3. Builds the exact YAML-shaped config dict the pipeline expects (pointing at
     the local files), including the mandatory CorrectedHeatIndex quality ref.
  4. In inference mode, pre-stages the saved model bundle into model_dir so
     prepare_models(train_mode=False) loads it.
  5. Calls run_pipeline(config) — unchanged business logic.
  6. Uploads outputs + (if training) the new model bundle to S3.
  7. Writes a result record to DynamoDB and emits CloudWatch metrics.

Fault tolerance: one sensor failing logs an ERROR, records a 'failed' result,
and continues. A batch only fails hard if EVERY sensor fails (so Step Functions
retries are meaningful) — partial success is the norm and is reported.

Invocation contract (matches the verified import quirk):
    WORKDIR must be the repo root; this file adds 'src' to sys.path exactly like
    run_pipeline.py does, so both `from src import ...` and `from gap_analysis
    import ...` resolve.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

# ── make the existing pipeline importable EXACTLY as run_pipeline.py does ─────
# run_pipeline.py: sys.path.insert(0, "src"); import main
# main.py uses `from src import ...`; neighbor_reconstruction uses `from
# gap_analysis import ...`. Both resolve only when CWD is the repo root AND
# 'src' is on sys.path. We replicate that precisely.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)            # enables `from src import ...`
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)                  # enables `from gap_analysis import ...`

import main as pipeline                        # noqa: E402  the untouched orchestrator

# aws/ modules (this file's own directory is on sys.path when run as a script)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import AwsConfig, load as load_cfg     # noqa: E402
from io_s3 import S3IO                              # noqa: E402
from io_dynamo import DynamoIO                      # noqa: E402
from cloudwatch_logger import CloudWatchLogger      # noqa: E402


# The pipeline hardcodes per_cov['CorrectedHeatIndex'] in load_neighbors, so the
# quality_reference MUST contain these three keys regardless of data columns.
_REQUIRED_QUALITY_REF = ["CorrectedTemp", "CorrectedHumidity", "CorrectedHeatIndex"]

# Defaults mirror config/config_1.yaml so behaviour is identical to local runs.
_DEFAULT_CONT_VARS = [
    "CurrentTemperature", "CorrectedTemp", "CurrentHumidity",
    "CorrectedHumidity", "AtmPressure", "WindSpeed",
]
_DEFAULT_JUMP = {
    "CurrentTemperature": 3, "CorrectedTemp": 3, "CurrentHumidity": 10,
    "CorrectedHumidity": 10, "AtmPressure": 1.5, "WindSpeed": 10,
}
_DEFAULT_CLIP = {
    "CurrentTemperature": [5, 55], "CorrectedTemp": [5, 55],
    "CurrentHumidity": [0, 100], "CorrectedHumidity": [0, 100],
    "AtmPressure": [940, 1060], "WindSpeed": [0, 60],
}
_DEFAULT_MODEL_PARAMS = {
    "xgboost": {"n_estimators": 100, "max_depth": 5, "learning_rate": 0.05},
    "lightgbm": {"n_estimators": 100, "max_depth": 5, "learning_rate": 0.05},
    "randomforest": {"n_estimators": 100, "max_depth": 10},
    "knn": {"n_neighbors": 5},
}


def _build_config(
    cfg: AwsConfig,
    device_id: str,
    target_local: str,
    neighbor_locals: List[str],
    neighbor_ids: List[str],
    neighbor_distances: List[float],
    sensor_meta: Dict[str, Any],
    out_dir: str,
    model_dir: str,
) -> Dict[str, Any]:
    """Assemble the exact dict shape main.run_pipeline expects.

    Pulls per-sensor knobs from DynamoDB metadata where present, falling back to
    the same defaults as config/config_1.yaml so a sensor with minimal metadata
    behaves identically to the committed local config.
    """
    ref_col = sensor_meta.get("ref_col", "CorrectedTemp")
    cont_vars = sensor_meta.get("continuous_variables", _DEFAULT_CONT_VARS)
    return {
        "timeline": {"frequency": "5min"},
        "execution": {"train_mode": cfg.train_mode},
        "gaps": {"isolated_max": 1, "medium_max": 5},
        "models": {"model_dir": model_dir, "n_splits": cfg.n_splits},
        "neighbor": {"quality_reference": list(_REQUIRED_QUALITY_REF)},
        "validation": {
            "split_fraction": 0.8,
            "gap_sizes": [1, 2, 3, 5],
            "n_gaps": cfg.val_n_gaps,
            "seed": cfg.val_seed,
        },
        "reference": {"ref_col": ref_col},
        "continuous_variables": cont_vars,
        "jump_thresholds": sensor_meta.get("jump_thresholds", _DEFAULT_JUMP),
        "clipping_ranges": sensor_meta.get("clipping_ranges", _DEFAULT_CLIP),
        "neighbor_distances": {
            "ids": neighbor_ids,
            "distances_km": neighbor_distances,
        },
        "input_paths": {
            "target_file": target_local,
            "neighbor_files": neighbor_locals,
        },
        "output_paths": {
            "output_dir": out_dir,
            "filled_dataset": os.path.join(out_dir, "filled_dataset.csv"),
            "audit_report": os.path.join(out_dir, "audit_report.csv"),
            "evaluation_report": os.path.join(out_dir, "evaluation_report.csv"),
            "neighbor_quality_report": os.path.join(out_dir, "neighbor_quality_report.csv"),
            "model_selection_report": os.path.join(out_dir, "model_selection_report.csv"),
        },
        "model_params": sensor_meta.get("model_params", _DEFAULT_MODEL_PARAMS),
    }


def process_sensor(
    cfg: AwsConfig,
    s3: S3IO,
    dynamo: DynamoIO,
    log: CloudWatchLogger,
    device_id: str,
) -> Dict[str, Any]:
    """Process a single sensor end-to-end. Never raises — returns a result dict."""
    t0 = time.time()
    sensor_dir = os.path.join(cfg.work_dir, "sensors", device_id)
    in_dir = os.path.join(sensor_dir, "input")
    out_dir = os.path.join(sensor_dir, "outputs")
    model_dir = os.path.join(sensor_dir, "models")
    for d in (in_dir, out_dir, model_dir):
        os.makedirs(d, exist_ok=True)

    try:
        meta = dynamo.get_sensor_metadata(device_id) or {}
        neighbors = meta.get("neighbors") or dynamo.get_neighbor_sensors(device_id)
        if not neighbors:
            raise RuntimeError(f"No neighbours configured for sensor {device_id}")

        neighbor_ids = [str(n["id"]) for n in neighbors]
        neighbor_distances = [float(n["distance_km"]) for n in neighbors]

        # ── download target (per-run) ────────────────────────────────────────
        raw_key = meta.get("s3_raw_key", cfg.raw_key(device_id))
        target_local = s3.load_dataset_from_s3(raw_key, os.path.join(in_dir, f"{device_id}.csv"))

        # ── download neighbours (cached per task) ────────────────────────────
        neighbor_locals = []
        for nid in neighbor_ids:
            nkey = cfg.neighbor_key(nid)
            neighbor_locals.append(s3.load_neighbor_cached(nid, nkey))

        # ── inference mode: pre-stage saved model bundle ─────────────────────
        if not cfg.train_mode:
            model_local = os.path.join(model_dir, "model_selection.joblib")
            got = s3.load_models_from_s3(cfg.model_key(device_id), model_local)
            if got is None:
                raise RuntimeError(
                    f"Inference mode but no saved model in S3 for {device_id} "
                    f"({cfg.model_key(device_id)}). Run a training pass first."
                )

        # ── build config + run the UNCHANGED pipeline ────────────────────────
        pipe_cfg = _build_config(
            cfg, device_id, target_local, neighbor_locals,
            neighbor_ids, neighbor_distances, meta, out_dir, model_dir,
        )
        log.info("Running pipeline", device_id=device_id, train_mode=cfg.train_mode,
                 neighbors=len(neighbor_ids))
        filled_df, eval_df, summary = pipeline.run_pipeline(pipe_cfg)

        # ── upload outputs ───────────────────────────────────────────────────
        filled_uri = s3.save_filled_data_to_s3(
            pipe_cfg["output_paths"]["filled_dataset"],
            cfg.filled_prefix_for(device_id),
        )
        report_paths = [
            pipe_cfg["output_paths"]["audit_report"],
            pipe_cfg["output_paths"]["evaluation_report"],
            pipe_cfg["output_paths"]["neighbor_quality_report"],
            pipe_cfg["output_paths"]["model_selection_report"],
            os.path.join(out_dir, "mae_vs_gapsize.png"),  # side-effect file
        ]
        s3.save_reports_to_s3(report_paths, cfg.reports_prefix_for(device_id))

        # ── training mode: persist new model bundle ──────────────────────────
        if cfg.train_mode:
            s3.save_models_to_s3(model_dir, f"{cfg.models_prefix}/{device_id}")

        method_counts = summary.get("method_counts", {})
        log.reconstruction_metrics(device_id, method_counts)

        duration = round(time.time() - t0, 1)
        log.info("Sensor done", device_id=device_id, duration_s=duration,
                 status=summary.get("status"))
        return {
            "device_id": device_id,
            "run_id": cfg.run_id,
            "run_date": cfg.run_date,
            "status": summary.get("status", "success"),
            "rows_total": int(summary.get("rows_total", len(filled_df))),
            "method_counts": method_counts,
            "filled_s3_uri": filled_uri,
            "reports_s3_prefix": f"s3://{cfg.data_bucket}/{cfg.reports_prefix_for(device_id)}",
            "train_mode": cfg.train_mode,
            "duration_seconds": duration,
        }

    except Exception as exc:  # fault isolation: never let one sensor kill the batch
        duration = round(time.time() - t0, 1)
        log.error("Sensor failed", device_id=device_id, error=str(exc), duration_s=duration)
        return {
            "device_id": device_id,
            "run_id": cfg.run_id,
            "run_date": cfg.run_date,
            "status": "failed",
            "error": str(exc),
            "train_mode": cfg.train_mode,
            "duration_seconds": duration,
        }
    finally:
        # Free per-sensor input/output to bound disk on long batches; KEEP the
        # shared neighbour cache (lives under work_dir/cache, not sensor_dir).
        _safe_rmtree(in_dir)
        _safe_rmtree(out_dir)


def _safe_rmtree(path: str) -> None:
    import shutil
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def run_batch(device_ids: List[str], cfg: Optional[AwsConfig] = None) -> Dict[str, Any]:
    """Process a list of sensors sequentially in ONE task. Fault tolerant."""
    cfg = cfg or load_cfg()
    log = CloudWatchLogger(
        cfg.cw_namespace, cfg.log_level,
        dimensions={"stage": "run_batch", "run_id": cfg.run_id or "adhoc"},
    )
    s3 = S3IO(cfg.data_bucket, cfg.region, cfg.work_dir)
    dynamo = DynamoIO(cfg.sensor_table, cfg.results_table, cfg.region)

    t0 = time.time()
    results: List[Dict[str, Any]] = []
    for device_id in device_ids:
        results.append(process_sensor(cfg, s3, dynamo, log, device_id))

    ok = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]

    # Persist all results in one batched write (≤25/call handled internally).
    written = dynamo.write_gap_fill_results(results)

    batch_seconds = round(time.time() - t0, 1)
    log.processing_metrics(len(results), len(ok), len(failed), batch_seconds)
    # Reasonable Fargate Spot sizing for this workload (see PHASE 10).
    log.cost_metrics(len(results), batch_seconds, vcpu=1.0, gb=2.0)
    log.info("Batch complete", total=len(results), succeeded=len(ok),
             failed=len(failed), results_written=written, batch_seconds=batch_seconds)

    # Hard-fail ONLY if every sensor failed, so Step Functions retry is meaningful.
    if results and not ok:
        raise RuntimeError(
            f"All {len(results)} sensors in batch failed; first error: "
            f"{failed[0].get('error')}"
        )

    return {
        "succeeded": len(ok),
        "failed": len(failed),
        "failed_device_ids": [r["device_id"] for r in failed],
        "batch_seconds": batch_seconds,
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Process a batch of sensors.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--device-ids", help="Comma-separated device ids.")
    g.add_argument("--batch-json", help="JSON string: {'device_ids': [...]} (Step Functions).")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    if args.batch_json:
        payload = json.loads(args.batch_json)
        device_ids = [str(d) for d in payload["device_ids"]]
    else:
        device_ids = [d.strip() for d in args.device_ids.split(",") if d.strip()]

    summary = run_batch(device_ids)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
