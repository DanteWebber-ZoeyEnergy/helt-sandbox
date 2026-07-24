# Helt telemetry sandbox

End-to-end test of the pack telemetry data path **without any hardware**. A fake
publisher on your laptop pushes the exact firmware schema through the *real* AWS
pipeline into InfluxDB, and a dashboard reads it back out through an API.

```
fake_pack.py ─MQTT/mTLS→ AWS IoT Core ─→ IoT Rule ─→ sandbox-ingest ─write→ InfluxDB Cloud
 (your laptop)                                        (= production Lambda)      │
                                                                                │ read (Flux)
 GitHub Pages dashboard ─poll HTTPS→ API Gateway ─→ sandbox-query ───────────────┘
```

Everything left of InfluxDB matches production, with one deliberate exception:
the **sandbox schema runs ahead of firmware v1**. `fake_pack.py` additionally
publishes `soh_pct`, `cycle_count`, `lat`, `lon` in every sample, and the
sandbox ingest Lambda parses them (the production `helt-iot-influx` doesn't
yet — fold the changes in when the firmware serializer catches up). Everything
else you prove here holds for real packs.

> **Division of labour:** every file here is pre-built. Your job is to run the
> AWS commands (your account, your bill) and paste two values into place. Follow
> the steps in order; each one is verifiable before you move on.

---

## Contents

| Path | What it is |
|---|---|
| `publisher/fake_pack.py` | Fake SI firmware — publishes schema-v1 telemetry over mTLS; `--packs 5` simulates a fleet |
| `publisher/WINDOWS_SETUP.md` | Run the publisher from a Windows PC (what to copy, install, run) |
| `API_SECURITY_SPEC.md` | Draft design for auth / entitlements / rate limiting on the API |
| `lambdas/ingest/` | `sandbox-ingest` — the production ingest Lambda (JSON → InfluxDB) |
| `lambdas/query/` | `sandbox-query` — reads InfluxDB (Flux), returns JSON for the dashboard |
| `aws/setup.sh` | Creates every AWS resource, step by step |
| `aws/teardown.sh` | Deletes everything (back to $0) |
| `aws/config.env.example` | Your settings — copy to `config.env` and fill in |
| `aws/sandbox_iot_policy.json` | Permissive **sandbox-only** IoT policy |
| `dashboard/index.html` | Static dashboard for GitHub Pages |

---

## 0. Prerequisites (one time)

**a) AWS CLI v2** — you don't have it yet. Install:

```bash
# macOS:
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "/tmp/AWSCLIV2.pkg"
sudo installer -pkg /tmp/AWSCLIV2.pkg -target /
aws --version        # expect aws-cli/2.x
```

**b) AWS credentials** — from the AWS console (IAM → your user → Security
credentials → Create access key), then:

```bash
aws configure        # paste Access Key, Secret, default region, output = json
aws sts get-caller-identity   # should print your account number
```

**c) Python deps** for the publisher:

```bash
cd publisher && pip3 install -r requirements.txt && cd ..
```

**d) InfluxDB Cloud** — you already have this from the Telegraf era. You need:
- the **region URL** (e.g. `https://us-east-1-1.aws.cloud2.influxdata.com`)
- the **org** name/ID and the **bucket** (`helt_telemetry`)
- a **WRITE** token (ingest) and a **READ** token (query). In the InfluxDB UI:
  *Load Data → API Tokens → Generate → Custom*, scope one to write and one to
  read on `helt_telemetry`. (For a quick sandbox you *may* reuse one all-access
  token for both, but two scoped tokens is the right habit.)

> **Region rule:** create the AWS resources in the **same region as InfluxDB**
> to avoid cross-region egress. Set `REGION` in `config.env` to match.

---

## 1. Fill in your config

```bash
cd aws
cp config.env.example config.env
# open config.env, fill REGION + all INFLUXDB_* values. Leave ACCOUNT/IOT_ENDPOINT/API_ID blank.
source config.env
chmod +x setup.sh teardown.sh
```

---

## 2. Stand up AWS

Run it all at once, or step by step (recommended the first time so you see each
piece work). Steps are in `setup.sh`; run an individual one with `./setup.sh step3`.

```bash
./setup.sh step1     # account id, IoT endpoint, download Amazon Root CA
./setup.sh step2     # IoT thing + device cert/key + sandbox policy   -> writes certs/
./setup.sh step3     # IAM role for the Lambdas (waits 10s to propagate)
./setup.sh step4     # create sandbox-ingest + sandbox-query Lambdas
./setup.sh step5     # IoT Rule -> ingest Lambda, + invoke permission
./setup.sh step6     # HTTP API in front of query Lambda -> prints your API URL
```

