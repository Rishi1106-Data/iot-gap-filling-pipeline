#!/usr/bin/env bash
# =============================================================================
# infrastructure/stepfunctions/deploy_statemachine.sh
# Substitutes placeholders into aws/stepfunctions_definition.json and
# creates/updates the STANDARD state machine (STANDARD, not EXPRESS, because the
# Map fan-out can run for many minutes and we want full execution history +
# the .sync ECS integration).
# =============================================================================
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
NAME="${SFN_NAME:-annam-gapfill}"
ROLE_ARN="${SFN_ROLE_ARN:?set SFN_ROLE_ARN}"

# Required substitution values
: "${ECS_CLUSTER_ARN:?}" "${RUN_BATCH_TASKDEF_ARN:?}" "${LIST_SENSORS_TASKDEF_ARN:?}"
: "${PRIVATE_SUBNET_1:?}" "${PRIVATE_SUBNET_2:?}" "${TASK_SG:?}"
: "${DATA_BUCKET:?}" "${FAILURE_TOPIC_ARN:?}"

DEF_SRC="$(dirname "$0")/../../aws/stepfunctions_definition.json"
DEF_OUT="$(mktemp)"

sed -e "s|\${EcsClusterArn}|${ECS_CLUSTER_ARN}|g" \
    -e "s|\${RunBatchTaskDefArn}|${RUN_BATCH_TASKDEF_ARN}|g" \
    -e "s|\${ListSensorsTaskDefArn}|${LIST_SENSORS_TASKDEF_ARN}|g" \
    -e "s|\${PrivateSubnet1}|${PRIVATE_SUBNET_1}|g" \
    -e "s|\${PrivateSubnet2}|${PRIVATE_SUBNET_2}|g" \
    -e "s|\${TaskSecurityGroup}|${TASK_SG}|g" \
    -e "s|\${DataBucket}|${DATA_BUCKET}|g" \
    -e "s|\${FailureTopicArn}|${FAILURE_TOPIC_ARN}|g" \
    "$DEF_SRC" > "$DEF_OUT"

echo ">> Validating substituted definition is JSON ..."
python3 -c "import json,sys; json.load(open('$DEF_OUT')); print('   valid')"

EXISTING="$(aws stepfunctions list-state-machines --region "$REGION" \
  --query "stateMachines[?name=='${NAME}'].stateMachineArn" --output text)"

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo ">> Updating existing state machine ${NAME} ..."
  aws stepfunctions update-state-machine \
    --region "$REGION" --state-machine-arn "$EXISTING" \
    --definition "file://${DEF_OUT}" --role-arn "$ROLE_ARN"
else
  echo ">> Creating state machine ${NAME} ..."
  aws stepfunctions create-state-machine \
    --region "$REGION" --name "$NAME" --type STANDARD \
    --definition "file://${DEF_OUT}" --role-arn "$ROLE_ARN" \
    --tags key=project,value=annam-gapfilling
fi

rm -f "$DEF_OUT"
echo ">> Done."
