# Running the fake pack publisher on a Windows PC

Everything the publisher needs is four files and a Python install — there is no
AWS CLI, no account access, and no repo checkout required on the Windows side.

## 1. What to copy over

Create a folder on the PC (e.g. `C:\helt-publisher\`) with:

```
C:\helt-publisher\
├── fake_pack.py            (from publisher/)
├── requirements.txt        (from publisher/)
└── certs\
    ├── sandbox.cert.pem        (from aws/certs/)
    ├── sandbox.private.key     (from aws/certs/)
    └── AmazonRootCA1.pem       (from aws/certs/)
```

You also need one value: the IoT endpoint (`IOT_ENDPOINT` in `aws/config.env`),
which looks like `xxxxxxxxxx-ats.iot.us-east-1.amazonaws.com`.

> **Transfer the key privately** (USB stick or your own OneDrive — not email or
> a chat app). `sandbox.private.key` is the sandbox device identity. It can only
> talk MQTT to the sandbox topics, but treat any private key as a secret. Do
> not copy `config.env` itself to the PC — it holds the InfluxDB tokens, and
> the publisher doesn't need them.

## 2. Install Python + the one dependency

1. Install Python 3.12+ from https://www.python.org/downloads/windows/ —
   during install, **tick "Add python.exe to PATH"**.
2. In *Command Prompt* or *PowerShell*:

```bat
py --version
py -m pip install -r C:\helt-publisher\requirements.txt
```

(`paho-mqtt` is the only dependency.)

## 3. Run it

Single pack:

```bat
cd C:\helt-publisher
py fake_pack.py --endpoint YOUR-ENDPOINT-ats.iot.us-east-1.amazonaws.com ^
    --cert certs\sandbox.cert.pem --key certs\sandbox.private.key ^
    --ca certs\AmazonRootCA1.pem --pack-id SANDBOX-01
```

The full 5-pack fleet (SANDBOX-01…05, per-pack profiles built into the script —
different home locations, capacities, and cycle counts):

```bat
py fake_pack.py --endpoint YOUR-ENDPOINT-ats.iot.us-east-1.amazonaws.com ^
    --cert certs\sandbox.cert.pem --key certs\sandbox.private.key ^
    --ca certs\AmazonRootCA1.pem --packs 5
```

You should see `CONNECT rc=Success -> ACCEPTED` per pack, then a `[pub]` line
per pack every 5 s. **Ctrl-C stops it cleanly** (publishes a retained
`offline` status for every pack).

Notes:
- Outbound TCP **8883** must be allowed (default-open on home/office networks;
  strict corporate firewalls sometimes block it).
- **Run the publisher in only one place at a time.** MQTT client IDs equal the
  pack IDs, and AWS IoT disconnects the older session when a duplicate client
  ID connects — a Mac and a PC both publishing SANDBOX-01 will fight forever.
- All 5 simulated packs share the one sandbox certificate. That works because
  the sandbox IoT policy is permissive; production packs each carry their own
  cert with CN = pack_id.

## 4. Optional: start automatically

Task Scheduler → *Create Basic Task* → trigger **At log on** → action *Start a
program*: program `py`, arguments as in the command above, *Start in*
`C:\helt-publisher`. Kill it from Task Manager (`python.exe`) — the broker's
Last-Will then flips the packs to offline within the MQTT keepalive (~60 s).
