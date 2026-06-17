# Terraform Extensions — Annam AI Pipeline

These files **extend** the existing `terraform/` infrastructure without
modifying any existing file. Drop them into your repo alongside the existing
Terraform directory.

---

## What's added

### New modules (in `modules/`)

| Module | Resources created | Purpose |
|---|---|---|
| `model_registry` | 1 DynamoDB table + 2 GSIs | Train-once/infer-many: per-sensor model tracking, version history, training schedule |
| `drift_detection` | 1 Step Functions EXPRESS machine + 1 EventBridge schedule + 2 IAM roles + 2 CW alarms | Autonomous retraining trigger based on schedule + MAE drift |
| `cost` | 1 DynamoDB table + 1 CW dashboard + 1 AWS Budget + 2 CW alarms | Cost visibility, per-sensor attribution, spend alerts |

### New environment files (in `environments/prod/`)

| File | Purpose |
|---|---|
| `extensions.tf` | Wires the three new modules into the prod environment; attaches IAM policies; creates SSM parameters |
| `variables_extensions.tf` | New variables with defaults (no required values) |
| `outputs_extensions.tf` | New outputs for all extension resources |

---

## File placement

```
your-repo/
├── terraform/                          ← EXISTING (unchanged)
│   ├── modules/
│   │   ├── networking/
│   │   ├── s3/
│   │   ├── ecr/
│   │   ├── dynamodb/
│   │   ├── iam/
│   │   ├── ecs/
│   │   ├── stepfunctions/
│   │   ├── eventbridge/
│   │   └── cloudwatch/
│   └── environments/
│       ├── dev/
│       ├── staging/
│       └── prod/
│           ├── main.tf                 ← EXISTING (unchanged)
│           ├── variables.tf            ← EXISTING (unchanged)
│           ├── outputs.tf              ← EXISTING (unchanged)
│           └── terraform.tfvars        ← EXISTING (unchanged)
│
└── terraform-extensions/               ← ADD THESE FILES
    ├── modules/
    │   ├── model_registry/
    │   │   ├── main.tf
    │   │   ├── iam_extension.tf        ← exports task_policy_json output
    │   │   ├── variables.tf
    │   │   ├── outputs.tf
    │   │   └── versions.tf
    │   ├── drift_detection/
    │   │   ├── main.tf
    │   │   ├── variables.tf
    │   │   ├── outputs.tf
    │   │   └── versions.tf
    │   └── cost/
    │       ├── main.tf
    │       ├── iam_extension.tf        ← exports task_policy_json output
    │       ├── variables.tf
    │       ├── outputs.tf
    │       └── versions.tf
    └── environments/
        └── prod/
            ├── extensions.tf           ← ADD to environments/prod/
            ├── variables_extensions.tf ← ADD to environments/prod/
            └── outputs_extensions.tf   ← ADD to environments/prod/
```

**After copying**, your `environments/prod/` folder will contain:
- `main.tf` (existing, unchanged)
- `variables.tf` (existing, unchanged)
- `outputs.tf` (existing, unchanged)
- `terraform.tfvars` (existing, unchanged)
- `extensions.tf` ← new
- `variables_extensions.tf` ← new
- `outputs_extensions.tf` ← new

Terraform treats all `.tf` files in a directory as one configuration.
No existing file needs to be edited.

---

## Apply order

```bash
cd terraform/environments/prod

# 1. Apply model_registry and cost first (no dependencies on each other)
terraform apply -target=module.model_registry -target=module.cost

# 2. Apply drift_detection (depends on model_registry and cost outputs)
terraform apply -target=module.drift_detection

# 3. Apply remaining resources (IAM policies, SSM params, drift scheduler policy)
terraform apply
```

After a clean initial deploy, subsequent `terraform apply` runs with no flags
work correctly — Terraform resolves the dependency graph automatically.

---

## Model Registry: how train-once/infer-many works

The registry adds a per-sensor training decision to the pipeline:

```
Before (current): Every daily run → train ALL sensors (tournament + retrain)
After:            drift-check at 01:00 UTC → query registry
                  → sensors with force_retrain=true OR overdue schedule: RETRAIN
                  → all others: INFER (load saved model, skip tournament)
```

The `run_batch.py` container reads `MODEL_REGISTRY_TABLE` (from SSM or env var)
and for each sensor checks:
- Is there an ACTIVE record for this sensor?
- Is `next_train_after` in the past? → schedule-based retrain
- Is `force_retrain = "true"`? → drift-triggered retrain
- Otherwise: inference mode (load model from S3, skip tournament)

The registry is updated by `run_batch.py` after each successful run:
- On training: writes MODEL#<id>/VAR#<var>#ACTIVE with new MAE, sets `next_train_after`
- On inference: updates `last_inferred_at` and rolling MAE estimate

**Expected cost saving:** ~85% reduction in training compute at 7-day retrain
interval with 20% MAE drift threshold (vs. training all sensors daily).

---

## Drift detection: what triggers retraining

Two triggers, checked daily at 01:00 UTC (90 min before main pipeline):

1. **Schedule-based**: `next_train_after <= now` (default: 7 days from last train)
2. **Drift-based**: `force_retrain = "true"` — written by `run_batch.py` when
   rolling evaluation MAE exceeds `baseline_mae × (1 + drift_mae_threshold_pct/100)`

The drift check fires a targeted training run with only the flagged sensors,
not the full fleet — reducing per-trigger cost proportionally.

---

## Cost dashboard: key metrics to watch

| Metric | Healthy | Investigate if |
|---|---|---|
| `CostPerSensorUSD` | ~$0.000477 | >$0.005 (10x) |
| `TaskCostUSD` daily | <$15 for 1000 sensors | >threshold alarm |
| `DriftForcedRetrainCount` | <5% of fleet/day | >20% → data quality issue |
| `SensorsFailed` | 0 | >5 → check run_batch logs |
| Budget | <80% | >80% actual → spending too fast |

---

## What the application code needs to do

The Terraform creates the infrastructure. The pipeline code (`run_batch.py`)
needs to implement:

1. **Write to model registry** after each training run:
   ```python
   # After prepare_models() in training mode:
   registry.put_model_record(
       sensor_id=device_id, variable=col,
       model_name=type(model).__name__, s3_key=model_s3_key,
       mae=eval_mae, trained_at=datetime.utcnow().isoformat(),
       next_train_after=(datetime.utcnow() + timedelta(days=7)).isoformat()
   )
   ```

2. **Check registry before running** to decide train vs infer:
   ```python
   record = registry.get_active_model(sensor_id, variable)
   should_retrain = (
       record is None or
       record['force_retrain'] == 'true' or
       record['next_train_after'] <= datetime.utcnow().isoformat()
   )
   ```

3. **Write to cost attribution table** after each sensor:
   ```python
   cost_table.put_item(Item={
       'PK': f'COST#{device_id}',
       'SK': f'DATE#{run_date}',
       'sensor_id': device_id, 'compute_cost_usd': cost,
       'duration_seconds': duration, 'rows_processed': len(filled_df),
       'train_mode': cfg.train_mode, 'project_tag': sensor_meta.get('project_tag', 'default'),
       'run_date': run_date, 'ttl': int(time.time()) + 90 * 86400
   })
   ```

4. **Update `force_retrain`** if rolling MAE drifts past threshold:
   ```python
   if rolling_mae > baseline_mae * 1.2:
       registry.flag_for_retrain(sensor_id, reason='mae_drift')
   ```
