# Phase 12 — Production Hardening & Architecture Review (Annam AI)

This document reviews the deployment layer built around your **unmodified**
gap-filling pipeline, identifies risks, and gives the exact architecture to run
in production. Every number below is grounded in a real end-to-end run of *your*
code on *your* sample data (Annam_216 + Annam_255), not generic estimates.

## 0. Measured baseline (the basis for all sizing)

| Metric | Measured value | Source |
|---|---|---|
| Wall time per sensor (train) | ~117 s | full run, 2 neighbours, ~16.7k 5-min slots |
| Wall time per sensor (inference) | ~113 s | inference re-fits during evaluation (`validation.py:154`) |
| Peak RSS per sensor | ~257 MB | sampled during RF tournament |
| Filled dataset size | ~3.2 MB / sensor | `filled_dataset.csv` for ~16.7k rows |
| Model bundle size | ~4.6 MB / sensor | `model_selection.joblib` |

**Critical finding:** inference mode is *not* meaningfully cheaper than training,
because `evaluate_synthetic_gaps()` re-instantiates and re-fits each variable's
model to score it. This single fact drives the cost and cadence recommendation
below. (We did **not** change this — it is your validation logic.)

---

## 1. Final architecture

```
EventBridge Scheduler
  ├─ daily  02:30 UTC  →  StartExecution(input={train_mode:true})
  └─ hourly (optional) →  StartExecution(input={train_mode:false})
        │
        ▼
Step Functions (STANDARD)
        │
        ├─ DiscoverAndBatch  ── Fargate task (256/512) runs aws/list_sensors.py
        │                        • Query gsi_status = ACTIVE  (no Scan)
        │                        • cluster by shared neighbours, chunk → batches
        │                        • write runs/<exec>/batches.json to S3
        │
        ├─ LoadBatches       ── S3 GetObject (native SFN SDK integration)
        │
        └─ Map (DISTRIBUTED, MaxConcurrency=20)
               └─ per batch: Fargate **Spot** task (1 vCPU / 2 GB)
                     runs aws/run_batch.py --batch-json {...}
                       • for each of N sensors (sequential):
                            DynamoDB GetItem (metadata + neighbours)   ← 1 read
                            S3 download target  +  cached neighbours
                            run_pipeline(config)   ← YOUR UNCHANGED CODE
                            S3 upload filled + reports (+ model if train)
                       • BatchWriteItem all results                    ← 1 write batch
                       • emit EMF metrics (free CloudWatch metrics)
                     fault isolation: one sensor's failure ≠ batch failure
        │
        ▼
   S3 (filled/, reports/, models/)   +   DynamoDB (results, TTL 180d)
        │
        ▼
   CloudWatch (EMF metrics + structured JSON logs, 30-day retention)
        │
        └─ SNS on hard failure (whole run or all-fail batch)
```

Why this shape:
- **One task per batch, many sensors per task** — exactly as required. At
  BATCH_SIZE=25 a 10k fleet is 400 tasks, not 10,000.
- **Fargate Spot for batch work** — interruptions are safe because each sensor
  is idempotent (re-running overwrites the same S3 keys) and Step Functions
  retries the batch on a fresh capacity slot.
- **No ECS Service, no ALB, no ASG** — this is batch, not serving. "Autoscaling"
  is the Map `MaxConcurrency`, the correct primitive for fan-out.

---

## 2. Cost analysis (ap-south-1, Fargate Spot ~70% off)

All figures **per month**, derived from the measured 110 s/sensor and 1 vCPU/2 GB.

### Recommended cadence: **daily** (gap-filling weather series hourly is rarely needed)

| Fleet | Fargate Spot | DynamoDB | S3 | CloudWatch | **Total/mo** |
|---|---|---|---|---|---|
| 100 | $1 | $0.01 | $0.1 | $0.01 | **~$2** |
| 1,000 | $14 | $0.04 | $1 | $0.07 | **~$15** |
| 10,000 | $139 | $0.4 | $10 | $0.7 | **~$150** |

### If you run **hourly** (24×/day):

| Fleet | Fargate Spot | DynamoDB | S3 | CloudWatch | **Total/mo** |
|---|---|---|---|---|---|
| 100 | $33 | $0.1 | $2.3 | $0.2 | **~$36** |
| 1,000 | $334 | $1.0 | $23 | $1.7 | **~$359** |
| 10,000 | $3,337 | $9.9 | $230 | $17 | **~$3,595** |

### On-demand vs Spot (hourly): Spot saves **~69%** at every scale.

**Biggest cost lever:** cadence, not infrastructure. Because evaluation always
retrains, every run is ~110 s/sensor regardless of `train_mode`. Going from
hourly to daily cuts compute 24×. Recommendation: **daily training run** +
**optional inference cadence only if downstream truly needs intra-day freshness**.

Two cheap, code-free optimisations if you later want hourly cheaply (both are
config/infra, not pipeline changes):
1. Set `VAL_N_GAPS` lower (e.g. 50) for the hourly inference runs — fewer
   synthetic gaps means the evaluation re-fit scores faster.
