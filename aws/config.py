"""aws/config.py — central, environment-driven configuration for the AWS layer.

Every value here comes from an environment variable so the SAME Docker image
runs unchanged across dev / staging / prod. Nothing about the AWS layer is
hardcoded; the ECS task definition (or `docker run -e ...`) supplies values.

This module deliberately contains NO business logic and NO imports from `src/`.
It is safe to import from list_sensors.py (Step Functions Lambda-style entry)
and from run_batch.py (Fargate entry) alike.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _env(key: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(key, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"Required environment variable {key} is not set.")
    return val if val is not None else ""


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    return int(raw) if raw not in (None, "") else default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class AwsConfig:
    """Resolved configuration for one pipeline invocation/environment."""

    # ── AWS region / identity ───────────────────────────────────────────────
    region: str = field(default_factory=lambda: _env("AWS_REGION", "ap-south-1"))

    # ── S3 layout ────────────────────────────────────────────────────────────
    # Single bucket, prefix-separated. Cheaper and simpler than many buckets.
    #   s3://<bucket>/raw/<device_id>/<device_id>.csv            (input sensor data)
    #   s3://<bucket>/models/<device_id>/model_selection.joblib  (per-sensor models)
    #   s3://<bucket>/filled/<run_date>/<device_id>/...          (filled output)
    #   s3://<bucket>/reports/<run_date>/<device_id>/...         (audit/eval/etc.)
    data_bucket: str = field(default_factory=lambda: _env("DATA_BUCKET", required=True))
    raw_prefix: str = field(default_factory=lambda: _env("RAW_PREFIX", "raw"))
    models_prefix: str = field(default_factory=lambda: _env("MODELS_PREFIX", "models"))
    filled_prefix: str = field(default_factory=lambda: _env("FILLED_PREFIX", "filled"))
    reports_prefix: str = field(default_factory=lambda: _env("REPORTS_PREFIX", "reports"))

    # ── DynamoDB tables ──────────────────────────────────────────────────────
    sensor_table: str = field(
        default_factory=lambda: _env("SENSOR_TABLE", "annam-sensor-metadata")
    )
    results_table: str = field(
        default_factory=lambda: _env("RESULTS_TABLE", "annam-gapfill-results")
    )

    # ── CloudWatch ───────────────────────────────────────────────────────────
    cw_namespace: str = field(
        default_factory=lambda: _env("CW_NAMESPACE", "AnnamAI/GapFilling")
    )
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO"))

    # ── Execution mode ───────────────────────────────────────────────────────
    # train_mode flows straight into the pipeline config 'execution.train_mode'.
    train_mode: bool = field(default_factory=lambda: _env_bool("TRAIN_MODE", False))

    # ── Local scratch (ephemeral container storage / Fargate task storage) ───
    work_dir: str = field(default_factory=lambda: _env("WORK_DIR", "/tmp/iot"))

    # ── Batch sizing ─────────────────────────────────────────────────────────
    # One Fargate task processes this many sensors sequentially (NOT one/task).
    batch_size: int = field(default_factory=lambda: _env_int("BATCH_SIZE", 25))

    # ── Run identity (set by Step Functions; falls back to a timestamp) ──────
    run_id: str = field(default_factory=lambda: _env("RUN_ID", ""))
    run_date: str = field(default_factory=lambda: _env("RUN_DATE", ""))

    # ── Pipeline static knobs that the AWS layer injects into the YAML config ─
    n_splits: int = field(default_factory=lambda: _env_int("N_SPLITS", 5))
    val_n_gaps: int = field(default_factory=lambda: _env_int("VAL_N_GAPS", 150))
    val_seed: int = field(default_factory=lambda: _env_int("VAL_SEED", 42))

    @property
    def models_uri(self) -> str:
        return f"s3://{self.data_bucket}/{self.models_prefix}"

    def raw_key(self, device_id: str) -> str:
        return f"{self.raw_prefix}/{device_id}/{device_id}.csv"

    def neighbor_key(self, neighbor_id: str) -> str:
        # Neighbours are just sensors; their raw data lives under the same layout.
        return f"{self.raw_prefix}/{neighbor_id}/{neighbor_id}.csv"

    def model_key(self, device_id: str) -> str:
        return f"{self.models_prefix}/{device_id}/model_selection.joblib"

    def filled_prefix_for(self, device_id: str) -> str:
        rd = self.run_date or "latest"
        return f"{self.filled_prefix}/{rd}/{device_id}"

    def reports_prefix_for(self, device_id: str) -> str:
        rd = self.run_date or "latest"
        return f"{self.reports_prefix}/{rd}/{device_id}"


def load() -> AwsConfig:
    """Factory used by every entry point so config resolution is consistent."""
    return AwsConfig()
