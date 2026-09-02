#!/usr/bin/env python3
"""
HardSID 4U — milestone 6: is the register write landing at all?

Engine start is solved (short packet ff ff 01 00 on bulk OUT). Delays work.
Blocks are consumed. But no tone.

A click with no tone is what you get when the VOLUME register moves and no
oscillator is running. So test the volume register on its own, loudly and
unambiguously, before worrying about voices.

TEST 1  volume click train - six clicks, 0.4 s apart, chip 0.
        Six clicks  -> register writes work; the bug is in voice setup.
        Silence     -> register writes are not landing; go to TEST 2.

TEST 2  the same click train with every plausible command-byte base, so we
        find which one the device actually listens to.

TEST 3  a clean note with a slow attack, no zero words anywhere.

Also fixes a latent bug in hs4u_m3_note.py: writing register 0x00 with data
0x00 emits the word 0x0000, which may terminate a block. Nothing here emits
a zero word.

Usage:
  python3 hs4u_m6_probe.py            # all three tests
  python3 hs4u_m6_probe.py --test 1   # just the click train
"""
import argparse
import struct
import sys
import time

import usb1

VID, PID = 0x6581, 0x8580
IFACE = 0
EP_IN, EP_OUT = 0x81, 0x02
BLOCK = 512
RING = 0x2000
TIMEOUT = 500
FILLER = b"\xff\xff"
START_CMD = b"\xff\xff\x01\x00"
PAL_CLOCK = 985248


def cmd(c, d):
    """Little-endian command word: data byte first. Verified by the delay
    stall test in milestone 3b."""
    return bytes((d & 0xFF, c & 0xFF))


def reg(base, r, d):
    return cmd((base | (r & 0x1F)) & 0xFF, d)


def delay(cycles):
    out = b""
    while cycles > 0:
        n = min(cycles, 0xFFFF)
        if n >= 0x100:
            out += cmd(0xEF, n >> 8)
        if n & 0xFF:
            out += cmd(0xEE, n & 0xFF)
        cycles -= n
    return out


# ------------------------------------------------------------------ link ---

def ptrs(h):
    raw = bytes(h.bulkRead(EP_IN, 64, timeout=TIMEOUT))
    rd, wr, state = struct.unpack_from("<HHH", raw, 0x1A)
    used = (wr - rd) & (RING - 1)
    return rd, wr, state, RING - used


