"""
config.py — All editable settings for the Annam reconstruction pipeline.

Everything that depends on Annam's specific AWS setup lives here.
Change values in this file; you should not need to edit the logic modules.

The table-name unknowns we could not confirm from screenshots are marked
"# CONFIRM:" — fill them in once your team verifies them. The pipeline runs
on the values below as-is.
"""

import os

# ──────────────────────────────────────────────────────────────────────────────
# AWS / region
# ──────────────────────────────────────────────────────────────────────────────
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# ──────────────────────────────────────────────────────────────────────────────
# Input data tables
#   The pipeline reads the TARGET sensor and its NEIGHBOR sensors from DynamoDB.
#   Which physical table a sensor lives in is derived from its MQTT topic
#   (e.g. "WS/SSMet_0126/205") via TOPIC_TO_TABLE below.
# ──────────────────────────────────────────────────────────────────────────────

# Map an MQTT-topic *prefix* to the DynamoDB table that stores that station's
# readings. Keys are matched by "topic startswith prefix" (longest match wins).
# CONFIRM these table names with your team — add/adjust as needed.
TOPIC_TO_TABLE = {
    "WS/SSMet_0126/": "WS_SSMet_0126_Data",
    "WS/SSMet_1225/": "WS_SSMet_1225_Data",
    "WS/SSMET_1225/": "WS_SSMet_1225_Data",   # seen in neighbor table (caps variant)
    "WS/Campus/":     "WS_Campus_Data",
    "WS/Annam_0426/": "WS_Data_Full",         # CONFIRM: Annam_0426 readings table
    "WS/Annam_0526/": "WS_Data_Full",         # CONFIRM: Annam_0526 readings table
    "WS/Polytechnic/": "WS_SSMet_Data",       # CONFIRM: Polytechnic readings table
}

# If a topic matches no prefix above, fall back to this table.
# CONFIRM: which table is the canonical "all sensors" source.
DEFAULT_DATA_TABLE = "WS_Data_Full"

# ──────────────────────────────────────────────────────────────────────────────
# Neighbor table
#   WS_Spatial_Neighbors is keyed by "deviceId_topic" (e.g. "205#WS/SSMet_0126/205")
#   and lists up to 3 nearest neighbors with distance / id / raw_id / topic.
#   There are variants _0 / _1 / _2 — set the one your team confirms is current.
# ──────────────────────────────────────────────────────────────────────────────
NEIGHBORS_TABLE = "WS_Spatial_Neighbors"   # CONFIRM: vs _0 / _1 / _2
NEIGHBORS_KEY_NAME = "deviceId_topic"      # partition key of the neighbors table
MAX_NEIGHBORS = 3                          # neighbor_1 .. neighbor_3

# ──────────────────────────────────────────────────────────────────────────────
# DynamoDB key / attribute names on the DATA tables
#   All WS_*_Data tables we inspected use DeviceId (PK) + TimeStamp (SK).
# ──────────────────────────────────────────────────────────────────────────────
DATA_PK = "DeviceId"        # partition key attribute
DATA_SK = "TimeStamp"       # sort key attribute (string timestamp)

# Optional: some DeviceIds interleave two physical units (different IMEINumber).
# Leave None to use all rows. Set a specific IMEI string to filter to one unit,
# or set IMEI_PICK = "most_temperature" to auto-pick the IMEI that actually
# reports CurrentTemperature most often.
IMEI_ATTR = "IMEINumber"
IMEI_FILTER = None              # e.g. "868651069501360"
IMEI_PICK = "most_temperature"  # "most_temperature" | None

# ──────────────────────────────────────────────────────────────────────────────
# Output — reconstructed rows are written back to DynamoDB (primary destination)
#
# The pipeline reads a sensor from its source DynamoDB table, reconstructs the
# gaps, and inserts ONLY the reconstructed (filled) rows back into that SAME
# source table. There is no reconstructed dataset in S3 during normal execution.
# ──────────────────────────────────────────────────────────────────────────────

# Optional S3 debug output. When True, write_output() additionally dumps the
# full filled dataset + report CSVs to S3 for offline debugging. Disabled by
# default so normal execution writes nothing to S3.
S3_REPORT_ENABLED = os.environ.get("S3_REPORT_ENABLED", "false").lower() == "true"

