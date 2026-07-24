#!/usr/bin/env bash
#
# setup.sh -- stands up the whole sandbox on AWS.
#
# You can run it all at once:      ./setup.sh
# ...or one step at a time:        ./setup.sh step3
# (steps are idempotent-ish; re-running a create that already exists just errors
#  on that line -- safe to read the error and move on.)
#
# Prereqs: aws CLI v2 configured, and a filled-in ./config.env  (see README).
set -uo pipefail
cd "$(dirname "$0")"
source ./config.env

CERTS=./certs
mkdir -p "$CERTS"

banner() { echo; echo "==================== $* ===================="; }

step1() {  # account id + IoT endpoint + Amazon Root CA
  banner "STEP 1  account, endpoint, root CA"
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
  IOT_ENDPOINT=$(aws iot describe-endpoint --endpoint-type iot:Data-ATS \
                   --region "$REGION" --query endpointAddress --output text)
  echo "ACCOUNT=$ACCOUNT"
  echo "IOT_ENDPOINT=$IOT_ENDPOINT"
  curl -fsSL https://www.amazontrust.com/repository/AmazonRootCA1.pem \
       -o "$CERTS/AmazonRootCA1.pem" && echo "saved $CERTS/AmazonRootCA1.pem"
  # persist the two discovered values back into config.env
  sed -i.bak "s|^export ACCOUNT=.*|export ACCOUNT=$ACCOUNT|" config.env
  sed -i.bak "s|^export IOT_ENDPOINT=.*|export IOT_ENDPOINT=$IOT_ENDPOINT|" config.env
  rm -f config.env.bak
  echo ">> wrote ACCOUNT and IOT_ENDPOINT into config.env"
}

step2() {  # IoT thing + certificate + policy
  banner "STEP 2  IoT thing, certificate, policy"
  aws iot create-thing --thing-name "$IOT_THING" --region "$REGION" >/dev/null \
    && echo "thing $IOT_THING created"
  aws iot create-keys-and-certificate --set-as-active --region "$REGION" \
    --certificate-pem-outfile "$CERTS/sandbox.cert.pem" \
    --public-key-outfile      "$CERTS/sandbox.public.key" \
    --private-key-outfile     "$CERTS/sandbox.private.key" \
    --query certificateArn --output text > "$CERTS/cert.arn"
  echo "cert ARN: $(cat "$CERTS/cert.arn")"
  aws iot create-policy --policy-name "$IOT_POLICY" \
    --policy-document file://sandbox_iot_policy.json --region "$REGION" >/dev/null \
    && echo "policy $IOT_POLICY created"
  aws iot attach-policy --policy-name "$IOT_POLICY" \
    --target "$(cat "$CERTS/cert.arn")" --region "$REGION" && echo "policy attached"
  aws iot attach-thing-principal --thing-name "$IOT_THING" \
    --principal "$(cat "$CERTS/cert.arn")" --region "$REGION" && echo "thing attached to cert"
}

step3() {  # IAM role for both Lambdas
  banner "STEP 3  Lambda execution role"
  aws iam create-role --role-name "$LAMBDA_ROLE" \
    --assume-role-policy-document file://lambda_trust_policy.json >/dev/null \
    && echo "role $LAMBDA_ROLE created"
  aws iam attach-role-policy --role-name "$LAMBDA_ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole \
    && echo "attached AWSLambdaBasicExecutionRole"
  echo ">> waiting 10s for the role to propagate..."
  sleep 10
}

step4() {  # ingest Lambda (writes InfluxDB) + query Lambda (reads InfluxDB)
  banner "STEP 4  Lambda functions"
  local role_arn="arn:aws:iam::${ACCOUNT}:role/${LAMBDA_ROLE}"

  ( cd ../lambdas/ingest && zip -j -q /tmp/ingest.zip lambda_function.py )
  cat > /tmp/ingest-env.json <<EOF
{"Variables":{"INFLUXDB_URL":"$INFLUXDB_URL","INFLUXDB_TOKEN":"$INFLUXDB_TOKEN","INFLUXDB_BUCKET":"$INFLUXDB_BUCKET","INFLUXDB_ORG":"$INFLUXDB_ORG"}}
EOF
  aws lambda create-function --function-name "$INGEST_FN" \
    --runtime python3.12 --architecture arm64 \
    --handler lambda_function.lambda_handler --role "$role_arn" \
    --zip-file fileb:///tmp/ingest.zip --timeout 30 --memory-size 128 \
    --environment file:///tmp/ingest-env.json --region "$REGION" >/dev/null \
    && echo "$INGEST_FN created"

  ( cd ../lambdas/query && zip -j -q /tmp/query.zip lambda_function.py )
  cat > /tmp/query-env.json <<EOF
{"Variables":{"INFLUXDB_URL":"$INFLUXDB_URL","INFLUXDB_READ_TOKEN":"$INFLUXDB_READ_TOKEN","INFLUXDB_BUCKET":"$INFLUXDB_BUCKET","INFLUXDB_ORG":"$INFLUXDB_ORG"}}
EOF
  aws lambda create-function --function-name "$QUERY_FN" \
    --runtime python3.12 --architecture arm64 \
    --handler lambda_function.lambda_handler --role "$role_arn" \
    --zip-file fileb:///tmp/query.zip --timeout 30 --memory-size 128 \
    --environment file:///tmp/query-env.json --region "$REGION" >/dev/null \
    && echo "$QUERY_FN created"
}

step5() {  # IoT Rule -> ingest Lambda, and permission for IoT to invoke it
  banner "STEP 5  IoT Rule"
  cat > /tmp/rule.json <<EOF
{"sql":"SELECT *, topic() AS mqtt_topic FROM 'helt/pack/+/+'","awsIotSqlVersion":"2016-03-23","ruleDisabled":false,"actions":[{"lambda":{"functionArn":"arn:aws:lambda:${REGION}:${ACCOUNT}:function:${INGEST_FN}"}}]}
EOF
  aws iot create-topic-rule --rule-name "$IOT_RULE" \
    --topic-rule-payload file:///tmp/rule.json --region "$REGION" \
    && echo "rule $IOT_RULE created"
  aws lambda add-permission --function-name "$INGEST_FN" \
    --statement-id iot-invoke --action lambda:InvokeFunction \
    --principal iot.amazonaws.com \
    --source-arn "arn:aws:iot:${REGION}:${ACCOUNT}:rule/${IOT_RULE}" \
    --region "$REGION" >/dev/null && echo "IoT may now invoke $INGEST_FN"
}

step6() {  # HTTP API in front of the query Lambda (quick-create adds route+perm)
  banner "STEP 6  HTTP API"
  API_ID=$(aws apigatewayv2 create-api --name "$API_NAME" --protocol-type HTTP \
    --target "arn:aws:lambda:${REGION}:${ACCOUNT}:function:${QUERY_FN}" \
    --cors-configuration AllowOrigins="*",AllowMethods="GET,OPTIONS",AllowHeaders="*" \
    --region "$REGION" --query ApiId --output text)
  sed -i.bak "s|^export API_ID=.*|export API_ID=$API_ID|" config.env && rm -f config.env.bak
  local endpoint="https://${API_ID}.execute-api.${REGION}.amazonaws.com"
  echo "API_ID=$API_ID"
  echo
  echo ">>> API base URL (put this in dashboard/index.html):"
  echo "    $endpoint"
  echo ">>> test it:"
  echo "    curl \"$endpoint/packs/${PACK_ID}/latest\""
}

all() { step1; step2; step3; step4; step5; step6; banner "DONE"; }

"${1:-all}"
