#!/usr/bin/env python3
"""
midi_latency_probe.py — measure Note On/Off transport latency on the
HardSID 4U over a real-time write path, bypassing hs4u.py's buffered,
delay-scheduled flush().

Why a separate path: flush() is built for pre-scheduled playback (SID-file
style) — it batches register writes and paces them with 0xEE/0xEF delay
codes baked into the stream. A MIDI performance needs the opposite: react
to a Note On the instant it arrives. This script sends exactly one
512-byte block per note event (freq + gate, padded with 0xFF filler, which
costs the device no time) and times how long bulkWrite() takes to return.

IMPORTANT CAVEAT: this measures USB transport latency, not audible
latency. Every 512-byte write still enters the device's 8KB ring and is
only acted on once the device's own delta-timed consumption reaches it
(see docs/protocol.md §5-6, §14). If the ring already has a backlog ahead
of our block, audible latency will be worse than what this script reports.
That's what --check-ring is for: it confirms the ring is staying empty
between notes rather than silently accumulating a queue. True audible
latency needs a mic/scope on the analog output as a follow-up measurement
— this script only tells you whether the transport is fast enough to be
worth that follow-up.

Modes
-----
--synthetic (default)  fire Note On/Off in a loop, no MIDI gear needed.
--midi                 open a virtual CoreMIDI destination named
                        "HardSID4U Probe" so Ableton/Cubase/a keyboard can
                        play it. Requires `pip install python-rtmidi`.

Usage
-----
    python3 midi_latency_probe.py
    python3 midi_latency_probe.py --notes 500 --interval 0.03 --check-ring
    python3 midi_latency_probe.py --midi
"""
import argparse
import os
import statistics
import sys
import time

# tools/ stays on the path so scripts can import each other as
# siblings; hs4u itself comes from the installed package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hardsid4u import hs4u
except ImportError:  # running from a source tree without the package installed
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "..", "src", "hardsid4u"))
    import hs4u  # noqa: E402


def build_note_on_block(chip, voice, freq, waveform):
    """freq lo/hi + control(gate on), padded to one 512-byte block."""
    b = hs4u.VOICE_BASE[voice]
    payload = (
        hs4u.encode_reg(chip, b + hs4u.R_FREQ_LO, freq & 0xFF)
        + hs4u.encode_delay(hs4u.MIN_CYCLES)
        + hs4u.encode_reg(chip, b + hs4u.R_FREQ_HI, (freq >> 8) & 0xFF)
        + hs4u.encode_delay(hs4u.MIN_CYCLES)
        + hs4u.encode_reg(chip, b + hs4u.R_CONTROL, waveform | hs4u.GATE)
    )
    return _pad(payload)


def build_note_off_block(chip, voice, waveform):
    """control(gate off) alone, padded to one 512-byte block."""
    b = hs4u.VOICE_BASE[voice]
    payload = hs4u.encode_reg(chip, b + hs4u.R_CONTROL, waveform & ~hs4u.GATE)
    return _pad(payload)


