#!/usr/bin/env bash
# S1/S2 acceptance demos (API_SECURITY_SPEC.md §9) against the LIVE API.
# Prints PASS/FAIL per check; never prints passwords or tokens.
# Note: anonymous unknown routes are 401 (not 404) -- the quick-create
# $default route is undeletable and JWT-locked; see setup.sh step8.
set -uo pipefail
cd "$(dirname "$0")"
source ./config.env

API="https://${API_ID}.execute-api.${REGION}.amazonaws.com"
IDP="https://cognito-idp.${REGION}.amazonaws.com/"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $1  -> $2"; }
check(){ [ "$2" = "$3" ] && ok "$1" || bad "$1" "got: $2  want: $3"; }

tok() {  # $1 email  $2 password -> access token on stdout
  curl -s "$IDP" -H 'Content-Type: application/x-amz-json-1.1' \
    -H 'X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth' \
    -d "{\"AuthFlow\":\"USER_PASSWORD_AUTH\",\"ClientId\":\"$COGNITO_CLIENT_ID\",\"AuthParameters\":{\"USERNAME\":\"$1\",\"PASSWORD\":\"$2\"}}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["AuthenticationResult"]["AccessToken"])'
}
code() { curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $1" "$API$2"; }
body() { curl -s -H "Authorization: Bearer $1" "$API$2"; }

echo "== S1: authentication =="
check "anonymous /packs -> 401" "$(curl -s -o /dev/null -w '%{http_code}' "$API/packs")" 401
check "garbage token -> 401" "$(code notatoken /packs)" 401
check "anon unknown route -> 401 (no route enum)" \
  "$(curl -s -o /dev/null -w '%{http_code}' "$API/nope")" 401

TA=$(tok "$COGNITO_CUSTA_EMAIL" "$COGNITO_CUSTA_PASSWORD") || exit 1
TB=$(tok "$COGNITO_CUSTB_EMAIL" "$COGNITO_CUSTB_PASSWORD") || exit 1
TO=$(tok "$COGNITO_OPS_EMAIL"   "$COGNITO_OPS_PASSWORD")   || exit 1
[ -n "$TA" ] && [ -n "$TB" ] && [ -n "$TO" ] && ok "tokens issued for all three users"

check "customer-a /packs -> 200" "$(code "$TA" /packs)" 200

echo "== S1: CORS =="
ALLOWED=$(curl -s -o /dev/null -w '%{header_json}' -X OPTIONS "$API/packs" \
  -H "Origin: https://dantewebber-zoeyenergy.github.io" \
  -H "Access-Control-Request-Method: GET" -H "Access-Control-Request-Headers: authorization" \
  | python3 -c 'import json,sys; h=json.load(sys.stdin); print(h.get("access-control-allow-origin",["-"])[0])')
check "preflight allows github.io origin" "$ALLOWED" "https://dantewebber-zoeyenergy.github.io"
DENIED=$(curl -s -o /dev/null -w '%{header_json}' -X OPTIONS "$API/packs" \
  -H "Origin: https://evil.example" \
  -H "Access-Control-Request-Method: GET" -H "Access-Control-Request-Headers: authorization" \
  | python3 -c 'import json,sys; h=json.load(sys.stdin); print(h.get("access-control-allow-origin",["-"])[0])')
check "preflight denies foreign origin" "$DENIED" "-"

echo "== S2: per-pack entitlement =="
check "A sees 01,02,03" \
  "$(body "$TA" /packs | python3 -c 'import json,sys; print(",".join(p["pack_id"] for p in json.load(sys.stdin)["packs"]))')" \
  "SANDBOX-01,SANDBOX-02,SANDBOX-03"
check "B sees 04,05" \
  "$(body "$TB" /packs | python3 -c 'import json,sys; print(",".join(p["pack_id"] for p in json.load(sys.stdin)["packs"]))')" \
  "SANDBOX-04,SANDBOX-05"
check "ops sees all five" \
  "$(body "$TO" /packs | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["packs"]))')" 5
check "A on B's pack -> 403" "$(code "$TA" /packs/SANDBOX-04/latest)" 403
check "A on nonexistent pack -> same 403" "$(code "$TA" /packs/GHOST-99/latest)" 403
check "403 body is existence-neutral" \
  "$(body "$TA" /packs/GHOST-99/latest)" '{"error": "forbidden"}'

echo "== S2: per-field entitlement (B has no location) =="
check "B /histories has no lat/lon" \
  "$(body "$TB" '/packs/SANDBOX-04/histories?range=7d' | python3 -c 'import json,sys; s=json.load(sys.stdin)["series"]; print(sorted(set(s)&{"lat","lon"}))')" \
  "[]"
check "A /histories HAS lat/lon" \
  "$(body "$TA" '/packs/SANDBOX-01/histories?range=7d' | python3 -c 'import json,sys; s=json.load(sys.stdin)["series"]; print(sorted(set(s)&{"lat","lon"}))')" \
  "['lat', 'lon']"
check "B /track -> 403" "$(code "$TB" '/packs/SANDBOX-04/track?range=7d')" 403
check "A /track -> 200" "$(code "$TA" '/packs/SANDBOX-01/track?range=7d')" 200
check "B /history field=lat -> 403" "$(code "$TB" '/packs/SANDBOX-04/history?field=lat')" 403
check "B /history field=soc_pct -> 200" "$(code "$TB" '/packs/SANDBOX-04/history?field=soc_pct')" 200
check "unknown field -> 400" "$(code "$TB" '/packs/SANDBOX-04/history?field=sneaky')" 400

echo
echo "RESULT: $PASS passed, $FAIL failed"
exit $((FAIL > 0))
