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

step7() {  # Cognito user pool + app client + headless users (spec §2, S1)
  banner "STEP 7  Cognito user pool"
  if [ -n "${COGNITO_POOL_ID:-}" ] && aws cognito-idp describe-user-pool \
       --user-pool-id "$COGNITO_POOL_ID" --region "$REGION" >/dev/null 2>&1; then
    echo "pool $COGNITO_POOL_ID already exists -- skipping create"
  else
    COGNITO_POOL_ID=$(aws cognito-idp create-user-pool --pool-name helt-users \
      --username-attributes email \
      --admin-create-user-config AllowAdminCreateUserOnly=true \
      --policies 'PasswordPolicy={MinimumLength=12,RequireUppercase=true,RequireLowercase=true,RequireNumbers=true,RequireSymbols=true}' \
      --region "$REGION" --query UserPool.Id --output text)
    echo "pool created: $COGNITO_POOL_ID"
    sed -i.bak "s|^export COGNITO_POOL_ID=.*|export COGNITO_POOL_ID=$COGNITO_POOL_ID|" config.env
    rm -f config.env.bak
  fi
  if [ -n "${COGNITO_CLIENT_ID:-}" ] && aws cognito-idp describe-user-pool-client \
       --user-pool-id "$COGNITO_POOL_ID" --client-id "$COGNITO_CLIENT_ID" \
       --region "$REGION" >/dev/null 2>&1; then
    echo "app client $COGNITO_CLIENT_ID already exists -- skipping create"
  else
    # public client (no secret): the client id ships in the dashboard HTML and
    # the customer doc; USER_PASSWORD_AUTH is the headless token-fetch flow.
    COGNITO_CLIENT_ID=$(aws cognito-idp create-user-pool-client \
      --user-pool-id "$COGNITO_POOL_ID" --client-name helt-api-client \
      --no-generate-secret \
      --explicit-auth-flows ALLOW_USER_PASSWORD_AUTH ALLOW_REFRESH_TOKEN_AUTH \
      --prevent-user-existence-errors ENABLED \
      --region "$REGION" --query UserPoolClient.ClientId --output text)
    echo "app client created: $COGNITO_CLIENT_ID"
    sed -i.bak "s|^export COGNITO_CLIENT_ID=.*|export COGNITO_CLIENT_ID=$COGNITO_CLIENT_ID|" config.env
    rm -f config.env.bak
  fi
  # three headless users: internal ops + two fake customers (the S2 demo pair).
  # Passwords are generated once and persisted into git-ignored config.env.
  local u email_var pass_var email pass
  for u in OPS CUSTA CUSTB; do
    email_var="COGNITO_${u}_EMAIL"; pass_var="COGNITO_${u}_PASSWORD"
    email="${!email_var}"; pass="${!pass_var}"
    if [ -z "$pass" ]; then
      pass="Helt#1-$(openssl rand -hex 6)"
      sed -i.bak "s|^export ${pass_var}=.*|export ${pass_var}=$pass|" config.env
      rm -f config.env.bak
    fi
    if aws cognito-idp admin-get-user --user-pool-id "$COGNITO_POOL_ID" \
         --username "$email" --region "$REGION" >/dev/null 2>&1; then
      echo "user $email already exists"
    else
      aws cognito-idp admin-create-user --user-pool-id "$COGNITO_POOL_ID" \
        --username "$email" \
        --user-attributes Name=email,Value="$email" Name=email_verified,Value=true \
        --message-action SUPPRESS --region "$REGION" >/dev/null
      aws cognito-idp admin-set-user-password --user-pool-id "$COGNITO_POOL_ID" \
        --username "$email" --password "$pass" --permanent --region "$REGION"
      echo "user $email created (password persisted to config.env)"
    fi
  done
}

