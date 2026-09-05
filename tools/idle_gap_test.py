#!/usr/bin/env python3
"""
idle_gap_test.py - A/B test: does a genuine idle gap (no ring traffic at
all) before a note cause it to fail more often than firing notes back to
back with minimal gap?

Follow-up to wrap_boundary_test.py, which disproved the wrap-address
hypothesis (after_wrap actually failed LESS often than a control position,
not more) but surfaced a bigger finding: 17.5% of ALL 40 trials failed in
that test, regardless of condition - and the one thing every trial there
shared was sparse traffic with real idle time between events (~1.3s
apart), unlike the heavy/dense synthetic endurance tests earlier this
session, which ran for 6-8 minutes of continuous traffic and never failed
once. This isolates idle time as the single variable: does a socket's
armed state (or something else) degrade if nothing real is sent to it for
a while, independent of ring address?

Setup: same as wrap_boundary_test.py - a direct line-level connection via
an audio interface on the chip pair's dry-out, mic-based onset detection
for automatic pass/fail scoring (reuses audible_latency_probe.py).

Usage
-----
    uv run --extra audio python3 tools/idle_gap_test.py \\
        --list-devices
    uv run --extra audio python3 tools/idle_gap_test.py \\
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


def run_trial(hs, ch, note, gap, hold=0.3):
    """Wait `gap` seconds of genuine idle (no writes at all), then fire
    one note-on/note-off pair."""
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
    ap.add_argument("--idle-gap", type=float, default=1.3,
                     help="seconds of genuine idle before a note in the "
                          "'idle' condition (default 1.3, matching the "
                          "gap that reproduced failures in the wrap test)")
    ap.add_argument("--dense-gap", type=float, default=0.03,
                     help="seconds between notes in the 'dense' condition "
                          "(default 0.03)")
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
    est_per_trial = args.hold + 0.2 + max(args.idle_gap, args.dense_gap)
    total_s = args.settle + total_trials * est_per_trial + 2.0
    frames = int(total_s * sr)

    print(f"[probe] recording {total_s:.1f}s from input device "
          f"{args.input_device}, channel {args.channel}")
    # latency='low'/blocksize=128 (as used in the shorter wrap-boundary
    # test) segfaulted deep inside PortAudio/CoreAudio's own callback
    # machinery during this test's longer continuous recording - unrelated
    # to the HardSID/USB side, which completed all trials fine before the
    # crash. Relaxed settings here; onset detection has a 400ms search
    # window, so we don't need the tightest possible capture latency.
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
        conditions = ([("idle", args.idle_gap)] * args.trials +
                      [("dense", args.dense_gap)] * args.trials)
        random.shuffle(conditions)

        print(f"[probe] {total_trials} trials ({args.trials} per "
              f"condition), randomized order, chip {args.chip}")
        for i, (cond, gap) in enumerate(conditions):
            t0 = run_trial(hs, ch, note, gap, hold=args.hold)
            trials.append((cond, t0))
            if args.verbose:
                print(f"  [{i + 1}/{total_trials}] {cond:6s} gap={gap:.2f}s "
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
    results = {"idle": [0, 0], "dense": [0, 0]}  # [passed, failed]
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
    for cond in ("idle", "dense"):
        passed, failed = results[cond]
        total = passed + failed
        rate = failed / total * 100 if total else 0
        print(f"  {cond:8s}: {passed} passed, {failed} failed / "
              f"{total} trials  ({rate:.0f}% failure rate)")
    idle_rate = results["idle"][1] / max(1, sum(results["idle"]))
    dense_rate = results["dense"][1] / max(1, sum(results["dense"]))
    if idle_rate > dense_rate + 0.15:
        print("\n  idle gap fails substantially more often than dense - "
              "supports an idle-time/de-arming-related mechanism.")
    elif dense_rate > idle_rate + 0.15:
        print("\n  dense fires fail substantially more often than idle - "
              "the opposite; rapid retriggering is implicated, not idle time.")
    else:
        print("\n  no substantial difference - idle time alone doesn't "
              "explain it either.")


if __name__ == "__main__":
    main()
