# IoT Weather-Sensor Gap-Filling Pipeline

Detects missing observations in IoT weather-sensor time series and reconstructs them,
choosing the method by gap length — with every filled value labelled by method and
confidence. Built during an internship at **ANNAM AI**, in two forms that live in this one repository:

| | Where | What it is |
|---|---|---|
| **A. Development / research pipeline** | `src/`, `run_pipeline.py`, `config/` | CSV in → CSV out. Model tournament, synthetic-gap accuracy evaluation, full audit reports. |
| **B. AWS production implementation** | `aws/production/` | Scheduled batch job: reads sensors from **DynamoDB**, reconstructs gaps, writes the filled rows back to DynamoDB. Docker + ECS Fargate + EventBridge Scheduler + Terraform. |

> Earlier-generation AWS material (S3 CSV + Step Functions design) is retained under `aws/`,
> `infrastructure/`, `terraform/` and `Docs/`, clearly labelled as superseded — see
> [Legacy material](#legacy-material).

---

## 1. Problem statement

Weather stations should report every 5 minutes, but readings go missing because of
connectivity loss, power cuts, hardware faults, or firmware issues. Downstream analytics,
dashboards and models need a complete, regularly spaced series — but silently inventing
data is worse than having holes. This project fills what can be filled defensibly,
**marks every filled value**, and leaves the rest unresolved.

## 2. How reconstruction works

**Gap detection.** Timestamps are rounded to the sampling interval (auto-detected as the
modal spacing; 5 min by default), de-duplicated, and placed on a complete regular grid.
A slot is *missing* when the reference variable is empty (`CorrectedTemp` in the local
config, `CurrentTemperature` in production).

**Gap classification** (by consecutive missing slots; local thresholds are configurable in
`config/*.yaml → gaps`, production uses the same 1 / 5 split in code):

| Gap | Size | Method | Confidence label |
|---|---|---|---|
| Isolated | 1 slot | Time interpolation (limit 1) | High |
| Medium | 2–5 slots | ML regression, filled iteratively | Medium |
| Long | > 5 slots | Blend of nearby-sensor trajectories, anchored to the target's own values either side of the gap; weighted by neighbour reliability, distance and coverage | Medium if exactly two neighbours contributed, otherwise Low |
| Not enough information | any | **Left empty**: marked `unresolved` in the local output; in production counted in the metadata and **not written** to the sensor table | unknown |

**ML approach.** Features per variable: calendar (hour, weekday, month), lagged and
rolling statistics built only from *original* readings (no leakage from filled values) and
the neighbours' concurrent readings. Candidate models: **XGBoost, LightGBM, Random Forest, KNN**.

- *Development pipeline:* a tournament picks the best model per variable using
  `TimeSeriesSplit` cross-validation; winners are saved (`train_mode: true`) or re-loaded (`false`).
- *Production:* the tournament is skipped. A predetermined model per variable
  (`PRODUCTION_MODEL_MAP` in `aws/production/config.py`: temperature → XGBoost,
  humidity & pressure → LightGBM, wind speed → Random Forest) is fitted once per sensor, and only
  for variables that actually have medium gaps. Setting `PRODUCTION_MODE=false` /
  `CV_ENABLED=true` restores the tournament + cross-validation path.

**Rainfall** is handled conservatively. In production a missing value is set to 0 only when there is
observed evidence of a dry period — at least one genuinely observed value within ±3 slots of the
target series or from a neighbour at the same slot — and no observed value is positive. Missing
values are never read as "dry", zeros the rule assigns are not reused as evidence for later slots,
and slots that could not be reconstructed never receive a rainfall value.

**Post-processing.** Values are clipped to physically plausible ranges and a continuity
correction pulls filled values that jump too far from their neighbours back toward them
(thresholds in the config files).

## 3. Validation and auditability

Every output row carries `gap_type`, `gap_id`, `gap_size`, `filled_flag`,
`imputation_method` (`original` / `interpolation` / `model` / `neighbor` / `unresolved`)
and `confidence_level`. Original readings are never overwritten.

**What production persists.** Original sensor observations are the source of truth. Only
*successfully reconstructed* rows (`imputation_method` of `interpolation`, `model` or `neighbor`
**and** a non-null reference value) are written to the sensor table, each flagged `filled_flag = 1`.
Unresolved slots are never written; they are counted in the reconstruction metadata instead
(`GapCount` = all gap slots, `FilledCount`, `UnresolvedCount`) and logged to CloudWatch.
`filled_flag` itself keeps its meaning (0 = original, 1 = non-original); the production writer decides
what to persist from the reconstruction method and the reference value. On every read, rows this
pipeline wrote earlier (`filled_flag = 1`, or a reconstruction/`unresolved` `imputation_method`) are
excluded from the target and neighbour series, so a later run never trains on, or reconstructs from,
its own earlier output. The incremental "new data?" check likewise looks only at the newest original
item. (The optional S3 debug dump, off by default, contains the full in-memory frame including
unresolved rows; it is never the sensor table.)

- **Development pipeline** additionally runs a *synthetic-gap evaluation*: it trains on the first
  80 % of the timeline, masks artificial gaps (sizes 1, 2, 3, 5) in the held-out tail, and reports
  MAE / RMSE / R² / bias per variable and gap size, plus an MAE-vs-gap-size plot.
- **Production** does **not** run that evaluation (the `RUN_EVALUATION` flag in `config.py` is
  defined but not wired to any code). Its safeguards are clipping, continuity correction, the
  provenance columns, and the offline evaluation done with the development pipeline.

## 4. Evolution of the project

```
Development pipeline (CSV → CSV)
        ↓
Gap detection + reconstruction (interpolation / ML / neighbours)
        ↓
Model & strategy validation (tournament, synthetic-gap evaluation)
        ↓
Production optimisation (fixed model per variable, neighbour cache,
incremental watermarks, bounded look-back window, thread pool)
        ↓
DynamoDB integration (source and destination)
        ↓
Dockerisation
        ↓
AWS infrastructure as code (Terraform)
        ↓
Scheduled weekly production processing
```

## 5. AWS production architecture

Everything below is taken from the code in `aws/production/`.

```
EventBridge Scheduler  (cron(0 2 ? * MON *), UTC)
        │  ecs:RunTask
        ▼
ECS Fargate — ONE task (image from ECR, ENTRYPOINT run_batch.py, 2 vCPU / 8 GB)
        │
        ├─ Sensor discovery ........ scan WS_Spatial_Neighbors (list_sensors.py)
        │
        └─ per sensor (ThreadPoolExecutor, MAX_WORKERS=4):
              1. incremental check ........ WS_Reconstruction_Metadata watermark vs newest source timestamp
                                            → skip if nothing new
              2. read target series ....... DynamoDB Query on the table resolved from the sensor's MQTT topic
                                            (last LOOKBACK_DAYS=60 days); previously reconstructed rows are dropped
              3. fast gap check ........... skip if no missing slots / rainfall NaNs
              4. read neighbours .......... up to 3 from WS_Spatial_Neighbors, cached per batch run
                                            (original observations only)
              5. reconstruct .............. interpolation → ML → neighbour blending → clip / continuity
              6. write-back ............... ONLY successfully reconstructed rows, into the SAME source table, as
                                            conditional PutItems (a real reading is never overwritten);
                                            unresolved slots are counted, not written
              7. update metadata .......... WS_Reconstruction_Metadata: watermark + GapCount / FilledCount / UnresolvedCount
        │
        ▼
   DynamoDB (reconstructed rows, flagged filled_flag=1)      CloudWatch Logs (/ecs/annam-recon, 14 days:
                                                              per-sensor filled / unresolved lines)
                                                              └ metric filter + alarm when a run has "0 ok"
```

**AWS services actually used**

| Service | Role in this repo |
|---|---|
| **DynamoDB** | Primary store: reads sensor tables and the neighbour table; writes successfully reconstructed rows back (conditional `PutItem`); holds the per-sensor metadata table (watermark + filled/unresolved counts, created by Terraform). |
| **Docker / ECR** | `aws/production/Dockerfile` (python:3.11-slim); Terraform creates the ECR repository (scan-on-push, keep last 5 images). |
| **ECS Fargate** | Runs one task per scheduled run (`run_batch.py` loops over all sensors in a single process). |
| **EventBridge Scheduler** | Weekly trigger (Mondays 02:00 UTC) that launches the Fargate task directly. |
| **CloudWatch** | Container logs (`awslogs`, 14-day retention), per-sensor timing/status lines, a log metric filter and an alarm for fully failed runs (optional SNS action). |
| **IAM** | Terraform creates execution, task and scheduler roles; the task policy is scoped to `WS_*` / `SSMet_*` tables, the watermark table and the optional debug bucket (`iam_task_policy.json` is a reference copy). |
| **S3** | *Optional only.* A debug dump of the full filled dataset/reports when `S3_REPORT_ENABLED=true` (off by default). Terraform also applies public-access block, encryption and lifecycle rules to an **existing** bucket. |
| **Terraform** | `aws/production/main.tf` defines all of the above in one file. |

**Not used by production:** Step Functions and Lambda. `run_sensor.py` (single-sensor
debugging) and `list_sensors.py` (sensor discovery) are plain Python scripts; the
`lambda_handler` left in `list_sensors.py` is not deployed. Step Functions exists only in the
[legacy material](#legacy-material).

## 6. Repository structure

```
iot-gap-filling-pipeline/
├── README.md
├── requirements.txt            runtime deps — development pipeline
├── requirements-dev.txt        + AWS deps, pytest, moto (full test suite)
├── run_pipeline.py             CLI for the development pipeline
├── config/
│   ├── config_sample.yaml      runnable against sample_data/
│   └── config_1.yaml           template with placeholder paths
├── src/                        development pipeline (gap_analysis, interpolation, model_selection,
│                               neighbor_reconstruction, validation, main)
├── sample_data/                two real sensor exports (216 = target, 255 = neighbour)
├── tests/                      pytest suite (pipeline + AWS layers, AWS mocked with moto)
├── aws/
│   ├── production/             ★ current production implementation
│   │   ├── DEPLOY.md  Dockerfile  main.tf  iam_task_policy.json  requirements.txt
│   │   └── config.py  io_dynamo.py  list_sensors.py  reconstruct.py  run_batch.py  run_sensor.py
│   └── *.py, *.json, *.md      legacy S3 + Step Functions layer (superseded)
├── infrastructure/             legacy CLI deployment scripts (superseded)
├── terraform/                  legacy modular Terraform (superseded)
├── Docs/                       legacy design/runbook documents (superseded)
└── .github/workflows/deploy.yml  CI (tests on push/PR); legacy deploy job is manual-only
```

## 7. Local setup (development pipeline)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run_pipeline.py --config config/config_sample.yaml   # ~5 minutes on the sample data
```

Outputs are written to `outputs/sample/` (filled dataset, audit, evaluation,
neighbour-quality and model-selection reports, and the MAE-vs-gap-size plot); trained models go to
`models/sample/`. To run on your own sensor, copy `config/config_1.yaml` and set
`input_paths`, `neighbor_distances` (neighbour ids and distances in km), and `output_paths`.
Expected CSV columns include `TimeStamp` plus the variables listed under `continuous_variables`.

## 8. AWS production setup

Prerequisites: AWS CLI configured, Docker, Terraform ≥ 1.5, existing DynamoDB source tables
and neighbour table, and an existing S3 bucket (see limitations). Full walkthrough:
**[`aws/production/DEPLOY.md`](aws/production/DEPLOY.md)**.

**Configuration** — edit `aws/production/config.py` (table names, `TOPIC_TO_TABLE`, key names,
model map) and the `locals` block in `main.tf` (`subnet_ids`, `security_groups`, `output_bucket`;
the AWS account id is resolved automatically). Values marked `# CONFIRM` in `config.py` are inferred
table names that must be verified for your environment. Runtime overrides are environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Region |
| `LOOKBACK_DAYS` / `START_DATE` | `60` / unset | Read window (`START_DATE` wins; `LOOKBACK_DAYS=0` = full history) |
| `INCREMENTAL_ENABLED` / `METADATA_TABLE` | `true` / `WS_Reconstruction_Metadata` | Skip sensors with no new data |
| `MAX_WORKERS` | `4` | Sensors processed in parallel |
| `NEIGHBOR_CACHE_ENABLED` | `true` | Reuse neighbour frames within a run |
| `PRODUCTION_MODE` / `CV_ENABLED` | `true` / `false` | Fixed model per variable vs. tournament + CV |
| `PERSIST_MODELS_ENABLED` / `MODEL_STORE_DIR` | `false` / `/tmp/annam_models` | Optional model reuse |
| `S3_REPORT_ENABLED` / `OUTPUT_BUCKET` | `false` / `annam-reconstructed` | Optional S3 debug dump |

**Docker** (the build context is `aws/production/`):

```bash
docker build -t annam-recon-worker aws/production
# Arguments go to run_batch.py. WARNING: with real credentials this reads AND WRITES your DynamoDB tables.
docker run --rm -e AWS_REGION=us-east-1 -e AWS_PROFILE=<profile> -v ~/.aws:/root/.aws:ro \
  annam-recon-worker --limit 1
```

Without Docker: `cd aws/production && pip install -r requirements.txt && python run_batch.py --limit 5`
(or `--keys "<id>#<topic>"`; `python run_sensor.py --device-key "<id>#<topic>"` for one sensor).

**Terraform** (creates real AWS resources — review the plan first):

```bash
cd aws/production
terraform init && terraform plan      # then: terraform apply
# build/push the image to the ECR URL from `terraform output ecr_repository_url`, run once manually, then let the schedule run
```

## 9. Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Includes `tests/test_aws_production.py`, which seeds a moto-mocked DynamoDB with synthetic sensors and
checks the production path: isolated → interpolation, medium → model, long → neighbours; unresolved
slots are not written and are counted separately (a sensor with only unresolved gaps is not counted
as reconstructed); reconstructed rows are excluded from later target/neighbour inputs; real readings
are never overwritten (including one arriving mid-run); rainfall is not zero-filled without evidence;
incremental skip and batch exit codes.
Nothing in the tests touches a real AWS account. The suite takes a few minutes (it trains models).

## 10. Limitations

- Reconstructed rows live in the same tables as raw readings (separation is by `filled_flag = 1` /
  `imputation_method`); consumers of those tables must filter on them. Unresolved slots are not stored
  at all — only counted in `WS_Reconstruction_Metadata` — and are retried only when new data arrives.
- Reconstructed rows are written one conditional `PutItem` at a time (batch writes cannot carry
  conditions), which is slower for very large gaps. If a real reading appears at a reconstructed
  slot's exact key it is kept and the slot is skipped. A real reading arriving with a *different*
  timestamp inside the same 5-minute slot leaves the earlier reconstructed row in the table (it is
  ignored by later runs, which only read original observations).
- Reconstructed rows are re-fitted from scratch on each run that sees new data, because earlier
  reconstructions are deliberately not reused as inputs.
- Rows written by older versions of this job (including `unresolved` placeholders) are excluded on
  read but are not deleted automatically.
- A missing rainfall value on an *original* row can still be zero-filled in memory when evidence exists, but
  only reconstructed rows are persisted, so that value is not written.
- No accuracy evaluation in the production path; a retry re-runs the whole batch (no checkpoint, mitigated by watermarks).
- Terraform looks up an existing S3 bucket even though S3 is only used for the optional debug dump.
- Table names for some stations in `config.py` are marked `# CONFIRM` and must be verified.
- Gap thresholds (1 / 5 slots) are hard-coded in the production path; reference variable differs between the two pipelines.
- The Dockerfile and Terraform were reviewed but not built/applied as part of preparing this repository (no AWS deployment was performed).

## 11. Future improvements

Separate offline model training from the scheduled job (persisted models are scaffolded but off by
default); optionally write reconstructed rows to a dedicated table and clean up legacy placeholder rows; wire up the
production evaluation flag; checkpoint/resume; CI-built images pushed to ECR; remove or archive the
legacy layer once no longer needed.

## Legacy material

`aws/*.py`, `aws/Dockerfile`, `aws/stepfunctions_definition.json`, `infrastructure/`, `terraform/`
and `Docs/` describe the **first** AWS design: CSVs in S3, DynamoDB metadata/results tables and a
Step Functions fan-out around `src/`. It is kept for reference (and `tests/test_aws_layer.py` still
covers it) but is **not** the production path. The GitHub Actions deploy job for it runs only when triggered manually.
