#!/usr/bin/env python3
"""
HardSID 4U — milestone 2: system-mode handshake.

Fixes the bad sanity check in hs4u_m1_status.py: rd == wr == 0x2000 means the
device buffer is EMPTY, not that the fields are garbage. These are counters,
so 0x2000 is a legal value.

  EP 0x81  IN   BULK  64   -> status block (only first 32 bytes are live)
  EP 0x02  OUT  BULK  64   -> command stream

Usage:  python3 hs4u_m2_handshake.py [--pad] [--iso-out]
        --pad      pad the command payload to 512 bytes with FF FF filler
        --iso-out  send on ISO OUT 0x04 instead of BULK OUT 0x02
"""
import struct
import sys
import time

import usb1

VID, PID = 0x6581, 0x8580
IFACE = 0
EP_IN, EP_BULK_OUT, EP_ISO_OUT = 0x81, 0x02, 0x04
STATUS_LEN = 64
TIMEOUT = 1000

SYS_MODE_SIDPLAY = 1
SYS_MODE_VST = 2

PAD = "--pad" in sys.argv
USE_ISO = "--iso-out" in sys.argv

FIELDS = [
    (0x12, "H", "capacity?"),
    (0x16, "H", "unknown (0x07D0 = 2000 when idle)"),
    (0x1A, "H", "read counter"),
    (0x1C, "H", "write counter"),
    (0x1E, "H", "state (low nibble = mode, bit 7 = ack)"),
]


def read_status(h):
    return bytes(h.bulkRead(EP_IN, STATUS_LEN, timeout=TIMEOUT))


def parse(data):
    rd, wr, state = struct.unpack_from("<HHH", data, 0x1A)
    free = 0x2000 if rd == wr else ((rd - wr) & 0x1FFF)
    return {"rd": rd, "wr": wr, "free": free, "state": state,
            "mode": state & 0x0F, "ack": bool(state & 0x80)}


def dump(tag, raw):
    st = parse(raw)
    print(f"  {tag}")
    print(f"    {raw[:32].hex(' ')}")
    for off, fmt, name in FIELDS:
        val = struct.unpack_from("<" + fmt, raw, off)[0]
        print(f"      +{off:#04x} = {val:#06x} ({val:5d})  {name}")
    print(f"    -> mode={st['mode']} ack={st['ack']} free={st['free']}")
    return st


def diff(before, after):
    changed = [(i, before[i], after[i]) for i in range(32)
               if before[i] != after[i]]
    if not changed:
        print("    (status block unchanged)")
    else:
        print("    changed bytes:")
        for i, b, a in changed:
            print(f"      +{i:#04x}: {b:02x} -> {a:02x}")


def send(h, payload):
    if PAD:
        payload = payload + b"\xff\xff" * ((512 - len(payload)) // 2)
    if USE_ISO:
        # one 512-byte iso packet per frame
        payload = payload.ljust(512, b"\xff")
        done = []
        t = h.getTransfer(iso_packets=1)
        t.setIsochronous(EP_ISO_OUT, payload, callback=lambda x: done.append(x))
        t.submit()
        while not done:
            h.getContext().handleEvents()
        return len(payload)
    return h.bulkWrite(EP_BULK_OUT, payload, timeout=TIMEOUT)


def handshake(h, data_first, mode=SYS_MODE_SIDPLAY):
    cmd = (lambda c, d: bytes((d, c))) if data_first else (lambda c, d: bytes((c, d)))
    payload = cmd(0xFF, 0xFF) + cmd(0x00, mode)

    label = "data-first (LE)" if data_first else "cmd-first (BE)"
    ep = "ISO 0x04" if USE_ISO else "BULK 0x02"
    print(f"\n[handshake] {label} on {ep}{' padded to 512' if PAD else ''}")
    print(f"  payload head: {payload.hex(' ')}")

    before = read_status(h)
    n = send(h, payload)
    print(f"  wrote {n} bytes")

    deadline = time.time() + 2.0
    after = before
    while time.time() < deadline:
        after = read_status(h)
        st = parse(after)
        if st["mode"] == mode and st["ack"]:
            print("  ACK received")
            dump("after", after)
            diff(before, after)
            return True
        time.sleep(0.02)

    print("  no ack within 2s")
    dump("after", after)
    diff(before, after)
    return False


def main():
    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found - powered on? DC LED green?")
        h.claimInterface(IFACE)
        try:
            print("[status] initial")
            dump("idle", read_status(h))

            if handshake(h, data_first=True):
                print("\nRESULT: wire order is DATA byte first (little-endian word).")
                print("        cmd(c, d) = bytes((d, c))")
            elif handshake(h, data_first=False):
                print("\nRESULT: wire order is COMMAND byte first (big-endian).")
                print("        cmd(c, d) = bytes((c, d)) - update the notes.")
            else:
                print("\nRESULT: no ack either way on this endpoint.")
                print("  Next, in order:")
                print("    python3 hs4u_m2_handshake.py --pad")
                print("    python3 hs4u_m2_handshake.py --iso-out")
                print("    python3 hs4u_m2_handshake.py --iso-out --pad")
                print("  If the write counter at +0x1C moves but +0x1E never acks,")
                print("  the device IS consuming data and only the mode field is")
                print("  wrong - send me the diff output.")
        finally:
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
