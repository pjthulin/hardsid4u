#!/usr/bin/env python3
"""
dearm_signature_test.py - does a DE-ARMED socket produce exactly the
failure the user keeps hitting?

THE OBSERVED FAILURE (forensic dump 20260904-175401)
----------------------------------------------------
A note hangs forever. Meanwhile the device reports perfect health:

    state=0x0081 (engine running)   free=7680 (ring at rest)
    0 pointer mismatches in 147 writes   0 USB errors   0 short writes

MIDI was perfectly balanced - 40 note-ons, 40 note-offs, nothing held -
and the trace shows the gate-off WAS written to every voice, the very last
write in the log being `40 0b` (voice 1 control = gate off). The device
consumed those bytes: rd tracked wr all the way, ending one block behind
as it does at rest. It simply did not act on them.

Then T (test tone), V (restore volume), A (re-arm sockets) and R (engine
reset) all produced nothing, and the tone kept sounding until the power
switch.

THE HYPOTHESIS
--------------
The socket is DE-ARMED. Per docs/protocol.md section 17 an un-armed socket
silently discards every register write, so:

  * the hanging note never stops - the SID keeps oscillating with whatever
    register state it had when the socket went deaf, and no gate-off can
    reach it
  * new notes are inaudible
  * the ring still drains and the engine still reports running, because
    the ENGINE is fine; it is the socket gate that is dropping writes
  * only a power cycle helps

Every one of those matches. What does NOT obviously match is that A
(re-arm) failed - re-arming is precisely the cure for a de-armed socket.
So this script asks two separate questions:

  Q1  Does de-arming reproduce the exact signature (hanging tone + healthy
      state + silence)?
  Q2  Does hs.init()'s re-arm actually recover a de-armed socket? If it
      does NOT, then the user's A key failing is explained too, and
      "de-armed" becomes the leading and possibly complete explanation.

WHAT IT DOES TO THE DEVICE
--------------------------
Writes 0x00 to register 0x1E, which is what hs4u.chip_init_stream() itself
sends as part of the reset half of its arming sequence - so this is the
device's own de-arm command, not an invented one. If the hypothesis is
right you will end up needing the power switch. Do not run this in the
middle of something you care about.

Usage
-----
    uv run python3 tools/dearm_signature_test.py
    uv run python3 tools/dearm_signature_test.py --chip 0
"""
import argparse
import os
import sys
import time

# tools/ stays on the path so scripts can import each other as
# siblings; hs4u itself comes from the installed package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hardsid4u import hs4u, midi
except ImportError:  # running from a source tree without the package installed
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "hardsid4u"))
    import hs4u  # noqa: E402
    import midi  # noqa: E402


def show(hs, label):
    rd, wr, st, free = hs.state()
    print(f"    [{label:<34s}] rd={rd:#06x} wr={wr:#06x} state={st:#06x} "
          f"free={free} running={bool(st & 0x80)}")
    return st


def status_snapshot(hs, samples=3):
    """Full 64-byte status block, with the volatile fields blanked.

    rd/wr (0x1A-0x1D) move on their own, and a couple of other bytes may
    be counters, so take several reads and keep only the bytes that agree
    across all of them. What survives is stable device state - which is
    where an 'armed' flag would live if the device exposes one at all.
    Only offsets 0x18, 0x1A, 0x1C and 0x1E are documented (protocol.md
    section 6); the rest of the block has never been decoded, and finding
    a socket-armed bit in it would turn a power-cycle failure into
    something midi.py can detect and repair by itself."""
    reads = []
    for _ in range(samples):
        reads.append(hs.status())
        time.sleep(0.02)
    stable = bytearray(64)
    mask = bytearray(64)
    for i in range(64):
        vals = {r[i] for r in reads}
        if len(vals) == 1:
            stable[i] = reads[0][i]
            mask[i] = 1
    return bytes(stable), bytes(mask)


