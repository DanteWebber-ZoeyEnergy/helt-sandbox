"""
sandbox-query -- read side of the sandbox. The dashboard calls THIS (never
InfluxDB directly), so the InfluxDB token stays server-side.

Fronted by an API Gateway HTTP API with a Cognito JWT authorizer on five
explicit routes (API_SECURITY_SPEC.md S1) -- the gateway proves WHO the
caller is before this code runs; this Lambda decides WHAT they may see
(spec S2) from the helt_entitlements DynamoDB table:

    user (Cognito sub) -> pack_id (or '*') -> field groups

Routes (all require `Authorization: Bearer <access token>`):
    GET /packs
        -> entitled packs only, joined with live online status
    GET /packs/{pack_id}/latest
        -> latest value of every ENTITLED field (+ status if 'ops' & ?status=1)
    GET /packs/{pack_id}/histories?range=1h
        -> { series: { field: [{t,v},...] } } entitled fields, ONE Influx query
    GET /packs/{pack_id}/history?field=soc_pct&range=1h
        -> one field (403 unless the field is in an entitled group)
    GET /packs/{pack_id}/track?range=1h
        -> GPS trail (requires the 'location' group)
    GET /packs/{pack_id}/faults?range=1h
        -> the pack's faults, newest first (requires the 'ops' group)

Cost rule (spec §5): the in-container cache stores the RAW InfluxDB read,
keyed by pack/range only -- NEVER per user -- and entitlement filtering is
applied per request AFTER the cache. N users on one pack still cost one
Influx query per 30 s window.

401 vs 403 (spec §3): the gateway 401s bad/missing tokens; this code 403s
valid users without entitlement -- '{"error":"forbidden"}', never revealing
whether the pack exists. Every decision emits a one-line JSON audit record.

CORS is handled entirely by the API Gateway CORS configuration (spec §6);
this Lambda no longer emits CORS headers.

Environment variables:
    INFLUXDB_URL / INFLUXDB_READ_TOKEN / INFLUXDB_BUCKET / INFLUXDB_ORG
    INFLUXDB_PROD_BUCKET -- optional. When set, real packs (any pack_id NOT
                            prefixed SANDBOX-) are read from this bucket and
                            INFLUXDB_BUCKET holds only the SANDBOX-* fakes --
                            the same split the two IoT rules make on the write
                            side (helt_to_influx / sandbox_to_influx). The read
                            token must cover both buckets. Unset = single bucket.
    ENTITLEMENTS_TABLE  -- DynamoDB table (default helt_entitlements)

Stdlib only, except boto3 (bundled in the Lambda runtime) for DynamoDB.
"""
import csv, io, json, os, re, time, urllib.request, urllib.error, urllib.parse

import boto3

URL    = os.environ["INFLUXDB_URL"]
TOKEN  = os.environ["INFLUXDB_READ_TOKEN"]
BUCKET = os.environ["INFLUXDB_BUCKET"]
ORG    = os.environ["INFLUXDB_ORG"]
PROD_BUCKET    = os.environ.get("INFLUXDB_PROD_BUCKET", "")
SANDBOX_PREFIX = "SANDBOX-"
DDB_TABLE = os.environ.get("ENTITLEMENTS_TABLE", "helt_entitlements")

_ddb = boto3.client("dynamodb")

# whitelist of identifiers we allow into a Flux string (defence-in-depth --
# never interpolate un-validated input into a query)
SAFE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
# range -> (Flux start, aggregateWindow every | None for raw). Windows are
# sized to keep any range at roughly 200-400 points regardless of sample rate.
RANGES = {
    "15m": ("-15m", None),
    "1h":  ("-1h",  "15s"),
    "6h":  ("-6h",  "2m"),
    "24h": ("-24h", "5m"),
    "7d":  ("-7d",  "30m"),
}

