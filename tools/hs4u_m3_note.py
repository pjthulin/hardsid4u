#!/usr/bin/env python3
"""
HardSID 4U — milestone 3: make a deliberate, sustained sound.

Established empirically by the milestone 2b probe:
  * BULK OUT 0x02 is the command pipe
  * writes MUST be a multiple of 512 bytes; short blocks are discarded
  * the ring is 8192 bytes of address space, 0x2000..0x3FFF
  * used = (wr - rd) & 0x1FFF ;  free = 0x2000 - used
  * the device enters state 0x81 by itself on first data - no handshake needed

Unresolved: the byte order of the 2-byte command word. That is what this
script settles, by ear.

Usage:
  python3 hs4u_m3_note.py --order le      # play a note, data-byte-first
  python3 hs4u_m3_note.py --order be      # play a note, cmd-byte-first
  python3 hs4u_m3_note.py --silence       # shut everything up
  python3 hs4u_m3_note.py --order le --chip 1
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

PAL_CLOCK = 985248          # cycles/sec
MIN_CYCLES = 4              # HS_MIN_CYCLE_SID_WRITE
FILLER = b"\xff\xff"

# SID registers (voice 1)
R_FREQ_LO, R_FREQ_HI = 0x00, 0x01
R_CTRL, R_AD, R_SR = 0x04, 0x05, 0x06
R_VOLUME = 0x18


# --------------------------------------------------------------- encoding ---

class Enc:
    def __init__(self, order):
        self.order = order

    def cmd(self, c, d):
        # 'le' = word (c<<8)|d stored little-endian -> data byte first
        return bytes((d, c)) if self.order == "le" else bytes((c, d))

    def reg(self, chip, r, d):
        return self.cmd(((chip & 3) << 5) | (r & 0x1F), d)

    def delay(self, cycles):
        out = b""
        while cycles > 0:
            n = min(cycles, 0xFFFF)
            if n >= 0x100:
                out += self.cmd(0xEF, n >> 8)
            if n & 0xFF:
                out += self.cmd(0xEE, n & 0xFF)
            cycles -= n
        return out


# ------------------------------------------------------------------- link ---

def read_status(h):
    return bytes(h.bulkRead(EP_IN, 64, timeout=TIMEOUT))


def ring_state(raw):
    rd, wr, state = struct.unpack_from("<HHH", raw, 0x1A)
    used = (wr - rd) & (RING - 1)
    return rd, wr, state, RING - used


def send_blocks(h, payload, verbose=False):
    """Pad to a 512-byte multiple, then push block by block with flow control."""
    pad = (-len(payload)) % BLOCK
    payload += FILLER * (pad // 2)

    for i in range(0, len(payload), BLOCK):
        # wait for room - never overrun the device ring
        for _ in range(500):
            rd, wr, state, free = ring_state(read_status(h))
            if free >= BLOCK * 2:
                break
            time.sleep(0.001)
        else:
            print("  ! timed out waiting for ring space", file=sys.stderr)
            return False

        h.bulkWrite(EP_OUT, payload[i:i + BLOCK], timeout=TIMEOUT)
        if verbose:
            print(f"    block {i // BLOCK:3d}  rd={rd:#06x} wr={wr:#06x} "
                  f"state={state:#06x} free={free}")
    return True


# --------------------------------------------------------------- programs ---

def prog_silence(e, chips=4):
    s = b""
    for chip in range(chips):
        for r in range(0x19):
            s += e.reg(chip, r, 0x00) + e.delay(MIN_CYCLES)
    return s


def prog_note(e, chip, freq, seconds):
    """Reset, max volume, slow-ish envelope, triangle, gate on, hold, gate off."""
    s = prog_silence(e, chips=1) if chip == 0 else b""
    for chip_r, val in ((R_VOLUME, 0x0F),
                        (R_AD, 0x28),        # attack 2, decay 8
                        (R_SR, 0xF8),        # sustain F, release 8
                        (R_FREQ_LO, freq & 0xFF),
                        (R_FREQ_HI, (freq >> 8) & 0xFF)):
        s += e.reg(chip, chip_r, val) + e.delay(MIN_CYCLES)

    s += e.reg(chip, R_CTRL, 0x11)                       # triangle + gate on
    s += e.delay(int(PAL_CLOCK * seconds))
    s += e.reg(chip, R_CTRL, 0x10)                       # gate off, release
    s += e.delay(int(PAL_CLOCK * 0.5))
    s += e.reg(chip, R_VOLUME, 0x00)                     # leave it quiet
    return s


def freq_for_hz(hz):
    return int(round(hz * 16777216 / PAL_CLOCK)) & 0xFFFF


# ------------------------------------------------------------------- main ---

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", choices=("le", "be"), default="le")
    ap.add_argument("--chip", type=int, default=0)
    ap.add_argument("--hz", type=float, default=440.0)
    ap.add_argument("--seconds", type=float, default=1.5)
    ap.add_argument("--silence", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    e = Enc(args.order)

    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found")
        h.claimInterface(IFACE)
        try:
            rd, wr, state, free = ring_state(read_status(h))
            print(f"[before] rd={rd:#06x} wr={wr:#06x} state={state:#06x} free={free}")

            if state == 0:
                print("  priming with one filler block to bring the device up...")
                h.bulkWrite(EP_OUT, FILLER * (BLOCK // 2), timeout=TIMEOUT)
                time.sleep(0.05)
                rd, wr, state, free = ring_state(read_status(h))
                print(f"  now state={state:#06x}")

            if args.silence:
                print("[silence] zeroing all registers on all four sockets")
                send_blocks(h, prog_silence(e), args.verbose)
            else:
                freq = freq_for_hz(args.hz)
                print(f"[note] order={args.order} chip={args.chip} "
                      f"{args.hz} Hz -> freq={freq:#06x} for {args.seconds}s")
                print("  listen on the black 'mixed out' jack")
                send_blocks(h, prog_note(e, args.chip, freq, args.seconds),
                            args.verbose)

            time.sleep(args.seconds + 1.0)
            rd, wr, state, free = ring_state(read_status(h))
            print(f"[after ] rd={rd:#06x} wr={wr:#06x} state={state:#06x} free={free}")

            print("\nWhat you should hear:")
            print("  correct order -> a clean sustained tone, then silence")
            print("  wrong order   -> clicks, a burst of noise, or nothing")
            print("If both orders sound wrong, try --chip 1/2/3: socket 1 may be")
            print("empty or the mixed-out jack may not be the one wired up.")
        finally:
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