def _pad(payload):
    payload += hs4u.FILLER * (((-len(payload)) % hs4u.BLOCK) // 2)
    assert len(payload) == hs4u.BLOCK
    return payload


def write_now(hs, block):
    """Bypass hs.flush()/hs._buf entirely: one immediate bulk write."""
    t0 = time.perf_counter()
    hs.h.bulkWrite(hs4u.EP_OUT, block, timeout=hs4u.TIMEOUT)
    return time.perf_counter() - t0


def midi_to_hz(note):
    return 440.0 * (2.0 ** ((note - 69) / 12.0))


def report(label, samples):
    if not samples:
        print(f"  {label}: no samples")
        return
    ms = sorted(s * 1000 for s in samples)
    n = len(ms)
    p95 = ms[int(n * 0.95)] if n > 1 else ms[0]
    print(f"  {label}: n={n}  min={ms[0]:.3f}ms  "
          f"median={statistics.median(ms):.3f}ms  "
          f"p95={p95:.3f}ms  max={ms[-1]:.3f}ms")


def run_synthetic(hs, args):
    freq = hs4u.freq_for_hz(args.hz)
    waveform = args.waveform
    on_times, off_times = [], []
    free_before, free_after = [], []

    print(f"[probe] {args.notes} note on/off pairs, "
          f"{args.interval * 1000:.0f}ms interval, chip {args.chip} "
          f"voice {args.voice}, {args.hz}Hz  (Ctrl+C to stop early)")

    try:
        for i in range(args.notes):
            if args.check_ring:
                free_before.append(hs.state()[3])

            on_times.append(write_now(hs, build_note_on_block(
                args.chip, args.voice, freq, waveform)))
            time.sleep(args.hold)
            off_times.append(write_now(hs, build_note_off_block(
                args.chip, args.voice, waveform)))

            if args.check_ring:
                free_after.append(hs.state()[3])

            time.sleep(args.interval)
            if args.verbose and (i + 1) % 20 == 0:
                print(f"  ...{i + 1}/{args.notes}")
    except KeyboardInterrupt:
        print("\n  stopped early")

    print("\n[results] write() call latency (transport only, not audible):")
    report("note-on ", on_times)
    report("note-off", off_times)

    if args.check_ring and free_before:
        print("\n[ring] free space in bytes (8192 = fully empty):")
        print(f"  before writes: min={min(free_before)} max={max(free_before)}")
        print(f"  after writes:  min={min(free_after)} max={max(free_after)}")
        if min(free_before) < hs4u.RING - hs4u.BLOCK:
            print("  NOTE: ring did not stay empty between notes - a backlog")
            print("  is accumulating, which means audible latency will run")
            print("  ahead of what the write() timings above suggest.")


class MidiHandler:
    """rtmidi callback: fires on its own internal thread, one at a time."""

    def __init__(self, hs, chip, voice, waveform):
        self.hs = hs
        self.chip = chip
        self.voice = voice
        self.waveform = waveform
        self.on_times = []
        self.off_times = []

    def __call__(self, event, data=None):
        message, _delta = event
        if len(message) < 3:
            return
        status = message[0] & 0xF0
        note, velocity = message[1], message[2]
        if status == 0x90 and velocity > 0:
            freq = hs4u.freq_for_hz(midi_to_hz(note))
            t = write_now(self.hs, build_note_on_block(
                self.chip, self.voice, freq, self.waveform))
            self.on_times.append(t)
            print(f"  note on  {note:3d}  write={t * 1000:.3f}ms")
        elif status == 0x80 or (status == 0x90 and velocity == 0):
            t = write_now(self.hs, build_note_off_block(
                self.chip, self.voice, self.waveform))
            self.off_times.append(t)
            print(f"  note off {note:3d}  write={t * 1000:.3f}ms")


def run_midi(hs, args):
    try:
        import rtmidi
    except ImportError:
        print("python-rtmidi is required for --midi: pip install python-rtmidi")
        sys.exit(1)

    handler = MidiHandler(hs, args.chip, args.voice, args.waveform)
    midi_in = rtmidi.MidiIn()
    midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
    midi_in.set_callback(handler)
    midi_in.open_virtual_port("HardSID4U Probe")
    print('[probe] virtual MIDI destination "HardSID4U Probe" open.')
    print("        Select it as a MIDI output in Ableton/Cubase, or route")
    print("        a controller into it. Ctrl+C to stop and print stats.")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        midi_in.close_port()
        del midi_in

    print("\n[results] write() call latency (transport only, not audible):")
    report("note-on ", handler.on_times)
    report("note-off", handler.off_times)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--midi", action="store_true",
                     help="listen on a virtual MIDI port instead of firing "
                          "synthetic notes")
    ap.add_argument("--chip", type=int, default=0)
    ap.add_argument("--voice", type=int, default=0, choices=(0, 1, 2))
    ap.add_argument("--hz", type=float, default=440.0,
                     help="frequency for synthetic mode (default 440Hz)")
    ap.add_argument("--waveform", type=lambda s: int(s, 0),
                     default=hs4u.PULSE,
                     help="waveform bits, default PULSE (0x40)")
    ap.add_argument("--notes", type=int, default=200,
                     help="synthetic mode: number of note on/off pairs")
    ap.add_argument("--interval", type=float, default=0.05,
                     help="synthetic mode: seconds between note pairs")
    ap.add_argument("--hold", type=float, default=0.08,
                     help="synthetic mode: seconds between note-on and "
                          "note-off")
    ap.add_argument("--check-ring", action="store_true",
                     help="read ring free-space before/after each write "
                          "(adds its own USB traffic - diagnostic only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=args.verbose)
    hs.open()
    try:
        # Fast attack/decay so onset is audible almost immediately; held
        # sustain so timing is easy to judge by ear; moderate release.
        hs.voice(args.chip, args.voice, waveform=args.waveform,
                  attack=0, decay=2, sustain=15, release=6)
        hs.volume(args.chip, level=15)
        hs.flush()
        hs.drain()

        if args.midi:
            run_midi(hs, args)
        else:
            run_synthetic(hs, args)
    finally:
        hs.silence(chips=(args.chip,))
        hs.close()


if __name__ == "__main__":
    main()
