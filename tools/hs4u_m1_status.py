#!/usr/bin/env python3
"""
HardSID 4U — milestone 1 + 2 over the BULK endpoints.

  EP 0x81  IN   BULK  64   -> status block
  EP 0x02  OUT  BULK  64   -> command stream

Milestone 1: read the status block, sanity-check the ring pointers.
Milestone 2: system-mode handshake, trying both command-word byte orders.

Usage:  python3 hs4u_m1_status.py [--verbose]
"""
import struct
import sys
import time

import usb1

VID, PID = 0x6581, 0x8580
IFACE = 0
EP_IN, EP_OUT = 0x81, 0x02
STATUS_LEN = 64
TIMEOUT = 1000

SYS_MODE_SIDPLAY = 1
VERBOSE = "--verbose" in sys.argv


def read_status(h):
    return bytes(h.bulkRead(EP_IN, STATUS_LEN, timeout=TIMEOUT))


def parse_status(data):
    """Fields per hardsid.dll; see protocol notes section 6."""
    rd, wr, state = struct.unpack_from("<HHH", data, 0x1A)
    if rd == wr:
        free = 0x2000
    else:
        free = (rd - wr) & 0x1FFF
    return {
        "rd": rd,
        "wr": wr,
        "free": free,
        "state": state,
        "mode": state & 0x0F,
        "ack": bool(state & 0x80),
        "plausible": rd < 0x2000 and wr < 0x2000,
    }


def show(tag, st, raw=None):
    print(f"  {tag}: rd={st['rd']:#06x} wr={st['wr']:#06x} free={st['free']:5d} "
          f"state={st['state']:#06x} mode={st['mode']} ack={st['ack']}")
    if VERBOSE and raw is not None:
        print(f"        raw: {raw.hex(' ')}")


def make_cmd(data_first):
    """Return a cmd(c, d) -> bytes builder for the chosen wire order."""
    if data_first:
        return lambda c, d: bytes((d, c))   # little-endian (cmd<<8)|data
    return lambda c, d: bytes((c, d))       # big-endian / cmd first


def try_handshake(h, data_first, mode=SYS_MODE_SIDPLAY):
    """FF FF escape, then 00 <mode>. Poll until status[0x1E] == mode | 0x80."""
    cmd = make_cmd(data_first)
    payload = cmd(0xFF, 0xFF) + cmd(0x00, mode)

    order = "data-first (LE)" if data_first else "cmd-first (BE)"
    print(f"\n[handshake] trying {order}: {payload.hex(' ')}")

    written = h.bulkWrite(EP_OUT, payload, timeout=TIMEOUT)
    print(f"  wrote {written} bytes")

    deadline = time.time() + 2.0
    while time.time() < deadline:
        raw = read_status(h)
        st = parse_status(raw)
        if st["mode"] == mode and st["ack"]:
            show("ACK", st, raw)
            return True
        time.sleep(0.02)

    show("no ack", st, raw)
    return False


def main():
    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found - powered on? front switch to ON? DC LED green?")

        h.claimInterface(IFACE)
        try:
            # --- milestone 1 -------------------------------------------------
            print("[milestone 1] status block via bulk IN 0x81")
            raw = read_status(h)
            st = parse_status(raw)
            show("initial", st, raw)

            if not st["plausible"]:
                print("\n  Ring pointers are out of range (>= 0x2000).")
                print("  Either the field offsets differ on this firmware, or the")
                print("  status block is on the ISO IN endpoint (0x83) instead.")
                print(f"  Full block:\n  {raw.hex(' ')}")
                return

            print("  ring pointers look sane - transport confirmed.")

            # does the device tick on its own?
            time.sleep(0.2)
            st2 = parse_status(read_status(h))
            show("after 200ms", st2)
            if (st2["rd"], st2["wr"]) != (st["rd"], st["wr"]):
                print("  pointers moved on their own - device is running a stream.")

            # --- milestone 2 -------------------------------------------------
            print("\n[milestone 2] system-mode handshake")
            if try_handshake(h, data_first=True):
                print("\nRESULT: handshake OK, wire order is DATA byte first "
                      "(little-endian word). Use cmd(c,d) = bytes((d,c)).")
            elif try_handshake(h, data_first=False):
                print("\nRESULT: handshake OK, wire order is COMMAND byte first. "
                      "Update the protocol notes - my LE reading was wrong.")
            else:
                print("\nRESULT: no ack either way. Next things to try, in order:")
                print("  1. pad the payload to 512 bytes with FF FF filler")
                print("  2. send it on the ISO OUT endpoint 0x04 instead")
                print("  3. read status from the ISO IN endpoint 0x83")
                print("  4. check whether status[0x1E] is really the mode field -")
                print("     dump the full 64 bytes before and after and diff them")
        finally:
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
