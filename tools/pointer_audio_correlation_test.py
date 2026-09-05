#!/usr/bin/env python3
"""
pointer_audio_correlation_test.py - does the ring-pointer "double count"
anomaly found in pointer_slip_test.py (~0.12% of writes, the device
reporting wr advancing by one extra 512-byte block versus what was
actually sent) coincide with an actual audible failure when it happens on
a note's own write?

Fires real notes continuously while tracking predicted vs actual wr on
EVERY note-on write (built the same way midi.py's ChipChannel does, sent
here directly so this test can capture the ring state immediately after),
and separately scores each note's audibility via mic-based onset
detection (reusing audible_latency_probe.py). Cross-tabulates: audible
failure rate for notes whose own write showed a pointer mismatch, vs
notes whose write matched prediction exactly.

Setup: same as previous tests - direct line-level connection via an audio
interface on the chip pair's dry-out, in a quiet room.

Usage
-----
    uv run --extra audio python3 tools/pointer_audio_correlation_test.py \\
        --list-devices
    uv run --extra audio python3 tools/pointer_audio_correlation_test.py \\
        --input-device 1 --channel 1 --chip 0 --minutes 15
"""
import argparse
import os
import sys
import threading
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


def advance(wr, n=hs4u.BLOCK):
    return ((wr - 0x2000 + n) % hs4u.RING) + 0x2000


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
    ap.add_argument("--minutes", type=float, default=15.0)
    ap.add_argument("--hold", type=float, default=0.3)
    ap.add_argument("--gap", type=float, default=1.0)
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--threshold-db", type=float, default=12.0)
    ap.add_argument("--stuck-window", type=float, default=0.1,
                     help="seconds of trailing audio checked before each "
                          "note for a stuck/non-releasing previous note "
                          "(default 0.1). MUST be well under --gap, or the "
                          "check window overlaps the previous note's own "
                          "--hold period and false-positives on every "
                          "note (this bit us once already: hold=0.15 "
                          "gap=0.3 with window=0.4 flagged note #1 as "
                          "'stuck' when it was just still legitimately "
                          "sounding)")
    ap.add_argument("--save-wav", default="",
                     help="path to save the full run recording as a "
                          "16-bit WAV, so any claimed stuck note can be "
                          "inspected offline instead of being lost when "
                          "the process exits (default: don't save)")
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

    class RollingRecorder:
        """Streams audio via a callback so the test can inspect recent
        samples live (to detect a stuck/non-releasing note and stop
        itself before burning the rest of the run on contaminated
        data), while keeping the full recording for post-hoc onset
        analysis exactly like the earlier sd.rec()-based tests did."""

        def __init__(self, device, channels, samplerate, window_s, select_channel=0):
            self.samplerate = samplerate
            self.select_channel = select_channel
            self.window_samples = max(1, int(window_s * samplerate))
            self.lock = threading.Lock()
            self.chunks = []
            self.recent = np.zeros(self.window_samples, dtype="float32")
            self.stream = sd.InputStream(
                device=device, channels=channels, samplerate=samplerate,
                dtype="float32", callback=self._callback)

        def _callback(self, indata, frames, time_info, status):
            data = indata[:, self.select_channel].copy()
            with self.lock:
                self.chunks.append(data)
                if len(data) >= self.window_samples:
                    self.recent = data[-self.window_samples:]
                else:
                    self.recent = np.concatenate(
                        [self.recent[len(data):], data])

        def start(self):
            self.stream.start()

        def stop(self):
            self.stream.stop()
            self.stream.close()

        def recent_peak(self):
            with self.lock:
                return float(np.max(np.abs(self.recent)))

        def full_recording(self):
            with self.lock:
                if not self.chunks:
                    return np.zeros(0, dtype="float32")
                return np.concatenate(self.chunks)

    sr = args.samplerate
    print(f"[probe] streaming from input device {args.input_device}, "
          f"channel {args.channel}")
    recorder = RollingRecorder(args.input_device, args.rec_channels, sr,
                                args.stuck_window, select_channel=args.channel - 1)
    recorder.start()
    t_start = time.perf_counter()

    # The capture chain delivers audio LATE by the stream's input latency
    # - the "trailing window" of recent samples reflects the past, not
    # now. An uncompensated check can therefore see the previous note's
    # own hold phase and cry "stuck". Measure it and wait it out before
    # every stuck-check. (Capped: a pathological reported latency
    # shouldn't stall the note loop.)
    input_lat = min(float(recorder.stream.latency) + 0.02, 0.30)
    print(f"[probe] input stream latency: "
          f"{recorder.stream.latency * 1000:.1f}ms reported -> waiting "
          f"{input_lat * 1000:.0f}ms before each stuck-check")

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    ch = None
    trials = []  # (trigger_time, mismatch_bool)
    n = 0
    mismatches = 0
    stuck_detected = False
    noise_floor = threshold = 0.0
    try:
        hs.init(chips=(args.chip,))
        ch = midi.ChipChannel(hs, args.chip)
        ch.patch["waveform"] = args.waveform
        ch.patch["attack"] = 0
        ch.patch["decay"] = 2
        ch.patch["sustain"] = 15
        ch.patch["release"] = 0  # fastest rate - a slower release's own
                                  # natural decay tail from full sustain
                                  # can still be legitimately sounding
                                  # well past hold+gap, which would look
                                  # exactly like a stuck note to the live
                                  # detector and isn't one

        time.sleep(args.settle)

        # Noise floor from the settle-period audio captured so far, same
        # method as audible_latency_probe.py, just computed mid-flight
        # off the streaming recorder instead of a fixed pre-allocated
        # buffer slice.
        settle_env = alp.envelope(recorder.full_recording(), sr)
        noise_floor = float(np.median(settle_env)) if len(settle_env) else 0.0001
        threshold = noise_floor * (10 ** (args.threshold_db / 20))
        print(f"[detect] noise floor={noise_floor:.5f}  "
              f"threshold={threshold:.5f}  "
              f"stuck-check window={args.stuck_window}s")

        note = 69  # A4
        b = hs4u.VOICE_BASE[args.voice]
        freq = hs4u.freq_for_hz(midi.midi_note_to_hz(note))
        slot = ch.voices[args.voice]

        rd, wr, st, free = hs.state()
        expected_wr = wr

        t_end = time.perf_counter() + args.minutes * 60
        print(f"[probe] firing continuous notes for {args.minutes} "
              f"minutes, tracking pointer state on every note-on write, "
              f"chip {args.chip}")

        # Deliberately much more conservative than the onset-detection
        # threshold: recent_peak() is a raw max, noisier than the smoothed
        # envelope onset detection uses, and was already seen to false-
        # positive on ordinary noise-floor jitter at the onset threshold
        # (0.00092 vs 0.00081 - barely over, before any note had even
        # fired). A genuine stuck note in this session has shown peaks of
        # 0.6-0.9 against a ~0.0002 baseline - 100-1000x over - so this
        # margin costs nothing in sensitivity to a real one.
        stuck_threshold = max(threshold * 8, 0.02)
        print(f"[detect] stuck-check threshold={stuck_threshold:.5f} "
              f"(deliberately conservative vs onset threshold "
              f"{threshold:.5f})")

        stuck_detected = False
        while time.perf_counter() < t_end:
            # Live self-check: is the tail of the recording still elevated
            # right when it should be silent (previous note released,
            # full gap elapsed)? If so, a note is stuck on - stop now
            # rather than contaminate the rest of the run with false
            # "passed" results from a continuously-sounding chip.
            time.sleep(input_lat)  # let the capture pipeline catch up so
                                    # the trailing window really reflects
                                    # the post-gap period, not the hold
            peak1 = recorder.recent_peak()
            if peak1 > stuck_threshold:
                time.sleep(0.05)
                peak2 = recorder.recent_peak()
                if peak2 > stuck_threshold:
                    print(f"\n*** STUCK NOTE DETECTED before note #{n + 1} "
                          f"*** - trailing {args.stuck_window}s still "
                          f"elevated (peak={peak2:.5f} > "
                          f"stuck_threshold={stuck_threshold:.5f}) when it "
                          f"should be silent. Stopping here rather than running "
                          f"the rest of the {args.minutes}-minute test "
                          f"on contaminated data.")
                    stuck_detected = True
                    break

            n += 1
            pairs = [
                (args.chip, b + hs4u.R_FREQ_LO, freq & 0xFF),
                (args.chip, b + hs4u.R_FREQ_HI, (freq >> 8) & 0xFF),
            ] + ch._voice_patch_pairs(slot, gate=True)
            slot.note = note
            slot.state = "held"
            block = midi._build_block(pairs)

            t0 = time.perf_counter()
            while hs.state()[3] < len(block):
                if time.perf_counter() - t0 > 0.5:
                    break
                time.sleep(0.001)

            predicted_wr = advance(expected_wr)
            hs.h.bulkWrite(hs4u.EP_OUT, block, timeout=hs4u.TIMEOUT)
            trigger_t = time.perf_counter()
            rd, actual_wr, st, free = hs.state()
            mismatch = actual_wr != predicted_wr
            expected_wr = actual_wr

            if mismatch:
                mismatches += 1
                print(f"  [MISMATCH #{mismatches}] note #{n}  "
                      f"predicted={predicted_wr:#06x} actual={actual_wr:#06x} "
                      f"diff={(actual_wr - predicted_wr):+d}  "
                      f"t={trigger_t - t_start:.1f}s")

            trials.append((trigger_t, mismatch))

            time.sleep(args.hold)
            slot.state = "releasing"
            ch.note_off(note)
            rd, wr, st, free = hs.state()
            expected_wr = wr  # resync after note_off too

            if args.verbose and n % 20 == 0:
                print(f"  ...note #{n}, {mismatches} mismatches so far, "
                      f"t={trigger_t - t_start:.1f}s")

            time.sleep(args.gap)
    finally:
        if ch is not None:
            try:
                ch.all_notes_off()
            except Exception:
                pass
        hs.close()
        recorder.stop()

    if stuck_detected:
        print(f"\n[note] stopped early due to a detected stuck note - "
              f"analyzing the {n} notes collected before that point.")

    samples = recorder.full_recording()

    if args.save_wav:
        import wave
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
        with wave.open(args.save_wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        print(f"[wav] full recording saved to {args.save_wav} "
              f"({len(samples) / sr:.1f}s)")

    env = alp.envelope(samples, sr)
    print(f"\n[detect] noise floor={noise_floor:.5f}  threshold={threshold:.5f} "
          f"(computed from the settle period, unchanged since)")

    search_s = min(0.4, args.gap * 0.8)
    results = {"mismatch": [0, 0], "clean": [0, 0]}  # [passed, failed]
    for trigger_t, mismatch in trials:
        onset = alp.find_onset(env, sr, t_start, trigger_t, search_s, threshold)
        key = "mismatch" if mismatch else "clean"
        if onset is None:
            results[key][1] += 1
        else:
            results[key][0] += 1

    print("\n=== RESULTS ===")
    for key in ("mismatch", "clean"):
        passed, failed = results[key]
        total = passed + failed
        rate = failed / total * 100 if total else 0
        print(f"  {key:8s}: {passed} passed, {failed} failed / "
              f"{total} notes  ({rate:.0f}% failure rate)")
    print(f"\n  total notes: {n}, pointer mismatches on note-on: "
          f"{mismatches} ({mismatches / max(n, 1) * 100:.3f}%)")

    mismatch_total = sum(results["mismatch"])
    clean_total = sum(results["clean"])
    if mismatch_total and clean_total:
        mismatch_rate = results["mismatch"][1] / mismatch_total
        clean_rate = results["clean"][1] / clean_total
        if mismatch_rate > clean_rate + 0.2:
            print("\n  Notes with a pointer mismatch on their own write "
                  "fail substantially more often - the double-count "
                  "anomaly is very likely the ghost-note mechanism.")
        elif clean_rate > mismatch_rate + 0.2:
            print("\n  Notes WITHOUT a mismatch fail more often, the "
                  "opposite of the hypothesis - unexpected, worth a "
                  "closer look at the data.")
        else:
            print("\n  No clear difference - either too few mismatch "
                  "samples yet, or the mismatch isn't directly causing "
                  "audible failures on the SAME note.")
    else:
        print(f"\n  Only {mismatch_total} mismatch sample(s) - run longer "
              f"for a meaningful comparison.")


if __name__ == "__main__":
    main()