def diff_status(before, after, label_a, label_b):
    (sa, ma), (sb, mb) = before, after
    diffs = []
    for i in range(64):
        if ma[i] and mb[i] and sa[i] != sb[i]:
            diffs.append((i, sa[i], sb[i]))
    print(f"\n  --- status block diff: {label_a} -> {label_b} ---")
    if not diffs:
        print("      no stable byte changed  (nothing to detect de-arming "
              "with, at least not here)")
    for i, a, b in diffs:
        note = ""
        if i in (0x1A, 0x1B): note = "  (ring rd - volatile)"
        elif i in (0x1C, 0x1D): note = "  (ring wr - volatile)"
        elif i in (0x1E, 0x1F): note = "  (device state)"
        elif i == 0x18: note = "  (busy/activity flag)"
        print(f"      offset {i:#04x}: {a:#04x} -> {b:#04x}{note}")
    return diffs


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chip", type=int, default=0)
    ap.add_argument("--wait", type=float, default=2.5,
                     help="listening pause at each step")
    args = ap.parse_args()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    try:
        hs.init(chips=(args.chip,))
        ch = midi.ChipChannel(hs, args.chip)
        ch.patch.update(waveform=hs4u.PULSE, attack=0, decay=8,
                        sustain=15, release=0)
        show(hs, "armed")

        print("\n=== STEP 1: sound a note and HOLD it (gate stays on) ===")
        ch.note_on(69, 100)
        show(hs, "note held")
        print(f">>> LISTEN {args.wait}s - you should hear a steady tone <<<")
        time.sleep(args.wait)

        snap_armed = status_snapshot(hs)

        print("\n=== STEP 2: DE-ARM the socket (write 0x1E = 0) while the "
              "note is still sounding ===")
        midi.write_regs_now(hs, [(args.chip, 0x1E, 0x00)])
        show(hs, "after de-arm")
        snap_dearmed = status_snapshot(hs)
        diff_status(snap_armed, snap_dearmed, "armed", "de-armed")
        print(f">>> LISTEN {args.wait}s - tone should still be sounding <<<")
        time.sleep(args.wait)

        print("\n=== STEP 3: send the note-off (Q1: is it discarded?) ===")
        ch.note_off(69)
        show(hs, "after note-off")
        print(f">>> LISTEN {args.wait}s - IF THE TONE CONTINUES, the "
              f"signature is reproduced <<<")
        time.sleep(args.wait)

        print("\n=== STEP 4: hard zero-sweep of every register ===")
        try:
            midi.write_regs_now(hs, [(args.chip, r, 0) for r in range(0x19)])
        except midi.BusBusy as e:
            print(f"    blocked: {e}")
        show(hs, "after zero-sweep")
        print(f">>> LISTEN {args.wait}s - still sounding? <<<")
        time.sleep(args.wait)

        print("\n=== STEP 5: Q2 - does hs.init() re-arm actually recover? "
              "(this is what the A key does) ===")
        hs.init(chips=(args.chip,))
        show(hs, "after re-arm")
        ch._write_chip_globals()
        show(hs, "after globals restore")
        snap_rearmed = status_snapshot(hs)
        diff_status(snap_dearmed, snap_rearmed, "de-armed", "re-armed")
        diff_status(snap_armed, snap_rearmed, "originally armed", "re-armed")
        print(f">>> LISTEN {args.wait}s - did the tone finally STOP? <<<")
        time.sleep(args.wait)

        print("\n=== STEP 6: can it play again? (test tone, octave up) ===")
        ch.note_on(81, 100)
        time.sleep(0.4)
        ch.note_off(81)
        show(hs, "after test tone")
        print(f">>> LISTEN {args.wait}s - a clean HIGH note? <<<")
        time.sleep(args.wait)

        print("\n=== REPORT, in order ===")
        print("  1 steady tone?   2 still sounding?   3 STILL sounding "
              "(= signature reproduced)?")
        print("  4 still sounding?   5 did re-arm stop it?   6 did the high "
              "note play?")
        print("\n  If 3 and 4 kept sounding: a de-armed socket reproduces the")
        print("  hanging note exactly, and register writes cannot reach the")
        print("  chip. If 5 did NOT stop it, then re-arming does not recover")
        print("  a de-armed socket either - which explains the A key failing")
        print("  and makes this the complete mechanism.")
    finally:
        try:
            hs.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
