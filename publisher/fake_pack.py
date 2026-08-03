#!/usr/bin/env python3
"""
fake_pack.py -- sandbox telemetry publisher (multi-pack capable).

Stands in for the SI firmware. Publishes the EXACT schema-v1 payload the real
firmware emits, to the same MQTT topics, over the same mutual-TLS transport --
so everything downstream (IoT Core -> Rule -> Lambda -> InfluxDB) is identical
to production. No hardware required. Runs on macOS/Linux/Windows (Python 3.9+).

SANDBOX SCHEMA NOTE: this publisher runs AHEAD of firmware schema v1 -- it adds
soh_pct, cycle_count, lat, lon to each sample, and models the power ports
explicitly: ac_input_w / solar_input_w / ac_output_w / dc_output_w plus
total_input_w / total_output_w (replacing power_w / inv_output_w / dc_input_w).
The firmware serializer in main/cloud_sync.c does not emit these yet.

SANDBOX AUTH NOTE: with --packs N all simulated packs share the one sandbox
certificate. That works because the sandbox IoT policy is permissive; in
production every pack has its own cert with CN == pack_id.

Usage (single pack):
    pip install -r requirements.txt
    python3 fake_pack.py \
        --endpoint xxxxxxxxxx-ats.iot.us-east-1.amazonaws.com \
        --cert   ../aws/certs/sandbox.cert.pem \
        --key    ../aws/certs/sandbox.private.key \
        --ca     ../aws/certs/AmazonRootCA1.pem \
        --pack-id SANDBOX-01

Usage (simulate a 5-pack fleet, built-in per-pack profiles):
    python3 fake_pack.py --endpoint ... --cert ... --key ... --ca ... --packs 5

Ctrl-C to stop (publishes a clean offline status for every pack on the way out).
"""
import argparse, json, logging, random, signal, ssl, sys, time
import paho.mqtt.client as mqtt

# Per-pack fleet profiles for --packs N (id, home location, capacity, wear).
# Homes are spread around the Western Cape so the dashboard map differentiates.
PROFILES = [
    {"pack_id": "SANDBOX-01", "home": (-33.9249, 18.4241), "capacity_wh": 2560.0, "cycles": 182},  # Cape Town
    {"pack_id": "SANDBOX-02", "home": (-33.9346, 18.8610), "capacity_wh": 2560.0, "cycles": 411},  # Stellenbosch
    {"pack_id": "SANDBOX-03", "home": (-33.7342, 18.9621), "capacity_wh": 5120.0, "cycles": 87},   # Paarl (double pack)
    {"pack_id": "SANDBOX-04", "home": (-34.0790, 18.8434), "capacity_wh": 2560.0, "cycles": 655},  # Somerset West
    {"pack_id": "SANDBOX-05", "home": (-33.8100, 18.4800), "capacity_wh": 2560.0, "cycles": 23},   # Bloubergstrand
]


def make_client(pack_id):
    """Build a paho client that works on both paho-mqtt 1.x and 2.x."""
    try:
        from paho.mqtt.enums import CallbackAPIVersion
        return mqtt.Client(CallbackAPIVersion.VERSION2,
                           client_id=pack_id, protocol=mqtt.MQTTv311)
    except ImportError:  # paho-mqtt 1.x
        return mqtt.Client(client_id=pack_id, protocol=mqtt.MQTTv311)


# ---------------------------------------------------------------------------
# Pack simulation
#
# Plausible-but-simple model: a 16S LFP pack cycling CHARGE / REST / DISCHARGE.
# Power flows through four ports: AC input (grid charger, ~constant while
# charging), SOLAR input (MPPT, wanders with cloud cover), AC output (inverter)
# and DC output (12V/USB rail). Battery power = inputs - outputs (with charger/
# inverter efficiency); SoC integrates that over the nominal capacity, voltage
# follows a linearised OCV curve with IR sag, thermals lag |power|, and cycle
# count is equivalent-full-cycle (accumulated discharge energy / capacity).
# Sign convention matches the firmware: positive internal W / A = charging.
# ---------------------------------------------------------------------------

