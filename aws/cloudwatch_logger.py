"""aws/cloudwatch_logger.py — structured logging + CloudWatch metrics.

Two responsibilities, kept separate:

1. Structured JSON logs to stdout. On Fargate the awslogs driver ships stdout
   straight to a CloudWatch Logs group, so we do NOT call PutLogEvents
   ourselves (that would double-bill and add latency). JSON lines are queryable
   with CloudWatch Logs Insights.

2. Custom metrics via the Embedded Metric Format (EMF). Emitting EMF on stdout
   means CloudWatch extracts metrics from the log stream for FREE — no
   PutMetricData API calls, which is the single biggest hidden CloudWatch cost
   at thousands-of-sensors scale.

If EMF-via-logs is ever undesirable, set METRICS_MODE=api to switch to
PutMetricData (batched, ≤20 metrics/call).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional


class CloudWatchLogger:
    """Structured logger + metric emitter for the gap-filling pipeline."""

    def __init__(
        self,
        namespace: str,
        log_level: str = "INFO",
        dimensions: Optional[Dict[str, str]] = None,
        metrics_mode: Optional[str] = None,
        region: Optional[str] = None,
    ) -> None:
        self.namespace = namespace
        self.level = log_level.upper()
        self.dimensions = dimensions or {}
        self.metrics_mode = (metrics_mode or os.environ.get("METRICS_MODE", "emf")).lower()
        self.region = region or os.environ.get("AWS_REGION", "ap-south-1")
        self._levels = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
        # Lazy boto client only if api mode is requested.
        self._cw = None
        if self.metrics_mode == "api":
            import boto3  # local import keeps EMF mode dependency-light

            self._cw = boto3.client("cloudwatch", region_name=self.region)

    # ── structured logging ───────────────────────────────────────────────────
    def _emit(self, level: str, message: str, **fields: Any) -> None:
        if self._levels[level] < self._levels.get(self.level, 20):
            return
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "level": level,
            "msg": message,
            **self.dimensions,
            **fields,
        }
        stream = sys.stderr if level == "ERROR" else sys.stdout
        print(json.dumps(record, default=str), file=stream, flush=True)

    def info(self, message: str, **fields: Any) -> None:
        self._emit("INFO", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._emit("WARNING", message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self._emit("ERROR", message, **fields)

    def debug(self, message: str, **fields: Any) -> None:
        self._emit("DEBUG", message, **fields)

    # ── metrics ────────────────────────────────────────────────────────────--
    def metric(
        self,
        name: str,
        value: float,
        unit: str = "Count",
        **extra_dims: str,
    ) -> None:
        """Emit a single custom metric.

        EMF mode (default): writes an EMF-formatted JSON line to stdout that
        CloudWatch parses into a metric for free.
        API mode: calls PutMetricData immediately.
        """
        dims = {**self.dimensions, **extra_dims}
        if self.metrics_mode == "api" and self._cw is not None:
            self._cw.put_metric_data(
                Namespace=self.namespace,
                MetricData=[
                    {
                        "MetricName": name,
                        "Value": float(value),
                        "Unit": unit,
                        "Dimensions": [
                            {"Name": k, "Value": str(v)} for k, v in dims.items()
                        ],
                    }
                ],
            )
            return

        # EMF: embed metric metadata so CloudWatch extracts it from the log.
        emf = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [list(dims.keys())] if dims else [[]],
                        "Metrics": [{"Name": name, "Unit": unit}],
                    }
                ],
            },
            name: float(value),
            **{k: str(v) for k, v in dims.items()},
        }
        print(json.dumps(emf, default=str), file=sys.stdout, flush=True)

    def metrics_batch(self, metrics: List[Dict[str, Any]], **shared_dims: str) -> None:
        """Emit several metrics in a single EMF line (cheapest at scale)."""
        dims = {**self.dimensions, **shared_dims}
        metric_defs = [{"Name": m["name"], "Unit": m.get("unit", "Count")} for m in metrics]
        emf: Dict[str, Any] = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [list(dims.keys())] if dims else [[]],
                        "Metrics": metric_defs,
                    }
                ],
            },
            **{k: str(v) for k, v in dims.items()},
        }
        for m in metrics:
            emf[m["name"]] = float(m["value"])
        print(json.dumps(emf, default=str), file=sys.stdout, flush=True)

    # ── domain-specific helpers ───────────────────────────────────────────────
    def processing_metrics(
        self, sensors_total: int, sensors_ok: int, sensors_failed: int, batch_seconds: float
    ) -> None:
        self.metrics_batch(
            [
                {"name": "SensorsProcessed", "value": sensors_total, "unit": "Count"},
                {"name": "SensorsSucceeded", "value": sensors_ok, "unit": "Count"},
                {"name": "SensorsFailed", "value": sensors_failed, "unit": "Count"},
                {"name": "BatchDurationSeconds", "value": batch_seconds, "unit": "Seconds"},
            ]
        )

    def reconstruction_metrics(self, device_id: str, method_counts: Dict[str, int]) -> None:
        """Per-sensor reconstruction provenance (original/model/neighbor/...)."""
        metrics = []
        for method in ("original", "interpolation", "model", "neighbor", "unresolved"):
            metrics.append(
                {"name": f"Rows_{method}", "value": method_counts.get(method, 0), "unit": "Count"}
            )
        self.metrics_batch(metrics, DeviceId=device_id)

    def cost_metrics(self, sensors_in_task: int, task_seconds: float, vcpu: float, gb: float) -> None:
        """Approximate per-task Fargate Spot cost so spend is visible in CW.

        Fargate Spot (ap-south-1, ~70% off on-demand) rough rates:
          vCPU-hour ≈ $0.01244, GB-hour ≈ $0.001365
        Adjust via env if your region/discount differs.
        """
        vcpu_rate = float(os.environ.get("FARGATE_SPOT_VCPU_HOUR", "0.01244"))
        gb_rate = float(os.environ.get("FARGATE_SPOT_GB_HOUR", "0.001365"))
        hours = task_seconds / 3600.0
        est_cost = hours * (vcpu * vcpu_rate + gb * gb_rate)
        per_sensor = est_cost / max(sensors_in_task, 1)
        self.metrics_batch(
            [
                {"name": "TaskCostUSD", "value": est_cost, "unit": "None"},
                {"name": "CostPerSensorUSD", "value": per_sensor, "unit": "None"},
            ]
        )
