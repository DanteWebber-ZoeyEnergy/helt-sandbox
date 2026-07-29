"""
sandbox-query -- read side of the sandbox. The dashboard calls THIS (never
InfluxDB directly), so the InfluxDB token stays server-side.

Fronted by an API Gateway HTTP API (quick-create $default route). Routes:
    GET /packs
        -> [{ "pack_id", "online", "last_seen" }, ...] packs seen in last 7d
        (becomes the entitlement-filtered list in API_SECURITY_SPEC.md S2)
    GET /packs/{pack_id}/latest
        -> latest value of every telemetry field + latest pack_status
    GET /packs/{pack_id}/histories?range=1h
        -> { series: { field: [{t,v},...], ... } } EVERY field in ONE
        InfluxDB query (the dashboard's refresh path -- 1 query instead of 10)
    GET /packs/{pack_id}/history?field=soc_pct&range=1h
        -> [{ "t": <epoch_s>, "v": <number> }, ...] for one field
        (ranges beyond 15m are server-side downsampled via aggregateWindow)
    GET /packs/{pack_id}/track?range=1h
        -> [{ "t": <epoch_s>, "lat": <deg>, "lon": <deg> }, ...] GPS trail

All responses are cached in-container for CACHE_TTL_S so concurrent viewers
share one InfluxDB query per window -- query executions are the dominant cost
(see API_SECURITY_SPEC.md §5). /latest omits the pack_status sub-query unless
?status=1 (the dashboard derives liveness from telemetry freshness instead).

Environment variables:
    INFLUXDB_URL        -- e.g. https://us-east-1-1.aws.cloud2.influxdata.com
    INFLUXDB_READ_TOKEN -- READ-scoped API token (secret)
    INFLUXDB_BUCKET     -- e.g. helt_telemetry
    INFLUXDB_ORG        -- org name or ID

Stdlib only.
"""
import csv, io, json, os, re, time, urllib.request, urllib.error, urllib.parse

URL    = os.environ["INFLUXDB_URL"]
TOKEN  = os.environ["INFLUXDB_READ_TOKEN"]
BUCKET = os.environ["INFLUXDB_BUCKET"]
ORG    = os.environ["INFLUXDB_ORG"]

CORS = {
    "Access-Control-Allow-Origin": "*",            # sandbox; lock to your github.io later
    "Access-Control-Allow-Methods": "GET,OPTIONS",
    "Access-Control-Allow-Headers": "*",
}
# whitelist of identifiers we allow into a Flux string (defence-in-depth even
# though this is a sandbox -- never interpolate un-validated input into a query)
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

# Per-container response cache. InfluxDB bills per query execution ($0.012/100)
# which dwarfs every other per-request cost, so N viewers polling the same pack
# must share one query per TTL window. Containers each hold their own cache;
# at sandbox traffic there are 1-2 warm containers, which is close enough.
CACHE_TTL_S = 30
_cache = {}


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