# Entitlements name GROUPS, not fields (spec §3): adding a telemetry field
# later means touching this map only, never the DynamoDB rows. This map is
# part of the payload dependency chain (HANDOFF.md §7).
FIELD_GROUPS = {
    # core = customer-facing performance; ops = internal electricals +
    # diagnostics (voltage/current/cell-temp/protections live there since
    # 2026-08-03); health = wear + ambient environment.
    # Firmware Phase L3 (2026-09-30): the 30 s interval means are core; the
    # surges, faults, component temperatures and the v1 instantaneous power
    # fields are ops; the network position (net_*) is location.
    "core":     {"soc_pct", "total_input_w", "total_output_w",
                 "ac_output_w", "dc_output_w", "ac_input_w", "solar_input_w",
                 "batt_power_avg_w", "ac_output_avg_w", "dc_output_avg_w"},
    "health":   {"soh_pct", "cycle_count", "enclosure_temp_c",
                 "enclosure_humidity_pct"},
    "location": {"lat", "lon",
                 "net_lat", "net_lon", "net_acc_m", "net_src", "net_age_s"},
    "ops":      {"si_state", "bms_state", "seq", "ts_synced",
                 "pack_voltage_v", "current_a", "max_cell_temp_c",
                 "bms_protections",
                 "power_w", "inv_output_w", "dc_input_w",
                 "interval_s", "ac_surge_count", "ac_surge_max_w", "fault_count",
                 "inv_filter_inductor_c", "inv_ntc1_c", "inv_control_circuitry_c",
                 "inv_rectifier_diode_hs_c", "inv_igbt1_c", "inv_ntc5_c",
                 "inv_dcdc_fet_hs_c", "inv_ac_charger_hs_c",
                 "mppt_solar_temp_c", "mppt_ac_temp_c",
                 # per-cell block (2026-10-01), one sample per 5 min
                 *(f"cell{i}_mv" for i in range(1, 15)),
                 *(f"cell_temp{i}_c" for i in range(1, 6))},
}
ALL_FIELDS = set().union(*FIELD_GROUPS.values())

# How a field is downsampled for ranges beyond 15m (aggregateWindow). Counts
# add up, a peak stays a peak, and a state or a source code is taken as it
# stood at the end of the window -- a mean of any of those would be a value
# the pack never reported. Everything else is a mean.
AGG_FN = {
    "ac_surge_count": "sum", "fault_count": "sum",
    "ac_surge_max_w": "max",
    "si_state": "last", "bms_state": "last", "net_src": "last",
}

# The faults route (pack_fault measurement, one point per fault) needs 'ops'.
FAULTS_GROUP = "ops"
FAULTS_LIMIT = 200

# Per-container response cache. InfluxDB bills per query execution ($0.012/100)
# which dwarfs every other per-request cost, so N viewers polling the same pack
# must share one query per TTL window. Holds RAW query results (pre-filter).
CACHE_TTL_S = 30
_cache = {}

# Entitlement cache: 60 s per user cuts DynamoDB reads ~20x at dashboard
# cadence; 60 s staleness on a grant/revoke is accepted (spec §3).
ENT_TTL_S = 60
_ent_cache = {}


def cached(key, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    val = fn()
    _cache[key] = (now + CACHE_TTL_S, val)
    if len(_cache) > 256:                      # bound the container's memory
        for k in [k for k, v in _cache.items() if v[0] <= now]:
            _cache.pop(k, None)
    return val


def entitlements(sub):
    """{pack_id or '*': set(group names)} for this user, cached ENT_TTL_S."""
    now = time.time()
    hit = _ent_cache.get(sub)
    if hit and hit[0] > now:
        return hit[1]
    ent = {}
    resp = _ddb.query(
        TableName=DDB_TABLE,
        KeyConditionExpression="user_id = :u",
        ExpressionAttributeValues={":u": {"S": sub}})
    for item in resp.get("Items", []):
        groups = set(item.get("field_groups", {}).get("SS", []))
        if "ALL" in groups:
            groups = set(FIELD_GROUPS)
        ent[item["pack_id"]["S"]] = groups & set(FIELD_GROUPS)
    _ent_cache[sub] = (now + ENT_TTL_S, ent)
    if len(_ent_cache) > 512:
        for k in [k for k, v in _ent_cache.items() if v[0] <= now]:
            _ent_cache.pop(k, None)
    return ent


def groups_for(ent, pack_id):
    """Groups this user holds on pack_id ('*' row covers every pack)."""
    return ent.get(pack_id) or ent.get("*") or None


def fields_for(groups):
    out = set()
    for g in groups:
        out |= FIELD_GROUPS[g]
    return out


def bucket_for(pack_id):
    """Which bucket holds this pack. Real packs -> PROD_BUCKET, SANDBOX-* fakes
    -> BUCKET; falls back to BUCKET for everything in single-bucket mode."""
    if PROD_BUCKET and not pack_id.startswith(SANDBOX_PREFIX):
        return PROD_BUCKET
    return BUCKET


def in_bucket(bucket, pack_id):
    """True if pack_id BELONGS in bucket under the split above. /packs applies
    this per bucket so a listed pack always resolves to the bucket its
    /latest will read (a stray row written before the IoT rules were scoped
    must not surface a pack whose data lives elsewhere)."""
    return bucket_for(pack_id) == bucket


def reply(code, body):
    return {"statusCode": code,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body)}


