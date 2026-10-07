"""
AWS IoT Rule -> InfluxDB Cloud.
Replaces Telegraf. Receives MQTT messages via IoT Rule SQL, transforms
JSON to InfluxDB line protocol, POSTs to the v2 write API.

ONE SOURCE, TWO DEPLOYMENTS (since Phase L3): this file is byte-identical to
helt-sandbox/lambdas/ingest/lambda_function.py. It runs as `helt-iot-influx`
(real packs -> bucket helt_prod) and as `sandbox-ingest` (SANDBOX-* fakes ->
helt_sandbox); only the environment differs. The field lists are the union
of what the firmware sends and what the sandbox publisher sends, so every
field keeps the one type it already has in each bucket (IOx locks a column's
type at its first write -- see HANDOFF.md §5 #2 and cloud/influxdb_schema.md).
Change both copies together.

Environment variables (set on the Lambda):
    INFLUXDB_URL     -- write endpoint, e.g. https://us-east-1-1.aws.cloud2.influxdata.com
    INFLUXDB_TOKEN   -- write-scoped API token (secret)
    INFLUXDB_BUCKET  -- target bucket, e.g. helt_prod
    INFLUXDB_ORG     -- org name or ID
    EVENTS_HOST      -- optional: the AppSync Events HTTP host
                        (<id>.appsync-api.<region>.amazonaws.com) of `helt-live`.
                        Set = every telemetry upload is also pushed live to the
                        dashboard's internal viewers (see publish_live). Unset =
                        no push, the write path exactly as before.

Trigger: AWS IoT Rule `helt_to_influx` (and `sandbox_to_influx`) on SQL
    SELECT *, topic() AS mqtt_topic FROM 'helt/pack/+/+'

Stdlib only -- no Lambda layers.
"""

import datetime, hashlib, hmac, http.client, json, os, re, time, urllib.request, urllib.error

INFLUXDB_URL    = os.environ["INFLUXDB_URL"]
INFLUXDB_TOKEN  = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]
INFLUXDB_ORG    = os.environ["INFLUXDB_ORG"]

WRITE_URL = (
    f"{INFLUXDB_URL}/api/v2/write"
    f"?org={INFLUXDB_ORG}&bucket={INFLUXDB_BUCKET}&precision=s"
)

# Telemetry fields by line-protocol type. A key not listed here is dropped.
# Adding a name is safe; moving a name to another list is NOT (its column's
# type is already locked in the bucket it lives in).
UINT_FIELDS = (
    # firmware v1
    "seq", "si_state", "bms_state", "soc_pct", "inv_output_w", "dc_input_w",
    "bms_protections",
    # sandbox power ports (fake_pack.py) + cycle count
    "total_input_w", "total_output_w", "ac_output_w", "dc_output_w",
    "ac_input_w", "solar_input_w", "cycle_count",
    # interval summary (Phase L3)
    "interval_s", "ac_output_avg_w", "dc_output_avg_w",
    "ac_surge_count", "ac_surge_max_w", "fault_count",
    # network position (Phase L2/L3)
    "net_acc_m", "net_src", "net_age_s",
    # per-cell block, every 5 min: cell1_mv .. cell14_mv
    *(f"cell{i}_mv" for i in range(1, 15)),
)
FLOAT_FIELDS = (
    "pack_voltage_v", "current_a", "max_cell_temp_c",
    "enclosure_temp_c", "enclosure_humidity_pct", "soh_pct", "lat", "lon",
    "net_lat", "net_lon",
    # interval summary: inverter NTC channels (0x301) and MPPTs (0x401)
    "inv_filter_inductor_c", "inv_ntc1_c", "inv_control_circuitry_c",
    "inv_rectifier_diode_hs_c", "inv_igbt1_c", "inv_ntc5_c",
    "inv_dcdc_fet_hs_c", "inv_ac_charger_hs_c",
    "mppt_solar_temp_c", "mppt_ac_temp_c",
    # per-cell block: cell_temp1_c .. cell_temp5_c
    *(f"cell_temp{i}_c" for i in range(1, 6)),
)
INT_FIELDS = (
    "power_w",            # firmware v1, signed
    "batt_power_avg_w",   # interval summary, + = charge
)

def fault_source(code):
    """Which node a fault code belongs to (firmware error_manager.h): the
    BMS 0x040 wire codes (incl. the 0x20 / 0x21 SYS-faults it raises for the
    inverter and DC board), the inverter 0x063 bitmap (0x40-0x5F), the DC
    board 0x070 bitmap (0x60-0x7F)."""
    if 0x01 <= code <= 0x3F: return "bms"
    if 0x40 <= code <= 0x5F: return "inv"
    if 0x60 <= code <= 0x7F: return "dc"
    return "other"

