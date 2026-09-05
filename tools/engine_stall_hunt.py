#!/usr/bin/env python3
"""
engine_stall_hunt.py - reproduce the engine stall on demand, and find out
which property of our traffic causes it.

WHY EVERY PREVIOUS STRESS TEST "PASSED"
---------------------------------------
They measured the wrong thing. bend_flood_test.py ran 64,026 writes and
32 MB and reported a perfectly clean bus; midi_stress_via_coremidi.py
pushed 180,000 bend messages and reported zero faults. Both were watching
USB errors, short writes, ring pointers and `free` - and every one of
those stays healthy through the failure. The device keeps reporting mode 1
acknowledged, keeps advancing rd and wr, keeps accepting every byte, and
silently stops executing any of it.

So those runs may well have stalled the engine minutes in and cheerfully
carried on measuring nothing. There is no way to know now.

What we finally have is a detector. Measured on the failed device, three
times in a row, and again after a USB re-enumeration:

    healthy   ~107ms      failed   4-5ms      (same 200000-cycle block)

hs4u.engine_executing() thresholds that. This script drives traffic and
polls it, so time-to-stall becomes a number we can compare across traffic
shapes instead of a thing the user notices by ear minutes later.

WHAT IS ALREADY RULED OUT (measured on the live failed device, before any
power cycle - it survives all of these):

    ACID64 itself cannot play on a stalled device either. That is
    conclusive: the wedge is device-level, not something about our
    host-side state, and no software sequence - not ours, not the
    vendor's - recovers it.

    hs.init() re-arm                    still 4ms
    system-mode bounce 0->1 and 2->1    still 4ms, and the mode never
                                        actually changed - state stayed
                                        0x0081 even when mode 0 was set,
                                        so the device ignores escape
                                        commands too
    setConfiguration(1)                 still 4ms
    USB resetDevice + re-enumerate      still 5ms

Only the front-panel switch clears it. So this is about PREVENTION, and
prevention needs to know which property of our traffic provokes it.

THE VARIABLES WORTH BISECTING
-----------------------------
ACID64 hammers this device for ten minutes without a scratch. It differs
from us in ways this script can switch on and off independently:

    --shape instant     one 512-byte block per event, ~8 cycles of delay
                        in it, ring drains immediately, engine idle
                        between events. This is what midi.py does.
    --shape timed       real delta-delays encoded in the stream so the
                        engine is continuously executing scheduled
                        content, ACID64-style.

RESULT - SHAPE IS NOT THE CAUSE. Writes until the stall:

    instant   82
    timed     41, 64, 527

Both shapes die, and the spread shows the failure is STOCHASTIC rather
than triggered by any particular pattern: roughly a 0.5% hazard per write,
about one stall every 180 writes. That finally matches the live sessions -
playing at 5-10 writes/second reaches a few hundred writes in 30-100
seconds, which is the 35-115s window measured weeks ago.

A per-write random hazard also puts the +512 pointer miscount back in
scope. That anomaly is likewise per-write and random, at 0.05-0.12% - the
same order, a bit rarer. Whether they are one phenomenon is untested.
    --rate N            blocks per second
    --idle-gaps         insert long silences, since the user's failures
                        follow bursts of playing separated by pauses

Usage
-----
    # does midi.py-shaped traffic stall it, and how fast?
    uv run python3 tools/engine_stall_hunt.py --shape instant

    # same load, ACID64-shaped timing
    uv run python3 tools/engine_stall_hunt.py --shape timed

    uv run python3 tools/engine_stall_hunt.py --minutes 10 --rate 30
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

PATCH = dict(waveform=hs4u.PULSE, attack=0, decay=6, sustain=13, release=2)


def note_pairs(chip, voice, freq, gate):
    b = hs4u.VOICE_BASE[voice]
    ctrl = PATCH["waveform"] | (hs4u.GATE if gate else 0)
    if not gate:
        return [(chip, b + hs4u.R_CONTROL, ctrl)]
    return [
        (chip, b + hs4u.R_FREQ_LO, freq & 0xFF),
        (chip, b + hs4u.R_FREQ_HI, (freq >> 8) & 0xFF),
        (chip, b + hs4u.R_AD, (PATCH["attack"] << 4) | PATCH["decay"]),
        (chip, b + hs4u.R_SR, (PATCH["sustain"] << 4) | PATCH["release"]),
        (chip, b + hs4u.R_CONTROL, ctrl),
    ]


def build_timed(chip, voice, freq, hold_cycles, gap_cycles):
    """One block carrying a whole note cycle with real delta timing."""
    s = hs4u.encode_delay(hs4u.MIN_CYCLES)
    for c, r, v in note_pairs(chip, voice, freq, True):
        s += hs4u.encode_reg(c, r, v) + hs4u.encode_delay(hs4u.MIN_CYCLES)
    s += hs4u.encode_delay(hold_cycles)
    for c, r, v in note_pairs(chip, voice, freq, False):
        s += hs4u.encode_reg(c, r, v) + hs4u.encode_delay(hs4u.MIN_CYCLES)
    s += hs4u.encode_delay(gap_cycles)
    return hs4u.pad_even(s)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chip", type=int, default=0)
    ap.add_argument("--shape", choices=("instant", "timed"), default="instant")
    ap.add_argument("--rate", type=float, default=20.0,
                     help="events per second (instant shape)")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--check-every", type=float, default=2.0,
                     help="seconds between engine-execution checks")
    ap.add_argument("--pad", choices=("filler", "delay0"), default="filler",
                     help="what to pad blocks with. 'filler' (0xFFFF) is the "
                          "vendor's choice but is an ESCAPE PREFIX, and a "
                          "block is 95%% padding - so it emits ~122 escape "
                          "pairs per block, ~4500/second at these rates, "
                          "where ACID64 emits almost none and never stalls. "
                          "'delay0' (0xEE 0x00) is a zero-cycle delay: no "
                          "time cost, but an ordinary command instead of an "
                          "escape. This is the A/B for the filler theory.")
    ap.add_argument("--idle-gaps", action="store_true",
                     help="insert a 3s silence every 20s, mimicking the way "
                          "the user actually plays (bursts, then pauses)")
    args = ap.parse_args()

    hs4u.PAD_WORD = (hs4u.DELAY_ZERO if args.pad == "delay0"
                     else hs4u.FILLER)
    print(f"hs4u.py v{hs4u.VERSION}")
    print(f"pad={args.pad} ({hs4u.PAD_WORD.hex(' ')})")
    print(f"shape={args.shape} rate={args.rate}/s "
          f"idle_gaps={args.idle_gaps} for {args.minutes} min")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    try:
        hs.init(chips=(args.chip,))
        ch = midi.ChipChannel(hs, args.chip)
        ch.patch.update(PATCH)

        ok, r = hs4u.engine_executing(hs)
        print(f"baseline: executing={ok} "
              f"({(r['measured_s'] or 0) * 1000:.0f}ms)")
        if not ok:
            print("Device is ALREADY stalled - power-cycle before testing.")
            return

        notes = [45, 52, 57, 60, 64, 67, 69, 72]
        t0 = time.perf_counter()
        t_end = t0 + args.minutes * 60
        # First check EARLY. It was t0+2.0, and two of the three runs so
        # far reported "stalled at t=2.0s" - which only ever meant "already
        # stalled by the time I first looked". Time-to-stall was being
        # manufactured by the instrument.
        next_check = t0 + 0.25
        next_gap = t0 + 20.0
        period = 1.0 / args.rate
        next_ev = t0
        writes = 0
        i = 0
        gated = False
        while time.perf_counter() < t_end:
            now = time.perf_counter()

            if args.idle_gaps and now >= next_gap:
                next_gap = now + 23.0
                if gated:
                    midi.write_regs_now(
                        hs, note_pairs(args.chip, 0, 0, False))
                    writes += 1
                    gated = False
                time.sleep(3.0)
                next_ev = time.perf_counter()
                continue

            if now >= next_ev:
                next_ev += period
                freq = hs4u.freq_for_hz(
                    midi.midi_note_to_hz(notes[i % len(notes)]))
                i += 1
                if args.shape == "timed":
                    # Derive the note's duration from --rate so this shape
                    # produces the SAME number of blocks per second as
                    # 'instant' does. Without that the comparison is
                    # worthless: timed blocks are paced by the content
                    # they carry, so a fixed 80ms note would emit ~8
                    # blocks/s against instant's ~41, and surviving would
                    # only prove that fewer writes is safer - which we
                    # already suspect and is not the question.
                    span = 1.0 / args.rate
                    blk = build_timed(args.chip, 0, freq,
                                      int(span * 0.6 * hs4u.PAL_CLOCK),
                                      int(span * 0.4 * hs4u.PAL_CLOCK))
                    t = time.time()
                    while hs.state()[3] < 2 * hs4u.BLOCK:
                        if time.time() - t > 10:
                            raise RuntimeError("ring stopped draining")
                        time.sleep(0.005)
                    hs.h.bulkWrite(hs4u.EP_OUT, blk, timeout=hs4u.TIMEOUT)
                    writes += 1
                else:
                    midi.write_regs_now(
                        hs, note_pairs(args.chip, 0, freq, True))
                    writes += 1
                    gated = True
                    time.sleep(min(period * 0.5, 0.06))
                    midi.write_regs_now(
                        hs, note_pairs(args.chip, 0, freq, False))
                    writes += 1
                    gated = False

            if time.perf_counter() >= next_check:
                # Check fast early on: the instant shape stalled at t=2.0s,
                # so a flat 2s interval quantises the answer to uselessness.
                el_now = time.perf_counter() - t0
                next_check = time.perf_counter() + (
                    0.25 if el_now < 20 else args.check_every)
                ok, r = hs4u.engine_executing(hs)
                el = time.perf_counter() - t0
                if not ok:
                    ms = (r["measured_s"] or 0) * 1000
                    print(f"\n*** ENGINE STALLED at t={el:.1f}s after "
                          f"{writes} writes ***")
                    print(f"    {ms:.0f}ms measured (healthy ~107ms)")
                    rd, wr, st, free = hs.state()
                    print(f"    state={st:#06x} rd={rd:#06x} wr={wr:#06x} "
                          f"free={free}")
                    print(f"    shape={args.shape} rate={args.rate}/s "
                          f"idle_gaps={args.idle_gaps}")
                    print(f"    achieved {writes / el:.1f} writes/s "
                          f"({writes} writes in {el:.1f}s)")
                    # VALIDATE THE DETECTOR BY EAR before trusting it.
                    # Everything now rests on engine_executing(), and it has
                    # never been checked against the one thing that actually
                    # defines the failure: whether sound stops. If these
                    # notes are audible, the detector is crying wolf and the
                    # last several conclusions are worthless.
                    print()
                    print("    >>> LISTEN NOW - playing 6 loud notes over "
                          "6 seconds <<<")
                    print("    >>> If you HEAR them, this detector is WRONG "
                          "and so am I <<<")
                    for n in (72, 76, 79, 72, 76, 79):
                        try:
                            f = hs4u.freq_for_hz(midi.midi_note_to_hz(n))
                            midi.write_regs_now(
                                hs, note_pairs(args.chip, 0, f, True))
                            time.sleep(0.5)
                            midi.write_regs_now(
                                hs, note_pairs(args.chip, 0, f, False))
                            time.sleep(0.5)
                        except Exception as e:
                            print(f"    write failed: {e}")
                    print("    >>> Did you hear ANY of those six notes? <<<")

                    # CLICKS-BUT-NO-PITCH TEST
                    # The user reports the six notes above come out as
                    # clicks with no pitch. That means the CONTROL writes
                    # are reaching the SID (the gate opens the envelope,
                    # giving a DC step you hear as a click) while the
                    # frequency never arrives - so writes land but the
                    # oscillator never starts.
                    #
                    # Which is exactly what losing delta-timing would do.
                    # With delays honoured our register writes are 8
                    # cycles apart; with delays ignored the whole block is
                    # issued back to back at bus speed, far faster than
                    # the SID's bus can latch, and plausibly only the last
                    # write of each block survives - CONTROL, carrying the
                    # gate.
                    #
                    # If that is right, spacing the SAME writes out in
                    # real time - one register per 512-byte block, paced
                    # by the host rather than by the device's delay engine
                    # - should make a proper note sound even on a stalled
                    # device, because host pacing does not depend on the
                    # broken timing at all.
                    print()
                    print("    >>> NOW: same note, but ONE REGISTER PER "
                          "BLOCK, spaced 25ms by the host <<<")
                    print("    >>> If THIS one has a real pitch, the fault "
                          "is lost delta-timing, not lost writes <<<")
                    f = hs4u.freq_for_hz(midi.midi_note_to_hz(69))
                    seq = note_pairs(args.chip, 0, f, True)
                    try:
                        for pair in seq:
                            midi.write_regs_now(hs, [pair])
                            time.sleep(0.025)
                        time.sleep(1.5)
                        midi.write_regs_now(
                            hs, note_pairs(args.chip, 0, f, False))
                    except Exception as e:
                        print(f"    write failed: {e}")
                    print("    >>> Did that produce a PITCHED note (A4), or "
                          "another click? <<<")
                    print("    POWER-CYCLE the device before the next run.")
                    return
                if int(el) % 30 < args.check_every:
                    print(f"  [{el:6.0f}s] writes={writes} "
                          f"executing OK ({(r['measured_s'] or 0) * 1000:.0f}ms)")

            slack = next_ev - time.perf_counter()
            if slack > 0.001:
                time.sleep(slack)

        el = time.perf_counter() - t0
        print(f"\nsurvived {el:.0f}s and {writes} writes with "
              f"shape={args.shape} rate={args.rate}/s "
              f"({writes / el:.1f} writes/s achieved)")
    finally:
        try:
            midi.write_regs_now(hs, note_pairs(args.chip, 0, 0, False))
        except Exception:
            pass
        hs.close()


if __name__ == "__main__":
    main()
