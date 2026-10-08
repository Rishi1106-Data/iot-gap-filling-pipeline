# Annam Sensor Reconstruction — AWS Deployment Guide

This package runs the reconstruction notebook as an automated **weekly batch**
on AWS. **One** Fargate task reconstructs **all** sensors sequentially in a
single process; EventBridge Scheduler triggers it once a week. There is no Step
Functions, no Lambda, and no per-sensor fan-out — that keeps the bill near zero
and the moving parts minimal for a <200-sensor, weekly workload.

```
EventBridge Scheduler (weekly cron)
        │  ecs:RunTask
        ▼
ECS Fargate — ONE task (run_batch.py)
        ├─ list all sensor keys      (scan WS_Spatial_Neighbors)
        ├─ for each sensor (thread pool, MAX_WORKERS, default 4):
        │     incremental check      (skip if no new data since last watermark)
        │     read target series     (DynamoDB Query, last LOOKBACK_DAYS days; rows this job
        │                             wrote earlier are excluded — originals only)
        │     fast gap check         (skip if nothing to fill)
        │     read neighbours        (WS_Spatial_Neighbors + cached DynamoDB Query)
        │     reconstruct            (interval → gaps → interpolation / ML / neighbours)
        │     write ONLY successfully reconstructed rows back to the SAME source table
        │                            (conditional PutItem: never overwrites a real reading;
        │                             unresolved slots are counted, not written)
        │     update metadata        (WS_Reconstruction_Metadata: watermark, GapCount,
        │                             FilledCount, UnresolvedCount)
        └─ log batch summary; exit non-zero only if EVERY sensor failed
        │
        ▼
   DynamoDB (source tables + metadata)  +  CloudWatch Logs (14-day retention)
   (S3 is used only for an optional debug dump, off by default)
```

## Files

| File | Purpose |
|---|---|
| `config.py` | All editable settings (table names, region, output bucket). **Start here.** |
| `io_dynamo.py` | DynamoDB reads, neighbor lookup/cache, conditional write-back of reconstructed rows, exclusion of earlier reconstructions on read, metadata; optional S3 debug dump |
| `reconstruct.py` | The reconstruction logic (ported from the notebook, unchanged) |
| `run_batch.py` | **Entrypoint** — reconstructs ALL sensors in one task |
| `run_sensor.py` | Local single-sensor debugging helper (not used by the schedule) |
| `list_sensors.py` | `list_sensor_keys()` — imported by `run_batch.py` to enumerate sensors |
| `Dockerfile` | Container image for the Fargate task |
| `requirements.txt` | Python dependencies |
| `main.tf` | **All AWS infrastructure** (ECR, ECS, IAM, scheduler, log group, alarm, S3 hardening) |
| `iam_task_policy.json` | Reference copy of the task-role policy (authoritative version is in `main.tf`) |

## Before you deploy — confirm these with your team

The pipeline runs on the values in `config.py`, but a few were inferred and
should be verified (search for `# CONFIRM` in `config.py`):

1. **Which data table is the input** for each station/topic (`TOPIC_TO_TABLE`).
   Confirmed: `WS_SSMet_0126_Data`, `WS_SSMet_1225_Data`, `WS_Campus_Data`. The
   Annam_0426 / Annam_0526 / Polytechnic table names are guesses.
2. **`WS_Data_Full` vs `WS_Data_30_Days`** — which is the canonical source.
3. **`gateway-prediction`** — input or prediction output? (Likely output; not
   used as input here, but the IAM policy allows reading it just in case.)
4. **Which neighbor table is current** — `WS_Spatial_Neighbors` vs `_0/_1/_2`.
5. **IMEI handling** — some sensors include `IMEINumber` (two physical units per
   DeviceId), some do not. Default auto-picks the IMEI reporting temperature
   most often; sensors with no IMEI column are handled automatically.
6. **Debug bucket name** — `OUTPUT_BUCKET` (placeholder `annam-reconstructed`). Used only when `S3_REPORT_ENABLED=true`, but `main.tf` currently looks the bucket up with a data source, so it must exist for `terraform apply` to succeed.

## Validated against real data

Both sample sensors were run end-to-end through `reconstruct.py` before release:

