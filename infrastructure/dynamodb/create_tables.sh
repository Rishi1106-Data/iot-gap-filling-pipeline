#!/usr/bin/env bash
# =============================================================================
# infrastructure/dynamodb/create_tables.sh
# Creates the two DynamoDB tables EXACTLY matching aws/io_dynamo.py.
#   • annam-sensor-metadata : metadata + neighbour topology (read-mostly)
#   • annam-gapfill-results : per-run results (write-heavy, TTL auto-expiry)
#
# PAY_PER_REQUEST (on-demand) is chosen deliberately:
#   - Traffic is spiky (hourly/daily batch bursts, idle between).
#   - On-demand bills per request with zero idle cost — far cheaper than
#     provisioned capacity sized for the burst. At 10k sensors x 1 run/hr the
#     read volume is trivial (≈10k GetItems/hr).
# =============================================================================
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
SENSOR_TABLE="${SENSOR_TABLE:-annam-sensor-metadata}"
RESULTS_TABLE="${RESULTS_TABLE:-annam-gapfill-results}"

echo ">> Creating ${SENSOR_TABLE} ..."
aws dynamodb create-table \
  --region "$REGION" \
  --table-name "$SENSOR_TABLE" \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions \
      AttributeName=PK,AttributeType=S \
      AttributeName=SK,AttributeType=S \
      AttributeName=status,AttributeType=S \
      AttributeName=device_id,AttributeType=S \
  --key-schema \
      AttributeName=PK,KeyType=HASH \
      AttributeName=SK,KeyType=RANGE \
  --global-secondary-indexes \
      '[{
          "IndexName": "gsi_status",
          "KeySchema": [
            {"AttributeName": "status", "KeyType": "HASH"},
            {"AttributeName": "device_id", "KeyType": "RANGE"}
          ],
          "Projection": {"ProjectionType": "ALL"}
      }]' \
  --tags Key=project,Value=annam-gapfilling Key=managed-by,Value=cli

echo ">> Creating ${RESULTS_TABLE} ..."
aws dynamodb create-table \
  --region "$REGION" \
  --table-name "$RESULTS_TABLE" \
  --billing-mode PAY_PER_REQUEST \
  --attribute-definitions \
      AttributeName=PK,AttributeType=S \
      AttributeName=SK,AttributeType=S \
  --key-schema \
      AttributeName=PK,KeyType=HASH \
      AttributeName=SK,KeyType=RANGE \
  --tags Key=project,Value=annam-gapfilling Key=managed-by,Value=cli

echo ">> Waiting for tables to become ACTIVE ..."
aws dynamodb wait table-exists --region "$REGION" --table-name "$SENSOR_TABLE"
aws dynamodb wait table-exists --region "$REGION" --table-name "$RESULTS_TABLE"

echo ">> Enabling TTL on ${RESULTS_TABLE} (attribute: ttl) ..."
aws dynamodb update-time-to-live \
  --region "$REGION" \
  --table-name "$RESULTS_TABLE" \
  --time-to-live-specification "Enabled=true, AttributeName=ttl"

echo ">> Done. Tables ready."