# Used ONLY by the optional S3 debug output above (and by the batch run summary,
# which is itself gated on S3_REPORT_ENABLED). Not used in normal execution.
# CONFIRM: real bucket name if you ever enable the debug output.
OUTPUT_BUCKET = os.environ.get("OUTPUT_BUCKET", "annam-reconstructed")
OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "reconstructed")
OUTPUT_FORMAT = "parquet"   # "parquet" | "csv" (debug S3 dump only)

# Deprecated (Phase 3): the reconstructed rows are now written back to the
# resolved SOURCE table, not to a single fixed output table, so these are no
# longer consulted. Kept defined for backward compatibility only.
WRITE_BACK_TO_DYNAMO = True   # retained no-op; source-table write is always primary
OUTPUT_DYNAMO_TABLE = None    # deprecated: writes now target the source table

# Static, device-level identity attributes copied from a representative source
# row onto each reconstructed row, so downstream consumers (and any index/join
# that relies on them) keep working and the row is not "thin" compared with
# original observations. These are per-device constants or slowly-changing
# device properties — NOT time-varying measurements. Time-varying telemetry
# (SignalStrength, BatteryVoltage, SDcardStatus, WindDirection, LightIntensity,
# ...) is deliberately NOT fabricated for a synthetic gap timestamp, since no
# true value exists for it. CONFIRM these attribute names match the real table
# schema (casing matters); attributes absent on the source row are simply
# skipped. Set to [] to disable identity preservation entirely.
PRESERVE_SOURCE_ATTRS = [
    "Topic", "Latitude", "Longitude", "IMEINumber", "FirmwareVersion",
]

# ──────────────────────────────────────────────────────────────────────────────
# Reconstruction parameters (mirror the notebook; safe to leave as-is)
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_INTERVAL_MIN = 5

# Variables reconstructed (only those present in a given table are used).
CONT_VARS = ["CurrentTemperature", "CorrectedTemp", "CurrentHumidity",
             "CorrectedHumidity", "AtmPressure", "WindSpeed"]

RAIN_VARS = ["RainfallHourly", "RainfallDaily", "RainfallWeekly"]

REF_COL = "CurrentTemperature"   # reference column for gap detection

# Physically plausible clip ranges
CLIP = {"CurrentTemperature": (5, 55), "CorrectedTemp": (5, 55),
        "CurrentHumidity": (0, 100), "CorrectedHumidity": (0, 100),
        "AtmPressure": (940, 1060), "WindSpeed": (0, 60)}

# Max step-change thresholds for continuity correction
JUMP = {"CurrentTemperature": 3, "CorrectedTemp": 3, "CurrentHumidity": 10,
        "CorrectedHumidity": 10, "AtmPressure": 1.5, "WindSpeed": 10}

# Neighbor feature short-names attached to the main frame
NEIGHBOR_FEATURE_MAP = {
    "CurrentTemperature": "Temp", "CorrectedTemp": "CorrTemp",
    "CurrentHumidity": "Hum", "CorrectedHumidity": "CorrHum",
    "AtmPressure": "Pres", "WindSpeed": "Wind", "RainfallHourly": "RainH",
}

# Only reconstruct readings on/after this date (None = full history).
# Useful if WS_Data_30_Days-style tables only hold a rolling window anyway.
START_DATE = os.environ.get("START_DATE")  # e.g. "2026-01-01"

# Run the (expensive) synthetic-gap accuracy evaluation? Off in production;
# turn on for offline validation runs.
RUN_EVALUATION = os.environ.get("RUN_EVALUATION", "false").lower() == "true"

# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 — read-cost / runtime optimisations (all optional, env-overridable)
# ──────────────────────────────────────────────────────────────────────────────

# (Improvement 3) Configurable historical window. Instead of reading full
# history every run, read only the latest LOOKBACK_DAYS days. This bounds the
# per-sensor DynamoDB read. Set 0 (or empty) to disable and keep full history.
# An explicit START_DATE always wins over LOOKBACK_DAYS when both are set.
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "60"))

