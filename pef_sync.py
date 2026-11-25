#!/usr/bin/env python3
"""Pull readings from an HCF Bluetooth peak flow meter (the one the ePEF app pairs with) straight to a PC, no phone.

Protocol captured 2026-11-22 by relaying the ePEF app (see docs/capture-example.log):
  app -> meter: AA <total_len> <cmd> <payload>      meter -> app: A5 <total_len> <cmd> <payload>
  F1 01              hello                      -> A5 03 F1
  FC yy mm dd hh mi ss   set clock              -> echo
  E1                 count of unsynced records  -> E1 <count:2>
  E2 <idx:2>         read record idx (1-based)  -> E2 <idx:2> yy mm dd hh mi ss <PEF L/min:2> <FEV1 mL:2>
  E3 <idx:2>         mark record idx as synced  -> echo
  E4                 end of sync                -> E4 00 00
  EA                 DELETES all data on the meter. Never send.
All multi-byte values big-endian. No checksum. Confirmed against the app's Dart code (blutter).
"""
import argparse, asyncio, json, os, shlex, subprocess, sys, time
from datetime import datetime, timedelta
from pathlib import Path

from bleak import BleakClient, BleakScanner

# Leave PEF_ADDR unset to use the first meter advertising as "HCF-...".
ADDR = os.environ.get("PEF_ADDR")
CHR = "00008200-0000-1000-8000-00805f9b34fb"
HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("PEF_DATA", HERE / "readings.jsonl"))
# Optional: a command that gets the summary line as its last argument (phone ping, chat bot...).
NOTIFY = shlex.split(os.environ.get("PEF_NOTIFY", ""))


def frame(cmd, payload=b""):
    return bytes([0xAA, 3 + len(payload), cmd]) + payload


def parse_record(p):
    """p = payload of an E2 reply: idx(2) yy mm dd hh mi ss pef(2) fev1_ml(2)."""
    if len(p) != 12:
        raise ValueError("unexpected record length: " + p.hex())
    yy, mo, dd, hh, mi, ss = p[2:8]
    ts = datetime(2000 + yy, mo, dd, hh, mi, min(ss, 59))
    return {
        "ts": ts.isoformat(timespec="seconds"),
        "pef": int.from_bytes(p[8:10], "big"),
        "fev1": int.from_bytes(p[10:12], "big") / 1000,
        "raw": p.hex(),
    }


def load():
    if not DATA.exists():
        return []
    return [json.loads(l) for l in DATA.read_text().splitlines() if l.strip()]


def summary(new, allrecs):
    """Text for the phone ping: best of this batch vs the 14-day personal best."""
    best = max(new, key=lambda r: r["pef"])
    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    recent = [r["pef"] for r in allrecs if r["ts"] >= cutoff and not r.get("ctx")] or [best["pef"]]
    pb = max(recent)
    pct = round(100 * best["pef"] / pb)
    line = "Lungs: PEF %d, FEV1 %.2f L (best of %d). %d%% of 14-day best %d." % (
        best["pef"], best["fev1"], len(new), pct, pb)
    if pct < 80:
        line += " Under 80% of your recent best."
    return line


class Meter:
    def __init__(self, client, verbose):
        self.c, self.q, self.verbose = client, asyncio.Queue(), verbose

    def _rx(self, _h, data):
        data = bytes(data)
        if self.verbose:
            print("  <", data.hex(), flush=True)
        self.q.put_nowait(data)

    async def start(self):
        await self.c.start_notify(CHR, self._rx)

    async def ask(self, cmd, payload=b"", timeout=3.0):
        f = frame(cmd, payload)
        for _ in range(2):
            if self.verbose:
                print("  >", f.hex(), flush=True)
            await self.c.write_gatt_char(CHR, f, response=False)
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    r = await asyncio.wait_for(self.q.get(), end - time.monotonic())
                except asyncio.TimeoutError:
                    break
                if len(r) >= 3 and r[0] == 0xA5 and r[2] == cmd and r[1] == len(r):
                    return r[3:]
        raise TimeoutError("no reply to %02x" % cmd)


async def pull(m, ack, known):
    """One sync pass. Returns records not seen before. Saves before acking."""
    count = int.from_bytes(await m.ask(0xE1), "big")
    fresh = []
    for i in range(1, count + 1):
        idx = i.to_bytes(2, "big")
        rec = parse_record(await m.ask(0xE2, idx))
        key = (rec["ts"], rec["pef"], rec["fev1"])
        if key not in known:
            known.add(key)
            rec["synced_at"] = datetime.now().isoformat(timespec="seconds")
            with DATA.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            fresh.append(rec)
        if ack:
            await m.ask(0xE3, idx)
    if ack and count:
        await m.ask(0xE4)
    return fresh


async def session(dev, args, known):
    got = []
    async with BleakClient(dev) as c:
        m = Meter(c, args.verbose)
        await m.start()
        await m.ask(0xF1, b"\x01")
        n = datetime.now()
        await m.ask(0xFC, bytes([n.year - 2000, n.month, n.day, n.hour, n.minute, n.second]))
        end = time.monotonic() + args.linger
        while c.is_connected:
            fresh = await pull(m, args.ack, known)
            for r in fresh:
                print("NEW", r["ts"], "PEF", r["pef"], "FEV1 %.2f" % r["fev1"], flush=True)
            got += fresh
            if time.monotonic() > end:
                break
            await asyncio.sleep(4)
    return got


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="one session then exit")
    ap.add_argument("--no-ack", dest="ack", action="store_false", help="leave records unsynced on the meter")
    ap.add_argument("--linger", type=int, default=45, help="seconds to stay connected polling for new blows")
    ap.add_argument("--no-notify", dest="notify", action="store_false")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    known = {(r["ts"], r["pef"], r["fev1"]) for r in load()}
    while True:
        dev = await (BleakScanner.find_device_by_address(ADDR, timeout=10) if ADDR else
                     BleakScanner.find_device_by_filter(lambda d, _ad: (d.name or "").startswith("HCF"), timeout=10))
        if not dev:
            if args.once:
                print("meter not found (is it on?)")
                return 1
            continue
        got = []
        try:
            got = await session(dev, args, known)
        except Exception as e:  # meter sleeps mid-session all the time; just retry on next wake
            print("session ended:", type(e).__name__, e, flush=True)
        if got and args.notify and NOTIFY:
            subprocess.run(NOTIFY + [summary(got, load())], check=False)
        if args.once:
            return 0
        # The meter stays awake while connected, so back off after an empty session to let it sleep.
        await asyncio.sleep(20 if got else 90)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        r = parse_record(bytes.fromhex("00021a0b16062d0501dc086b"))
        assert (r["ts"], r["pef"], r["fev1"]) == ("2026-11-22T06:45:05", 476, 2.155), r
        r = parse_record(bytes.fromhex("00011a0b16062d01017607a1"))
        assert (r["pef"], r["fev1"]) == (374, 1.953), r
        assert frame(0xE2, b"\x00\x01").hex() == "aa05e20001" and frame(0xE1).hex() == "aa03e1"
        print("selftest ok")
        sys.exit(0)
    sys.exit(asyncio.run(main()) or 0)
