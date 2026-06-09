# AWS Deployment Layer — Annam AI Gap-Filling Pipeline

This deployment layer wraps your **existing, unchanged** pipeline (`src/`,
`run_pipeline.py`) for cost-efficient batch processing on AWS Fargate. Your
business logic was treated as production-ready and is deployed byte-for-byte
identical (verified by md5 cross-check).

It integrates through the pipeline's verified public contract **only**:

```
main.run_pipeline(config: dict) -> (filled_df, eval_df, execution_summary: dict)
```

---

## File map (what each new file is and why it exists)

### `aws/` — application layer (runs inside the container)
| File | Purpose |
|---|---|
| `config.py` | Central, env-driven settings (`AwsConfig`). One image, every environment. S3 key helpers. |
| `cloudwatch_logger.py` | Structured JSON logs + **EMF metrics** (free CloudWatch metric extraction; no PutMetricData bill). |
| `io_s3.py` | `load_dataset_from_s3`, `load_neighbor_cached` (per-task disk cache), `load_models_from_s3`, `save_filled_data_to_s3`, `save_reports_to_s3`, `save_models_to_s3`. |
| `io_dynamo.py` | `get_sensor_metadata`, `get_neighbor_sensors` (zero extra reads), `list_active_sensors` (GSI Query, no Scan), `write_gap_fill_results` (BatchWriteItem, TTL). Full table schema in the module docstring. |
| `list_sensors.py` | `list_active_sensors` + `create_sensor_batches` (clusters by shared neighbours for cache hits). Writes batches to S3 for Step Functions. |
| `run_batch.py` | **Fargate entrypoint.** Processes many sensors per task; fault-isolated; reuses neighbour cache; calls your `run_pipeline` unchanged. |
| `Dockerfile` | Multi-stage, Python 3.11, non-root, `libgomp1` for xgboost/lightgbm. WORKDIR = repo root (required by the import structure). |
| `requirements-aws.txt` | boto3 only — your pipeline deps stay the single source of truth. |
| `stepfunctions_definition.json` | ASL: discover → batch → Distributed Map over Fargate Spot → summarize, with retries + SNS failure path. |
| `PRODUCTION_HARDENING.md` | Phase 12: measured baseline, cost tables, scalability/reliability/security review, final architecture. |

### `infrastructure/` — infrastructure as code (deployed via CLI)
| Path | Purpose |
|---|---|
| `dynamodb/create_tables.sh` | Creates both tables + `gsi_status` GSI + TTL. |
| `dynamodb/seed_sensors.py` | Seeds metadata + computes neighbour topology from lat/long (haversine). |
| `ecs/taskdef-run-batch.json` | Batch task: 1 vCPU / 2 GB (sized from measured 257 MB RSS). |
| `ecs/taskdef-list-sensors.json` | Discovery task: 0.25 vCPU / 0.5 GB. |
| `ecs/deploy_cluster.sh` | Cluster + Fargate/Fargate-Spot capacity providers + log groups. |
| `eventbridge/deploy_schedules.sh` | Daily training + optional hourly inference schedules. |
| `iam/policies.json` | Four least-privilege policies (task, execution, step functions, scheduler). |
| `stepfunctions/deploy_statemachine.sh` | Substitutes placeholders + creates/updates the state machine. |

### `.github/workflows/deploy.yml`
Test → build image → push to ECR → register task defs → deploy state machine.
Deploy gated on `main` + passing tests. OIDC auth (no static keys).

### `tests/test_aws_layer.py`
12 moto-based tests for the AWS layer (config, batching, DynamoDB, S3 caching,
run_batch fault isolation). Your existing pipeline tests are untouched and still
run in CI if present.

---

## Migration steps (zero changes to `src/`)

```bash
# 0. Prereqs: AWS CLI v2, Docker, an ECR repo named annam-gapfill, a data bucket.
export AWS_REGION=ap-south-1
export DATA_BUCKET=your-annam-bucket
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

# 1. DynamoDB tables
bash infrastructure/dynamodb/create_tables.sh

# 2. Seed sensors + neighbour topology from a coords CSV (device_id,latitude,longitude)
python infrastructure/dynamodb/seed_sensors.py --from-coords sensors.csv --k 2 --max-km 30

# 3. Upload each sensor's history to s3://$DATA_BUCKET/raw/<id>/<id>.csv
#    (matches config.raw_key()). Your CSV schema is already what the pipeline reads.

# 4. Build + push the image
aws ecr get-login-password | docker login --username AWS --password-stdin \
    $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com
docker build -f aws/Dockerfile -t annam-gapfill .
docker tag annam-gapfill $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/annam-gapfill:latest
docker push $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/annam-gapfill:latest

# 5. IAM roles — create from infrastructure/iam/policies.json (substitute ${...}).

# 6. ECS cluster + task definitions
bash infrastructure/ecs/deploy_cluster.sh

# 7. State machine (export the ARNs/subnets/SG/topic it needs first)
bash infrastructure/stepfunctions/deploy_statemachine.sh

# 8. Schedules
bash infrastructure/eventbridge/deploy_schedules.sh

# 9. Smoke test one batch locally (no AWS calls to the pipeline itself):
#    docker run --rm -e DATA_BUCKET=$DATA_BUCKET -e AWS_REGION=$AWS_REGION \
#        annam-gapfill python aws/run_batch.py --device-ids 201
```

### Placeholders you must supply before deploy
`${AccountId}`, `${Region}`, `${DataBucket}`, VPC `${PrivateSubnet1/2}`,
`${TaskSecurityGroup}`, the ECS cluster/task-def ARNs, and `${FailureTopicArn}`
(SNS). The deploy scripts read these from environment variables.

### One required network addition
Add **VPC endpoints** for S3, DynamoDB, ECR, and CloudWatch Logs so private-subnet
tasks reach AWS without a NAT gateway (NAT data-processing would otherwise become
the largest line item at scale). This needs your VPC IDs, so it isn't scripted here.

---

## Key design decisions (and the evidence behind them)

- **Both train + inference modes**, selected per run via `TRAIN_MODE` env →
  `execution.train_mode`. (Your evaluation always re-fits, so inference isn't
  much cheaper — see `PRODUCTION_HARDENING.md` §0.)
- **Neighbour cache on local task disk** (`/tmp/iot/cache/neighbors/`): each
  unique neighbour CSV is downloaded once per task and reused across all targets
  in the batch that share it.
- **Neighbour topology in DynamoDB** as a metadata attribute, so discovering a
  sensor's neighbours costs **zero** reads beyond the one metadata GetItem.
- **`quality_reference` always includes `CorrectedHeatIndex`** because
  `neighbor_reconstruction.py:78` hardcodes that key. The AWS layer injects it
  regardless of per-sensor metadata, so a run never crashes on a missing key.
- **WORKDIR = repo root** in the container, because `main.py` uses
  `from src import ...` while `neighbor_reconstruction.py` uses
  `from gap_analysis import ...`; both resolve only from the repo root with
  `src` on `sys.path`. `run_batch.py` replicates that exact setup.
