#!/usr/bin/env python3
"""
engine_health_probe.py - endurance stress test + delta-timing health probe.

Built to answer one question precisely: when the device goes silent mid-
session, is the playback engine actually EXECUTING delta-timed delays, or
is state[0x1E] bit 7 ("running") a stale flag that no longer reflects
reality? hs.state() alone can't tell you this - it only reports ring
pointers and a status bit the device sets, not whether the engine is truly
consuming content according to the encoded timing. Every recovery attempt
built into midi.py so far (panic, re-arm) reads that same bit as "healthy"
right up to the failures it couldn't fix - the same "green diagnostic that
lied" trap documented in docs/journey.md's Phase 4, one level up the stack.

The test: send one 512-byte block of maximum-length delay pairs (128 x
0xFFFF cycles ~ 8.4M cycles ~ 8.5s at PAL clock) and watch how fast ring
free-space recovers.
  - Recovers within ~2s -> the delay was NOT honored. The engine drained
    the block without executing it. Its "running" bit is lying, and per
    hs4u.py's own start_engine() (which no-ops if running() is already
    True, specifically to avoid toggling a genuinely-running engine off),
    NO software path can recover from this - only a power cycle clears it.
  - Stays flat for the full ~2s window -> the delay genuinely is being
    honored (it would take the full ~8.5s to drain). The engine is alive;
    silence has a different cause.

This script takes exclusive USB access - stop midi.py first.

Usage
-----
    uv run --extra midi python3 tools/engine_health_probe.py \\
        --minutes 5
"""
import argparse
import os
import random
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


# delta_probe() and report_probe() now live in hs4u.py (a driver-level
# diagnostic, shared with midi.py's background health monitor) - use
# hs4u.delta_probe(...) / hs4u.report_probe(...) here.


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--probe-interval", type=float, default=45.0,
                     help="seconds between health probes during the run "
                          "(kept coarse so most of the time is genuine "
                          "stress traffic, not probe recovery)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    hs.init()

    print("\n=== BASELINE PROBE (known-good, freshly power-cycled) ===")
    baseline = hs4u.delta_probe(hs, verbose=True)
    hs4u.report_probe("baseline", baseline)
    if not baseline["honored"]:
        print("\nBASELINE FAILED - the engine is not honoring delays even "
              "right after a fresh power cycle + init. That changes the "
              "whole picture; stopping here rather than running a long "
              "endurance test against an already-broken baseline.")
        hs.close()
        return

    # Drive the REAL dispatcher (raw MIDI bytes through MidiToSid.__call__),
    # not ChipChannel directly - this exercises the actual debounce/panic
    # code path exactly as Ableton would, not a shortcut around it.
    dispatcher = midi.MidiToSid(hs, verbose=False)

    def send(message):
        dispatcher((message, 0.0))

    rng = random.Random()
    active = []  # list of (channel_idx, note) currently held

    print(f"\n=== ENDURANCE RUN: {args.minutes} minutes, "
          f"probing every {args.probe_interval:.0f}s ===")
    print("    traffic includes periodic panic bursts (4-14 rapid CC102 "
          "sends) - every real incident's log had this in common, and the "
          "first endurance run (heavy notes only, no panics) found nothing")
    t_start = time.perf_counter()
    t_last_probe = t_start
    t_end = t_start + args.minutes * 60
    probe_n = 0
    caught = False

    try:
        while time.perf_counter() < t_end:
            now = time.perf_counter()

            action = rng.random()
            if action < 0.50:
                ch_idx = rng.randrange(4)
                note = rng.randint(36, 72)
                send([0x90 | ch_idx, note, rng.randint(60, 127)])
                active.append((ch_idx, note))
                if len(active) > 40:
                    active.pop(0)
            elif action < 0.78 and active:
                idx = rng.randrange(len(active))
                ch_idx, note = active.pop(idx)
                send([0x80 | ch_idx, note, 0])
            elif action < 0.88:
                ch_idx = rng.randrange(4)
                send([0xB0 | ch_idx, midi.CC_FILTER_CUTOFF, rng.randint(0, 127)])
            elif action < 0.99:
                ch_idx = rng.randrange(4)
                send([0xE0 | ch_idx, rng.randint(0, 127), rng.randint(0, 127)])
            else:
                ch_idx = rng.randrange(4)
                n = rng.randint(4, 14)
                if args.verbose:
                    print(f"  >>> panic burst x{n} on ch{ch_idx + 1} "
                          f"at t+{now - t_start:.0f}s")
                for _ in range(n):
                    send([0xB0 | ch_idx, midi.CC_PANIC, 0])

            if args.verbose and rng.random() < 0.01:
                print(f"  ...{now - t_start:.0f}s elapsed, "
                      f"{len(active)} notes held")

            if now - t_last_probe >= args.probe_interval:
                t_last_probe = now
                probe_n += 1
                result = hs4u.delta_probe(hs)
                hs4u.report_probe(f"t+{now - t_start:.0f}s #{probe_n}", result)
                if not result["honored"]:
                    print("\n*** FAILURE CAUGHT LIVE ***")
                    print(f"  elapsed: {now - t_start:.1f}s, probe #{probe_n}")
                    print(f"  {len(active)} notes were held at time of failure")
                    print("  Stopping endurance loop - this is the evidence "
                          "we needed.")
                    caught = True
                    break

            time.sleep(rng.uniform(0.02, 0.08))
    finally:
        print("\n=== FINAL PROBE ===")
        try:
            final = hs4u.delta_probe(hs)
            hs4u.report_probe("final", final)
        except Exception as e:
            print(f"  final probe failed: {e}")
        try:
            dispatcher.all_notes_off()
        except Exception:
            pass
        hs.close()
        print(f"done - failure caught: {caught}")


if __name__ == "__main__":
    main()
