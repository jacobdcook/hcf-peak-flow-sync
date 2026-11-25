# hcf-peak-flow-sync

Pull readings from a cheap Bluetooth peak flow meter straight to a Linux PC, with no phone app.

It works with the meters the **ePEF** app pairs with. They advertise over Bluetooth LE as `HCF-<serial>`, and Amazon sells them as "Upgrade Spirometer" / digital peak flow meter (PEF + FEV1). Each blow is saved as one JSON line:

```json
{"ts": "2026-11-22T06:45:05", "pef": 476, "fev1": 2.155, "raw": "00021a0b16062d0501dc086b", "synced_at": "2026-11-22T06:46:01"}
```

`ts` is the meter's clock (the script sets it on every connect), `pef` is in L/min and `fev1` in liters.

## Install

```bash
git clone https://github.com/jacobdcook/hcf-peak-flow-sync && cd hcf-peak-flow-sync
python3 -m venv .venv && .venv/bin/pip install bleak
.venv/bin/python pef_sync.py --selftest      # decoder check, no meter needed
```

## Use

Turn the meter on (press its button), keep it within Bluetooth range, then:

```bash
.venv/bin/python pef_sync.py --once -v       # one sync, show the traffic
.venv/bin/python pef_sync.py                 # keep running: syncs whenever the meter wakes
```

| Setting | What it does |
|---|---|
| `PEF_ADDR=AA:BB:...` | Use this meter. Unset = the first `HCF-...` meter seen. |
| `PEF_DATA=/path/readings.jsonl` | Where readings go (default: next to the script). |
| `PEF_NOTIFY="cmd args"` | Run `cmd args "<summary>"` after new blows, e.g. a phone push. Summary: best of the batch and its % of your 14-day best. |
| `--no-ack` | Leave records unsynced on the meter so the ePEF app still sees them. |
| `--linger N` | Seconds to stay connected after a sync (default 45). The meter screen freezes while connected, so 3 is nicer for a background service. |

Add `"ctx": "..."` to a line (for example `"after-medication"` or `"other-person"`) to leave it out of the 14-day best.

### Run in the background (systemd user service)

```ini
# ~/.config/systemd/user/peak-flow.service
[Unit]
Description=Pull peak flow meter readings over Bluetooth
After=bluetooth.target

[Service]
WorkingDirectory=%h/hcf-peak-flow-sync
ExecStart=%h/hcf-peak-flow-sync/.venv/bin/python -u pef_sync.py --linger 3
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
```

`systemctl --user enable --now peak-flow.service`

## Protocol

Reverse-engineered by relaying the ePEF app's traffic (a sample is in `docs/capture-example.log`) and checked against the app's code. One GATT characteristic, `00008200-0000-1000-8000-00805f9b34fb`, used for both writes and notifications.

| App -> meter | Meter reply | Meaning |
|---|---|---|
| `AA 04 F1 01` | `A5 03 F1` | hello |
| `AA 09 FC yy mm dd hh mi ss` | echo | set the clock |
| `AA 03 E1` | `A5 05 E1 <count:2>` | number of unsynced records |
| `AA 05 E2 <idx:2>` | `A5 0F E2 <idx:2> yy mm dd hh mi ss <PEF:2> <FEV1 mL:2>` | read record idx (1-based) |
| `AA 05 E3 <idx:2>` | echo | mark record synced |
| `AA 03 E4` | `A5 05 E4 00 00` | end of sync |
| `AA 03 EA` | | **erases every record on the meter. The script never sends it.** |

Frame = start byte (`AA` out, `A5` in), total length, command, payload. Big-endian, no checksum. The meter keeps up to about 300 records until they are synced.

## Troubleshooting

- `meter not found`: press the button; the meter sleeps within seconds unless something is connected.
- Meter screen frozen: a stale Bluetooth link. `bluetoothctl disconnect <address>`.
- Two people share one meter? It cannot tell them apart. Tag the other person's blows with a `ctx`.

Not a medical device or medical advice. Ask your clinician what your numbers mean.