| Sensor | Columns | History | Rows | Runtime | Peak memory |
|---|---|---|---|---|---|
| 205 (SSMet_0126) | 40 | 16 days | 4,485 | ~8 s | ~290 MB |
| 201 (Annam) | 13 | 61 days | 17,588 | ~52 s | ~340 MB |

Key takeaways baked into the config:
- The code uses **only the weather columns that are present**, so wildly
  different schemas (40 cols vs 13) both reconstruct cleanly.
- **Runtime scales with history length.** 2 months ≈ 52 s/sensor. A full year
  would be ~6–7x that. At 143 sensors that is a ~40-min batch for short history
  but could approach several hours for a full year of 5-min data per sensor.
  If your sensors carry long histories, set `START_DATE` in `config.py` to bound
  it, or raise `task_cpu` / `task_memory` in `main.tf`.
- Memory peak stays well under 1 GB per sensor; the 8 GB task has wide headroom.

## Step-by-step deploy

### 0. Prerequisites
- AWS CLI configured (`aws configure`), Docker installed, Terraform ≥ 1.5.
- The S3 bucket named in `main.tf` must **already exist** (Terraform references it but never
  creates/destroys it). It is only written to when the optional debug dump is enabled:
  ```bash
  aws s3 mb s3://annam-reconstructed --region us-east-1
  ```
  Put the real name in both `config.py` (`OUTPUT_BUCKET`) and `main.tf`
  (`local.output_bucket`).

### 1. Fill in the four REPLACE values in `main.tf`
In the `locals` block at the top:
- `subnet_ids` — a subnet that can reach DynamoDB and S3.
- `security_groups` — a security group allowing outbound 443.
- `output_bucket` — your real bucket name (must match `config.py`).

The AWS account id is read automatically from your credentials (`aws_caller_identity`).
- `alarm_sns_topic_arn` — optional; an SNS topic to notify on a fully-failed run.

**Networking — cheapest correct option:** use a subnet that has **VPC gateway
endpoints** for S3 and DynamoDB. Gateway endpoints are free and need no NAT
gateway. Do **not** add a NAT gateway — it costs ~$32/month and would be ~30x
the entire rest of this bill. If you instead use a public subnet, keep
`assign_public_ip = true` and ensure the security group allows outbound 443.

### 2. Apply the infrastructure
```bash
terraform init
terraform apply
```
This creates the ECR repository, ECS cluster, IAM roles, log group (14-day
retention), the weekly schedule, a failure alarm, and hardens the S3 bucket
(public-access block, default encryption, 180-day lifecycle on `reconstructed/`).
Note the `ecr_repository_url` output.

### 3. Build & push the container image
Use the ECR URL from the apply output.
```bash
REGION=us-east-1
REPO_URL=$(terraform output -raw ecr_repository_url)

aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin "${REPO_URL%/*}"

# x86 (matches the Terraform default cpu_architecture = "X86_64"):
docker build -t "$REPO_URL:latest" .
docker push "$REPO_URL:latest"
```
**If you switched `cpu_architecture` to `"ARM64"` in `main.tf`** for the ~20%
saving, build for arm64 instead — and confirm xgboost/lightgbm/pyarrow wheels
resolve for aarch64:
```bash
docker buildx build --platform linux/arm64 -t "$REPO_URL:latest" --push .
```
Build/runtime architecture mismatch is the #1 cause of "task won't start
(exec format error)". Pick one and keep Dockerfile + Terraform in agreement.

### 4. Run it once by hand to validate
Before trusting the schedule, run the batch manually and watch it land output:
```bash
terraform output -raw manual_run_command   # prints a ready-to-run command
# ...paste and run it...
```
Then confirm:
- Logs in CloudWatch group `/ecs/annam-recon` show `Batch done ... N ok`.
- Reconstructed rows exist in the source DynamoDB tables (items with `filled_flag = 1` and an `imputation_method` of `interpolation` / `model` / `neighbor`). There are no `unresolved` items.
- `WS_Reconstruction_Metadata` has one item per processed sensor with `LastDataTimestamp`, `GapCount`, `FilledCount` and `UnresolvedCount`.
- CloudWatch shows a `RECONSTRUCTED device=... filled=N unresolved=M` line per sensor, an `UNRESOLVED ...` warning when `M > 0`, and `filled slots=... unresolved slots=...` on the `Batch done` line.

