#!/usr/bin/env python3
"""
HardSID 4U — replay the captured Windows session byte for byte.

hs4u_capture_writes.bin holds every byte ACID64 wrote to BULK OUT 0x02 during
a real playback session, concatenated in order. hs4u_capture_writes.json gives
the original transfer boundaries and timestamps.

If this plays music on macOS, the transport is proven end to end and the only
thing wrong with our own streams is their content. If it is silent while the
same bytes made sound under Windows, the difference is in how the host drives
the device, not in what it sends.

The capture used no vendor control transfers at all - only GET_DESCRIPTOR and
SET_CONFIGURATION - so there is nothing else to reproduce.

Usage:
  python3 hs4u_replay.py                 # replay with original transfer sizes
  python3 hs4u_replay.py --init-only     # just the 5 init writes, then stop
  python3 hs4u_replay.py --poll          # also poll status ~1600/s like ACID64
"""
import argparse
import json
import os
import struct
import sys
import threading
import time

import usb1

VID, PID = 0x6581, 0x8580
IFACE = 0
EP_IN, EP_OUT = 0x81, 0x02
RING = 0x2000
TIMEOUT = 1000
FILLER = b"\xff\xff"

HERE = os.path.dirname(os.path.abspath(__file__))
BLOB = os.path.join(HERE, "hs4u_capture_writes.bin")
INDEX = os.path.join(HERE, "hs4u_capture_writes.json")


def ptrs(h):
    raw = bytes(h.bulkRead(EP_IN, 64, timeout=TIMEOUT))
    rd, wr, state = struct.unpack_from("<HHH", raw, 0x1A)
    used = (wr - rd) & (RING - 1)
    return rd, wr, state, RING - used


def ensure_started(h):
    rd, wr, state, free = ptrs(h)
    print(f"[link] rd={rd:#06x} wr={wr:#06x} state={state:#06x} free={free}")
    if state & 0x80:
        print("  engine already running")
        return
    for attempt in range(1, 9):
        rd, wr, state, free = ptrs(h)
        if free >= 1024:
            h.bulkWrite(EP_OUT, FILLER * 256, timeout=TIMEOUT)
            time.sleep(0.15)
            if ptrs(h)[2] & 0x80:
                print(f"  started after {attempt} filler block(s)")
                return
        h.bulkWrite(EP_OUT, b"\xff\xff\x01\x00", timeout=TIMEOUT)
        time.sleep(0.15)
        if ptrs(h)[2] & 0x80:
            print(f"  started after short packet (attempt {attempt})")
            return
    sys.exit("  could not start engine - power-cycle and retry")


class Poller(threading.Thread):
    """ACID64 reads status ~1600 times/sec. If the device needs that to run
    its engine, replaying writes alone would not be enough."""

    def __init__(self, h):
        super().__init__(daemon=True)
        self.h = h
        self.stop = threading.Event()
        self.count = 0

    def run(self):
        while not self.stop.is_set():
            try:
                self.h.bulkRead(EP_IN, 64, timeout=200)
                self.count += 1
            except usb1.USBError:
                pass
            time.sleep(0.0006)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init-only", action="store_true")
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--realtime", action="store_true",
                    help="honour the original inter-write delays")
    args = ap.parse_args()

    if not os.path.exists(BLOB):
        sys.exit(f"missing {BLOB}")
    blob = open(BLOB, "rb").read()
    idx = json.load(open(INDEX))
    if args.init_only:
        idx = idx[:5]
    print(f"[capture] {len(idx)} transfers, {sum(i['len'] for i in idx)} bytes")

    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found")
        h.claimInterface(IFACE)
        poller = None
        try:
            ensure_started(h)

            if args.poll:
                poller = Poller(h)
                poller.start()
                print("[poll] background status polling started")

            t_start = time.time()
            for n, item in enumerate(idx, 1):
                chunk = blob[item["off"]:item["off"] + item["len"]]

                if args.realtime:
                    target = t_start + item["t"]
                    while time.time() < target:
                        time.sleep(0.001)

                # flow control: wait for room for this whole transfer
                waited = 0.0
                while True:
                    rd, wr, state, free = ptrs(h)
                    if free >= item["len"] + 512:
                        break
                    time.sleep(0.002)
                    waited += 0.002
                    if waited > 10:
                        print(f"  transfer {n}: gave up waiting for room")
                        break

                h.bulkWrite(EP_OUT, chunk, timeout=TIMEOUT)
                rd, wr, state, free = ptrs(h)
                print(f"  #{n:2d} len={item['len']:5d} t={item['t']:7.3f}s  "
                      f"rd={rd:#06x} wr={wr:#06x} state={state:#06x} free={free}")

            print("\n[drain] waiting for the device to finish playing...")
            t0 = time.time()
            while time.time() - t0 < 25:
                rd, wr, state, free = ptrs(h)
                if free >= RING - 512:
                    break
                time.sleep(0.1)
            rd, wr, state, free = ptrs(h)
            print(f"[end] rd={rd:#06x} wr={wr:#06x} state={state:#06x} free={free}")
            if poller:
                print(f"[poll] {poller.count} status reads during replay")

            print("\nDid you hear music?")
            print("  yes -> transport is proven; our own streams were the problem")
            print("  no  -> the difference is in how the host drives the device,")
            print("         not in the bytes. Try --poll, then --realtime.")
        finally:
            if poller:
                poller.stop.set()
                poller.join(timeout=1)
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
