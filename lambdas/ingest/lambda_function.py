"""
sandbox-ingest -- production helt-iot-influx Lambda PLUS sandbox-ahead fields.

DIVERGENCE NOTE: the sandbox schema runs ahead of firmware v1 -- this copy
additionally parses soh_pct, cycle_count, lat, lon, and the power-port fields
total_input_w / total_output_w / ac_output_w / dc_output_w / ac_input_w /
solar_input_w (which replace power_w / inv_output_w / dc_input_w). Fold these
into the production cloud/aws/lambda_function.py when the firmware serializer
catches up.

AWS IoT Rule -> InfluxDB Cloud. Receives MQTT messages via the IoT Rule SQL,
transforms JSON to InfluxDB line protocol, POSTs to the v2 write API.

Environment variables (set on the Lambda):
    INFLUXDB_URL     -- write endpoint, e.g. https://us-east-1-1.aws.cloud2.influxdata.com
    INFLUXDB_TOKEN   -- WRITE-scoped API token (secret)
    INFLUXDB_BUCKET  -- target bucket, e.g. helt_telemetry
    INFLUXDB_ORG     -- org name or ID

Trigger: IoT Rule on SQL  SELECT *, topic() AS mqtt_topic FROM 'helt/pack/+/+'
Stdlib only -- no Lambda layers.
"""
import json, os, time, urllib.request, urllib.error

INFLUXDB_URL    = os.environ["INFLUXDB_URL"]
INFLUXDB_TOKEN  = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]
INFLUXDB_ORG    = os.environ["INFLUXDB_ORG"]

WRITE_URL = (
    f"{INFLUXDB_URL}/api/v2/write"
    f"?org={INFLUXDB_ORG}&bucket={INFLUXDB_BUCKET}&precision=s"
)


def lambda_handler(event, context):
    topic = event.get("mqtt_topic", "")
    parts = topic.split("/")
    if len(parts) != 4 or parts[0] != "helt" or parts[1] != "pack":
        print(f"WARN: unexpected topic: {topic}")
        return {"statusCode": 400}

    pack_id, suffix = parts[2], parts[3]
    lines = []

    if suffix == "telemetry" and event.get("schema") == "v1":
        for s in event.get("samples", []):
            tags = f"pack_id={esc_tag(pack_id)},ts_synced={'true' if s.get('ts_synced') else 'false'}"
            fields = []
            for f in ("seq", "si_state", "bms_state", "soc_pct", "total_input_w", "total_output_w",
                      "ac_output_w", "dc_output_w", "ac_input_w", "solar_input_w",
                      "bms_protections", "cycle_count"):
                if f in s: fields.append(f"{f}={int(s[f])}u")
            for f in ("pack_voltage_v", "current_a", "max_cell_temp_c", "enclosure_temp_c", "enclosure_humidity_pct", "soh_pct", "lat", "lon"):
                if f in s: fields.append(f"{f}={float(s[f])}")
            if fields:
                lines.append(f"telemetry,{tags} {','.join(fields)} {s.get('ts', 0)}")

    elif suffix == "status":
        tags = f"pack_id={esc_tag(pack_id)}"
        ts = int(time.time())
        if "ack" in event:
            ack = event["ack"]
            atags = f"{tags},status={esc_tag(str(ack.get('status', '?')))}"
            af = []
            if "request_id" in ack: af.append(f"request_id={int(ack['request_id'])}u")
            if ack.get("result"):   af.append(f'result="{esc_fstr(str(ack["result"]))}"')
            if af: lines.append(f"pack_command_ack,{atags} {','.join(af)} {ts}")
        elif "online" in event:
            sf = [f"online={'true' if event['online'] else 'false'}"]
            for k in ("fw_version", "ip"):
                if event.get(k): sf.append(f'{k}="{esc_fstr(str(event[k]))}"')
            for k in ("uptime_s", "si_state"):
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
            raise
    return {"statusCode": 200, "body": f"{len(lines)} lines"}


def esc_tag(s):  return s.replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")
def esc_fstr(s): return s.replace('"', '\\"').replace("\\", "\\\\")