### 5. Let the schedule run
The EventBridge Scheduler rule (`annam-recon-weekly`, Mondays 02:00 UTC) now
triggers the task automatically. Adjust `local.schedule_expression` to retime.

## Cost notes (143 sensors, weekly ≈ 4.33 runs/month, us-east-1)

| Service | Estimate | Notes |
|---|---|---|
| Fargate 2 vCPU / 8 GB | ~$0.30–0.70/mo (short history) | Billed per-second only while running. Long history pushes this up; ARM64 saves ~20%. |
| DynamoDB (on-demand) | ~$0.10/mo | A handful of reads per sensor weekly. **Confirm tables are PAY_PER_REQUEST.** |
| S3 | ~$0 | Not written to in normal runs (debug dump disabled by default). |
| CloudWatch Logs | ~$0.10/mo | 14-day retention; chatty modules logged at WARNING. |
| ECR storage | ~$0.05/mo | One image, lifecycle keeps last 5. |
| **Total** | **~$0.80–1.40/mo** | |

**Avoid:** a NAT gateway (~$32/mo — use VPC endpoints), provisioned DynamoDB
capacity on tables that sit idle all week, and unbounded log retention.

## Reliability & monitoring

- **Per-sensor isolation:** one sensor failing is caught and recorded; the batch
  continues. The task exits non-zero only if **every** sensor failed.
- **Partial failures look "green"** at the task level by design. To see them,
  read the per-sensor `[i/N] ... -> status` lines in CloudWatch Logs.
- **Failure alarm:** `main.tf` creates a CloudWatch alarm on a "0 ok" run. Wire
  `alarm_sns_topic_arn` to actually get notified.
- **Retries:** the scheduler retries a failed *launch* twice. A retry re-runs the
  ENTIRE batch from the first sensor (no checkpoint); the incremental watermark
  skips sensors already completed, and re-written filled rows reuse the same keys.

## Known limitations / future work
- A predetermined model per variable is fitted per sensor per run (no tournament in `PRODUCTION_MODE`). Splitting training
  into a separate cached job would cut runtime for long histories.
- No checkpoint/resume; a mid-batch failure reprocesses from the start on retry.
- The synthetic-gap accuracy evaluation exists only in the local pipeline
  (`src/validation.py`). The production path does not run it: `RUN_EVALUATION` is
  defined in `config.py` but is not used by any production code.
- Slots with no usable information are `unresolved`: they are **not** written to the
  sensor table; they are counted (`UnresolvedCount`) and logged. `filled_flag` keeps its
  meaning (0 = original, 1 = non-original); the writer persists only rows whose
  `imputation_method` is `interpolation` / `model` / `neighbor` and whose reference
  value is non-null. A sensor with only unresolved gaps is not counted as reconstructed.
- Original sensor observations are the source of truth: rows with `filled_flag = 1` (or a
  reconstruction / `unresolved` `imputation_method`) are excluded when series are loaded, so
  earlier reconstructions are never reused as training data or neighbour evidence. The cost is
  that each run with new data re-fits the whole look-back window.
- Reconstructed rows share the table with raw readings; consumers must filter on
  `filled_flag` / `imputation_method`. Rows are written with conditional `PutItem`
  (create if the key is free or the existing item is itself a reconstruction), so a real
  reading is never overwritten; such slots are skipped and logged. This is one request per
  row (batch writes cannot carry conditions); the task role therefore needs only `dynamodb:PutItem`.
- Rainfall is zero-filled only with observed evidence (an observed value within ±3 slots or a
  neighbour at that slot, none positive); unresolved slots never receive a rainfall value.
- The same exclusion applies to the incremental "new data?" probe and to the identity attributes copied onto
  reconstructed rows: both look only at the newest *original* item, so a reconstructed row can never look like
  new data or supply identity attributes.
- The optional S3 debug dump (`S3_REPORT_ENABLED=true`, off by default) contains the full in-memory frame,
  including unresolved rows (as empty values). It is a debugging aid and is never the sensor table.
- Rows written by older versions of this job (including `unresolved` placeholders) are excluded on read
  but not deleted automatically.