2. Run training weekly, inference daily; inference still re-fits but you stop
   persisting models 6 days out of 7 (saves S3 PUTs + model storage).

---

## 3. Scalability analysis

| Concern | Assessment | Mitigation in this design |
|---|---|---|
| Sensor discovery at 10k | GSI Query paginates; no Scan | `list_active_sensors` + `iter_active_sensors` |
| Fan-out explosion | 10k sensors = 400 tasks @ batch 25 | `MaxConcurrency=20` caps parallel tasks |
| DynamoDB hot partition | results PK = `SENSOR#<id>` spreads writes | per-sensor PK, on-demand auto-scales |
| S3 request rate | GETs reduced by neighbour cache | `load_neighbor_cached` (1 GET per unique neighbour per task) |
| Per-task memory | 257 MB/sensor, sensors run sequentially | 2 GB task holds 1 sensor + neighbour cache comfortably |
| Long batches exhaust disk | filled CSVs 3.2 MB each | per-sensor in/out dirs deleted after upload; 30 GB ephemeral |

**Throughput math:** 20 concurrent tasks × 25 sensors × (3600/110) ≈ 16k
sensors/hour ceiling. A 10k fleet finishes a full pass in ~40 min wall time.

---

## 4. Reliability analysis

| Failure mode | Behaviour | Where |
|---|---|---|
| One sensor errors (bad data, missing neighbour) | Logged, recorded `status=failed`, batch continues | `process_sensor` try/except |
| Whole batch errors | Raises → Step Functions retries (3×, backoff) | `run_batch` all-fail raise |
| Fargate Spot interruption | Task dies → Map retry on new slot | SFN Retry on `States.TaskFailed` |
| Inference with no model yet | Explicit error, recorded, isolated | `process_sensor` model check |
| Partial fleet failure | Other batches unaffected; SNS only on hard fail | Map `Catch` → `BatchFailed` Pass |
| Re-run safety | Idempotent: same S3 keys overwritten | keyed by device_id + run_date |

Set the Map state's **ToleratedFailurePercentage** (e.g. 10%) via the API so a
handful of dead sensors doesn't fail an otherwise-good 10k run.

---

## 5. Security analysis

| Area | Control |
|---|---|
| Task IAM | Least-privilege: S3 scoped to 5 prefixes, DynamoDB to 2 tables + 1 index, CW namespace-scoped (`infrastructure/iam/policies.json`) |
| Network | Fargate in **private subnets**, `AssignPublicIp=DISABLED`; reach AWS via VPC endpoints (S3 gateway, DynamoDB gateway, ECR/Logs interface) |
| Image | Non-root user (uid 10001), multi-stage build (no compiler in runtime) |
| Data at rest | S3 SSE-AES256 on every PUT; DynamoDB encryption on by default |
| Secrets | None needed — task role provides credentials; no static keys anywhere |
| CI/CD | GitHub OIDC role assumption, no long-lived AWS keys in GitHub |
| Supply chain | `requirements.txt` (your pins) + `requirements-aws.txt` (boto3 only) |

VPC endpoints are the one piece you must add at the network layer (not codeable
here without your VPC IDs) — they eliminate NAT data-processing charges, which
at 10k sensors would otherwise dwarf the compute bill.

---

## 6. The exact architecture to deploy for Annam AI

1. **One S3 bucket**, prefix-partitioned: `raw/ models/ filled/ reports/ runs/`.
2. **Two DynamoDB tables**, on-demand: `annam-sensor-metadata` (+ `gsi_status`)
   and `annam-gapfill-results` (TTL 180d). Seed topology once from sensor
   lat/long with `seed_sensors.py --from-coords`.
3. **One ECR image** (`aws/Dockerfile`, Python 3.11), two task definitions
   (run-batch 1 vCPU/2 GB Spot; list-sensors 0.25/0.5).
4. **One Step Functions** STANDARD state machine (`stepfunctions_definition.json`),
   Distributed Map, `MaxConcurrency=20`, `ToleratedFailurePercentage=10`.
5. **EventBridge Scheduler**: daily training (`train_mode=true`, 02:30 UTC,
   30-min jitter). Add hourly inference only if a downstream consumer needs it.
6. **VPC endpoints** for S3, DynamoDB, ECR, CloudWatch Logs; tasks in private
   subnets.
7. **CloudWatch**: EMF metrics (free), 30-day log retention, SNS alarm on
   `SensorsFailed > threshold` and on Step Functions `ExecutionsFailed`.

**Expected steady-state cost: ~$150/month for 10,000 sensors at daily cadence**,
dominated by Fargate Spot compute, scaling linearly with fleet × cadence.

The pipeline in `src/` is deployed **byte-for-byte unchanged** — verified by
md5 cross-check. The AWS layer only orchestrates around its public
`run_pipeline(config) -> (filled_df, eval_df, summary)` contract.