PHASE_STATES = {              # phase -> (si_state, bms_state)
    "CHARGE":    (3, 2),      # placeholder enums; real values come from the
    "DISCHARGE": (4, 3),      # BMS STATE_MACHINE_SPEC when the fw catches up
    "REST":      (5, 4),
}

# Port ratings (W)
AC_OUT_MAX_W = 2000.0         # inverter continuous rating
DC_OUT_MAX_W = 580.0          # DC / USB rail
SOLAR_MAX_W  = 400.0          # MPPT input
AC_IN_W      = 720.0          # grid charger, roughly constant while charging


class PackSim:
    def __init__(self, capacity_wh, cycles, home_lat, home_lon):
        self.capacity_wh = capacity_wh
        self.soc = random.uniform(35.0, 80.0)   # % -- float inside, int on wire
        self.cycles = cycles
        self.discharge_wh = random.uniform(0.0, capacity_wh)  # progress to next EFC
        self.encl_temp = 23.5
        self.cell_temp = 26.0
        self.humidity = 45.0
        self.home = (home_lat, home_lon)
        self.phase = "REST"
        self.phase_left = 5.0                   # start almost immediately
        self.ac_in_w = 0.0                      # per-port setpoints (W)
        self.solar_w = 0.0
        self.ac_out_w = 0.0
        self.dc_out_w = 0.0

    def _next_phase(self):
        if self.phase in ("CHARGE", "DISCHARGE"):
            self.phase = "REST"
            self.ac_in_w = self.solar_w = self.ac_out_w = self.dc_out_w = 0.0
            self.phase_left = random.uniform(60, 300)
            return
        # leaving REST: bias direction by SoC so we don't slam the limits
        if self.soc < 35.0:
            go_charge = random.random() < 0.8
        elif self.soc > 70.0:
            go_charge = random.random() < 0.2
        else:
            go_charge = random.random() < 0.5
        if go_charge:
            self.phase = "CHARGE"
            self.ac_in_w = AC_IN_W
            self.solar_w = random.uniform(0.3, 1.0) * SOLAR_MAX_W
            self.ac_out_w = self.dc_out_w = 0.0
        else:
            self.phase = "DISCHARGE"
            self.ac_in_w = self.solar_w = 0.0
            self.ac_out_w = random.uniform(0.1, 1.0) * AC_OUT_MAX_W
            self.dc_out_w = random.uniform(0.0, 1.0) * DC_OUT_MAX_W
        self.phase_left = random.uniform(180, 900)

    def sample(self, seq, ts, dt):
        """Advance the model by dt seconds and return one schema-v1 sample."""
        self.phase_left -= dt
        if (self.phase_left <= 0
                or (self.phase == "CHARGE" and self.soc >= 95.0)
                or (self.phase == "DISCHARGE" and self.soc <= 15.0)):
            self._next_phase()

        if self.phase == "CHARGE":
            ac_in = self.ac_in_w * random.uniform(0.98, 1.02)   # ~constant grid draw
            self.solar_w = min(SOLAR_MAX_W, max(0.0,            # cloud-cover wander
                self.solar_w + random.uniform(-15.0, 15.0)))
            solar_in = self.solar_w
            ac_out = dc_out = 0.0
            power = (ac_in + solar_in) * 0.95                   # charger efficiency
        elif self.phase == "DISCHARGE":
            ac_in = solar_in = 0.0
            ac_out = min(AC_OUT_MAX_W, self.ac_out_w * random.uniform(0.97, 1.03))
            dc_out = min(DC_OUT_MAX_W, self.dc_out_w * random.uniform(0.97, 1.03))
            power = -(ac_out + dc_out) / 0.94                   # inverter losses
        else:  # REST
            ac_in = solar_in = ac_out = dc_out = 0.0
            power = -15.0 + random.uniform(-8.0, 8.0)           # standby drain

        self.soc += power * dt / 3600.0 / self.capacity_wh * 100.0
        self.soc = min(100.0, max(0.0, self.soc))

        if power < 0:                                   # equivalent full cycles
            self.discharge_wh += -power * dt / 3600.0
            if self.discharge_wh >= self.capacity_wh:
                self.discharge_wh -= self.capacity_wh
                self.cycles += 1

        v_oc = 48.0 + 5.4 * self.soc / 100.0            # 16S LFP, linearised
        current = power / v_oc
        v = v_oc + current * 0.04                       # ~40 mOhm pack resistance

        cell_target = self.encl_temp + abs(power) / 1500.0 * 12.0
        self.cell_temp += (cell_target - self.cell_temp) * min(1.0, dt / 300.0)
        self.encl_temp = min(35.0, max(18.0,
            self.encl_temp + random.uniform(-0.02, 0.02) * dt))
        self.humidity = min(60.0, max(30.0,
            self.humidity + random.uniform(-0.05, 0.05) * dt))

        si_state, bms_state = PHASE_STATES[self.phase]
        return {
            "ts": ts,
            "ts_synced": True,
            "seq": seq,
            "si_state": si_state,
            "bms_state": bms_state,
            "soc_pct": int(round(self.soc)),
            "pack_voltage_v": round(v, 2),
            "current_a": round(current, 2),
            "total_input_w": int(ac_in + solar_in),
            "total_output_w": int(ac_out + dc_out),
            "max_cell_temp_c": round(self.cell_temp, 1),
            "ac_output_w": int(ac_out),
            "dc_output_w": int(dc_out),
            "ac_input_w": int(ac_in),
            "solar_input_w": int(solar_in),
            "bms_protections": 0,
            "enclosure_temp_c": round(self.encl_temp, 1),
            "enclosure_humidity_pct": round(self.humidity, 1),
            "soh_pct": round(100.0 - 0.006 * self.cycles, 1),
            "cycle_count": self.cycles,
            # stationary pack + ~3 m of GPS noise
            "lat": round(self.home[0] + random.gauss(0, 0.00003), 6),
            "lon": round(self.home[1] + random.gauss(0, 0.00003), 6),
        }


