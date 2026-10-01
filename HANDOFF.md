# Handoff — Helt pack telemetry, current working state

Single source of truth for the *currently running* system. Written at the point
where the end-to-end sandbox first worked. Read this in full before changing
anything.

---

## 1. The two repos

| Repo | Path | What it is |
|---|---|---|
| **Firmware** | `~/Documents/GitHub/System-Interface-Firmware/ESP32_S3_AMOLED_ESP_IDF` | ESP32-S3 System Interface (SI) firmware. Branch `main`. |
| **Sandbox** | `~/Documents/GitHub/helt-sandbox` | Hardware-free test rig for the whole cloud data path. Not yet pushed to GitHub. |

**Firmware git state:** the AWS IoT Core migration is complete and merged.

- `771eaf9` Phase A — NVS schema for mTLS, BLE protocol → `0x04`, chunked-write provisioning
- `861607f` Phase B — esp-mqtt over mutual TLS, Amazon Root CA 1 embedded
- `fe96084` Phase C — `cloud/aws/` files, docs rewritten, Telegraf removed
- `3b0067f` merge of `aws-migration` into `main`
- Revert path: branch `hivemq-fallback` at `1b376e3`, tag `hivemq-stable-v1`

**Authoritative firmware doc:** `CLOUD_SYNC_DESIGN.md` (in the firmware repo). It
holds the locked architecture decisions, MQTT schema, units convention, and open
issues. It supersedes commit messages. `AWS_MIGRATION_PLAN.md` was deleted at the
end of Phase C — its content lives in `CLOUD_SYNC_DESIGN.md` + `cloud/aws/RUNBOOK.md`.

---

## 2. The data flow

Everything from the pack to InfluxDB is **push**. From InfluxDB to a viewer it is
**pull** (the consumer asks).

```
[1] SI firmware ──MQTT/mTLS:8883──▶ [2] AWS IoT Core ──▶ [3] IoT Rule ──▶ [4] ingest Lambda ──▶ [5] InfluxDB
     (or fake_pack.py in sandbox)      (MQTT broker)       (routing SQL)     (JSON→line protocol)   (store of record)
                                                                                                         │
   [7] dashboard / customer ◀──HTTPS/JSON── [6] API Gateway + query Lambda ◀───────────────────────────┘
       (polls every 3s)                          (reads InfluxDB, returns JSON)
```

1. **SI firmware** samples the pack (BMS/inverter/DC/sensors) at 1 Hz into a PSRAM
   ring buffer, batches, and publishes every 30 s (`CONFIG_CLOUD_BATCH_PERIOD_S`)
   or at 4096 bytes, whichever first.
2. **AWS IoT Core** authenticates the pack by X.509 client cert (CN = `pack_id`).
3. **IoT Rule** — standing SQL `SELECT *, topic() AS mqtt_topic FROM 'helt/pack/+/+'`
   **plus a `WHERE` on the pack_id segment (since 2026-09-21):** the production
   rule `helt_to_influx` takes `NOT startswith(topic(3), 'SANDBOX-')`, the
   sandbox rule `sandbox_to_influx` takes `startswith(topic(3), 'SANDBOX-')`.
   Rules are account-wide and every matching rule fires, so without the split a
   real pack would be double-ingested into both buckets. Never give a real pack
   a `SANDBOX-` id. `topic()` is load-bearing: it's how the Lambda gets
   `pack_id` without trusting the JSON body.
4. **Ingest Lambda** transforms JSON → InfluxDB line protocol, POSTs to the v2
   write API. Stdlib only, Python 3.12, arm64, 128 MB.
5. **InfluxDB Cloud Serverless** is the store of record.
6. **API Gateway (HTTP API) + query Lambda** reads InfluxDB via Flux, returns JSON.
7. **Consumer** polls. Nothing is pushed to the consumer.

**Cost:** everything is serverless/managed; there is no VM or server to run
anywhere. Lambda's perpetual free tier (1M requests + 400k GB-s/month) covers
~100 packs; all-in AWS is roughly $1.60 / $16 / $160 per month at 10 / 100 / 1000
packs. InfluxDB Cloud is a separate vendor bill.

---

## 3. The firmware contract (do not break without updating both ends)

### MQTT topics

| Direction | Topic | QoS | Retained |
|---|---|---|---|
| SI → cloud | `helt/pack/{pack_id}/telemetry` | 1 | no |
| SI → cloud | `helt/pack/{pack_id}/status` | 1 | **yes** |
| cloud → SI | `helt/pack/{pack_id}/cmd` (subscribe) | 1 | — |
| LWT | `helt/pack/{pack_id}/status` → `{"online":false,...}` | 1 | **yes** |