def ensure_started(h):
    """Bring the engine up.

    The start command is NOT a cold start - it only took effect in testing
    when the ring already held data (m5 phase A had written one filler block
    before phase B's short packet worked). On another occasion a filler block
    alone was enough. Since neither is reliable on its own, alternate them
    until bit 7 of state comes up.
    """
    rd, wr, state, free = ptrs(h)
    print(f"[link] rd={rd:#06x} wr={wr:#06x} state={state:#06x} free={free}")
    if state & 0x80:
        print("  engine already running")
        return

    for attempt in range(1, 9):
        # 1) put a block in the ring
        rd, wr, state, free = ptrs(h)
        if free >= BLOCK * 2:
            h.bulkWrite(EP_OUT, FILLER * (BLOCK // 2), timeout=TIMEOUT)
            time.sleep(0.15)
            rd, wr, state, free = ptrs(h)
            print(f"  attempt {attempt}: after filler block  "
                  f"state={state:#06x} rd={rd:#06x} wr={wr:#06x}")
            if state & 0x80:
                print("  engine started (filler block)")
                return

        # 2) then the short start packet, now that the ring is non-empty
        h.bulkWrite(EP_OUT, START_CMD, timeout=TIMEOUT)
        time.sleep(0.15)
        rd, wr, state, free = ptrs(h)
        print(f"  attempt {attempt}: after start packet  "
              f"state={state:#06x} rd={rd:#06x} wr={wr:#06x}")
        if state & 0x80:
            print("  engine started (short packet after buffered data)")
            return

        # 3) a little polling, which preceded the one success we have seen
        for _ in range(25):
            rd, wr, state, free = ptrs(h)
            if state & 0x80:
                print("  engine started (during polling)")
                return
            time.sleep(0.02)

        if free < BLOCK * 3:
            print("  ring is full and nothing is draining")
            break

    sys.exit("  could not start engine - power-cycle the unit and retry")


def send(h, payload):
    payload += FILLER * (((-len(payload)) % BLOCK) // 2)
    for i in range(0, len(payload), BLOCK):
        for _ in range(4000):
            rd, wr, state, free = ptrs(h)
            if free >= BLOCK * 2:
                break
            time.sleep(0.001)
        else:
            print("    stalled waiting for ring space")
            return
        h.bulkWrite(EP_OUT, payload[i:i + BLOCK], timeout=TIMEOUT)


def drain(h, limit=30.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        rd, wr, state, free = ptrs(h)
        if free >= RING - BLOCK:
            return
        time.sleep(0.05)


# --------------------------------------------------------------- streams ---

def click_train(base, n=6, gap=0.4):
    """Volume 15 / volume 1, repeatedly. Never writes 0, so no zero word.
    If the volume register is reachable this is plainly audible."""
    s = b""
    for _ in range(n):
        s += reg(base, 0x18, 0x0F) + delay(int(PAL_CLOCK * gap / 2))
        s += reg(base, 0x18, 0x01) + delay(int(PAL_CLOCK * gap / 2))
    return s


def clean_note(base, hz=440.0, seconds=1.5):
    freq = int(round(hz * 16777216 / PAL_CLOCK)) & 0xFFFF
    lo, hi = freq & 0xFF, (freq >> 8) & 0xFF
    if lo == 0:
        lo = 1                      # never emit a zero word
    s = b""
    s += reg(base, 0x18, 0x0F) + delay(8)     # volume max
    s += reg(base, 0x05, 0x11) + delay(8)     # attack 1, decay 1
    s += reg(base, 0x06, 0xF1) + delay(8)     # sustain F, release 1
    s += reg(base, 0x02, 0x00) + delay(8)     # pulse width lo
    s += reg(base, 0x03, 0x08) + delay(8)     # pulse width hi = 50%
    s += reg(base, 0x00, lo) + delay(8)
    s += reg(base, 0x01, hi) + delay(8)
    s += reg(base, 0x04, 0x11) + delay(int(PAL_CLOCK * seconds))   # tri + gate
    s += reg(base, 0x04, 0x10) + delay(int(PAL_CLOCK * 0.4))       # gate off
    s += reg(base, 0x18, 0x01)
    return s


def announce(msg):
    print(f"\n  >>> {msg}")
    print("      LISTEN...", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", type=int, default=0, help="1, 2 or 3; 0 = all")
    args = ap.parse_args()

    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found")
        h.claimInterface(IFACE)
        try:
            ensure_started(h)

            if args.test in (0, 1):
                print("\n" + "=" * 58)
                print("TEST 1  volume click train, chip 0 (base 0x00)")
                print("=" * 58)
                announce("six clicks, 0.4 s apart")
                send(h, click_train(0x00))
                drain(h)
                print("      done. Did you hear six clicks?")

            if args.test in (0, 2):
                print("\n" + "=" * 58)
                print("TEST 2  click train across command-byte bases")
                print("=" * 58)
                print("  Count which position clicks. Bases in order:")
                bases = [0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0]
                print("   " + "  ".join(f"{i+1}:{b:#04x}" for i, b in enumerate(bases)))
                for i, b in enumerate(bases):
                    announce(f"position {i+1}  base={b:#04x}  (3 clicks)")
                    send(h, click_train(b, n=3, gap=0.3))
                    drain(h)
                    time.sleep(0.6)

            if args.test in (0, 3):
                print("\n" + "=" * 58)
                print("TEST 3  clean note, no zero words, all four chips")
                print("=" * 58)
                for chip in range(4):
                    base = chip << 5
                    announce(f"chip {chip} (base {base:#04x}) 440 Hz")
                    send(h, clean_note(base))
                    drain(h)
                    time.sleep(0.5)

            rd, wr, state, free = ptrs(h)
            print(f"\n[end] rd={rd:#06x} wr={wr:#06x} "
                  f"state={state:#06x} free={free}")
            print("\nReport back:")
            print("  TEST 1 clicks?      -> volume register is reachable")
            print("  TEST 2 which slot?  -> that base is the real encoding")
            print("  TEST 3 any tone?    -> voices work, we are done")
        finally:
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
