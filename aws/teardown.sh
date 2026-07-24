#!/usr/bin/env bash
#
# teardown.sh -- delete everything setup.sh created, so the sandbox costs $0.
# Safe to run repeatedly; "does not exist" errors are fine.
set -uo pipefail
cd "$(dirname "$0")"
source ./config.env

echo "Tearing down sandbox in $REGION ..."

# API
[ -n "${API_ID:-}" ] && aws apigatewayv2 delete-api --api-id "$API_ID" --region "$REGION" \
  && echo "deleted API $API_ID"

# Lambdas
aws lambda delete-function --function-name "$INGEST_FN" --region "$REGION" 2>/dev/null \
  && echo "deleted $INGEST_FN"
aws lambda delete-function --function-name "$QUERY_FN" --region "$REGION" 2>/dev/null \
  && echo "deleted $QUERY_FN"

# IoT Rule
aws iot delete-topic-rule --rule-name "$IOT_RULE" --region "$REGION" 2>/dev/null \
  && echo "deleted rule $IOT_RULE"

# IoT thing + cert + policy (must detach before delete)
if [ -f certs/cert.arn ]; then
  ARN=$(cat certs/cert.arn); CERT_ID=${ARN##*/}
  aws iot detach-thing-principal --thing-name "$IOT_THING" --principal "$ARN" --region "$REGION" 2>/dev/null
  aws iot detach-policy --policy-name "$IOT_POLICY" --target "$ARN" --region "$REGION" 2>/dev/null
  aws iot update-certificate --certificate-id "$CERT_ID" --new-status INACTIVE --region "$REGION" 2>/dev/null
  aws iot delete-certificate --certificate-id "$CERT_ID" --force-delete --region "$REGION" 2>/dev/null \
    && echo "deleted cert $CERT_ID"
fi
aws iot delete-policy --policy-name "$IOT_POLICY" --region "$REGION" 2>/dev/null \
  && echo "deleted policy $IOT_POLICY"
aws iot delete-thing --thing-name "$IOT_THING" --region "$REGION" 2>/dev/null \
  && echo "deleted thing $IOT_THING"

# IAM role
aws iam detach-role-policy --role-name "$LAMBDA_ROLE" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole 2>/dev/null
aws iam delete-role --role-name "$LAMBDA_ROLE" 2>/dev/null && echo "deleted role $LAMBDA_ROLE"

echo "Teardown complete. (CloudWatch log groups /aws/lambda/$INGEST_FN and /$QUERY_FN"
echo "linger with tiny cost; delete them in the CloudWatch console if you like.)"