def influx_query(flux):
    """POST a Flux query, return a list of dict rows (plain CSV, no annotations)."""
    payload = json.dumps({
        "query": flux,
        "dialect": {"header": True, "annotations": []},
    }).encode()
    req = urllib.request.Request(
        f"{URL}/api/v2/query?org={urllib.parse.quote(ORG)}",
        data=payload, method="POST",
        headers={"Authorization": f"Token {TOKEN}",
                 "Content-Type": "application/json",
                 "Accept": "application/csv"})
    with urllib.request.urlopen(req, timeout=15) as r:
        text = r.read().decode()
    # InfluxDB starts every table (or run of tables with one schema) with its
    # own header row, and the column ORDER can differ between them -- e.g. in
    # a union, aggregateWindow(fn:last) yields _time,_value,_field where
    # fn:mean yields _time,_field,_value. So each header row replaces the
    # current one; a header row is the one whose result/table columns read
    # "result"/"table" (data rows carry "_result" and a table number).
    rows = []
    header = None
    for cells in csv.reader(io.StringIO(text)):
        if not any(cells):
            continue                                  # blank separator line
        if len(cells) > 2 and cells[1] == "result" and cells[2] == "table":
            header = cells
            continue
        if header is None:
            continue
        row = dict(zip(header, cells))
        row.pop("", None)
        rows.append(row)
    return rows


# ---- raw data readers: everything below returns UNFILTERED data and is what
# ---- the response cache stores; entitlement filtering happens in _handle().

def all_packs():
    # Liveness = telemetry freshness, NOT status events. A stably-connected
    # pack can go days without a connect/disconnect event (the LWT still lands
    # promptly on a real drop, but fresh data is the honest "online" signal).
    # pack_id is a tag, so InfluxDB returns one table per pack; last() then
    # yields exactly one row per pack seen in the window.
    now = time.time()
    packs = []
    buckets = [BUCKET] + ([PROD_BUCKET] if PROD_BUCKET else [])
    for bucket in buckets:                      # one query per bucket, cached together
        flux = (
            f'from(bucket:"{bucket}")'
            f'|> range(start:-30d)'
            f'|> filter(fn:(r)=> r._measurement=="telemetry" and r._field=="soc_pct")'
            f'|> last()'
            f'|> keep(columns:["pack_id","_time"])'
        )
        for r in influx_query(flux):
            pid = r.get("pack_id")
            if pid and in_bucket(bucket, pid):
                last_seen = _iso_to_epoch(r.get("_time", ""))
                packs.append({"pack_id": pid,
                              "online": (now - last_seen) < 90,   # 3x fw batch period
                              "last_seen": last_seen})
    # One entry per pack: a pack's points are split into one table per
    # ts_synced tag value, so last() returns a row for each -- keep the newest.
    newest = {}
    for p in packs:
        if p["pack_id"] not in newest or p["last_seen"] > newest[p["pack_id"]]["last_seen"]:
            newest[p["pack_id"]] = p
    packs = sorted(newest.values(), key=lambda p: p["pack_id"])
    return packs