def lambda_handler(event, context):
    topic = event.get("mqtt_topic", "")
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "helt" or parts[1] != "pack":
        print(f"WARN: unexpected topic: {topic}")
        return {"statusCode": 400}

    pack_id, suffix = parts[2], parts[3]
    lines = []
    # what this upload adds, exactly as written: pushed after the write
    live = {"pack_id": pack_id, "samples": [], "faults": []}

    if suffix == "telemetry" and event.get("schema") == "v1":
        samples = event.get("samples", [])
        # Pre-SNTP samples ship with ts=0 / ts_synced=false and the cloud side
        # post-corrects (CLOUD_SYNC_DESIGN.md decision 6): anchor them on the
        # first synced sample of the batch via the seq counter, which advances
        # once per CONFIG_CLOUD_SAMPLE_PERIOD_MS (1000 ms). A batch with no
        # synced sample drops them -- ts=0 is 1970, outside the bucket's
        # retention, and IOx rejects the line (HTTP 400 "partial write").
        anchor = next((s for s in samples if s.get("ts_synced") and s.get("ts")), None)
        dropped = 0
        dropped_faults = 0
        for s in samples:
            ts = int(s.get("ts", 0))
            if ts <= 0:
                if anchor is None or "seq" not in s:
                    dropped += 1
                    dropped_faults += len(s.get("faults") or [])
                    continue
                ts = int(anchor["ts"]) - (int(anchor["seq"]) - int(s["seq"]))
            tags = f"pack_id={esc_tag(pack_id)},ts_synced={'true' if s.get('ts_synced') else 'false'}"
            fields = []
            vals = {}                  # the same values, for the live push
            for f in UINT_FIELDS:
                if f in s: vals[f] = int(s[f]); fields.append(f"{f}={vals[f]}u")
            for f in FLOAT_FIELDS:
                if f in s: vals[f] = float(s[f]); fields.append(f"{f}={vals[f]}")
            for f in INT_FIELDS:
                if f in s: vals[f] = int(s[f]); fields.append(f"{f}={vals[f]}i")
            if fields:
                lines.append(f"telemetry,{tags} {','.join(fields)} {ts}")
                live["samples"].append({"ts": ts, "ts_synced": bool(s.get("ts_synced")), **vals})

            # Interval summary faults (Phase L3): one point each, dated at the
            # sample's (anchored) ts minus its age. Two of the same code in
            # the same second share a point -- fault_count above has them all.
            for flt in s.get("faults") or []:
                try:
                    code, age = int(flt["code"]), int(flt["age_s"])
                except (KeyError, TypeError, ValueError):
                    print(f"WARN: {pack_id}: malformed fault entry {flt!r}")
                    continue
                if not (0 <= code <= 0xFF) or age < 0:
                    print(f"WARN: {pack_id}: fault out of range {flt!r}")
                    continue
                ftags = (f"pack_id={esc_tag(pack_id)},src={fault_source(code)},"
                         f"code=0x{code:02X}")
                lines.append(f"pack_fault,{ftags} n=1u {ts - age}")
                live["faults"].append({"t": ts - age, "src": fault_source(code),
                                       "code": f"0x{code:02X}"})
        if dropped:
            print(f"WARN: {pack_id}: dropped {dropped} unsynced samples "
                  f"({dropped_faults} faults) (no synced anchor in batch)")

    elif suffix == "status":
        tags = f"pack_id={esc_tag(pack_id)}"
        ts = int(time.time())
        if "ack" in event:
            ack = event["ack"]
            atags = f"{tags},status={esc_tag(str(ack.get('status','?')))}"
            af = []
            if "request_id" in ack: af.append(f"request_id={int(ack['request_id'])}u")
            if ack.get("result"):   af.append(f'result="{esc_fstr(str(ack["result"]))}"')
            if af: lines.append(f"pack_command_ack,{atags} {','.join(af)} {ts}")
        elif "online" in event:
            sf = [f"online={'true' if event['online'] else 'false'}"]
            for k in ("fw_version","ip"):
                if event.get(k): sf.append(f'{k}="{esc_fstr(str(event[k]))}"')
            for k in ("uptime_s","si_state"):
                if k in event: sf.append(f"{k}={int(event[k])}u")
            lines.append(f"pack_status,{tags} {','.join(sf)} {ts}")

    if lines:
        body = "\n".join(lines).encode()
        req = urllib.request.Request(WRITE_URL, data=body, method="POST",
            headers={"Authorization": f"Token {INFLUXDB_TOKEN}", "Content-Type": "text/plain"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as e:
            print(f"ERROR: InfluxDB {e.code}: {e.read().decode()[:300]}")
            if e.code < 500:
                # A 4xx is a payload / schema / retention rejection. The IoT
                # Rule invokes us asynchronously, and Lambda's default retry
                # would replay the identical batch twice more to the same
                # answer -- so swallow it; the ERROR line above is the record.
                return {"statusCode": 200, "body": f"influx {e.code}: {len(lines)} lines rejected"}
            raise
        if live["samples"]:
            publish_live(live)
    return {"statusCode": 200, "body": f"{len(lines)} lines"}


# ---- live push (dashboard "H"): internal viewers get each upload in seconds --
# After the InfluxDB write -- never before, never instead -- the upload's
# samples (as written: anchored ts, known fields, typed) and faults go to the
# AppSync Events channel /packs/<pack_id> of the `helt-live` Event API, which
# only internal users may subscribe to (its onSubscribe handler checks the '*'
# ALL row in helt_entitlements). Best-effort: a short timeout, every failure
# logged as a WARN and swallowed, so the write and the Lambda's result never
# depend on it (a lost push costs the viewer nothing: the dashboard re-reads
# the API on reconnect and falls back to polling). Signed with the function
# role's credentials (appsync:EventPublish on the `packs` namespace).
EVENTS_HOST = os.environ.get("EVENTS_HOST", "")
EVENTS_TIMEOUT_S = 2.0
# a channel segment: 1-50 alphanumerics and dashes, not starting/ending with one
CHANNEL_SEG = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,48}[A-Za-z0-9])?$")
_events_conn = None            # kept across warm invocations: no TLS handshake each time


