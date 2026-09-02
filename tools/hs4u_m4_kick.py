#!/usr/bin/env python3
"""
HardSID 4U — milestone 4: find the kick that starts the consume engine.

Observed: after a cold power-on, bulk writes are ACCEPTED (wr advances) but
never CONSUMED (rd stays at 0x2000, state stays 0x0000). Yet in the very
first probe session a single filler block took state to 0x0081 and rd began
tracking wr.

The difference: that session touched the ISOCHRONOUS endpoints before the
bulk writes. This script tries candidate kicks one at a time and reports
which one gets rd moving.

Everything sent is FFFF filler. No register writes, so nothing here can make
an unexpected noise.

POWER-CYCLE THE UNIT BEFORE RUNNING so the ring starts empty - if the device
never consumes, the 8 KB ring fills after 16 blocks and later tests are
meaningless.

Usage:  python3 hs4u_m4_kick.py
"""
import struct
import sys
import time

import usb1

VID, PID = 0x6581, 0x8580
IFACE = 0
EP_BULK_IN, EP_BULK_OUT = 0x81, 0x02
EP_ISO_IN, EP_ISO_OUT = 0x83, 0x04
BLOCK = 512
RING = 0x2000
TIMEOUT = 500
FILLER = b"\xff\xff"
FILLER_BLOCK = FILLER * (BLOCK // 2)


def status(h):
    return bytes(h.bulkRead(EP_BULK_IN, 64, timeout=TIMEOUT))


def ptrs(h):
    raw = status(h)
    rd, wr, state = struct.unpack_from("<HHH", raw, 0x1A)
    used = (wr - rd) & (RING - 1)
    return rd, wr, state, RING - used


# ------------------------------------------------------------- candidates --

def kick_none(h, ctx):
    return "bulk filler only (control - known to fail cold)"


def kick_iso_in(h, ctx):
    got = []
    t = h.getTransfer(iso_packets=1)
    t.setIsochronous(EP_ISO_IN, 64, callback=lambda x: got.append(x.getStatus()))
    t.submit()
    end = time.time() + 0.3
    while not got and time.time() < end:
        ctx.handleEvents()
    return f"iso IN 0x83 read (status={got[0] if got else 'timeout'})"


def kick_iso_out(h, ctx):
    done = []
    t = h.getTransfer(iso_packets=1)
    t.setIsochronous(EP_ISO_OUT, FILLER_BLOCK,
                     callback=lambda x: done.append(x.getStatus()))
    t.submit()
    end = time.time() + 0.3
    while not done and time.time() < end:
        ctx.handleEvents()
    return f"iso OUT 0x04 filler (status={done[0] if done else 'timeout'})"


def kick_iso_both(h, ctx):
    kick_iso_out(h, ctx)
    kick_iso_in(h, ctx)
    return "iso OUT then iso IN"


def kick_iso_sustained(h, ctx):
    """Keep several iso OUT transfers queued, the way a real streaming host
    would. A firmware that only runs while frames are arriving needs this,
    not a single lonely packet."""
    done = []
    transfers = []
    for _ in range(8):
        t = h.getTransfer(iso_packets=1)
        t.setIsochronous(EP_ISO_OUT, FILLER_BLOCK,
                         callback=lambda x: done.append(x.getStatus()))
        t.submit()
        transfers.append(t)
    end = time.time() + 0.5
    while len(done) < 8 and time.time() < end:
        ctx.handleEvents()
    return f"8 queued iso OUT frames ({len(done)} completed)"


def kick_altsetting(h, ctx):
    h.setInterfaceAltSetting(IFACE, 0)
    return "explicit setInterfaceAltSetting(0, 0)"


def kick_clear_halt(h, ctx):
    for ep in (EP_BULK_OUT, EP_BULK_IN):
        try:
            h.clearHalt(ep)
        except usb1.USBError as e:
            return f"clearHalt failed: {e}"
    return "clearHalt on both bulk endpoints"


CANDIDATES = [
    kick_none,
    kick_iso_in,
    kick_iso_out,
    kick_iso_both,
    kick_iso_sustained,
    kick_altsetting,
    kick_clear_halt,
]


def main():
    with usb1.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            sys.exit("device not found")
        h.claimInterface(IFACE)
        try:
            rd, wr, state, free = ptrs(h)
            print(f"[cold] rd={rd:#06x} wr={wr:#06x} state={state:#06x} free={free}")
            if free < RING - BLOCK:
                print("  WARNING: ring is not empty. Power-cycle first for a")
                print("           clean run, or results will be confusing.")

            winner = None
            for fn in CANDIDATES:
                rd0, wr0, st0, free0 = ptrs(h)
                if free0 < BLOCK * 3:
                    print("\n  ring is full and nothing is draining - stopping.")
                    print("  Power-cycle and re-run to test the remaining kicks.")
                    break

                try:
                    desc = fn(h, ctx)
                except usb1.USBError as e:
                    print(f"\n  {fn.__name__}: FAILED {e}")
                    continue

                # one bulk block, then watch for consumption
                h.bulkWrite(EP_BULK_OUT, FILLER_BLOCK, timeout=TIMEOUT)
                time.sleep(0.20)
                rd1, wr1, st1, free1 = ptrs(h)

                moved = (rd1 - rd0) & (RING - 1)
                print(f"\n  {fn.__name__}")
                print(f"    {desc}")
                print(f"    rd {rd0:#06x} -> {rd1:#06x}  (advanced {moved})")
                print(f"    wr {wr0:#06x} -> {wr1:#06x}")
                print(f"    state {st0:#06x} -> {st1:#06x}   free {free0} -> {free1}")

                if moved > 0 or st1 & 0x80:
                    print("    *** CONSUMING - this is the kick ***")
                    winner = fn.__name__
                    break

            print("\n" + "=" * 58)
            if winner:
                print(f"Engine started by: {winner}")
                print("Re-run the note test with this kick applied first.")
            else:
                print("Nothing started it. rd never advanced and bit 7 never set.")
                print("At this point static analysis has given us all it can.")
                print("Next step is a capture of the Windows driver doing it:")
                print("  - Windows VM, USB passthrough of the HardSID")
                print("  - usbmon on the Linux/macOS host, or USBPcap in the VM")
                print("  - install the HardSID driver, run the VSTi, play a note")
                print("  - the first few hundred bytes will show the real init")
        finally:
            h.releaseInterface(IFACE)


if __name__ == "__main__":
    main()
