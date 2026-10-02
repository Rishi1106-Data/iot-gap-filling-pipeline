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
        ├─ for each sensor (sequential loop):
        │     read target series + 3 neighbors   (DynamoDB Query)
        │     reconstruct                          (interval → gaps → models → fill)
        │     write filled_dataset.parquet + reports  ──► S3
        ├─ write one batch-summary CSV            ──► S3
        └─ exit (non-zero only if EVERY sensor failed)
        │
        ▼
   S3 (Parquet output)  +  CloudWatch Logs (14-day retention)
```

## Files

| File | Purpose |
|---|---|
| `config.py` | All editable settings (table names, region, output bucket). **Start here.** |
| `io_dynamo.py` | DynamoDB reads, neighbor lookup, S3 output |
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
6. **Output bucket name** — `OUTPUT_BUCKET` (placeholder `annam-reconstructed`).

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
- The S3 output bucket must **already exist** (Terraform references it but never
  creates/destroys it, to protect your data):
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
ACCOUNT=975048338421
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
- `s3://<bucket>/reconstructed/device=<id>/filled_dataset.parquet` exists.
- `s3://<bucket>/reconstructed/_batch_runs/run_*.csv` (the batch summary) exists.

### 5. Let the schedule run
The EventBridge Scheduler rule (`annam-recon-weekly`, Mondays 02:00 UTC) now
triggers the task automatically. Adjust `local.schedule_expression` to retime.

## Cost notes (143 sensors, weekly ≈ 4.33 runs/month, us-east-1)

| Service | Estimate | Notes |
|---|---|---|
| Fargate 2 vCPU / 8 GB | ~$0.30–0.70/mo (short history) | Billed per-second only while running. Long history pushes this up; ARM64 saves ~20%. |
| DynamoDB (on-demand) | ~$0.10/mo | A handful of reads per sensor weekly. **Confirm tables are PAY_PER_REQUEST.** |
| S3 (PUT + storage) | ~$0.20–0.40/mo | Parquet + reports; lifecycle keeps storage flat. |
| CloudWatch Logs | ~$0.10/mo | 14-day retention; chatty modules logged at WARNING. |
| ECR storage | ~$0.05/mo | One image, lifecycle keeps last 5. |
| **Total** | **~$0.80–1.40/mo** | |

**Avoid:** a NAT gateway (~$32/mo — use VPC endpoints), provisioned DynamoDB
capacity on tables that sit idle all week, and unbounded log retention.

## Reliability & monitoring

- **Per-sensor isolation:** one sensor failing is caught and recorded; the batch
  continues. The task exits non-zero only if **every** sensor failed.
- **Partial failures look "green"** at the task level by design. To see them,
  read the batch summary CSV in `s3://<bucket>/reconstructed/_batch_runs/`.
- **Failure alarm:** `main.tf` creates a CloudWatch alarm on a "0 ok" run. Wire
  `alarm_sns_topic_arn` to actually get notified.
- **Retries:** the scheduler retries a failed *launch* twice. A retry re-runs the
  ENTIRE batch from the first sensor (no checkpoint); already-written sensors are
  recomputed and overwritten — wasteful but safe.

## Known limitations / future work
- Models are trained per sensor per run (as in the notebook). Splitting training
  into a separate cached job would cut runtime for long histories.
- No checkpoint/resume; a mid-batch failure reprocesses from the start on retry.
- The synthetic-gap evaluation is omitted from the production path for speed; run
  it offline with `RUN_EVALUATION=true` when validating accuracy.