class Pack:
    """One simulated pack: its own MQTT connection (client_id = pack_id,
    mirroring production), LWT, retained status, and simulation state."""

    def __init__(self, profile, args):
        self.pack_id = profile["pack_id"]
        self.base = f"helt/pack/{self.pack_id}"
        self.seq = 0
        self.connected = False
        home = profile["home"]
        self.sim = PackSim(profile["capacity_wh"], profile["cycles"],
                           home[0], home[1])

        c = make_client(self.pack_id)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.enable_logger()
        c.tls_set(ca_certs=args.ca, certfile=args.cert, keyfile=args.key,
                  tls_version=ssl.PROTOCOL_TLSv1_2)
        # Last-Will (retained) -- mirrors the firmware LWT: if this process
        # dies ungracefully, the broker publishes offline on our behalf.
        c.will_set(f"{self.base}/status",
                   json.dumps({"online": False, "pack_id": self.pack_id}),
                   qos=1, retain=True)
        self.client = c

    def _on_connect(self, client, userdata, flags, rc, *rest):
        ok = (rc == 0) if isinstance(rc, int) else (not rc.is_failure)
        self.connected = ok
        verdict = "ACCEPTED" if ok else "REJECTED  <-- connect not authorized (cert/policy)"
        print(f"[mqtt] {self.pack_id} CONNECT rc={rc}  -> {verdict}")

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        rc = args[1] if len(args) >= 3 else (args[0] if args else "?")
        print(f"[mqtt] {self.pack_id} DISCONNECT rc={rc}")

    def start(self, endpoint):
        self.client.connect(endpoint, 8883, keepalive=60)
        self.client.loop_start()
        self.client.publish(f"{self.base}/status", json.dumps({
            "online": True, "pack_id": self.pack_id,
            "fw_version": "sandbox-0.3", "uptime_s": 0, "si_state": 4,
            "ip": "127.0.0.1"}), qos=1, retain=True)

    def publish_batch(self, n_samples, period):
        dt = period / n_samples
        t0 = time.time()
        # spread sample timestamps across the batch period, newest last,
        # so points don't collide in InfluxDB (same tags + same second)
        batch = {"schema": "v1", "pack_id": self.pack_id,
                 "samples": [self.sim.sample(self.seq + n,
                                             int(t0 - (n_samples - 1 - n) * dt),
                                             dt)
                             for n in range(n_samples)]}
        self.seq += n_samples
        info = self.client.publish(f"{self.base}/telemetry",
                                   json.dumps(batch), qos=1)
        last = batch["samples"][-1]
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS and self.connected
        note = "sent" if ok else \
               f"NOT DELIVERED (rc={info.rc}, connected={self.connected})"
        print(f"[pub] {self.pack_id}  seq<={self.seq}  {self.sim.phase:<9} "
              f"soc={last['soc_pct']}%  in={last['total_input_w']}W  "
              f"out={last['total_output_w']}W  -> {note}")

    def stop(self, uptime):
        self.client.publish(f"{self.base}/status", json.dumps(
            {"online": False, "pack_id": self.pack_id, "uptime_s": uptime}),
            qos=1, retain=True)


