#!/usr/bin/env python3
"""
HardSID 4U — milestone 5: bisect the start trigger.

In session 1 the engine came alive. The sequence that preceded it was:

    descriptors -> m1 (claim + 1 status read)
                -> m2 (4-byte write 'ff ff 01 00', then ~200 status reads
                       over 4 s, twice)
                -> m2b -> first 512-byte bulk filler -> state 0x0081, running

Since the power cycle we have only run m3/m4, which never send a short packet
and never poll hard, and the engine has never started. So the trigger is in
what m1/m2 did and m3/m4 do not.

Three candidates, tested in isolation, cheapest first:

    A  sustained polling of the status endpoint, no writes at all
    B  a 4-byte SHORT PACKET on bulk OUT (ff ff 01 00)
    C  a short packet of a different shape (ff ff only)

After each, one 512-byte filler block, then check whether rd advances.

All traffic is filler or the 4-byte command. No register writes.

POWER-CYCLE THE UNIT FIRST.

Usage:  python3 hs4u_m5_bisect.py
"""
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
FILLER_BLOCK = FILLER * (BLOCK // 2)


def ptrs(h):
    raw = bytes(h.bulkRead(EP_IN, 64, timeout=TIMEOUT))
    rd, wr, state = struct.unpack_from("<HHH", raw, 0x1A)
    used = (wr - rd) & (RING - 1)
    return rd, wr, state, RING - used


def running(h):
    """Write one filler block and see whether the device eats it."""
    rd0, wr0, st0, _ = ptrs(h)
    h.bulkWrite(EP_OUT, FILLER_BLOCK, timeout=TIMEOUT)
    time.sleep(0.25)
    rd1, wr1, st1, free1 = ptrs(h)
    moved = (rd1 - rd0) & (RING - 1)
    alive = moved > 0 or (st1 & 0x80)
    print(f"    -> rd {rd0:#06x}->{rd1:#06x} (+{moved})  "
          f"wr {wr0:#06x}->{wr1:#06x}  state {st0:#06x}->{st1:#06x}")
    return alive


def phase(name, fn, h):
    print(f"\n[{name}]")
    fn(h)
    if running(h):
        print("    *** ENGINE RUNNING ***")
        return True
    return False


# ---------------------------------------------------------------- phases --

def p_poll(h):
    """Candidate A: hammer the status endpoint the way m2's poll loop did."""
    print("    polling status for 4 s, no writes...")
    t0 = time.time()
    n = 0
    last = None
    while time.time() - t0 < 4.0:
        s = ptrs(h)
        n += 1
        if s[2] != last:
            print(f"      t={time.time()-t0:4.1f}s state={s[2]:#06x}")
            last = s[2]
        time.sleep(0.02)
    print(f"    {n} status reads done")


def p_short4(h):
    """Candidate B: the exact 4-byte short packet m2 sent."""
    print("    bulk OUT short packet: ff ff 01 00")
    h.bulkWrite(EP_OUT, b"\xff\xff\x01\x00", timeout=TIMEOUT)
    time.sleep(0.1)
    print(f"      state now {ptrs(h)[2]:#06x}")


def p_short2(h):
    """Candidate C: a bare 2-byte short packet."""
    print("    bulk OUT short packet: ff ff")
    h.bulkWrite(EP_OUT, b"\xff\xff", timeout=TIMEOUT)
    time.sleep(0.1)
    print(f"      state now {ptrs(h)[2]:#06x}")


def p_short_then_poll(h):
    """Candidate B+A together, which is what m2 actually did."""
    p_short4(h)
    p_poll(h)


PHASES = [
    ("A  sustained polling only", p_poll),
    ("B  4-byte short packet ff ff 01 00", p_short4),
    ("C  2-byte short packet ff ff", p_short2),
    ("D  short packet THEN sustained polling (m2's real behaviour)",
     p_short_then_poll),
]


def main():
    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found")
        h.claimInterface(IFACE)
        try:
            rd, wr, state, free = ptrs(h)
            print(f"[cold] rd={rd:#06x} wr={wr:#06x} "
                  f"state={state:#06x} free={free}")
            if free < RING - BLOCK:
                print("  WARNING: ring not empty - power-cycle for a clean run")

            for name, fn in PHASES:
                _, _, _, free = ptrs(h)
                if free < BLOCK * 3:
                    print("\n  ring full, nothing draining - stopping.")
                    print("  Power-cycle and re-run to test remaining phases.")
                    break
                if phase(name, fn, h):
                    print("\n" + "=" * 58)
                    print(f"TRIGGER FOUND: {name}")
                    print("Apply this before anything else, then run:")
                    print("  python3 hs4u_m3_note.py --order le")
                    return

            print("\n" + "=" * 58)
            print("No trigger found. Time to capture the real thing.")
            print()
            print("Windows VM route (UTM or Parallels on Apple Silicon,")
            print("VirtualBox/VMware on Intel):")
            print("  1. pass the HardSID through to the VM")
            print("  2. install the hardsidusb driver from your .inf/.sys")
            print("  3. install hardsid.dll + the VSTi, play one note")
            print("  4. capture with USBPcap inside the VM, or better,")
            print("     usbmon + Wireshark on a Linux box with the VM there")
            print()
            print("Send me the first few hundred bytes on EP 0x02 and the")
            print("first status reads on 0x81 and the init will be obvious.")
        finally:
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