# (Improvement 2) Incremental processing. When enabled, the pipeline records
# per-sensor watermarks in a DynamoDB metadata table and skips any sensor whose
# newest data timestamp has not advanced since the last successful run.
INCREMENTAL_ENABLED = os.environ.get("INCREMENTAL_ENABLED", "true").lower() == "true"
METADATA_TABLE = os.environ.get("METADATA_TABLE", "WS_Reconstruction_Metadata")
METADATA_KEY_NAME = "DeviceId"   # partition key of the metadata table

# (Improvement 1) In-memory neighbour cache. Reuse a neighbour's loaded frame
# across every target that shares it, within a single batch run. On by default;
# set to false to force a fresh read per lookup (e.g. for debugging).
NEIGHBOR_CACHE_ENABLED = os.environ.get("NEIGHBOR_CACHE_ENABLED", "true").lower() == "true"

# (Improvement 4) Parallel processing. Number of worker threads used by the
# batch runner. 1 = fully sequential (unchanged from Phase 1). ECS task has
# 2 vCPU / 8 GB, so 4 threads overlaps I/O waits without over-committing memory.
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))

# ──────────────────────────────────────────────────────────────────────────────
# Phase 3 — ML execution optimisations (all optional, env-overridable)
#
# These control ONLY how models are selected/trained/reused. They do NOT touch
# the reconstruction algorithms, interpolation, neighbour blending, gap
# detection, feature-engineering formulas, DynamoDB I/O, batch flow, or logging.
# With PRODUCTION_MODE=true the pipeline trains exactly one predetermined model
# per variable (no tournament, no cross-validation). Offline experimentation
# (PRODUCTION_MODE=false) restores the original tournament + TimeSeriesSplit.
# ──────────────────────────────────────────────────────────────────────────────

# Master switch. true  -> production path (predetermined model, no CV).
#                false -> offline experimentation (tournament + CV, unchanged).
PRODUCTION_MODE = os.environ.get("PRODUCTION_MODE", "true").lower() == "true"

# (Optimisation 1) Predetermined best model per variable. Keys are the actual
# column names used in CONT_VARS; values in {"XGBoost", "LightGBM",
# "RandomForest"}. These are the models the team already validated as best for
# each variable, so the per-run tournament is redundant. Edit freely — this is
# the single source of truth for which model each variable uses in production.
#   Temperature -> XGBoost      Humidity  -> LightGBM
#   Pressure    -> LightGBM      WindSpeed -> RandomForest
PRODUCTION_MODEL_MAP = {
    "CurrentTemperature": "XGBoost",
    "CorrectedTemp":      "XGBoost",
    "CurrentHumidity":    "LightGBM",
    "CorrectedHumidity":  "LightGBM",
    "AtmPressure":        "LightGBM",
    "WindSpeed":          "RandomForest",
}
# Fallback model for any continuous variable not listed above.
DEFAULT_PRODUCTION_MODEL = os.environ.get("DEFAULT_PRODUCTION_MODEL", "XGBoost")

# (Optimisation 3) Cross-validation. In production it is skipped entirely: the
# predetermined model is fit once on all training rows (identical to the
# tournament's final refit). Set CV_ENABLED=true only for offline runs to
# restore TimeSeriesSplit selection.
CV_ENABLED = os.environ.get("CV_ENABLED", "false").lower() == "true"

# (Optimisation 4) Only train a model for a continuous variable that actually
# has missing values at the timestamps a model would fill (medium gaps).
# Variables with no such gaps are skipped — no model is fit for them. Under the
# pipeline's whole-row gap semantics every continuous variable is missing at a
# reference gap, so this selects exactly the set trained today (output
# unchanged); it only skips work for partial-schema tables.
TRAIN_ONLY_REQUIRED_VARS = os.environ.get(
    "TRAIN_ONLY_REQUIRED_VARS", "true").lower() == "true"

# (Optimisation 5) Persisted models. Infrastructure only; OFF by default so the
# default run trains fresh each time exactly as before. When enabled, a model is
# loaded from MODEL_STORE_DIR if present (skipping retraining) and otherwise
# trained and saved there. No monthly cadence is hardcoded — retraining policy
# is entirely up to how/when the store is cleared.
PERSIST_MODELS_ENABLED = os.environ.get(
    "PERSIST_MODELS_ENABLED", "false").lower() == "true"
MODEL_STORE_DIR = os.environ.get("MODEL_STORE_DIR", "/tmp/annam_models")
