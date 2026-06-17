# Terraform Infrastructure — Annam AI IoT Gap-Filling Pipeline

This directory contains production-grade Terraform for all AWS infrastructure.
It replaces the shell scripts in the original `infrastructure/` directory.

---

## Folder Structure

```
terraform/
│
├── bootstrap/                      Remote state bucket + DynamoDB lock (run once)
│   └── main.tf
│
├── modules/                        Reusable, independently-testable modules
│   ├── networking/                 VPC endpoints + security groups
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── s3/                         Data bucket + lifecycle rules
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── ecr/                        Container image repository
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── dynamodb/                   sensor-metadata + gapfill-results tables
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── iam/                        4 least-privilege roles + inline policies
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── ecs/                        Cluster, log groups, 2 task definitions
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   ├── stepfunctions/              SNS topic + STANDARD state machine
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── definition.json.tftpl   ASL template (templatefile()-rendered)
│   ├── eventbridge/                Daily training + hourly inference schedules
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   └── cloudwatch/                 5 alarms + operational dashboard
│       ├── main.tf
│       ├── variables.tf
│       └── outputs.tf
│
└── environments/                   Per-environment root modules
    ├── dev/
    │   ├── main.tf                 Dev-specific overrides (force_destroy, no schedules)
    │   ├── variables.tf
    │   └── terraform.tfvars        Fill in VPC/subnet IDs
    ├── staging/
    │   ├── main.tf                 Prod-like sizing, schedules disabled by default
    │   ├── variables.tf
    │   └── terraform.tfvars
    └── prod/
        ├── main.tf                 Full prod settings
        ├── variables.tf
        ├── terraform.tfvars        Fill in VPC/subnet IDs + ops_email
        └── outputs.tf              All values needed post-deploy
```

---

## Quick Start

### Step 1 — Bootstrap remote state (one time per account)

```bash
cd terraform/bootstrap
terraform init
terraform apply \
  -var="aws_region=ap-south-1" \
  -var="project=annam"
```

Copy the output `backend_config_snippet` into each `environments/*/main.tf` backend block,
replacing the placeholder `REPLACE_WITH_ACCOUNT_ID`.

### Step 2 — Fill in your values

```bash
# prod
cp terraform/environments/prod/terraform.tfvars .env.tfvars.prod
# Edit: vpc_id, private_subnet_ids, image_tag
export TF_VAR_ops_email="ops@yourcompany.com"

# dev (optional)
cp terraform/environments/dev/terraform.tfvars .env.tfvars.dev
```

### Step 3 — Deploy an environment

```bash
cd terraform/environments/prod
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

### Step 4 — View outputs

```bash
terraform output
# data_bucket_name       = "annam-prod-data-123456789012"
# ecr_repository_url     = "123456789012.dkr.ecr.ap-south-1.amazonaws.com/annam-gapfill"
# state_machine_arn      = "arn:aws:states:..."
# dashboard_url          = "https://console.aws.amazon.com/cloudwatch/..."
```

---

## Deploying a New Image

Terraform manages infrastructure, not image contents. Push images via CI/CD:

```bash
# Build + push (tag with git SHA for traceability)
GIT_SHA=$(git rev-parse --short HEAD)
docker build -f aws/Dockerfile -t annam-gapfill:$GIT_SHA .
docker push $(terraform output -raw ecr_repository_url):$GIT_SHA

