# HELT Telemetry API — Customer Access Guide

*This is the template we send (filled in) when onboarding an API customer.
Angle-bracket values are per-customer; the client id is filled from
`aws/config.env` after setup.sh step 7 and is not a secret.*

---

## Your credentials

| | |
|---|---|
| API base URL | `https://cfc04rjifh.execute-api.us-east-1.amazonaws.com` |
| Sign-in email | `<EMAIL>` |
| Password | `<PASSWORD — sent separately>` |
| Client ID | `60kuha3oi1585r65ncri52hi3r` |
| Your packs | `<PACK LIST>` |

Keep the password secret. The client ID is public configuration.

## How authentication works

Every API call needs an **access token** in the `Authorization` header.
Tokens are fetched with your email + password and **expire after 1 hour** —
your integration should fetch one, reuse it for ~55 minutes, then fetch a
fresh one (one extra HTTPS call per hour). If a call ever returns **401**,
fetch a fresh token and retry once.

### Fetch a token (curl)

```bash
curl -s https://cognito-idp.us-east-1.amazonaws.com/ \
  -H 'Content-Type: application/x-amz-json-1.1' \
  -H 'X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth' \
  -d '{"AuthFlow":"USER_PASSWORD_AUTH","ClientId":"60kuha3oi1585r65ncri52hi3r",
       "AuthParameters":{"USERNAME":"<EMAIL>","PASSWORD":"<PASSWORD>"}}'
```

The JSON response contains `AuthenticationResult.AccessToken` (and
`ExpiresIn`, seconds). Then:

```bash
curl -s -H "Authorization: Bearer <ACCESS_TOKEN>" \
  https://cfc04rjifh.execute-api.us-east-1.amazonaws.com/packs
```

### Ready-to-run 24/7 poller (Python, no dependencies)

```python
import json, time, urllib.request

REGION    = "us-east-1"
CLIENT_ID = "60kuha3oi1585r65ncri52hi3r"
EMAIL     = "<EMAIL>"
PASSWORD  = "<PASSWORD>"
API       = "https://cfc04rjifh.execute-api.us-east-1.amazonaws.com"

_tok = {"v": None, "exp": 0}

def token():
    """Cached access token, refreshed 5 min before the 1 h expiry."""
    if time.time() < _tok["exp"] - 300:
        return _tok["v"]
    req = urllib.request.Request(
        f"https://cognito-idp.{REGION}.amazonaws.com/",
        data=json.dumps({
            "AuthFlow": "USER_PASSWORD_AUTH", "ClientId": CLIENT_ID,
            "AuthParameters": {"USERNAME": EMAIL, "PASSWORD": PASSWORD},
        }).encode(),
        headers={"Content-Type": "application/x-amz-json-1.1",
                 "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth"})
    with urllib.request.urlopen(req) as r:
        a = json.load(r)["AuthenticationResult"]
    _tok.update(v=a["AccessToken"], exp=time.time() + a["ExpiresIn"])
    return _tok["v"]

def get(path):
    req = urllib.request.Request(
        API + path, headers={"Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)

while True:
    for pack in get("/packs")["packs"]:
        latest = get(f"/packs/{pack['pack_id']}/latest")
        print(pack["pack_id"], latest["updated_ts"],
              latest["telemetry"].get("soc_pct"), "%")
    time.sleep(30)          # data updates every 30 s — faster polling
                            # only re-reads a cached answer
```

## Endpoints

All are `GET`, all return JSON.

| Endpoint | Returns |
|---|---|
| `/packs` | your packs: `{packs:[{pack_id, online, last_seen}]}` |
| `/packs/{pack_id}/latest` | newest value of each field you're entitled to |
| `/packs/{pack_id}/histories?range=1h` | all entitled fields' history in one call |
| `/packs/{pack_id}/history?field=soc_pct&range=1h` | one field's history |
| `/packs/{pack_id}/track?range=1h` | GPS trail (location entitlement only) |

`range` is one of `15m, 1h, 6h, 24h, 7d` (longer ranges are downsampled
server-side to ~200–400 points). Timestamps are epoch seconds (UTC).

## What you can see

Access is per-pack and per-field-group. Groups:

| Group | Fields |
|---|---|
| `core` | soc_pct, total_input_w, total_output_w, ac_output_w, dc_output_w, ac_input_w, solar_input_w |
| `health` | soh_pct, cycle_count, enclosure_temp_c, enclosure_humidity_pct |
| `location` | lat, lon (and the `/track` endpoint) |
| `ops` | si_state, bms_state, seq, ts_synced, pack_voltage_v, current_a, max_cell_temp_c, bms_protections |

Your grant: `<GROUPS PER PACK>`. Fields outside your grant are simply absent
from responses; endpoints outside it return `403 {"error":"forbidden"}`.

## Errors and etiquette

| Code | Meaning | What to do |
|---|---|---|
| 401 | token missing/expired/invalid | fetch a fresh token, retry once |
| 403 | not entitled to that pack/field | check your pack ids; contact us |
| 404 | unknown endpoint | check the path against the table above |
| 429/5xx | throttled / transient | back off and retry (≥ 5 s) |

Telemetry is batched from the pack every **30 seconds** — that is the data's
native cadence. Poll at 30 s; polling faster returns cached answers and may
run into rate limits.

Questions: dante@zoeyenergy.co.za