Command ACKs are echoed onto `status` (non-retained). No separate ack topic.

### Telemetry payload (`schema: "v1"`)

```json
{ "schema":"v1", "pack_id":"HELT-0001",
  "samples":[ { "ts":1746479412, "ts_synced":true, "seq":4192,
    "si_state":4, "bms_state":3, "soc_pct":78,
    "pack_voltage_v":51.74, "current_a":-8.42, "power_w":-436,
    "max_cell_temp_c":31.0, "inv_output_w":412, "dc_input_w":0,
    "bms_protections":0, "enclosure_temp_c":24.7,
    "enclosure_humidity_pct":47.3 } ] }
```

(This is what the *firmware* emits today. The sandbox payload has diverged —
extra fields AND renamed power fields — see the schema-ahead note in §4.)

**Firmware Phase L3 additions (2026-09-30, `CLOUD_SYNC_DESIGN.md` "Interval
summary" + "Network position keys" is the authoritative schema):** every 30 s
one sample also carries `interval_s`, `batt_power_avg_w` (signed, + charge),
`ac_output_avg_w`, `dc_output_avg_w`, `ac_surge_count`, `ac_surge_max_w`,
`fault_count` + a `faults` list of `{code, age_s}`, `soh_pct`, the inverter
NTC temperatures `inv_*_c` and `mppt_solar_temp_c` / `mppt_ac_temp_c`; and a
sample without a usable GNSS fix may carry the network position `net_lat`,
`net_lon`, `net_acc_m`, `net_src`, `net_age_s` (behind the firmware switch
`GEOLOC_LOOKUP_ENABLE`). All follow the absent-key rule.

**Per-cell block (2026-10-01, `CLOUD_SYNC_DESIGN.md` "Per-cell block"):** one
sample per 5 min, and the sample of every MQTT connect, also carries
`cell1_mv` .. `cell14_mv` (uint, mV) and `cell_temp1_c` .. `cell_temp5_c`
(float, °C, 1 dp). Each group all there or all absent. Listed in the ingest
Lambda (union lists) and the query Lambda's `ops` group. The dashboard derives
the rest client-side (`cellStats()`, a moment counts only with all 14 cells):
"Cell voltages · latest reading" (one dot per cell, lowest / highest
labelled), "Cell imbalance · highest − lowest" (`cell_spread_mv`), "Lowest /
highest cell" (which cell in legend + tooltip) and "Cell temperatures".

Other payloads: `v1.cells` (per-cell mV, off by default, not stored -- the
ingest Lambda reads `schema: "v1"` only), status on connect
(`online, pack_id, fw_version, uptime_s, si_state, ip`), command ACK
(`{"ack":{"request_id","status","result"}}`), inbound command
(`{"cmd_id","request_id","args"}`).

### Units convention (keep consistent across CSV log, BLE, MQTT)

cell voltages mV int · pack voltage V float 2dp · current A float 2dp · power W int ·
temps °C float 1dp · humidity %RH float 1dp · time epoch seconds int.

### Auth / provisioning

- **mTLS only.** Device X.509 cert + private key in NVS. No username/password anywhere.
- CN of the cert **must equal** `pack_id` — the IoT policy uses
  `${iot:Certificate.Subject.CommonName}` so one policy serves every pack and each
  cert is confined to its own topic subtree. Certs are generated by OpenSSL CSR +
  `aws iot create-certificate-from-csr` (AWS-auto-generated certs get a UUID CN and
  break this).
- BLE protocol `0x06` (the client refuses any other version). Cert/key are ~1.5 KB blobs pushed via
  `BLE_CMD_PROV_CHUNK_WRITE` (0x45) / `_CHUNK_ABORT` (0x46), NVS keys 10 / 11.
  Short values still use `PROV_SET` (keys: wifi_ssid 1, wifi_pass 2, aws_endpt 3,
  pack_id 6, cloud_en 7).

### Firmware files that matter

`main/cloud_sync.c` (task, ring buffer, serializer, command dispatch) ·
`main/cloud_sync_mqtt.{c,h}` (esp-mqtt mTLS transport) ·
`main/app_config.{c,h}` (NVS) · `main/ble_prov_handler.c` (chunked write) ·
`main/certs/aws_root_ca1.pem` (embedded via `EMBED_TXTFILES`) ·
`main/cloud_commands.c` (only a placeholder `shutdown` handler today).

### Effect on the rest of the system

Cloud sync is an isolated subsystem. It touches the state machine in exactly two
places — `cloud_sync_stop()/wifi_sta_stop()` on STORAGE entry (WiFi modem clocks
block light sleep) and `wifi_sta_start()/cloud_sync_start()` on STORAGE exit. It
runs on its own FreeRTOS task at priority 2 with a 6 KB stack, allocates from
PSRAM, and never blocks or gates a state transition. The cloud cannot currently
cause any action on the pack — the only registered command is a logging placeholder.

---

## 4. The sandbox (this repo) — currently working

Replaces **only** the SI firmware with `fake_pack.py`; everything downstream is
the real pipeline.

**Sandbox schema runs ahead of firmware v1** (since 2026-07-24): every sample
additionally carries `soh_pct` (float 1dp), `cycle_count` (uint), `lat`/`lon`
(float 6dp, WGS84). **Power-port re-cut (2026-08-03):** the sandbox REPLACES
the firmware's `power_w` / `inv_output_w` / `dc_input_w` with six port fields
(all uint W): `ac_input_w` (grid charger, ~720 W while charging),
`solar_input_w` (MPPT, ≤400 W), `ac_output_w` (inverter, ≤2000 W),
`dc_output_w` (12V/USB rail, ≤580 W), plus `total_input_w` / `total_output_w`
(the port sums). The deployed sandbox ingest parses the new names and has
DROPPED the old three — real firmware pointed at the sandbox would lose its
power fields. The production `cloud/aws/lambda_function.py` in the firmware
repo has **none** of this — the two copies deliberately diverge until the
firmware serializer catches up. Firmware prerequisites for catch-up: BMS must
expose SoH + cycle count over CAN (check `CAN_COMM.md`); GPS needs a source
decision — the SI board has **no GNSS hardware** (options: GNSS module in a
hardware rev, WiFi geolocation, or a provisioned static install location);
and the SI must meter the four power ports individually to emit the port
fields.

`fake_pack.py` also simulates realistic behaviour instead of random values: a
CHARGE/REST/DISCHARGE phase machine driving the four power ports (AC in +
solar while charging, AC + DC loads while discharging), SoC integrated from
net port power (with charger/inverter efficiency) over a nominal
capacity (`--capacity-wh`, default 2560), linearised OCV + IR sag for voltage,
thermal lag, equivalent-full-cycle counting (`--cycles` seed), and GPS jitter
around `--home-lat/--home-lon` (default Cape Town). Batch timestamps are spread
across the publish period so same-second points no longer overwrite each other
in InfluxDB.

```
helt-sandbox/
├── publisher/fake_pack.py       # fake SI: real schema-v1 over mTLS, instrumented
├── lambdas/ingest/              # byte-identical to the firmware repo's cloud/aws/lambda_function.py (L3)
├── lambdas/query/               # reads InfluxDB via Flux, serves the dashboard
├── aws/setup.sh                 # 6 numbered steps, creates everything
├── aws/teardown.sh              # deletes everything
├── aws/config.env               # GIT-IGNORED: region, account, tokens, IDs
├── aws/sandbox_iot_policy.json  # permissive SANDBOX-ONLY policy
├── dashboard/index.html         # static dashboard, polls the API every 30s
└── README.md                    # full setup walkthrough
```

**Deployed AWS resources** (region `us-east-1`; account ID, endpoint and API ID
are in `aws/config.env`, which is git-ignored):

- IoT Thing `SANDBOX-01` + an active cert with `sandbox-pack-policy` attached
- IoT Rule `sandbox_to_influx` (scoped to `SANDBOX-*` pack_ids)
- Lambdas `sandbox-ingest`, `sandbox-query`, role `sandbox-lambda-role`
- API Gateway HTTP API (quick-create, `$default` catch-all route, CORS `*`)

**Production resources (stood up 2026-09-21, firmware repo
`cloud/aws/RUNBOOK.md` is their runbook; same account/region):**
- IoT Policy `helt-pack-policy` v2 (default since 2026-09-30; CN-scoped;
  `iot:RetainPublish` on `status`; Device Location reserved topics). v1 (no
  location) is kept as the previous version
- IoT Rule `helt_to_influx` (every pack_id NOT prefixed `SANDBOX-`) →
  Lambda `helt-iot-influx` (role `helt-lambda-role`, log group at 7-day
  retention) → bucket `helt_prod`. The Lambda is the firmware repo's
  `cloud/aws/lambda_function.py`; since firmware Phase L3 (2026-09-30) that
  file and `lambdas/ingest` are ONE source deployed twice (`helt-iot-influx`,
  `sandbox-ingest`), with the union of both field lists so every field keeps
  the type it already has in each bucket, and faults written to their own
  measurement `pack_fault` (tags `pack_id`, `src` bms/inv/dc, `code` "0x06";
  field `n=1u`; one point per fault at the sample's ts minus `age_s`).
- Things/certs are minted per pack at provisioning time (RUNBOOK §4). One so
  far: `HELT-0002` (2026-09-21), cert `9d3c67b8…303c`, bench pack.

**InfluxDB:** org `Helt`. Three buckets —
- `helt_prod` — **production**, real packs, 30-day retention, schema v1 as the
  firmware emits it today. Written only by `helt-iot-influx`.
- `helt_sandbox` — the sandbox's bucket, 7-day retention, `SANDBOX-*` fakes
  only, schema-ahead (§4 above).
- `helt_telemetry` — **retired.** Telegraf-era IOx schema with every `uint`
  field locked as int64; nothing writes here any more. Delete when convenient.

**The query Lambda reads both live buckets.** `sandbox-query` takes an
optional `INFLUXDB_PROD_BUCKET`; when set, any pack_id not prefixed `SANDBOX-`
is read from that bucket (`bucket_for()` in `lambdas/query/lambda_function.py`),
`/packs` unions both, and the read token must cover both buckets. Unset =
single-bucket mode, byte-identical to the old behaviour. Access is unchanged:
the DynamoDB entitlements decide who sees which pack regardless of bucket, so
a real pack is visible to `helt-ops` (`*`) and to whoever is explicitly
granted it, never to the sandbox demo customers. Since firmware Phase L3
(2026-09-30) the v1 power fields (`power_w` / `inv_output_w` / `dc_input_w`)
are in `ops`, the 30 s means in `core`, surges / faults / component
temperatures in `ops`, `net_*` in `location`; downsampling sums the counts,
keeps the surge peak's max and the states' last value; `/latest` adds
`telemetry_ts` (per-field last time); `GET /packs/{id}/faults` (ops) lists
`pack_fault` points. The dashboard hides cards a pack has no data for, so a
real pack and a sandbox fake each show their own fields.

**Multi-pack fleet simulation** (since 2026-07-24): `fake_pack.py --packs N`
(N ≤ 5) simulates a fleet from built-in per-pack profiles — distinct Western
Cape home locations, capacities (SANDBOX-03 is a 5120 Wh double pack), and
cycle-count seeds. One MQTT connection per pack (client_id = pack_id, as in
production), publishing staggered round-robin so the ingest Lambda never sees
an N-pack burst. All simulated packs share the single sandbox cert — allowed
by the permissive sandbox IoT policy only; production is one CN-scoped cert
per pack. Runs from any machine with Python + the cert files: see
`publisher/WINDOWS_SETUP.md`. Run the publisher in ONE place at a time
(duplicate MQTT client IDs evict each other).

**API endpoints** (S1+S2 of `API_SECURITY_SPEC.md` are LIVE since 2026-07-31:
every route requires `Authorization: Bearer <Cognito access token>` and every
response is entitlement-filtered per user — see §7 and `API_ACCESS.md`):
- `GET /packs` → `{packs:[{pack_id, online, last_seen},...]}` (only packs the
  caller is entitled to)
- `GET /packs/{pack_id}/latest` → `{pack_id, updated_ts, telemetry:{...}, status:{...}}`
  (`status` is `{}` unless `?status=1` — costs a second InfluxDB query and
  liveness derives from telemetry freshness now)
- `GET /packs/{pack_id}/histories?range=1h` → `{series:{field:[{t,v},...],…}}`
  — every field + GPS in ONE InfluxDB query; the dashboard's refresh path
- `GET /packs/{pack_id}/history?field=soc_pct&range=1h` → `{series:[{t,v},...]}`
  (`range` ∈ 15m, 1h, 6h, 24h, 7d; ranges beyond 15m are server-side
  downsampled via `aggregateWindow` mean to keep any range at ~200–400 points)
- `GET /packs/{pack_id}/track?range=1h` → `{series:[{t,lat,lon},...]}` — GPS
  trail for the map (lat/lon pivoted into pairs, same downsampling)

All query-Lambda responses are cached in-container for 30 s so N viewers share
one InfluxDB query per window — query executions are the dominant variable
cost (measured ≈$0.44/hr per open tab before; ≈$0.036/hr after; the fixed
pipeline is ≈$10.7/mo at the 5-pack sandbox rate — see API_SECURITY_SPEC.md §5).
The dashboard polls every 30 s to match, with a 404-triggered fallback to the
old per-field endpoints so Pages deploys never race Lambda deploys.

**To run it:** `source aws/config.env`, start the publisher with the four cert
flags (see README §2), then `cd dashboard && python3 -m http.server 8000`.
The dashboard now opens with a sign-in gate — use `helt-ops@example.com`
(password in git-ignored `aws/config.env`, `COGNITO_OPS_PASSWORD`).

---

## 5. Bugs found by live AWS testing

Fixed **in the sandbox**:

1. **`iot:RetainPublish` missing from the IoT policy.** AWS IoT requires this
   *separate* permission for any retained message — including a retained Last-Will
   presented during CONNECT. Without it AWS rejects the CONNECT by dropping the
   TCP connection with **no CONNACK**, which looks like a mysterious TLS failure.
   Symptom: client never connects, endless reconnect churn, nothing at the broker.
2. **InfluxDB IOx schema conflict.** IOx locks each column's type at first write.
   The `telemetry` table in `helt_telemetry` was created by Telegraf with
   `bms_protections` as **integer**; the Lambda writes **uinteger** (`u` suffix).
   InfluxDB returns HTTP 400 and discards the **entire batch** ("no data written").
   Worked around by writing to the fresh `helt_sandbox` bucket.
3. **API Gateway had no permission to invoke the query Lambda** — `create-api
   --target` quick-create did not add it, giving a 500 with no Lambda log group.
   Fixed with an explicit `lambda add-permission`.
4. **Query Lambda parsing** — InfluxDB emits one CSV header per result table, so
   repeated headers leaked in as a bogus `"_field":"_value"` entry; and `online`
   came back as the string `"true"` instead of a boolean. Both fixed.
5. **`aws/setup.sh` lacks `set -e`**, so a failed step continues silently. This is
   how #3 went unnoticed. Worth fixing.
6. **The AWS account is capped at 10 concurrent Lambda executions** (new-account
   default; confirmed via `aws lambda get-account-settings`). A dashboard page
   load firing 11 parallel API calls got 503s on the overflow — the dashboard now
   pools its history fetches (4 at a time + one retry). Matters for the customer
   API design: request a limit increase before any real fleet, and rate-limit at
   the gateway so one client can't starve the pool.

**Resolved in the FIRMWARE repo / production stack (2026-09-21, firmware
Phase 7A):**

- `cloud/aws/iot_policy.json` now grants `["iot:Publish","iot:RetainPublish"]`
  on the `status` topic (telemetry stays `iot:Publish`); deployed as
  `helt-pack-policy` v1.
- The schema conflict (#2) is sidestepped by writing production to a **new
  bucket `helt_prod`** with the Lambda unchanged. Investigation note for the
  record: the clash was never just `bms_protections`. The old `telegraf.conf`
  declared nine fields `uint`, but Telegraf's `influxdb_v2` output downcasts
  uint to int64 unless `influx_uint_support` is on, so **every** `u`-suffixed
  field the Lambda writes (`seq`, `si_state`, `bms_state`, `soc_pct`,
  `inv_output_w`, `dc_input_w`, `bms_protections`, `uptime_s`, `request_id`)
  collided with `helt_telemetry`. "Match Telegraf's types" would have meant
  retyping all of them to `i`, diverging from the sandbox ingest's `u` typing
  that the payload catch-up (§7) later has to merge. `helt_telemetry` is
  retired; the tokens in `config.env` cannot see it (they are bucket-scoped),
  so confirm its retention/deletion from the InfluxDB UI.

**Found by the first real pack on the bench (2026-09-21, firmware Phase 7C,
`HELT-0002`):**

7. **Ingest Lambda wrote pre-SNTP samples as 1970 and raised.** The firmware
   ships the samples captured before SNTP sync with `ts=0, ts_synced=false`
   (design decision 6: "the cloud side post-corrects") -- but the Lambda never
   did. IOx answered HTTP 400 *"partial write … observed timestamp
   1970-01-01 … outside of the retention period"*: the synced lines of the
   batch **were** written, the four `ts=0` lines dropped, and because the
   Lambda `raise`d, Lambda's async default retry replayed the identical batch
   twice more (same RequestId, same 400). **Fixed in the production Lambda**
   (`cloud/aws/lambda_function.py`, redeployed `helt-iot-influx` 15:27Z):
   unsynced samples are anchored on the first synced sample of the batch via
   the 1 Hz `seq` counter (tag stays `ts_synced=false`), dropped with a WARN
   if the batch has no anchor, and a 4xx from InfluxDB is logged and swallowed
   instead of raised (5xx still raises so real outages retry). Verified on the
   next boot: `seq 0-3` landed at `15:30:16-19Z` contiguous with `seq 4` at the
   SNTP-sync second, one clean invocation. **Ported to `lambdas/ingest`** in
   firmware Phase L3 (2026-09-30: the two copies are now one file). Known gap:
   the anchor assumes one seq step per second, which only holds at the WiFi
   rate -- on cellular the firmware keeps one sample per 30 s (10 s moving).
8. **Telemetry cadence is ~21 s, not 30 s.** The firmware's size trigger
   (`21 samples × 200 B estimate ≥ 4096`) fires before the 30 s timer at the
   1 Hz sampler; real batches are ~5.5 KB. Nothing to fix -- the dashboard's
   freshness-based liveness and 30 s polling are fine with it -- but the
   RUNBOOK and this doc used to say 30 s.
9. **`fw_version` is truncated** to 31 chars (`hivemq-stable-v1-83-gb8a28b0a-d`):
   ESP-IDF fills `esp_app_desc_t.version` (32 bytes) from `git describe`.
   Cosmetic; noted in the firmware design doc's open issues.
10. **Firmware, not cloud: the AMOLED showed stale bands after boot** once Wi-Fi
    was live. The SPI driver bounces every PSRAM-sourced flush chunk through a
    per-chunk internal DMA allocation, and with the STA up there was
    persistently no ~19 KB contiguous internal block -- 28 of 29 chunks of the
    first dynamic-screen paint were dropped and DIRECT mode never repaints
    them. Fixed in the same firmware commit (rotation strip pinned in internal
    DMA RAM, SH8601 component now propagates the error, flush retries). It is
    a symptom of a thin internal-RAM budget with Wi-Fi + BLE + TLS up; the
    audit is the firmware repo's next open issue (`CLOUD_SYNC_DESIGN.md`).

Bench conditions that are *not* bugs but will show up in the console: no SD
card was fitted, so the data logger retries the mount with backoff and each
attempt tears down and rebuilds the LCD SPI bus (`sd_card: Failed to initialise
the card`, `data_logger: Flush failed: ESP_ERR_TIMEOUT`); and `cloud_sync_start()`
runs ~2.5 s before the STA has an IP, so the very first TLS attempt logs
`esp-tls: couldn't get hostname` and esp-mqtt retries ~10 s later.

**Also outstanding (documented, deliberately deferred):** there is no
`cloud_sync_deinit()`. esp-mqtt stores only the *pointer* to the cert/key PEM
buffers and re-reads them on every reconnect, so Phase B parks them in
`cloud_sync_state_t.mqtt_cert_pem/.mqtt_key_pem` for the client's lifetime. On any
future destroy path they must be wiped and freed **after** the client is
destroyed. The private key therefore sits as cleartext in PSRAM for device uptime.
See `CLOUD_SYNC_DESIGN.md` decision #12 and Open Issues.

---

## 6. Architecture decision: API-first

**Decided:** the API is the single product surface. Every customer gets the same
thing — a dashboard **and** API access. Business customers who rent out packs use
the same API to feed their own dashboard.

**Why:** onboarding a customer to an API is a data-plane operation (a row in an
entitlements table + a credential) with no per-customer infrastructure. A
server-to-server push (webhook) would require provisioning and monitoring
per-customer delivery infrastructure forever. Also, telemetry only updates every
30 s, so polling costs nothing in freshness — and the API layer is the only clean
place to enforce per-pack and per-field access control, which InfluxDB cannot do
(its tokens are bucket-scoped only).

**Rejected for now:** webhook/push delivery to customer-hosted endpoints. Not
closed off — the ingest Lambda is the natural fan-out point if a specific customer
ever needs it — but it is not on the critical path and should not shape the API.

**Intended (not yet built):** an entitlements store mapping customer → allowed
pack_ids → allowed fields; two credential types (short-lived JWTs from a login for
end users in a browser, API keys for business backends) both converging on one
authorization check; rate limits / usage plans.

**Implemented (2026-07-31, phases S1+S2):** `API_SECURITY_SPEC.md` v0.2 —
Cognito User Pool (headless users; Hosted UI deliberately deferred, spec §10),
API Gateway JWT authorizer on every route, DynamoDB `helt_entitlements`
(user → pack → field groups `core/health/location/ops`, managed by
`aws/grant.py`), enforcement + audit in the query Lambda, CORS lockdown.
Phase S3 (throttles, SSM, concurrency increase) remains. See §7.

---

## 7. Work status + what the next chat is for

**Done (2026-07-24..27), sandbox-side:** payload expanded (soh_pct, cycle_count,
lat/lon — see §4 schema-ahead note; the *firmware* serializer has NOT changed),
realistic multi-pack simulation, dashboard rebuilt on the official brand with
charts/map/pack-picker (live on GitHub Pages), query-cost fixes (30 s cache,
bundled `/histories`, 30 s polling), freshness-based liveness.

**Done (2026-07-31): API security phases S1+S2, live and verified.** The
spec's §10 questions are answered in the spec (headline: Hosted UI login
DEFERRED — the single customer gets email+password in a document,
`API_ACCESS.md`, and fetches 1 h tokens by script; same accounts later log
into a real login page with zero migration). What exists now:

- Cognito pool `helt-users` + public app client (`USER_PASSWORD_AUTH`);
  pool/client ids and the three demo users' generated passwords live in
  git-ignored `aws/config.env` (`COGNITO_*`). Admin-create only.
- JWT authorizer on all five explicit routes; CORS locked to the github.io
  + localhost:8000 origins. **Two live findings, baked into setup.sh
  step8:** quick-create's `$default` route is ApiGatewayManaged and
  UNDELETABLE → it is JWT-locked instead (anon unknown path = 401); and
  CORS preflights need an explicit unauthenticated `OPTIONS /{proxy+}`
  route because `$default` matching everything disables the gateway's
  auto-preflight answers.
- DynamoDB `helt_entitlements` + `aws/grant.py` (grant/revoke/list; shells
  out to the aws CLI so it shares your `aws login` session — boto3 can't
  read those creds). Demo grants: customer-a → 01-03 ALL; customer-b →
  04-05 core,health,ops (no location); helt-ops → `*` ALL.
- Query Lambda enforces per-pack + per-field-group access AFTER its 30 s
  response cache (cache holds raw Influx reads keyed by pack/range — never
  per user), 403s are existence-neutral, and every request emits a one-line
  JSON audit record to CloudWatch.
- Dashboard has a minimal sign-in gate (sessionStorage tokens, hourly
  refresh, sign-out); the map hides for users without `location`.
- `aws/acceptance.sh`: 20 live checks covering both spec demos (S1:
  anon 401 → token → data; S2: A sees 01-03, B sees 04-05 without
  location, B's `/track` 403s, B's map hides). All passing 2026-07-31.

**Done (2026-08-03): power-port schema re-cut, deployed end-to-end.**
`power_w` / `inv_output_w` / `dc_input_w` replaced by the six port fields
(see §4). `fake_pack.py` models the ports; both Lambdas updated and
redeployed (`update-function-code`, verified `Successful`); the query
Lambda's `core` group now grants the six new fields; the dashboard's
`FIELDS` roster shows 12 charts and the KPI tile is "Net power"
(`total_input_w − total_output_w`, same ±30 W charging/discharging
thresholds). Old-name data stays in InfluxDB but stops accruing.

**Done (2026-09-21): first-real-pack groundwork, AWS side (firmware Phase
7A).** Production IoT policy / role / Lambda / rule / log group created from
the firmware repo's RUNBOOK via CLI (nothing had been executed before — the
account held only sandbox resources); `helt_prod` bucket; both IoT rules
scoped on the `SANDBOX-` prefix; `sandbox-query` grew `INFLUXDB_PROD_BUCKET`
routing (`setup.sh` step4/5 and `config.env.example` updated to match).
Verified end-to-end without hardware: `aws iot-data publish` of a v1 sample
for `TEST-0001` (ts = now − 29 d so it ages out of `helt_prod` within a day)
invoked `helt-iot-influx` once (654 ms, no error) and landed all 13 fields in
`helt_prod` at the SI timestamp with the locked IOx types exactly as
`cloud/influxdb_schema.md` specifies (`unsignedLong` uints, `long` `power_w`,
`double` floats); a `SANDBOX-01` control publish invoked only `sandbox-ingest`;
`helt_sandbox` holds no `TEST-0001` rows; a direct invoke of `sandbox-query`
`/packs` lists `TEST-0001` for `helt-ops` and nothing for `customer-a`.

**Done (2026-09-21): the first real pack is live (firmware Phases 7B + 7C).**
7B: `SI-Mac-Client` 0.28.0 (`116e5ba`) provisions keys 1/2/3/6/7 and streams
the cert/key blobs (keys 10/11) with `PROV_CHUNK_WRITE` in 235-byte ACKed
chunks. 7C: Thing `HELT-0002` + CSR cert (CN = pack_id, cert id
`9d3c67b8…303c`, `helt-pack-policy` attached; key/cert kept outside every git
tree in `~/helt-packs/HELT-0002/`), `mosquitto_pub` smoke test CONNACK(0),
provisioned live over BLE (cert 1208 B, key 1679 B, both "Set on SI"), and the
boot chain verified from the console: STA +3.4 s → IP +6.2 s → SNTP +7.7 s →
`MQTT_EVENT_CONNECTED` + retained status +20.6 s → first batch +25 s → ~21 s
cadence. Cloud side: `helt-iot-influx` invoked per message, rows in
`helt_prod` at the SI timestamps, `sandbox-query` `/packs` lists `HELT-0002`
online and `/packs/HELT-0002/latest` returns live telemetry + status for
`helt-ops` (the v1 power fields stay absent from the API -- §4 caveat). Four
findings in §5 (#7-#10); the Lambda one is fixed and redeployed, the display
one is fixed in firmware. `TEST-0001` from 7A still shows in `/packs` (offline)
until its rows age out of `helt_prod`.

**Done (2026-09-30): firmware Phase L3 — telemetry payload catch-up +
network location, cloud side.** Deployed, each with the user's go-ahead:
the shared ingest source to `helt-iot-influx` and `sandbox-ingest` (both
`update-function-code`, verified `Successful`; HELT-0002's batches kept
landing in `helt_prod`), `sandbox-query` (field groups, downsampling per
field, `telemetry_ts`, `/faults`), API route `GET /packs/{pack_id}/faults`
(JWT, `helt-jwt`; `setup.sh` step 8 lists it), and this dashboard (30 s power
means, surge dots, inverter + MPPT temperature charts with a value legend,
faults list, state name in the header, the network position as a blue marker
+ accuracy circle apart from the red GNSS trail). Read-only checks before the
deploy: live Lambdas and `helt-pack-policy` v1 matched the committed sources;
no L3 field name existed in `helt_prod`. A follow-up `sandbox-query` fix
(`37190fa`, 14:57Z) reads each CSV table's own header (the histories union
returns `last()` tables with their columns in another order) and lists each
pack once in `/packs` (one row came back per `ts_synced` tag value).
`helt-pack-policy` **v2** (default, 15:05:54Z; v1 kept) grants the AWS IoT
Core Device Location reserved topics under each pack's own CN. Bench-proven
on HELT-0002 the same afternoon: before the grant AWS dropped every connect
0.4 s after the location SUBSCRIBE; after it, a WiFi lookup answered in
497 ms (± 138 m, right on the map), and every L3 field landed in `helt_prod`
with its designed type. Record: firmware `LOCATION_DESIGN.md` §10-§11.
Open: the pack's motion detection reads "moving" on the bench (10 s
publishing over cellular), and the pre-SNTP `seq` anchor is wrong on
cellular (§5 #7).

**Done (2026-10-01): per-cell block (§3), cloud side.** Firmware `c1bd8e62`
sends `cell1_mv`..`cell14_mv` + `cell_temp1_c`..`cell_temp5_c` on one sample
per 5 min and on every connect sample. Deployed with the user's go-ahead: the
shared ingest source to `helt-iot-influx` + `sandbox-ingest` and
`sandbox-query` (`ops` group), all `Successful`; the live code of all three
matched the committed sources first. Checked after: `history?field=cell7_mv`
answers 200 (the field is known), HELT-0002's `latest` / `histories` unchanged,
no ingest ERROR / WARN. Dashboard: the four cell cards (§3), previewed with
sample data. Not yet seen with real data: HELT-0002 runs firmware without the
block until it is flashed. `fake_pack.py` sends no cells, so the cards stay
hidden for SANDBOX-* packs.

**Next for the real pack:** the firmware repo's open issue on the internal-RAM
budget with Wi-Fi up (instrument the boot, trim the Wi-Fi buffer pools, LVGL
allocation audit) is the one thing 7C surfaced that is not fixed -- it is a
latent risk for any new internal-RAM consumer, cellular included. Then, on
the cloud side, the payload catch-up (§4) so the dashboard's power charts
work for real packs, porting the §5 #7 fix into `lambdas/ingest` at the same
time.

**Next: spec phase S3** — stage/route throttles, Influx tokens to SSM
SecureString, Lambda concurrency-increase request. Then the InfluxDB
retention policy (§7 below). Data flow stays pull-only — push delivery, a
cursor/mirroring endpoint, and a DynamoDB latest-state table were analysed
(see chat history 2026-07-27ff) and deliberately deferred.

**Payload dependency chain (for any future field change):** the serializer in
`main/cloud_sync.c` (or `fake_pack.py` in sandbox) → the ingest Lambda's three
type lists (`UINT_FIELDS` / `FLOAT_FIELDS` / `INT_FIELDS`, one file for both
deployments since L3) → `cloud/influxdb_schema.md` → the query Lambda's
`FIELD_GROUPS` (and `AGG_FN` if a mean is wrong for it) → the dashboard's
`FIELDS` / `MULTI` arrays. **And** any new or retyped field collides with the
IOx locked schema in whichever bucket it lands.

**Also outstanding:** InfluxDB retention policy on `helt_sandbox` (storage
grows ~0.35 GB/mo); firmware catch-up prerequisites in §4; the two firmware-repo
items in §5.

Constraints that still apply: firmware work follows the workflow rules in
`CLAUDE.md` (verify APIs against local ESP-IDF sources, `idf.py build` at every
phase boundary, one commit per phase staging only touched files, phase == chat
boundary). `ninja` is not on `$PATH` — use
`/opt/ST/STM32CubeCLT_1.18.0/Ninja/bin/ninja`.
