#!/usr/bin/env python3
"""
filler_injection_test.py - A/B test: does firing a note immediately after
one or more standalone filler-only blocks fail more often than firing it
after an equal-duration idle gap with NO writes to the device at all?

Follow-up to two earlier tests this session:
  - wrap_boundary_test.py: 17.5% failures overall. Every note was preceded
    by pad_to() injecting a variable number of standalone filler-only
    blocks (to land the note at a specific ring address).
  - idle_gap_test.py: 0% failures. Every note was preceded by a pure
    time.sleep() of similar duration, with ZERO writes to the device
    during the gap.

Address was ruled out (the wrap test's own after_wrap vs control
comparison showed no effect), and idle time alone was ruled out (the idle
test's own idle vs dense comparison showed no effect). The one structural
difference LEFT between those two tests is whether filler-only blocks
were actually written to the device during the gap before the note. This
isolates that as the sole variable, holding gap duration and everything
else fixed.

Setup: same as the previous two tests - direct line-level connection via
an audio interface on the chip pair's dry-out, mic-based onset detection
for automatic pass/fail scoring (reuses audible_latency_probe.py).

Usage
-----
    uv run --extra audio python3 tools/filler_injection_test.py \\
        --list-devices
    uv run --extra audio python3 tools/filler_injection_test.py \\
        --input-device 1 --channel 1 --chip 0 --trials 20
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


def send_filler_blocks(hs, n):
    """Send n standalone 512-byte, pure-FILLER blocks (no real command
    content at all) - the thing pad_to() did as a side effect of
    positioning, isolated here as the thing itself being tested."""
    filler_block = hs4u.FILLER * (hs4u.BLOCK // 2)
    for _ in range(n):
        t0 = time.perf_counter()
        while hs.state()[3] < len(filler_block):
            if time.perf_counter() - t0 > 0.5:
                break
            time.sleep(0.001)
        hs.h.bulkWrite(hs4u.EP_OUT, filler_block, timeout=hs4u.TIMEOUT)


def run_trial(hs, ch, note, gap, inject_filler, rng, hold=0.3):
    """Wait `gap` seconds total before firing a note-on/off pair. If
    inject_filler, spend part of that gap sending a random number (1-15,
    matching the range pad_to() could naturally produce) of standalone
    filler-only blocks first, then sleep out the remainder so total gap
    duration matches the no-filler condition exactly. Otherwise just
    sleep the full gap with zero writes to the device."""
    if inject_filler:
        n = rng.randint(1, 15)
        t_pre = time.perf_counter()
        send_filler_blocks(hs, n)
        elapsed = time.perf_counter() - t_pre
        time.sleep(max(0.0, gap - elapsed))
    else:
        time.sleep(gap)
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
    ap.add_argument("--rec-channels", type=int, default=2)
    ap.add_argument("--channel", type=int, default=1, choices=(1, 2),
                     help="which recorded channel has the dry-out signal")
    ap.add_argument("--samplerate", type=int, default=48000)
    ap.add_argument("--chip", type=int, default=0,
                     help="0=ch1/C1, 1=ch2/C2, 2=ch3/C3, 3=ch4/C4")
    ap.add_argument("--voice", type=int, default=0, choices=(0, 1, 2))
    ap.add_argument("--waveform", type=lambda s: int(s, 0), default=hs4u.PULSE)
    ap.add_argument("--trials", type=int, default=20,
                     help="trials PER condition (default 20, so 40 total)")
    ap.add_argument("--hold", type=float, default=0.3)
    ap.add_argument("--gap", type=float, default=1.3,
                     help="seconds before each note, both conditions "
                          "(default 1.3, matching the earlier tests)")
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
    total_s = args.settle + total_trials * (args.hold + args.gap + 0.2) + 2.0
    frames = int(total_s * sr)

    print(f"[probe] recording {total_s:.1f}s from input device "
          f"{args.input_device}, channel {args.channel}")
    # No latency='low'/blocksize=128 here - those segfaulted deep inside
    # PortAudio/CoreAudio's own callback machinery during a similar long
    # continuous recording earlier this session (unrelated to the
    # HardSID/USB side, which had completed all its trials fine before
    # that crash). Default settings are stable; a 400ms onset-detection
    # search window doesn't need the tightest possible capture latency.
    rec_buf = sd.rec(frames, samplerate=sr, channels=args.rec_channels,
                      device=args.input_device, dtype="float32")
    t_start = time.perf_counter()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    trials = []  # list of (condition, absolute_trigger_time)
    ch = None
    rng = random.Random()
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
        conditions = ([("filler", True)] * args.trials +
                      [("no_filler", False)] * args.trials)
        rng.shuffle(conditions)

        print(f"[probe] {total_trials} trials ({args.trials} per "
              f"condition), randomized order, chip {args.chip}, "
              f"gap={args.gap}s fixed both conditions")
        for i, (cond, inject) in enumerate(conditions):
            t0 = run_trial(hs, ch, note, args.gap, inject, rng, hold=args.hold)
            trials.append((cond, t0))
            if args.verbose:
                print(f"  [{i + 1}/{total_trials}] {cond:10s} "
                      f"t={t0 - t_start:.2f}s")
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

    search_s = 0.4
    peak_seen = 0.0
    results = {"filler": [0, 0], "no_filler": [0, 0]}  # [passed, failed]
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

    print(f"[detect] peak envelope seen in any trigger window: {peak_seen:.5f}")

    print("\n=== RESULTS ===")
    for cond in ("filler", "no_filler"):
        passed, failed = results[cond]
        total = passed + failed
        rate = failed / total * 100 if total else 0
        print(f"  {cond:10s}: {passed} passed, {failed} failed / "
              f"{total} trials  ({rate:.0f}% failure rate)")
    filler_rate = results["filler"][1] / max(1, sum(results["filler"]))
    no_filler_rate = results["no_filler"][1] / max(1, sum(results["no_filler"]))
    if filler_rate > no_filler_rate + 0.15:
        print("\n  filler-injected notes fail substantially more often - "
              "supports filler-block-transition as the trigger.")
    elif no_filler_rate > filler_rate + 0.15:
        print("\n  no_filler notes fail substantially more often - the "
              "opposite; filler injection is not implicated.")
    else:
        print("\n  no substantial difference - filler injection alone "
              "doesn't explain it either.")


if __name__ == "__main__":
    main()
