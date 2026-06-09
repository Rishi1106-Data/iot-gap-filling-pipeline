#!/usr/bin/env bash
# =============================================================================
# infrastructure/ecs/deploy_cluster.sh
# Creates the ECS cluster with Fargate + Fargate Spot capacity providers and
# registers both task definitions.
#
# This workload is BATCH (no long-running service, no load balancer, no
# autoscaling group). Step Functions calls ecs:runTask per batch, so we do NOT
# create an ECS Service. "Autoscaling" here = Step Functions Map MaxConcurrency,
# which scales the number of concurrent RunTask calls. That is the correct,
# cheapest scaling primitive for fan-out batch jobs.
# =============================================================================
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
CLUSTER="${ECS_CLUSTER:-annam-gapfill}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo ">> Creating ECS cluster ${CLUSTER} with Fargate + Fargate Spot ..."
aws ecs create-cluster \
  --region "$REGION" \
  --cluster-name "$CLUSTER" \
  --capacity-providers FARGATE FARGATE_SPOT \
  --default-capacity-provider-strategy \
      capacityProvider=FARGATE_SPOT,weight=4 \
      capacityProvider=FARGATE,weight=1,base=0 \
  --settings name=containerInsights,value=enabled \
  --tags key=project,value=annam-gapfilling

echo ">> Creating CloudWatch log groups ..."
for g in /ecs/annam-run-batch /ecs/annam-list-sensors; do
  aws logs create-log-group --region "$REGION" --log-group-name "$g" 2>/dev/null || true
  # 30-day retention keeps log storage cost negligible.
  aws logs put-retention-policy --region "$REGION" --log-group-name "$g" --retention-in-days 30
done

echo ">> Registering task definitions (substituting placeholders) ..."
DATA_BUCKET="${DATA_BUCKET:?set DATA_BUCKET}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

for tdf in taskdef-run-batch.json taskdef-list-sensors.json; do
  tmp="$(mktemp)"
  sed -e "s/\${AccountId}/${ACCOUNT_ID}/g" \
      -e "s/\${Region}/${REGION}/g" \
      -e "s/\${DataBucket}/${DATA_BUCKET}/g" \
      -e "s/\${ImageTag}/${IMAGE_TAG}/g" \
      "$(dirname "$0")/${tdf}" > "$tmp"
  echo "   registering ${tdf} ..."
  aws ecs register-task-definition --region "$REGION" --cli-input-json "file://${tmp}"
  rm -f "$tmp"
done

echo ">> ECS cluster + task definitions ready."
