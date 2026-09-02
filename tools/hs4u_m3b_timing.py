#!/usr/bin/env python3
"""
HardSID 4U — milestone 3b: determine the TIMING MODEL, then sweep by ear.

Two competing models explain everything we have seen so far:

  A) DELTA-TIMED   0xEE/0xEF words are explicit cycle delays. The device
                   consumes words as fast as it can and pauses on delays.

  B) FIXED TICK    every 2-byte word is one tick of a fixed 8000 Hz clock
                   (125 us). 0xFFFF means "no register write this tick".
                   This matches the VSTi manual's "8000Hz data rate".

Under model B our milestone-3 note lasted about 4 ms -> a click, which is
exactly what was heard.

Test 1 settles it without any guessing: fill the ring, watch the drain rate.
    ~16000 bytes/sec  -> model B (8000 words/sec)
    near-instant      -> model A

Test 2 then plays a note under whichever model(s) you ask for, in both byte
orders, announcing each attempt so you can note which one makes sound.

Usage:
  python3 hs4u_m3b_timing.py                # test 1 only (safe, silent-ish)
  python3 hs4u_m3b_timing.py --sweep        # test 1, then the note sweep
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
TIMEOUT = 1000
FILLER = b"\xff\xff"

TICK_HZ = 8000.0
PAL_CLOCK = 985248


def status(h):
    return bytes(h.bulkRead(EP_IN, 64, timeout=TIMEOUT))


def ptrs(raw):
    rd, wr, state = struct.unpack_from("<HHH", raw, 0x1A)
    used = (wr - rd) & (RING - 1)
    return rd, wr, state, RING - used


def write_block(h, block):
    assert len(block) == BLOCK
    h.bulkWrite(EP_OUT, block, timeout=TIMEOUT)


def wait_room(h, need=BLOCK, limit=3.0):
    deadline = time.time() + limit
    while time.time() < deadline:
        rd, wr, state, free = ptrs(status(h))
        if free >= need + BLOCK:
            return True, free
    return False, 0


# ------------------------------------------------------------- test 1 -----

def measure_drain(h, block, label):
    """Fill the ring with `block`, then watch the read pointer advance."""
    print(f"\n[drain test] {label}")

    # fill until nearly full
    filled = 0
    for _ in range(32):
        rd, wr, state, free = ptrs(status(h))
        if free < BLOCK * 2:
            break
        write_block(h, block)
        filled += BLOCK
    print(f"  filled {filled} bytes")

    rd0, wr0, st0, free0 = ptrs(status(h))
    t0 = time.time()
    consumed = 0
    prev = rd0
    samples = []

    while time.time() - t0 < 1.5:
        time.sleep(0.05)
        rd, wr, state, free = ptrs(status(h))
        step = (rd - prev) & (RING - 1)
        consumed += step
        prev = rd
        samples.append((time.time() - t0, consumed, free))

    elapsed = samples[-1][0]
    rate = consumed / elapsed if elapsed else 0
    print(f"  start: rd={rd0:#06x} wr={wr0:#06x} free={free0} state={st0:#06x}")
    print(f"  consumed {consumed} bytes in {elapsed:.2f}s "
          f"-> {rate:.0f} bytes/s = {rate/2:.0f} words/s")

    if consumed == 0:
        print("  VERDICT: nothing consumed. Device is buffering but not")
        print("           running. Something still has to start the stream.")
    elif 12000 < rate < 22000:
        print("  VERDICT: ~8000 words/s -> MODEL B, fixed tick. Each word is")
        print("           125 us. FFFF is a timing unit, not just padding.")
    elif rate > 60000:
        print("  VERDICT: drains fast -> MODEL A, delta-timed. 0xEE/0xEF are")
        print("           real delays and this filler had none in it.")
    else:
        print(f"  VERDICT: unexpected rate. Note it and we work from there.")
    return rate


# ------------------------------------------------------------- test 2 -----

class Enc:
    def __init__(self, order):
        self.order = order

    def cmd(self, c, d):
        return bytes((d, c)) if self.order == "le" else bytes((c, d))

    def reg(self, chip, r, d):
        return self.cmd(((chip & 3) << 5) | (r & 0x1F), d)


def note_stream(e, chip, hz, seconds, model):
    freq = int(round(hz * 16777216 / PAL_CLOCK)) & 0xFFFF
    s = b""
    setup = [(0x18, 0x0F), (0x05, 0x28), (0x06, 0xF8),
             (0x00, freq & 0xFF), (0x01, (freq >> 8) & 0xFF)]

    if model == "tick":
        for r, v in setup:
            s += e.reg(chip, r, v)
        s += e.reg(chip, 0x04, 0x11)                 # gate on
        s += FILLER * int(TICK_HZ * seconds)         # hold, one word per tick
        s += e.reg(chip, 0x04, 0x10)                 # gate off
        s += FILLER * int(TICK_HZ * 0.3)
        s += e.reg(chip, 0x18, 0x00)
    else:
        def delay(cycles):
            out = b""
            while cycles > 0:
                n = min(cycles, 0xFFFF)
                if n >= 0x100:
                    out += e.cmd(0xEF, n >> 8)
                if n & 0xFF:
                    out += e.cmd(0xEE, n & 0xFF)
                cycles -= n
            return out
        for r, v in setup:
            s += e.reg(chip, r, v) + delay(4)
        s += e.reg(chip, 0x04, 0x11)
        s += delay(int(PAL_CLOCK * seconds))
        s += e.reg(chip, 0x04, 0x10)
        s += delay(int(PAL_CLOCK * 0.3))
        s += e.reg(chip, 0x18, 0x00)
    return s


def play(h, payload, label):
    print(f"\n  >>> {label}  ({len(payload)} bytes) - LISTEN")
    payload += FILLER * (((-len(payload)) % BLOCK) // 2)
    t0 = time.time()
    for i in range(0, len(payload), BLOCK):
        ok, _ = wait_room(h)
        if not ok:
            print("      stalled waiting for ring space")
            return
        write_block(h, payload[i:i + BLOCK])
    # let the device finish playing what is buffered
    while True:
        rd, wr, state, free = ptrs(status(h))
        if free >= RING - BLOCK or time.time() - t0 > 20:
            break
        time.sleep(0.05)
    print(f"      done in {time.time()-t0:.1f}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--chip", type=int, default=0)
    ap.add_argument("--hz", type=float, default=440.0)
    args = ap.parse_args()

    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found")
        h.claimInterface(IFACE)
        try:
            rd, wr, state, free = ptrs(status(h))
            print(f"[start] rd={rd:#06x} wr={wr:#06x} state={state:#06x} free={free}")
            if state == 0:
                write_block(h, FILLER * (BLOCK // 2))
                time.sleep(0.05)
                print(f"  primed, state now {ptrs(status(h))[2]:#06x}")

            # Test 1a: pure filler, no delay opcodes anywhere
            measure_drain(h, FILLER * (BLOCK // 2), "pure FFFF filler")

            time.sleep(0.5)

            # Test 1b: a block full of maximum delay opcodes. Under model A
            # this should drain far SLOWER than the filler. Under model B it
            # should drain at exactly the same rate.
            delay_block = (bytes((0xFF, 0xEF)) + bytes((0xFF, 0xEE))) * (BLOCK // 4)
            measure_drain(h, delay_block, "0xEF/0xEE max-delay words (LE order)")

            if not args.sweep:
                print("\nRun again with --sweep to try playing a note under both")
                print("timing models and both byte orders.")
                return

            print("\n" + "=" * 60)
            print("NOTE SWEEP - note which attempt produces a sustained tone")
            print("=" * 60)
            for model in ("tick", "delta"):
                for order in ("le", "be"):
                    e = Enc(order)
                    s = note_stream(e, args.chip, args.hz, 1.0, model)
                    play(h, s, f"model={model:5s} order={order}")
                    time.sleep(1.0)

            print("\nIf exactly one combination sang, that is the protocol.")
            print("If the tick models both clicked and the delta models were")
            print("silent, the tick rate is right but the register encoding is")
            print("wrong - tell me which and we narrow from there.")
        finally:
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