def sigv4(method, host, path, query, headers, body, region, service, key_id, secret, token, now):
    """AWS Signature V4: `headers` plus host, x-amz-date, the session token
    (if any) and authorization. `query` is the canonical query string."""
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    h = {k.lower(): str(v).strip() for k, v in headers.items()}
    h.update({"host": host, "x-amz-date": amz_date})
    if token:
        h["x-amz-security-token"] = token
    signed = ";".join(sorted(h))
    canonical = "\n".join([method, path, query,
                           "".join(f"{k}:{h[k]}\n" for k in sorted(h)),
                           signed, hashlib.sha256(body).hexdigest()])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + secret).encode()
    for part in (day, region, service, "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = (f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, "
                          f"SignedHeaders={signed}, Signature={sig}")
    return h


def publish_live(upload):
    """POST one upload to /packs/<pack_id>. Never raises."""
    global _events_conn
    pack_id = upload["pack_id"]
    if not EVENTS_HOST or not CHANNEL_SEG.match(pack_id):
        return
    t0 = time.monotonic()
    try:
        body = json.dumps({"channel": f"/packs/{pack_id}",
                           "events": [json.dumps(upload, separators=(",", ":"))]},
                          separators=(",", ":")).encode()
        for attempt in (1, 2):
            reused = _events_conn is not None
            try:
                if _events_conn is None:
                    _events_conn = http.client.HTTPSConnection(EVENTS_HOST, timeout=EVENTS_TIMEOUT_S)
                headers = sigv4("POST", EVENTS_HOST, "/event", "",
                                {"content-type": "application/json"}, body,
                                os.environ.get("AWS_REGION", "us-east-1"), "appsync",
                                os.environ["AWS_ACCESS_KEY_ID"], os.environ["AWS_SECRET_ACCESS_KEY"],
                                os.environ.get("AWS_SESSION_TOKEN"),
                                datetime.datetime.now(datetime.timezone.utc))
                _events_conn.request("POST", "/event", body=body, headers=headers)
                r = _events_conn.getresponse()
                ans = r.read()
                break
            except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError):
                # the kept connection was closed by the far end while idle:
                # once, on a fresh one, if there is time
                _events_conn = None
                if attempt == 2 or not reused or time.monotonic() - t0 > EVENTS_TIMEOUT_S / 2:
                    raise
        if r.status != 200 or json.loads(ans or b"{}").get("failed"):
            print(f"WARN: live push {pack_id}: HTTP {r.status} {ans[:200]!r}")
    except Exception as e:
        _events_conn = None
        print(f"WARN: live push {pack_id}: {type(e).__name__}: {e} "
              f"({(time.monotonic() - t0) * 1000:.0f} ms)")

def esc_tag(s):  return s.replace(" ","\\ ").replace(",","\\,").replace("=","\\=")
# Backslash first, then the quote: the other order doubles the backslash it
# just added in front of a quote and breaks the line.
def esc_fstr(s): return s.replace("\\","\\\\").replace('"','\\"')
