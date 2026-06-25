#!/usr/bin/env bash
set -euo pipefail

BUCKET="btgconnectai-sandbox-artifacts-439354306218"
CODE_KEY="deploy-20260625163912"
SRC="src/lambdas"
TMPDIR="/tmp/lambda-zips"
rm -rf "$TMPDIR" && mkdir -p "$TMPDIR"

# Each lambda: <source-dir>:<zip-name>
# Some share the same source (transfer_breb has multiple handlers)
LAMBDAS=(
  "ai_agent:ai-agent"
  "auth_service:auth-service"
  "balance_query:balance-query"
  "email_service:email-service"
  "message_handler_notify:message-handler-notify"
  "message_processor:message-processor"
  "otp_service:otp-service"
  "statement_generator:statement-generator"
  "transfer_breb:transfer-breb-initiator"
  "transfer_breb:transfer-breb-validate"
  "transfer_breb:transfer-breb-execute"
  "webhook_receiver:webhook-receiver"
)

echo "==> Packaging Lambdas..."
for entry in "${LAMBDAS[@]}"; do
  dir="${entry%%:*}"
  name="${entry##*:}"
  zipfile="${TMPDIR}/${name}-${CODE_KEY}.zip"
  (cd "$SRC" && zip -qr "$zipfile" "$dir/")
  echo "  ${name} -> $(basename "$zipfile")"
done

echo "==> Uploading Layer..."
aws s3 cp layer/shared-layer.zip "s3://${BUCKET}/layers/shared-layer-${CODE_KEY}.zip" --quiet

echo "==> Uploading Lambda ZIPs..."
aws s3 sync "$TMPDIR/" "s3://${BUCKET}/lambdas/" --quiet

echo "==> Uploading ASL..."
aws s3 cp cloudformation/state-machines/transfer-breb.asl.json \
  "s3://${BUCKET}/state-machines/transfer-breb-${CODE_KEY}.asl.json" --quiet

echo "==> Done. CODE_KEY=${CODE_KEY}"