def latest_data(pack_id, include_status):
    bucket = bucket_for(pack_id)
    flux = (
        f'from(bucket:"{bucket}")'
        f'|> range(start:-15m)'
        f'|> filter(fn:(r)=> r._measurement=="telemetry" and r.pack_id=="{pack_id}")'
        f'|> last()'
    )
    # telemetry_ts: when each field was last reported. Fields are not all in
    # every sample (the interval summary rides one sample per 30 s, the GNSS
    # and network positions come and go), so "latest" alone cannot say
    # whether a value is current -- the dashboard compares these with
    # updated_ts, e.g. to tell which position is where the pack is now.
    telemetry = {}
    telemetry_ts = {}
    newest = 0
    for r in influx_query(flux):
        f, v = r.get("_field"), r.get("_value")
        t = _iso_to_epoch(r.get("_time", ""))
        if f and v not in (None, ""):
            telemetry[f] = _num(v)
            telemetry_ts[f] = t
        newest = max(newest, t)

    # status costs a second InfluxDB query and liveness now comes from
    # telemetry freshness, so it's opt-in (?status=1, 'ops' group). 30d
    # lookback: the retained status is event-driven, so on a stable connection
    # the newest row can legitimately be days old.
    status = {}
    if include_status:
        flux_status = (
            f'from(bucket:"{bucket}")'
            f'|> range(start:-30d)'
            f'|> filter(fn:(r)=> r._measurement=="pack_status" and r.pack_id=="{pack_id}")'
            f'|> last()'
        )
        for r in influx_query(flux_status):
            f, v = r.get("_field"), r.get("_value")
            if f and v not in (None, ""):
                status[f] = _num(v)

    return {"updated_ts": newest, "telemetry": telemetry,
            "telemetry_ts": telemetry_ts, "status": status}


def _field_is(fields, negate=False):
    """Flux predicate on r._field for a set of KNOWN field names (constants
    from AGG_FN, never user input): an or-chain, or its negation."""
    if negate:
        return " and ".join(f'r._field!="{f}"' for f in sorted(fields))
    return " or ".join(f'r._field=="{f}"' for f in sorted(fields))


def _downsampled(base, every):
    """Flux for `base` (a stream expression) aggregated per window with each
    field's AGG_FN -- still ONE query: the branches are unioned server-side.
    `every` None = raw points (the 15m range)."""
    if not every:
        return base
    by_fn = {}
    for f, fn in AGG_FN.items():
        by_fn.setdefault(fn, set()).add(f)
    lines = [f'base = {base}',
             f'mean_ = base |> filter(fn:(r)=> {_field_is(AGG_FN, negate=True)})'
             f' |> aggregateWindow(every:{every}, fn:mean, createEmpty:false)']
    names = ["mean_"]
    for fn in sorted(by_fn):
        lines.append(f'{fn}_ = base |> filter(fn:(r)=> {_field_is(by_fn[fn])})'
                     f' |> aggregateWindow(every:{every}, fn:{fn}, createEmpty:false)')
        names.append(f"{fn}_")
    lines.append(f'union(tables:[{", ".join(names)}])')
    return "\n".join(lines)


def histories_data(pack_id, rng):
    """Every telemetry field for one pack in ONE Flux query."""
    start, every = RANGES.get(rng, RANGES["1h"])
    base = (
        f'from(bucket:"{bucket_for(pack_id)}")'
        f'|> range(start:{start})'
        f'|> filter(fn:(r)=> r._measurement=="telemetry" and r.pack_id=="{pack_id}")'
    )
    flux = (
        f'{_downsampled(base, every)}'
        f'|> keep(columns:["_time","_field","_value"])'
    )
    series = {}
    for r in influx_query(flux):
        f, v, t = r.get("_field"), r.get("_value"), r.get("_time")
        if f and t and v not in (None, ""):
            series.setdefault(f, []).append({"t": _iso_to_epoch(t), "v": _num(v)})
    for pts in series.values():
        pts.sort(key=lambda p: p["t"])
    return series