def main():
    ap = argparse.ArgumentParser(description="Sandbox fake-pack MQTT publisher")
    ap.add_argument("--endpoint", required=True,
                    help="AWS IoT Data-ATS endpoint host (no scheme, no port)")
    ap.add_argument("--cert", required=True, help="device certificate PEM")
    ap.add_argument("--key", required=True, help="device private key PEM")
    ap.add_argument("--ca", required=True, help="Amazon Root CA 1 PEM")
    ap.add_argument("--packs", type=int, default=1, choices=range(1, 6),
                    help="simulate N packs from the built-in fleet profiles")
    ap.add_argument("--pack-id", default=None,
                    help="pack id override (single-pack mode only)")
    ap.add_argument("--period", type=float, default=5.0,
                    help="seconds between batches per pack (real firmware: 30)")
    ap.add_argument("--samples", type=int, default=3,
                    help="samples per batch")
    ap.add_argument("--capacity-wh", type=float, default=None,
                    help="capacity override (single-pack mode only)")
    ap.add_argument("--cycles", type=int, default=None,
                    help="starting cycle count override (single-pack mode only)")
    ap.add_argument("--home-lat", type=float, default=None,
                    help="latitude override (single-pack mode only)")
    ap.add_argument("--home-lon", type=float, default=None,
                    help="longitude override (single-pack mode only)")
    a = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s paho %(message)s")

    profiles = [dict(p) for p in PROFILES[:a.packs]]
    if a.packs == 1:            # single-pack overrides keep the old CLI working
        if a.pack_id:              profiles[0]["pack_id"] = a.pack_id
        if a.capacity_wh:          profiles[0]["capacity_wh"] = a.capacity_wh
        if a.cycles is not None:   profiles[0]["cycles"] = a.cycles
        if a.home_lat is not None and a.home_lon is not None:
            profiles[0]["home"] = (a.home_lat, a.home_lon)
    elif a.pack_id or a.capacity_wh or a.cycles is not None or a.home_lat is not None:
        ap.error("--pack-id/--capacity-wh/--cycles/--home-* only apply to --packs 1")

    packs = [Pack(p, a) for p in profiles]
    print(f"[mqtt] connecting {len(packs)} pack(s) to {a.endpoint}:8883 ...")
    for p in packs:
        p.start(a.endpoint)

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("flag", True))
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))

    start = time.time()
    # Rotate through the fleet, one pack per slot, so each pack publishes every
    # `period` seconds and the ingest Lambda never sees an N-pack burst.
    slot = a.period / len(packs)
    i = 0
    try:
        while not stop["flag"]:
            packs[i % len(packs)].publish_batch(a.samples, a.period)
            i += 1
            t_end = time.time() + slot
            while time.time() < t_end and not stop["flag"]:
                time.sleep(0.1)   # small slices keep Ctrl-C responsive
    finally:
        uptime = int(time.time() - start)
        for p in packs:
            p.stop(uptime)
        time.sleep(0.5)  # let the offline statuses flush
        for p in packs:
            p.client.loop_stop()
            p.client.disconnect()
        print(f"\n[mqtt] clean shutdown, published offline status for {len(packs)} pack(s)")


if __name__ == "__main__":
    sys.exit(main())