# Update task definitions to use the new tag
cd terraform/environments/prod
terraform apply -var="image_tag=$GIT_SHA"
```

---

## Environment Differences

| Setting | dev | staging | prod |
|---|---|---|---|
| `force_destroy` on S3 | `true` | `false` | `false` |
| `enable_pitr` on DynamoDB | `false` | `true` | `true` |
| Schedules enabled | `false` | `false` | `true` |
| Hourly inference | `false` | `false` | configurable |
| `batch_size` | 5 | 25 | 25 |
| `val_n_gaps` | 20 | 50 | 150 |
| `map_max_concurrency` | 5 | 10 | 20 |
| Log retention | 7d | 14d | 30d |
| Log level | `DEBUG` | `INFO` | `INFO` |
| P1 alarm threshold (SensorsFailed) | 5 | 20 | 50 |

---

## Shell Scripts Retirement Guide

The following table lists every shell script from the original `infrastructure/` directory
and its Terraform replacement. Scripts marked **RETIRED** can be deleted once `terraform apply`
has been run successfully for all environments. Scripts marked **KEEP** remain necessary.

| Script | Status | Replaced by | Notes |
|---|---|---|---|
| `infrastructure/iam/create_roles.sh` | **RETIRED** | `modules/iam/main.tf` | All 4 roles + policies now in Terraform. Idempotent by design. |
| `infrastructure/network/create_vpc_endpoints.sh` | **RETIRED** | `modules/networking/main.tf` | Gateway + interface endpoints, both SGs, SG rules — all managed. |
| `infrastructure/cloudwatch/deploy_dashboard.sh` | **RETIRED** | `modules/cloudwatch/main.tf` | Dashboard JSON now in `aws_cloudwatch_dashboard` resource. |
| `infrastructure/cloudwatch/deploy_alarms.sh` | **RETIRED** | `modules/cloudwatch/main.tf` | All 5 alarms as `aws_cloudwatch_metric_alarm` resources. |
| `infrastructure/dynamodb/create_tables.sh` | **RETIRED** | `modules/dynamodb/main.tf` | Both tables + GSI + TTL managed by Terraform. |
| `infrastructure/ecs/deploy_cluster.sh` | **RETIRED** | `modules/ecs/main.tf` | Cluster, capacity providers, log groups, both task defs. |
| `infrastructure/stepfunctions/deploy_statemachine.sh` | **RETIRED** | `modules/stepfunctions/main.tf` | State machine + SNS topic in Terraform; ASL in `definition.json.tftpl`. |
| `infrastructure/eventbridge/deploy_schedules.sh` | **RETIRED** | `modules/eventbridge/main.tf` | Both schedules, jitter windows, schedule group — all in Terraform. |
| `infrastructure/dynamodb/seed_sensors.py` | **KEEP** | N/A — data seeding, not infrastructure | Seeds sensor metadata rows. This is a **data operation**, not infrastructure. Terraform manages table schema, not row contents. Run once after `terraform apply`. |
| `infrastructure/github-actions-deploy.yml` | **KEEP** | N/A — CI/CD workflow | Updated to call `terraform apply` instead of individual scripts. |
| `bootstrap/main.tf` (new) | **KEEP** | Replaces manual S3+DDB creation | Run once, before any environment deploy. |

### Scripts that have no equivalent (never had one)

The following were done manually and now have Terraform equivalents:
- Manual `aws ecr create-repository` → `modules/ecr/main.tf`
- Manual `aws s3api create-bucket` → `modules/s3/main.tf`
- Manual `aws sns create-topic` → `modules/stepfunctions/main.tf`

---

## Dependency Order (Terraform handles this automatically)

```
bootstrap (once)
   ↓
networking  s3  ecr  dynamodb
   ↓          ↓      ↓
            iam  ←───────────────┐
              ↓                  │
             ecs                 │
              ↓                  │
        stepfunctions ───────────┘
              ↓
        eventbridge   cloudwatch
```

Terraform's dependency graph resolves this via `depends_on` blocks and implicit
references between module outputs and inputs. You do not need to run modules in
any particular order manually.

---

## Updating the State Machine Definition

The ASL is in `modules/stepfunctions/definition.json.tftpl`. Template variables
(subnet IDs, cluster ARNs, etc.) are injected via `templatefile()` at plan time —
no manual string substitution required.

To update the ASL:
1. Edit `definition.json.tftpl`
2. `terraform plan` — shows the state machine will be updated in-place
3. `terraform apply` — zero-downtime update (Step Functions updates state machine between executions)

---

## Importing Existing Resources

If you ran the shell scripts before adopting Terraform, import existing resources:

```bash
cd terraform/environments/prod

# Example: import existing DynamoDB table
terraform import module.dynamodb.aws_dynamodb_table.sensor_metadata \
  annam-prod-sensor-metadata

# Example: import existing IAM role
terraform import module.iam.aws_iam_role.task \
  annam-prod-task-role

# Example: import existing S3 bucket
terraform import module.s3.aws_s3_bucket.data \
  annam-prod-data-123456789012

# After importing, always run plan to verify no destructive changes:
terraform plan
```

Import all resources before running `apply` on an account that already has
infrastructure created by the shell scripts.

---

## Sensitive Values

Never store secrets in `.tfvars` files or state. Use these patterns:

```bash
# Option 1: environment variables (simplest)
export TF_VAR_ops_email="ops@company.com"

# Option 2: AWS Secrets Manager (recommended for CI/CD)
# Reference in variables.tf with a data source:
# data "aws_secretsmanager_secret_version" "ops_email" { secret_id = "annam/ops-email" }

# Option 3: Terraform Cloud / HCP Terraform variable sets
```

---

## Useful Commands

```bash
# Validate all module syntax without AWS access
terraform validate

# Show planned changes before applying
terraform plan -out=tfplan

# Apply previously saved plan
terraform apply tfplan

# Inspect a specific output
terraform output state_machine_arn

# Refresh state without applying changes
terraform refresh

# Destroy a dev environment (force_destroy must be true on S3)
cd terraform/environments/dev
terraform destroy

# Show dependency graph (requires graphviz)
terraform graph | dot -Tpng > graph.png
```
