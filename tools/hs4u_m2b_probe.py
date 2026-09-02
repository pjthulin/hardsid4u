#!/usr/bin/env python3
"""
HardSID 4U — milestone 2b: systematic transport probe.

The question we are answering is NOT "does the handshake work" but the more
basic one underneath it:

    does ANY payload, on ANY endpoint, move the write counter at +0x1C?

Until something moves that counter, the command semantics are untestable.
So we sweep {endpoint} x {payload shape} and print the status delta for each.

Reads status from BOTH the bulk IN (0x81) and the iso IN (0x83) so we also
find out which one is live.

Usage:  python3 hs4u_m2b_probe.py
"""
import struct
import sys
import time

import usb1

VID, PID = 0x6581, 0x8580
IFACE = 0
EP_BULK_IN, EP_BULK_OUT = 0x81, 0x02
EP_ISO_IN, EP_ISO_OUT = 0x83, 0x04
ISO_OUT_SIZE = 512
TIMEOUT = 500

FILLER = b"\xff\xff"


# ---------------------------------------------------------------- status ---

def status_bulk(h):
    try:
        return bytes(h.bulkRead(EP_BULK_IN, 64, timeout=TIMEOUT))
    except usb1.USBError as e:
        return f"ERR {e}"


def status_iso(h, ctx):
    try:
        got = []
        t = h.getTransfer(iso_packets=1)
        t.setIsochronous(EP_ISO_IN, 64,
                         callback=lambda x: got.append(bytes(x.getBuffer())))
        t.submit()
        deadline = time.time() + 0.5
        while not got and time.time() < deadline:
            ctx.handleEvents()
        return got[0] if got else "no data"
    except usb1.USBError as e:
        return f"ERR {e}"


def fields(raw):
    if not isinstance(raw, bytes) or len(raw) < 32:
        return None
    rd, wr, state = struct.unpack_from("<HHH", raw, 0x1A)
    return rd, wr, state


def brief(raw):
    f = fields(raw)
    if f is None:
        return str(raw)
    return f"rd={f[0]:#06x} wr={f[1]:#06x} state={f[2]:#06x}"


# ----------------------------------------------------------------- sends ---

def send_bulk(h, payload):
    return h.bulkWrite(EP_BULK_OUT, payload, timeout=TIMEOUT)


def send_iso(h, ctx, payload):
    payload = payload.ljust(ISO_OUT_SIZE, b"\xff")[:ISO_OUT_SIZE]
    done = []
    t = h.getTransfer(iso_packets=1)
    t.setIsochronous(EP_ISO_OUT, payload, callback=lambda x: done.append(x.getStatus()))
    t.submit()
    deadline = time.time() + 0.5
    while not done and time.time() < deadline:
        ctx.handleEvents()
    return f"iso status {done[0] if done else 'timeout'}"


# --------------------------------------------------------------- payloads ---

def p_filler(n):
    """Pure NOP filler - no command semantics at all. If this moves the
    write counter, the transport is correct and only our command encoding
    is wrong. This is the single most informative test here."""
    return FILLER * (n // 2)


def p_handshake_le(pad_to=0):
    p = b"\xff\xff\x01\x00"
    return p.ljust(pad_to, b"\xff") if pad_to else p


def p_handshake_be(pad_to=0):
    p = b"\xff\xff\x00\x01"
    return p.ljust(pad_to, b"\xff") if pad_to else p


def p_regwrite_le(pad_to=0):
    """Volume register: chip 0, reg 0x18, value 0x0F. data-first order."""
    p = bytes((0x0F, 0x18))
    return p.ljust(pad_to, b"\xff") if pad_to else p


def p_regwrite_be(pad_to=0):
    p = bytes((0x18, 0x0F))
    return p.ljust(pad_to, b"\xff") if pad_to else p


CASES = [
    ("filler 512",          p_filler(512)),
    ("filler 64",           p_filler(64)),
    ("handshake LE  4B",    p_handshake_le()),
    ("handshake LE  512B",  p_handshake_le(512)),
    ("handshake BE  512B",  p_handshake_be(512)),
    ("regwrite  LE  512B",  p_regwrite_le(512)),
    ("regwrite  BE  512B",  p_regwrite_be(512)),
    ("filler 1024",         p_filler(1024)),
]


def main():
    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found")
        h.claimInterface(IFACE)
        try:
            print("[baseline]")
            print(f"  bulk IN 0x81: {brief(status_bulk(h))}")
            print(f"  iso  IN 0x83: {brief(status_iso(h, ctx))}")

            for ep_name, sender in (("BULK 0x02", "bulk"), ("ISO  0x04", "iso")):
                print(f"\n{'='*62}\nOUT endpoint: {ep_name}\n{'='*62}")
                for label, payload in CASES:
                    before = status_bulk(h)
                    try:
                        if sender == "bulk":
                            res = f"wrote {send_bulk(h, payload)}"
                        else:
                            res = send_iso(h, ctx, payload)
                    except usb1.USBError as e:
                        print(f"  {label:20s} SEND FAILED: {e}")
                        continue

                    time.sleep(0.05)
                    after = status_bulk(h)

                    fb, fa = fields(before), fields(after)
                    moved = "  <-- CHANGED" if fb != fa else ""
                    print(f"  {label:20s} {res:22s} {brief(after)}{moved}")
                    if moved and isinstance(after, bytes):
                        print(f"      before: {before[:32].hex(' ')}")
                        print(f"      after : {after[:32].hex(' ')}")

            print("\n" + "="*62)
            print("Reading it:")
            print("  filler moves the counter  -> transport OK, encoding wrong")
            print("  only 512B moves it        -> fixed block size confirmed")
            print("  only ISO moves it         -> bulk pipe is not the command pipe")
            print("  nothing moves at all      -> device needs an init we have")
            print("                               not found yet; next step is a")
            print("                               usbmon/Wireshark capture of the")
            print("                               Windows driver doing it properly")
        finally:
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
