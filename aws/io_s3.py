"""aws/io_s3.py — S3 integration for the gap-filling pipeline.

The existing pipeline reads CSVs with pandas.read_csv(local_path) and writes
outputs with DataFrame.to_csv(local_path). We do NOT change that. Instead this
module downloads inputs to local disk before the run and uploads outputs after,
so `src/` stays byte-for-byte untouched.

Design notes tied to the verified contract:
  • load_target / load_neighbors read LOCAL paths → we materialise files locally.
  • Models persist to {model_dir}/model_selection.joblib (+ .txt mapping).
  • Outputs are 6 CSVs + mae_vs_gapsize.png in output_dir.
  • Neighbour CSVs are shared across many targets in a batch → local-disk cache
    keyed by neighbour device id, downloaded at most once per Fargate task.
"""

from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional

import boto3
from botocore.config import Config as BotoConfig


# A single shared client per process; boto3 clients are thread-safe.
_BOTO_CFG = BotoConfig(
    retries={"max_attempts": 5, "mode": "adaptive"},
    max_pool_connections=32,
)


class S3IO:
    """Thin, cost-aware wrapper around the S3 operations the pipeline needs."""

    def __init__(self, bucket: str, region: str, work_dir: str = "/tmp/iot") -> None:
        self.bucket = bucket
        self.region = region
        self.work_dir = work_dir
        self.client = boto3.client("s3", region_name=region, config=_BOTO_CFG)

        # Local-disk neighbour cache (decision: cache on local disk per task).
        self._neighbor_cache_dir = os.path.join(work_dir, "cache", "neighbors")
        self._cached_neighbors: Dict[str, str] = {}  # device_id -> local path
        self._cache_lock = threading.Lock()
        os.makedirs(self._neighbor_cache_dir, exist_ok=True)

    # ── inputs ─────────────────────────────────────────────────────────────--
    def load_dataset_from_s3(self, key: str, local_path: str) -> str:
        """Download one object to a local path. Returns the local path.

        Used for the TARGET sensor file each iteration (not cached — each target
        is processed once per run).
        """
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        self.client.download_file(self.bucket, key, local_path)
        return local_path

    def load_neighbor_cached(self, neighbor_id: str, key: str) -> str:
        """Download a neighbour CSV at most once per task, reuse thereafter.

        Many target sensors in a batch share the same physical neighbours, so a
        local-disk cache eliminates duplicate S3 GETs (the dominant request cost
        at scale).
        """
        with self._cache_lock:
            if neighbor_id in self._cached_neighbors:
                return self._cached_neighbors[neighbor_id]

        local_path = os.path.join(self._neighbor_cache_dir, f"{neighbor_id}.csv")
        if not os.path.exists(local_path):
            self.client.download_file(self.bucket, key, local_path)

        with self._cache_lock:
            self._cached_neighbors[neighbor_id] = local_path
        return local_path

    def load_models_from_s3(self, key: str, local_path: str) -> Optional[str]:
        """Download a saved model bundle for inference mode.

        Returns the local path, or None if the object does not exist yet
        (e.g. the very first training run for a sensor). The caller decides
        whether a missing model is fatal (inference) or expected (train).
        """
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        try:
            self.client.download_file(self.bucket, key, local_path)
            return local_path
        except self.client.exceptions.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    # ── outputs ────────────────────────────────────────────────────────────--
    def _upload(self, local_path: str, key: str, content_type: str = "text/csv") -> str:
        self.client.upload_file(
            local_path,
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "ServerSideEncryption": "AES256"},
        )
        return f"s3://{self.bucket}/{key}"

    def save_filled_data_to_s3(self, local_path: str, dest_prefix: str) -> str:
        """Upload the (large) filled dataset CSV. Compressed if a .gz exists."""
        fname = os.path.basename(local_path)
        return self._upload(local_path, f"{dest_prefix}/{fname}")

    def save_reports_to_s3(self, local_paths: List[str], dest_prefix: str) -> List[str]:
        """Upload the small report CSVs and the MAE plot in one logical batch."""
        uris = []
        for p in local_paths:
            if not os.path.exists(p):
                continue
            fname = os.path.basename(p)
            ct = "image/png" if fname.endswith(".png") else "text/csv"
            uris.append(self._upload(p, f"{dest_prefix}/{fname}", content_type=ct))
        return uris

    def save_models_to_s3(self, model_dir: str, dest_key_prefix: str) -> List[str]:
        """Persist the trained model bundle + mapping after a training run.

        Mirrors what prepare_models() wrote locally:
          {model_dir}/model_selection.joblib
          {model_dir}/model_mapping_report.txt
        """
        uris = []
        for fname, ct in (
            ("model_selection.joblib", "application/octet-stream"),
            ("model_mapping_report.txt", "text/plain"),
        ):
            local = os.path.join(model_dir, fname)
            if os.path.exists(local):
                uris.append(self._upload(local, f"{dest_key_prefix}/{fname}", content_type=ct))
        return uris
