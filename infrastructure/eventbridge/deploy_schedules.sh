#!/usr/bin/env bash
# =============================================================================
# infrastructure/eventbridge/deploy_schedules.sh
# Creates EventBridge Scheduler schedules that start the Step Functions state
# machine. Two cadences:
#   • Hourly  — inference mode (TRAIN_MODE=false), keeps filled data fresh.
#   • Daily   — training mode (TRAIN_MODE=true), refreshes per-sensor models.
#
# We use EventBridge SCHEDULER (not classic rules) — it supports flexible time
# windows (jitter) which spreads task starts and avoids a thundering-herd of
# Fargate launches at the top of the hour, reducing throttling and Spot
# contention. The TRAIN_MODE override is passed via the state machine input.
# =============================================================================
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
SFN_ARN="${SFN_ARN:?set SFN_ARN to the state machine ARN}"
SCHEDULER_ROLE="${SCHEDULER_ROLE_ARN:?set SCHEDULER_ROLE_ARN}"

echo ">> Creating hourly INFERENCE schedule ..."
aws scheduler create-schedule \
  --region "$REGION" \
  --name annam-gapfill-hourly-inference \
  --schedule-expression "rate(1 hour)" \
  --flexible-time-window "Mode=FLEXIBLE,MaximumWindowInMinutes=10" \
  --target "{
      \"Arn\": \"${SFN_ARN}\",
      \"RoleArn\": \"${SCHEDULER_ROLE}\",
      \"Input\": \"{\\\"train_mode\\\": false}\"
  }" \
  --description "Hourly gap-fill in inference mode"

echo ">> Creating daily TRAINING schedule (02:30 UTC) ..."
aws scheduler create-schedule \
  --region "$REGION" \
  --name annam-gapfill-daily-training \
  --schedule-expression "cron(30 2 * * ? *)" \
  --flexible-time-window "Mode=FLEXIBLE,MaximumWindowInMinutes=30" \
  --target "{
      \"Arn\": \"${SFN_ARN}\",
      \"RoleArn\": \"${SCHEDULER_ROLE}\",
      \"Input\": \"{\\\"train_mode\\\": true}\"
  }" \
  --description "Daily gap-fill in training mode (refresh per-sensor models)"

echo ">> Schedules created."
echo "   NOTE: the state machine must map the 'train_mode' input into the"
echo "   TRAIN_MODE env override on the ECS container overrides. See the"
echo "   commented 'TRAIN_MODE' wiring note in stepfunctions_definition.json."
