#!/usr/bin/env python3
"""
wrap_boundary_test.py - A/B test: does a note landing exactly at the ring's
wrap boundary (address 0x2000, immediately after wr crosses 0x3E00->0x2000)
fail to sound more often than a note landing at a control position mid-ring?

Built after a live --trace-io run during real Ableton play showed exactly
this: "ch1 note on 43" landed at wr=0x2000 (the first block position right
after an observed wrap) and did not sound, while surrounding notes at
other addresses did. One coincidence isn't proof - this runs many trials
of each condition and scores pass/fail automatically via mic-based onset
detection (same method as audible_latency_probe.py), so we get a real
failure-rate comparison instead of an anecdote.

Mechanics: every write is a 512-byte-aligned block, so wr only ever lands
on one of 16 fixed addresses (0x2000, 0x2200, ..., 0x3E00). Padding the
ring with pure-filler blocks (which "cost no time" per docs/protocol.md,
so this is fast) steps wr through those addresses deterministically until
it hits the target, then the next real write - the test note-on - lands
exactly there.

Setup: position a mic on the dry-out for the chip pair being tested (this
hardware has separate outputs for chips 1/2 and 3/4), in a quiet room.
Uses midi.py's own ChipChannel/write_regs_now, so this exercises the exact
code path that produced the original failure - not a reimplementation.

Usage
-----
    uv run --extra audio python3 tools/wrap_boundary_test.py \\
        --list-devices
    uv run --extra audio python3 tools/wrap_boundary_test.py \\
        --input-device 1 --chip 0 --trials 20
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
import audible_latency_probe as alp  # noqa: E402

WRAP_TARGET = 0x2000     # first block position immediately after a wrap
CONTROL_TARGET = 0x3000  # mid-ring, as far from the boundary as possible


def pad_to(hs, target_wr):
    """Send exactly enough filler-only 512-byte blocks to bring wr to
    target_wr.

    Computes the required block count ONCE, deterministically, from the
    starting position via modular arithmetic - rather than re-polling
    state() between each send to check for convergence, which raced with
    the device's own status reporting: a status read taken immediately
    after a bulkWrite() can return a snapshot that hasn't caught up with
    that write yet, causing the old per-iteration-check version to send
    one extra block and overshoot the target by exactly one block size
    (observed: landed at 0x2200 when 0x2000 was requested).
    """
    filler_block = hs4u.FILLER * (hs4u.BLOCK // 2)
    rd, wr, st, free = hs.state()
    steps = ((target_wr - wr) & (hs4u.RING - 1)) // hs4u.BLOCK
    for _ in range(steps):
        t0 = time.perf_counter()
        while hs.state()[3] < len(filler_block):
            if time.perf_counter() - t0 > 0.5:
                break
            time.sleep(0.001)
        hs.h.bulkWrite(hs4u.EP_OUT, filler_block, timeout=hs4u.TIMEOUT)
    time.sleep(0.02)  # let the final status read catch up with the last write
    rd, wr, st, free = hs.state()
    if wr != target_wr:
        raise RuntimeError(f"padding landed at {wr:#06x}, wanted "
                            f"{target_wr:#06x} after {steps} blocks")


def run_trial(hs, ch, note, target_wr, hold=0.3):
    """Pad to target_wr, then fire one note-on at that exact address."""
    pad_to(hs, target_wr)
    rd, wr, st, free = hs.state()
    assert wr == target_wr, (
        f"padding landed at {wr:#06x}, wanted {target_wr:#06x}")
    t0 = time.perf_counter()
    ch.note_on(note, 100)
    time.sleep(hold)
    ch.note_off(note)
    return t0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--input-device", type=int, default=None)
    ap.add_argument("--rec-channels", type=int, default=2,
                     help="how many channels to record from the device "
                          "(default 2 - records both, --channel picks "
                          "which one to analyze)")
    ap.add_argument("--channel", type=int, default=1, choices=(1, 2),
                     help="which recorded channel (1=left/first, "
                          "2=right/second) has the dry-out signal "
                          "(default 1)")
    ap.add_argument("--samplerate", type=int, default=48000)
    ap.add_argument("--chip", type=int, default=0,
                     help="0=ch1/C1, 1=ch2/C2, 2=ch3/C3, 3=ch4/C4")
    ap.add_argument("--voice", type=int, default=0, choices=(0, 1, 2))
    ap.add_argument("--waveform", type=lambda s: int(s, 0), default=hs4u.PULSE)
    ap.add_argument("--trials", type=int, default=20,
                     help="trials PER condition (default 20, so 40 total)")
    ap.add_argument("--hold", type=float, default=0.3)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--threshold-db", type=float, default=12.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        alp.list_devices()
        return
    if args.input_device is None:
        print("--input-device is required (see --list-devices)")
        sys.exit(1)

    import numpy as np
    import sounddevice as sd

    sr = args.samplerate
    total_trials = args.trials * 2
    total_s = args.settle + total_trials * (args.hold + args.interval) + 2.0
    frames = int(total_s * sr)

    print(f"[probe] recording {total_s:.1f}s from input device "
          f"{args.input_device}, channel {args.channel}")
    # latency='low'/blocksize=128 segfaulted deep inside PortAudio/
    # CoreAudio's own callback machinery during a 75s continuous
    # recording (unrelated to the HardSID/USB side, which completed all
    # its trials fine before that crash). Default settings are stable; a
    # 400ms onset-detection search window doesn't need the tightest
    # possible capture latency, and this run is going to be much longer.
    rec_buf = sd.rec(frames, samplerate=sr, channels=args.rec_channels,
                      device=args.input_device, dtype="float32")
    t_start = time.perf_counter()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    trials = []  # list of (condition, absolute_trigger_time)
    ch = None
    try:
        hs.init(chips=(args.chip,))
        ch = midi.ChipChannel(hs, args.chip)
        ch.patch["waveform"] = args.waveform
        ch.patch["attack"] = 0
        ch.patch["decay"] = 2
        ch.patch["sustain"] = 15
        ch.patch["release"] = 6

        time.sleep(args.settle)

        note = 69  # A4, fixed pitch for a consistent onset profile
        conditions = ([("after_wrap", WRAP_TARGET)] * args.trials +
                      [("control", CONTROL_TARGET)] * args.trials)
        random.shuffle(conditions)

        print(f"[probe] {total_trials} trials ({args.trials} per "
              f"condition), randomized order, chip {args.chip}")
        for i, (cond, target) in enumerate(conditions):
            t0 = run_trial(hs, ch, note, target, hold=args.hold)
            trials.append((cond, t0))
            if args.verbose:
                print(f"  [{i + 1}/{total_trials}] {cond:10s} @ "
                      f"{target:#06x}  t={t0 - t_start:.2f}s")
            time.sleep(args.interval)
    finally:
        if ch is not None:
            try:
                ch.all_notes_off()
            except Exception:
                pass
        hs.close()

    sd.wait()
    samples = rec_buf[:, args.channel - 1]
    env = alp.envelope(samples, sr)
    noise_end_idx = int(args.settle * sr * 0.9)
    noise_floor = float(np.median(env[:max(1, noise_end_idx)]))
    threshold = noise_floor * (10 ** (args.threshold_db / 20))
    print(f"\n[detect] noise floor={noise_floor:.5f}  threshold={threshold:.5f}")

    search_s = min(0.4, args.interval * 0.8)
    peak_seen = 0.0
    results = {"after_wrap": [0, 0], "control": [0, 0]}  # [passed, failed]
    for cond, trigger_t in trials:
        start_idx = int((trigger_t - t_start) * sr)
        end_idx = min(start_idx + int(search_s * sr), len(env))
        if 0 <= start_idx < len(env):
            peak_seen = max(peak_seen, float(np.max(env[start_idx:end_idx])))
        onset = alp.find_onset(env, sr, t_start, trigger_t, search_s, threshold)
        if onset is None:
            results[cond][1] += 1
        else:
            results[cond][0] += 1

    print(f"[detect] peak envelope seen in any trigger window: {peak_seen:.5f}  "
          f"(vs noise floor {noise_floor:.5f}, threshold {threshold:.5f})")

    print("\n=== RESULTS ===")
    for cond in ("after_wrap", "control"):
        passed, failed = results[cond]
        total = passed + failed
        rate = failed / total * 100 if total else 0
        print(f"  {cond:12s}: {passed} passed, {failed} failed / "
              f"{total} trials  ({rate:.0f}% failure rate)")
    aw_rate = results["after_wrap"][1] / max(1, sum(results["after_wrap"]))
    ctl_rate = results["control"][1] / max(1, sum(results["control"]))
    if aw_rate > ctl_rate + 0.2:
        print("\n  after_wrap fails substantially more often than control - "
              "supports the wrap-boundary hypothesis.")
    elif ctl_rate > aw_rate + 0.2:
        print("\n  control fails substantially more often than after_wrap - "
              "the opposite of the hypothesis; something else is going on.")
    else:
        print("\n  no substantial difference between conditions - the "
              "wrap-boundary hypothesis is not supported by this run.")


if __name__ == "__main__":
    main()