def reply(code, body):
    return {"statusCode": code,
            "headers": {**CORS, "Content-Type": "application/json"},
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
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        vals = {k: v for k, v in row.items() if k}
        if not any(vals.values()):
            continue                                  # blank separator line
        if all(v == k for k, v in vals.items()):
            continue                                  # repeated header row:
            # InfluxDB emits one header per result table, and DictReader turns
            # every header after the first into a data row whose every value
            # equals its own column name.
        rows.append(row)
    return rows


def get_packs():
    # Liveness = telemetry freshness, NOT status events. A stably-connected
    # pack can go days without a connect/disconnect event (the LWT still lands
    # promptly on a real drop, but fresh data is the honest "online" signal).
    # pack_id is a tag, so InfluxDB returns one table per pack; last() then
    # yields exactly one row per pack seen in the window.
    flux = (
        f'from(bucket:"{BUCKET}")'
        f'|> range(start:-30d)'
        f'|> filter(fn:(r)=> r._measurement=="telemetry" and r._field=="soc_pct")'
        f'|> last()'
        f'|> keep(columns:["pack_id","_time"])'
    )
    now = time.time()
    packs = []
    for r in influx_query(flux):
        pid = r.get("pack_id")
        if pid:
            last_seen = _iso_to_epoch(r.get("_time", ""))
            packs.append({"pack_id": pid,
                          "online": (now - last_seen) < 90,   # 3x fw batch period
                          "last_seen": last_seen})
    packs.sort(key=lambda p: p["pack_id"])
    return reply(200, {"packs": packs})


def get_latest(pack_id, include_status=False):
    flux = (
        f'from(bucket:"{BUCKET}")'
        f'|> range(start:-15m)'
        f'|> filter(fn:(r)=> r._measurement=="telemetry" and r.pack_id=="{pack_id}")'
        f'|> last()'
    )
    telemetry = {}
    newest = 0
    for r in influx_query(flux):
        f, v = r.get("_field"), r.get("_value")
        if f and v not in (None, ""):
            telemetry[f] = _num(v)
        newest = max(newest, _iso_to_epoch(r.get("_time", "")))

    # status costs a second InfluxDB query and liveness now comes from
    # telemetry freshness, so it's opt-in (?status=1). 30d lookback: the
    # retained status is event-driven, so on a stable connection the newest
    # row can legitimately be days old (a real drop still writes a fresh
    # online:false via the LWT immediately).
    status = {}
    if include_status:
        flux_status = (
            f'from(bucket:"{BUCKET}")'
            f'|> range(start:-30d)'
            f'|> filter(fn:(r)=> r._measurement=="pack_status" and r.pack_id=="{pack_id}")'
            f'|> last()'
        )
        for r in influx_query(flux_status):
            f, v = r.get("_field"), r.get("_value")
            if f and v not in (None, ""):
                status[f] = _num(v)

    return reply(200, {"pack_id": pack_id, "updated_ts": newest,
                       "telemetry": telemetry, "status": status})


def get_histories(pack_id, rng):
    """Every telemetry field for one pack in ONE Flux query -- replaces the
    dashboard's 9 history + 1 track calls per refresh."""
    start, every = RANGES.get(rng, RANGES["1h"])
    agg = f'|> aggregateWindow(every:{every}, fn:mean, createEmpty:false)' if every else ''
    flux = (
        f'from(bucket:"{BUCKET}")'
        f'|> range(start:{start})'
        f'|> filter(fn:(r)=> r._measurement=="telemetry" and r.pack_id=="{pack_id}")'
        f'{agg}'
        f'|> keep(columns:["_time","_field","_value"])'
    )
    series = {}
    for r in influx_query(flux):
        f, v, t = r.get("_field"), r.get("_value"), r.get("_time")
        if f and t and v not in (None, ""):
            series.setdefault(f, []).append({"t": _iso_to_epoch(t), "v": _num(v)})
    for pts in series.values():
        pts.sort(key=lambda p: p["t"])
    return reply(200, {"pack_id": pack_id, "range": rng, "series": series})


def get_history(pack_id, field, rng):
    if not SAFE.match(field):
        return reply(400, {"error": "invalid field"})
    start, every = RANGES.get(rng, RANGES["1h"])
    agg = f'|> aggregateWindow(every:{every}, fn:mean, createEmpty:false)' if every else ''
    flux = (
        f'from(bucket:"{BUCKET}")'
        f'|> range(start:{start})'
        f'|> filter(fn:(r)=> r._measurement=="telemetry" and r.pack_id=="{pack_id}" and r._field=="{field}")'
        f'{agg}'
        f'|> keep(columns:["_time","_value"])'
    )
    series = [{"t": _iso_to_epoch(r["_time"]), "v": _num(r["_value"])}
              for r in influx_query(flux) if r.get("_time") and r.get("_value") not in (None, "")]
    series.sort(key=lambda p: p["t"])
    return reply(200, {"pack_id": pack_id, "field": field, "series": series})


def get_track(pack_id, rng):
    start, every = RANGES.get(rng, RANGES["1h"])
    agg = f'|> aggregateWindow(every:{every}, fn:mean, createEmpty:false)' if every else ''
    flux = (
        f'from(bucket:"{BUCKET}")'
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
    return reply(200, {"pack_id": pack_id, "series": series})


def lambda_handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    if method == "OPTIONS":
        return reply(200, {})
    path = event.get("rawPath", "") or http.get("path", "")
    q = event.get("queryStringParameters") or {}

    segs = [s for s in path.split("/") if s]
    if len(segs) == 1 and segs[0] == "packs":
        return cached(("packs",), get_packs)
    # expected: packs / {pack_id} / {kind}
    if len(segs) >= 3 and segs[0] == "packs":
        pack_id, kind = segs[1], segs[2]
        if not SAFE.match(pack_id):
            return reply(400, {"error": "invalid pack_id"})
        rng = q.get("range", "1h")
        if kind == "latest":
            inc = q.get("status") == "1"
            return cached(("latest", pack_id, inc),
                          lambda: get_latest(pack_id, inc))
        if kind == "histories":
            return cached(("histories", pack_id, rng),
                          lambda: get_histories(pack_id, rng))
        if kind == "history":
            field = q.get("field", "soc_pct")
            return cached(("history", pack_id, field, rng),
                          lambda: get_history(pack_id, field, rng))
        if kind == "track":
            return cached(("track", pack_id, rng),
                          lambda: get_track(pack_id, rng))
    return reply(404, {"error": "not found",
                       "hint": "GET /packs | /packs/{id}/latest | /packs/{id}/histories?range=1h | /packs/{id}/history?field=..&range=1h | /packs/{id}/track?range=1h"})


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
