#!/usr/bin/env python3
"""
audible_latency_probe.py — measure NOTE-ON-to-heard-sound latency on the
HardSID 4U end to end, by recording its analog output while firing
controlled note triggers with known host timestamps.

midi_latency_probe.py only measures how long bulkWrite() takes to return
(median ~0.8ms, ring never backs up under sustained triggering). That
proves the USB transport is fast; it says nothing about how long the
device takes to turn a queued command into actual sound, or how far the
analog output lags. This script closes that gap: it records the real
analog signal and compares detected onsets against trigger timestamps.

CAVEAT — read before trusting the number: this measures the WHOLE chain,
trigger-timestamp -> heard onset, which includes things that are NOT the
device's fault:
  - your audio interface's own input buffering (can be several ms,
    sometimes 10-20ms+ depending on buffer size/driver)
  - room/mic propagation delay if you're using a mic on a speaker rather
    than a direct cable into a line/instrument input — prefer a direct
    cable connection, it removes this term entirely
So the number this script reports is an UPPER BOUND on the device's true
contribution, not a clean isolation of it. A rough sanity check on your
own interface's floor: clap or tap near the mic a few times before/after
the real run and eyeball that those onsets look fast too.

Setup
-----
Connect the HardSID 4U's audio output to an interface input, then:

    uv run --extra audio python3 tools/audible_latency_probe.py \\
        --list-devices
    uv run --extra audio python3 tools/audible_latency_probe.py \\
        --input-device 2 --notes 30
"""
import argparse
import os
import statistics
import sys
import time

# midi_latency_probe is a sibling tool, so tools/ must stay on the
# path; hs4u comes from the installed package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hardsid4u import hs4u
except ImportError:  # source tree without the package installed
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "hardsid4u"))
    import hs4u  # noqa: E402
import midi_latency_probe as mlp  # noqa: E402


def report(label, samples):
    if not samples:
        print(f"  {label}: no samples")
        return
    ms = sorted(s * 1000 for s in samples)
    n = len(ms)
    p95 = ms[int(n * 0.95)] if n > 1 else ms[0]
    print(f"  {label}: n={n}  min={ms[0]:.2f}ms  "
          f"median={statistics.median(ms):.2f}ms  "
          f"p95={p95:.2f}ms  max={ms[-1]:.2f}ms")


def list_devices():
    import sounddevice as sd
    print(sd.query_devices())


def envelope(samples, samplerate, window_ms=5):
    import numpy as np
    win = max(1, int(samplerate * window_ms / 1000))
    kernel = np.ones(win, dtype="float32") / win
    return np.convolve(np.abs(samples), kernel, mode="same")


def find_onset(env, samplerate, t_start, trigger_time, search_s, threshold):
    import numpy as np
    start_idx = int((trigger_time - t_start) * samplerate)
    end_idx = min(start_idx + int(search_s * samplerate), len(env))
    if start_idx < 0 or start_idx >= len(env):
        return None
    window = env[start_idx:end_idx]
    above = np.where(window > threshold)[0]
    if len(above) == 0:
        return None
    onset_idx = start_idx + int(above[0])
    return t_start + onset_idx / samplerate