def history_data(pack_id, field, rng):
    start, every = RANGES.get(rng, RANGES["1h"])
    fn = AGG_FN.get(field, "mean")
    agg = f'|> aggregateWindow(every:{every}, fn:{fn}, createEmpty:false)' if every else ''
    flux = (
        f'from(bucket:"{bucket_for(pack_id)}")'
        f'|> range(start:{start})'
        f'|> filter(fn:(r)=> r._measurement=="telemetry" and r.pack_id=="{pack_id}" and r._field=="{field}")'
        f'{agg}'
        f'|> keep(columns:["_time","_value"])'
    )
    series = [{"t": _iso_to_epoch(r["_time"]), "v": _num(r["_value"])}
              for r in influx_query(flux) if r.get("_time") and r.get("_value") not in (None, "")]
    series.sort(key=lambda p: p["t"])
    return series


def track_data(pack_id, rng):
    start, every = RANGES.get(rng, RANGES["1h"])
    agg = f'|> aggregateWindow(every:{every}, fn:mean, createEmpty:false)' if every else ''
    flux = (
        f'from(bucket:"{bucket_for(pack_id)}")'
        f'|> range(start:{start})'
        f'|> filter(fn:(r)=> r._measurement=="telemetry" and r.pack_id=="{pack_id}"'
        f' and (r._field=="lat" or r._field=="lon"))'
        f'{agg}'
        f'|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")'
        f'|> keep(columns:["_time","lat","lon"])'
    )
    series = [{"t": _iso_to_epoch(r["_time"]), "lat": _num(r["lat"]), "lon": _num(r["lon"])}
              for r in influx_query(flux)
              if r.get("_time") and r.get("lat") not in (None, "") and r.get("lon") not in (None, "")]
    series.sort(key=lambda p: p["t"])
    return series


def faults_data(pack_id, rng):
    """The pack's faults in the range, newest first (firmware Phase L3: one
    pack_fault point per fault, dated when it was raised; tags src + code).
    Never downsampled -- each row is an event -- and capped at FAULTS_LIMIT."""
    start, _ = RANGES.get(rng, RANGES["1h"])
    flux = (
        f'from(bucket:"{bucket_for(pack_id)}")'
        f'|> range(start:{start})'
        f'|> filter(fn:(r)=> r._measurement=="pack_fault" and r.pack_id=="{pack_id}"'
        f' and r._field=="n")'
        f'|> keep(columns:["_time","src","code"])'
        f'|> group()'
        f'|> sort(columns:["_time"], desc:true)'
        f'|> limit(n:{FAULTS_LIMIT})'
    )
    out = [{"t": _iso_to_epoch(r["_time"]), "src": r.get("src", ""), "code": r.get("code", "")}
           for r in influx_query(flux) if r.get("_time")]
    out.sort(key=lambda p: p["t"], reverse=True)
    return out


# ---- request handling: authorize, read through the cache, filter, audit ----