step8() {  # JWT authorizer + explicit routes + CORS lockdown (spec §6, S1)
  banner "STEP 8  API auth routes + CORS"
  local issuer="https://cognito-idp.${REGION}.amazonaws.com/${COGNITO_POOL_ID}"
  local auth_id integ rid defid rk
  auth_id=$(aws apigatewayv2 get-authorizers --api-id "$API_ID" --region "$REGION" \
    --query "Items[?Name=='helt-jwt'].AuthorizerId | [0]" --output text)
  if [ "$auth_id" = "None" ] || [ -z "$auth_id" ]; then
    auth_id=$(aws apigatewayv2 create-authorizer --api-id "$API_ID" \
      --authorizer-type JWT --name helt-jwt \
      --identity-source '$request.header.Authorization' \
      --jwt-configuration "Audience=$COGNITO_CLIENT_ID,Issuer=$issuer" \
      --region "$REGION" --query AuthorizerId --output text)
    echo "JWT authorizer created: $auth_id"
  else
    echo "JWT authorizer exists: $auth_id"
  fi
  # reuse the Lambda-proxy integration that quick-create made for $default
  integ=$(aws apigatewayv2 get-integrations --api-id "$API_ID" --region "$REGION" \
    --query "Items[0].IntegrationId" --output text)
  for rk in "GET /packs" "GET /packs/{pack_id}/latest" \
            "GET /packs/{pack_id}/histories" "GET /packs/{pack_id}/history" \
            "GET /packs/{pack_id}/track"; do
    rid=$(aws apigatewayv2 get-routes --api-id "$API_ID" --region "$REGION" \
      --query "Items[?RouteKey=='$rk'].RouteId | [0]" --output text)
    if [ "$rid" = "None" ] || [ -z "$rid" ]; then
      aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$rk" \
        --target "integrations/$integ" \
        --authorization-type JWT --authorizer-id "$auth_id" \
        --region "$REGION" >/dev/null && echo "route '$rk' created (JWT required)"
    else
      aws apigatewayv2 update-route --api-id "$API_ID" --route-id "$rid" \
        --target "integrations/$integ" \
        --authorization-type JWT --authorizer-id "$auth_id" \
        --region "$REGION" >/dev/null && echo "route '$rk' updated (JWT required)"
    fi
  done
  # the wide-open catch-all must go: unknown paths now 404 at the gateway
  defid=$(aws apigatewayv2 get-routes --api-id "$API_ID" --region "$REGION" \
    --query 'Items[?RouteKey==`"$default"`].RouteId | [0]' --output text)
  if [ "$defid" != "None" ] && [ -n "$defid" ]; then
    aws apigatewayv2 delete-route --api-id "$API_ID" --route-id "$defid" \
      --region "$REGION" && echo "\$default catch-all route deleted"
  fi
  aws apigatewayv2 update-api --api-id "$API_ID" --cors-configuration \
    '{"AllowOrigins":["https://dantewebber-zoeyenergy.github.io","http://localhost:8000"],"AllowMethods":["GET","OPTIONS"],"AllowHeaders":["authorization","content-type"],"MaxAge":3600}' \
    --region "$REGION" >/dev/null && echo "CORS locked to the two dashboard origins"
}

step9() {  # DynamoDB entitlements table + query-Lambda read access (spec §3, S2)
  banner "STEP 9  entitlements table"
  if aws dynamodb describe-table --table-name helt_entitlements \
       --region "$REGION" >/dev/null 2>&1; then
    echo "table helt_entitlements already exists"
  else
    aws dynamodb create-table --table-name helt_entitlements \
      --attribute-definitions AttributeName=user_id,AttributeType=S \
                              AttributeName=pack_id,AttributeType=S \
      --key-schema AttributeName=user_id,KeyType=HASH \
                   AttributeName=pack_id,KeyType=RANGE \
      --billing-mode PAY_PER_REQUEST --region "$REGION" >/dev/null \
      && echo "table helt_entitlements created (on-demand billing)"
    aws dynamodb wait table-exists --table-name helt_entitlements --region "$REGION"
  fi
  # the query Lambda only READS entitlements; grant.py writes with your creds
  cat > /tmp/entitlements-policy.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Action":["dynamodb:Query"],
 "Resource":"arn:aws:dynamodb:${REGION}:${ACCOUNT}:table/helt_entitlements"}]}
EOF
  aws iam put-role-policy --role-name "$LAMBDA_ROLE" \
    --policy-name entitlements-read \
    --policy-document file:///tmp/entitlements-policy.json \
    && echo "entitlements-read policy attached to $LAMBDA_ROLE"
  # NOTE: --environment REPLACES the whole env, so restate the Influx vars
  cat > /tmp/query-env.json <<EOF
{"Variables":{"INFLUXDB_URL":"$INFLUXDB_URL","INFLUXDB_READ_TOKEN":"$INFLUXDB_READ_TOKEN","INFLUXDB_BUCKET":"$INFLUXDB_BUCKET","INFLUXDB_ORG":"$INFLUXDB_ORG","ENTITLEMENTS_TABLE":"helt_entitlements"}}
EOF
  aws lambda update-function-configuration --function-name "$QUERY_FN" \
    --environment file:///tmp/query-env.json --region "$REGION" >/dev/null \
    && echo "$QUERY_FN env updated (ENTITLEMENTS_TABLE)"
}

all() { step1; step2; step3; step4; step5; step6; step7; step8; step9; banner "DONE"; }

"${1:-all}"