def run(args):
    import numpy as np
    import sounddevice as sd

    sr = args.samplerate
    total_s = args.settle + args.notes * (args.hold + args.interval) + 1.0
    frames = int(total_s * sr)

    print(f"[probe] recording {total_s:.1f}s from input device "
          f"{args.input_device} at {sr}Hz")
    rec_buf = sd.rec(frames, samplerate=sr, channels=1,
                      device=args.input_device, dtype="float32",
                      latency="low", blocksize=128)
    t_start = time.perf_counter()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=args.verbose)
    hs.open()
    triggers = []
    try:
        hs.voice(args.chip, args.voice, waveform=args.waveform,
                  attack=0, decay=2, sustain=15, release=6)
        hs.volume(args.chip, level=15)
        hs.flush()
        hs.drain()

        time.sleep(args.settle)  # capture a noise-floor window, untouched

        freq = hs4u.freq_for_hz(args.hz)
        print(f"[probe] {args.notes} triggered notes, chip {args.chip} "
              f"voice {args.voice}, {args.hz}Hz, {args.interval}s apart")
        for i in range(args.notes):
            t0 = time.perf_counter()
            mlp.write_now(hs, mlp.build_note_on_block(
                args.chip, args.voice, freq, args.waveform))
            triggers.append(t0)
            time.sleep(args.hold)
            mlp.write_now(hs, mlp.build_note_off_block(
                args.chip, args.voice, args.waveform))
            time.sleep(args.interval)
            if args.verbose:
                print(f"  ...{i + 1}/{args.notes}")
    finally:
        hs.silence(chips=(args.chip,))
        hs.close()

    sd.wait()
    samples = rec_buf[:, 0]
    env = envelope(samples, sr)

    noise_end_idx = int(args.settle * sr * 0.9)  # margin off the start
    noise_floor = float(np.median(env[:max(1, noise_end_idx)]))
    threshold = noise_floor * (10 ** (args.threshold_db / 20))
    print(f"\n[detect] noise floor envelope={noise_floor:.5f}  "
          f"threshold (+{args.threshold_db}dB)={threshold:.5f}")

    search_s = min(0.4, args.interval * 0.8)
    latencies = []
    misses = 0
    peak_seen = 0.0
    for i, t0 in enumerate(triggers):
        start_idx = int((t0 - t_start) * sr)
        end_idx = min(start_idx + int(search_s * sr), len(env))
        if 0 <= start_idx < len(env):
            peak_seen = max(peak_seen, float(np.max(env[start_idx:end_idx])))
        onset = find_onset(env, sr, t_start, t0, search_s, threshold)
        if onset is None:
            misses += 1
            if args.verbose:
                print(f"  note {i:2d}: MISS")
            continue
        lat = onset - t0
        latencies.append(lat)
        if args.verbose:
            print(f"  note {i:2d}: {lat * 1000:.2f}ms")

    print(f"[detect] peak envelope seen in trigger windows: {peak_seen:.5f}  "
          f"(vs noise floor {noise_floor:.5f}, threshold {threshold:.5f})")

    print(f"\n[results] trigger-to-heard-onset latency "
          f"({len(latencies)}/{len(triggers)} detected, {misses} missed):")
    report("audible", latencies)
    if misses:
        print("  missed onsets: raise input gain, lower --threshold-db, or "
              "check the physical connection to the analog output.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-devices", action="store_true",
                     help="show audio devices and exit")
    ap.add_argument("--input-device", type=int, default=None,
                     help="sounddevice input device index (see --list-devices)")
    ap.add_argument("--samplerate", type=int, default=48000)
    ap.add_argument("--chip", type=int, default=0)
    ap.add_argument("--voice", type=int, default=0, choices=(0, 1, 2))
    ap.add_argument("--hz", type=float, default=440.0)
    ap.add_argument("--waveform", type=lambda s: int(s, 0), default=hs4u.PULSE,
                     help="waveform bits, default PULSE (0x40) for a clear "
                          "transient")
    ap.add_argument("--notes", type=int, default=30)
    ap.add_argument("--hold", type=float, default=0.15,
                     help="seconds between note-on and note-off")
    ap.add_argument("--interval", type=float, default=1.0,
                     help="seconds between note events - keep this well "
                          "above the release tail so onsets don't overlap")
    ap.add_argument("--settle", type=float, default=1.0,
                     help="seconds of untouched recording at the start, "
                          "used to measure the noise floor")
    ap.add_argument("--threshold-db", type=float, default=12.0,
                     help="onset threshold, dB above the measured noise floor")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    if args.input_device is None:
        print("--input-device is required (see --list-devices)")
        sys.exit(1)

    run(args)


if __name__ == "__main__":
    main()