def _handle(event, audit):
    http = event.get("requestContext", {}).get("http", {})
    if http.get("method", "GET") == "OPTIONS":     # gateway handles preflight;
        audit["route"] = "OPTIONS"                 # kept for direct invokes
        audit["decision"] = "allow"
        return reply(200, {})
    path = event.get("rawPath", "") or http.get("path", "")
    q = event.get("queryStringParameters") or {}

    # identity comes ONLY from the gateway-validated JWT -- never body/URL
    claims = (event.get("requestContext", {}).get("authorizer", {})
              .get("jwt", {}).get("claims", {}) or {})
    sub = claims.get("sub")
    audit["user_id"] = sub
    if not sub:
        # unreachable behind the JWT authorizer; guards route misconfig drift
        return reply(401, {"error": "unauthorized"})
    ent = entitlements(sub)

    segs = [s for s in path.split("/") if s]
    if len(segs) == 1 and segs[0] == "packs":
        audit["route"] = "packs"
        audit["decision"] = "allow"
        packs = cached(("packs",), all_packs)
        return reply(200, {"packs": [p for p in packs
                                     if groups_for(ent, p["pack_id"])]})

    # expected: packs / {pack_id} / {kind}
    if len(segs) >= 3 and segs[0] == "packs":
        pack_id, kind = segs[1], segs[2]
        audit["route"] = kind
        audit["pack_id"] = pack_id
        if not SAFE.match(pack_id):
            return reply(400, {"error": "invalid pack_id"})
        groups = groups_for(ent, pack_id)
        if not groups:
            # existence-neutral: same answer whether the pack exists or not
            return reply(403, {"error": "forbidden"})
        allowed = fields_for(groups)
        rng = q.get("range", "1h")

        if kind == "latest":
            # status is ops-only; key the cache on what we READ, not the user
            inc = q.get("status") == "1" and "ops" in groups
            data = cached(("latest", pack_id, inc),
                          lambda: latest_data(pack_id, inc))
            audit["decision"] = "allow"
            return reply(200, {
                "pack_id": pack_id, "updated_ts": data["updated_ts"],
                "telemetry": {k: v for k, v in data["telemetry"].items()
                              if k in allowed},
                "telemetry_ts": {k: v for k, v in data["telemetry_ts"].items()
                                 if k in allowed},
                "status": data["status"] if inc else {}})

        if kind == "histories":
            data = cached(("histories", pack_id, rng),
                          lambda: histories_data(pack_id, rng))
            audit["decision"] = "allow"
            return reply(200, {"pack_id": pack_id, "range": rng,
                               "series": {k: v for k, v in data.items()
                                          if k in allowed}})

        if kind == "history":
            field = q.get("field", "soc_pct")
            audit["field"] = field
            # tighter than the old regex-only check: must be a KNOWN field...
            if not SAFE.match(field) or field not in ALL_FIELDS:
                return reply(400, {"error": "invalid field"})
            # ...and in a group this user holds on this pack
            if field not in allowed:
                return reply(403, {"error": "forbidden"})
            data = cached(("history", pack_id, field, rng),
                          lambda: history_data(pack_id, field, rng))
            audit["decision"] = "allow"
            return reply(200, {"pack_id": pack_id, "field": field, "series": data})

        if kind == "track":
            if "location" not in groups:
                return reply(403, {"error": "forbidden"})
            data = cached(("track", pack_id, rng),
                          lambda: track_data(pack_id, rng))
            audit["decision"] = "allow"
            return reply(200, {"pack_id": pack_id, "series": data})

        if kind == "faults":
            if FAULTS_GROUP not in groups:
                return reply(403, {"error": "forbidden"})
            data = cached(("faults", pack_id, rng),
                          lambda: faults_data(pack_id, rng))
            audit["decision"] = "allow"
            return reply(200, {"pack_id": pack_id, "range": rng, "faults": data})

    audit["route"] = "unknown"
    return reply(404, {"error": "not found",
                       "hint": "GET /packs | /packs/{id}/latest | /packs/{id}/histories?range=1h | /packs/{id}/history?field=..&range=1h | /packs/{id}/track?range=1h | /packs/{id}/faults?range=1h"})


def lambda_handler(event, context):
    t0 = time.time()
    audit = {"ts": int(t0), "user_id": None, "route": None,
             "pack_id": None, "field": None, "decision": "deny"}
    try:
        return _handle(event, audit)
    finally:
        audit["latency_ms"] = int((time.time() - t0) * 1000)
        print(json.dumps(audit))               # structured audit -> CloudWatch


def _num(v):
    """CSV values arrive as strings; give the client real JSON types."""
    if isinstance(v, str):
        low = v.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return v


def _iso_to_epoch(iso):
    """RFC3339 -> epoch seconds, without pulling in a date library."""
    if not iso:
        return 0
    try:
        from datetime import datetime, timezone
        iso = iso.replace("Z", "+00:00")
        # trim nanoseconds to microseconds if present
        if "." in iso:
            head, tail = iso.split(".", 1)
            frac = tail
            tzpart = ""
            for sign in ("+", "-"):
                if sign in tail:
                    frac, tzpart = tail.split(sign, 1)
                    tzpart = sign + tzpart
                    break
            iso = f"{head}.{frac[:6]}{tzpart}"
        return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0