`step1` and `step6` write discovered values (`ACCOUNT`, `IOT_ENDPOINT`,
`API_ID`) back into `config.env`. After step 6, **copy the printed API base
URL** — you need it for the dashboard.

**Verify the ingest half before the dashboard.** Open the AWS IoT console →
**MQTT test client** → subscribe to `helt/pack/+/#`. Then run the publisher:

```bash
cd ../publisher
python3 fake_pack.py \
  --endpoint "$IOT_ENDPOINT" \
  --cert ../aws/certs/sandbox.cert.pem \
  --key  ../aws/certs/sandbox.private.key \
  --ca   ../aws/certs/AmazonRootCA1.pem \
  --pack-id "$PACK_ID"
```

You should see batches appear in the MQTT test client within a second or two.
Then confirm the data reached InfluxDB two ways:

- **CloudWatch** → Log groups → `/aws/lambda/sandbox-ingest` → newest stream
  shows `200 ... "3 lines"`.
- **InfluxDB Data Explorer** → query the `telemetry` measurement for your pack.

Then test the read API from your terminal (leave the publisher running):

```bash
curl "https://<API_ID>.execute-api.<region>.amazonaws.com/packs/$PACK_ID/latest"
# expect JSON: {"pack_id":"SANDBOX-01","updated_ts":..., "telemetry":{...}, "status":{...}}
```

If that returns data, **the whole pipeline works.** The dashboard is just a
pretty face on that `curl`.

---

## 3. The dashboard

1. Edit `dashboard/index.html` — set the `API` constant (top of the `<script>`)
   to the base URL from step 6. Set `PACK` if you changed the pack id.
2. Test locally first:
   ```bash
   cd dashboard && python3 -m http.server 8000
   # open http://localhost:8000  -- cards should fill and the SoC sparkline draw
   ```
3. Publish on GitHub Pages:
   ```bash
   cd ..                      # helt-sandbox/
   git init && git add . && git commit -m "Helt telemetry sandbox"
   gh repo create helt-sandbox --public --source=. --push   # or create on github.com and push
   ```
   Then in the repo: **Settings → Pages → Deploy from branch → `main` / `/root`**
   (or point Pages at the `dashboard/` folder). Your dashboard goes live at
   `https://<you>.github.io/helt-sandbox/dashboard/`.

Because github.io is HTTPS and API Gateway is HTTPS, there's no mixed-content
issue. CORS is already `*` on the API for the sandbox — lock it to your
github.io origin later (in `setup.sh` step 6, replace `AllowOrigins="*"`).

> **Do not commit secrets.** `.gitignore` already excludes `config.env` and
> `certs/`. Double-check `git status` before your first commit — you should
> **not** see `config.env`, any `*.pem`, or `*.key`.

---

## 4. Tear down (when done)

```bash
cd aws && ./teardown.sh
```

Deletes the API, both Lambdas, the IoT rule/thing/cert/policy, and the IAM role.
Sandbox cost is negligible even if you leave it (well within free tier), but this
returns you to zero. Your InfluxDB data stays (delete the sandbox points in the
InfluxDB UI if you want a clean bucket).

---

## Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| `fake_pack.py` hangs at "connecting" | wrong endpoint, cert not active/attached, or policy missing | re-run `./setup.sh step2`; confirm `$IOT_ENDPOINT` is the `-ats` host |
| Batches show in MQTT client but nothing in InfluxDB | ingest Lambda erroring, or missing invoke permission | CloudWatch `/aws/lambda/sandbox-ingest`; re-run `./setup.sh step5` |
| Lambda logs `InfluxDB 401` | wrong/unscoped write token | fix `INFLUXDB_TOKEN`, then `aws lambda update-function-configuration ...` or re-create |
| `curl` to API returns `{"error":"not found"}` | wrong path | must be `/packs`, `/packs/<id>/latest`, `/packs/<id>/history?field=..&range=1h`, or `/packs/<id>/track?range=1h` |
| API returns 500 | query Lambda erroring (read token/org) | CloudWatch `/aws/lambda/sandbox-query` |
| Dashboard shows ⚠ but curl works | `API` constant wrong, or CORS | confirm the URL has no trailing slash; hard-refresh |
| `create-*` says "already exists" | you re-ran a step | fine — the resource is already there |

## How this maps to production

- `sandbox-ingest` **is** `helt-iot-influx` — same code, same InfluxDB writes.
- The IoT Rule SQL is identical to production.
- `sandbox-query` + the HTTP API are a *preview* of the customer-facing API you'll
  design properly later (auth, per-pack/field entitlements, rate limits). Here it
  is wide-open and single-pack — **sandbox only**.
- The sandbox IoT policy is permissive; production uses the CN-scoped per-cert
  policy in `../System-Interface-Firmware/.../cloud/aws/iot_policy.json`.
