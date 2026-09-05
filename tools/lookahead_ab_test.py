#!/usr/bin/env python3
"""
lookahead_ab_test.py - A/B test of the two traffic shapes this device has
seen, scored by ring-pointer mismatch rate.

The single most discriminating fact of the whole failure investigation:
Acid64 hammered this unit for 10 minutes - including the suspect 6581
chips - with zero failures, while our midi.py-style traffic fails in
seconds-to-minutes and shows a ~0.12%/write pointer "double count"
anomaly (pointer_slip_test.py). The difference between the two is traffic
shape, so this script plays the SAME note pattern two ways and compares:

  Mode A ("instant", midi.py-shaped)
      Each note-on and note-off is its own 512-byte block, mostly
      zero-time filler, sent immediately; pacing comes from wall-clock
      sleeps; the ring drains instantly and the device engine idles
      between events.

  Mode B ("scheduled", Acid64-shaped)
      Each note cycle is one 512-byte block whose delays are encoded IN
      the stream (real 0xEE/0xEF cycle counts for hold and gap); pacing
      comes from the device consuming the ring; the engine is
      continuously busy executing timed content and the ring stays
      stocked. No wall-clock sleeps at all.

Both modes track predicted-vs-actual wr after every single write, exactly
like pointer_slip_test.py.

RESULT (this test has been run - hypothesis REFUTED): 15 min per mode,
~31k writes, instant 0.082% vs scheduled 0.092% mismatches - statistically
identical, both matching the ~0.12% baseline. Keeping the engine busy did
NOT reduce the +512 anomaly, so the traffic-shape theory is wrong and a
look-ahead mode for midi.py would add latency for no benefit. The +512
double-count is intrinsic to this HardSID/libusb/macOS transport path.
Kept as a regression/characterization tool, not a live investigation.

Pure USB test - no audio interface needed, though the notes are audible,
so glitches can be heard while it runs.

Usage
-----
    uv run python3 tools/lookahead_ab_test.py --minutes 15
    uv run python3 tools/lookahead_ab_test.py --minutes 1 --smoke
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
from pointer_slip_test import advance  # noqa: E402


def _pad(payload):
    payload += hs4u.FILLER * (((-len(payload)) % hs4u.BLOCK) // 2)
    return payload


def note_on_pairs(chip, voice, freq, patch):
    """Same register set midi.py's note_on writes, in the same order."""
    b = hs4u.VOICE_BASE[voice]
    ctrl = patch["waveform"] | hs4u.GATE
    return [
        (chip, b + hs4u.R_FREQ_LO, freq & 0xFF),
        (chip, b + hs4u.R_FREQ_HI, (freq >> 8) & 0xFF),
        (chip, b + hs4u.R_AD, ((patch["attack"] & 0xF) << 4) | (patch["decay"] & 0xF)),
        (chip, b + hs4u.R_SR, ((patch["sustain"] & 0xF) << 4) | (patch["release"] & 0xF)),
        (chip, b + hs4u.R_PW_LO, patch["pulse_width"] & 0xFF),
        (chip, b + hs4u.R_PW_HI, (patch["pulse_width"] >> 8) & 0x0F),
        (chip, b + hs4u.R_CONTROL, ctrl),
    ]


def note_off_pairs(chip, voice, patch):
    b = hs4u.VOICE_BASE[voice]
    return [(chip, b + hs4u.R_CONTROL, patch["waveform"] & ~hs4u.GATE)]


class PointerTracker:
    """Predicted-vs-actual wr bookkeeping shared by both modes."""

    def __init__(self, hs, label):
        self.hs = hs
        self.label = label
        self.writes = 0
        self.mismatches = 0
        rd, wr, st, free = hs.state()
        self.expected_wr = wr

    def tracked_write(self, block):
        assert len(block) % hs4u.BLOCK == 0
        predicted = self.expected_wr
        for _ in range(len(block) // hs4u.BLOCK):
            predicted = advance(predicted)
        self.hs.h.bulkWrite(hs4u.EP_OUT, block, timeout=hs4u.TIMEOUT)
        self.writes += 1
        rd, wr, st, free = self.hs.state()
        if wr != predicted:
            self.mismatches += 1
            print(f"  [{self.label} MISMATCH #{self.mismatches}] "
                  f"write #{self.writes}  predicted={predicted:#06x} "
                  f"actual={wr:#06x} diff={(wr - predicted):+d}  "
                  f"rd={rd:#06x} state={st:#06x}")
        self.expected_wr = wr  # resync either way
        return free

    def summary(self):
        rate = self.mismatches / max(self.writes, 1) * 100
        return (f"  {self.label:9s}: {self.mismatches} mismatches / "
                f"{self.writes} writes  ({rate:.4f}%)")


def build_reg_stream(pairs, inter_delay=hs4u.MIN_CYCLES):
    """Register pairs -> wire bytes with MIN_CYCLES spacing (no padding)."""
    s = b""
    for i, (chip, reg, val) in enumerate(pairs):
        if i:
            s += hs4u.encode_delay(inter_delay)
        s += hs4u.encode_reg(chip, reg, val)
    return s


def run_mode_a(hs, tracker, args, patch, notes, t_end):
    """midi.py-shaped: instant filler-padded blocks, wall-clock pacing."""
    i = 0
    while time.perf_counter() < t_end:
        freq = hs4u.freq_for_hz(midi.midi_note_to_hz(notes[i % len(notes)]))
        i += 1
        on_block = _pad(build_reg_stream(
            note_on_pairs(args.chip, args.voice, freq, patch)))
        off_block = _pad(build_reg_stream(
            note_off_pairs(args.chip, args.voice, patch)))

        t0 = time.perf_counter()
        while hs.state()[3] < len(on_block):
            if time.perf_counter() - t0 > 0.5:
                break
            time.sleep(0.001)
        tracker.tracked_write(on_block)
        time.sleep(args.hold)

        t0 = time.perf_counter()
        while hs.state()[3] < len(off_block):
            if time.perf_counter() - t0 > 0.5:
                break
            time.sleep(0.001)
        tracker.tracked_write(off_block)
        time.sleep(args.gap)


def run_mode_b(hs, tracker, args, patch, notes, t_end):
    """Acid64-shaped: delays encoded in-stream, paced by ring consumption.

    Each note cycle is one 512-byte block: note-on regs, delay(hold),
    note-off, delay(gap). The host pushes blocks as fast as the ring
    accepts them; the device's own delta-timed execution provides all
    pacing, so the engine never idles while music is pending."""
    hold_cycles = int(args.hold * hs4u.PAL_CLOCK)
    gap_cycles = int(args.gap * hs4u.PAL_CLOCK)
    i = 0
    while time.perf_counter() < t_end:
        freq = hs4u.freq_for_hz(midi.midi_note_to_hz(notes[i % len(notes)]))
        i += 1
        payload = build_reg_stream(
            note_on_pairs(args.chip, args.voice, freq, patch))
        payload += hs4u.encode_delay(hold_cycles)
        payload += build_reg_stream(
            note_off_pairs(args.chip, args.voice, patch))
        payload += hs4u.encode_delay(gap_cycles)
        block = _pad(payload)
        assert len(block) == hs4u.BLOCK, len(block)

        # Throttle on ring space only - no wall-clock sleeps. The write
        # blocks here whenever the ring is full, i.e. whenever the device
        # already holds ~16 blocks (~a second-plus) of scheduled music.
        t0 = time.perf_counter()
        while hs.state()[3] < 2 * hs4u.BLOCK:
            if time.perf_counter() - t0 > 10.0:
                raise RuntimeError("ring stopped draining in mode B")
            time.sleep(0.005)
        tracker.tracked_write(block)
    hs.drain(limit=30.0)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chip", type=int, default=0)
    ap.add_argument("--voice", type=int, default=0, choices=(0, 1, 2))
    ap.add_argument("--minutes", type=float, default=15.0,
                     help="duration PER MODE (default 15, so 30 total)")
    ap.add_argument("--hold", type=float, default=0.05)
    ap.add_argument("--gap", type=float, default=0.05)
    ap.add_argument("--order", choices=("ab", "ba"), default="ab",
                     help="which mode runs first (swap to control for "
                          "warm-up/ordering effects across runs)")
    args = ap.parse_args()

    patch = dict(waveform=hs4u.PULSE, attack=0, decay=2, sustain=15,
                  release=0, pulse_width=0x800)
    notes = [69, 64]  # alternate A4/E4 so dropped notes are easy to hear

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    trackers = {}
    try:
        hs.init(chips=(args.chip,))
        ch = midi.ChipChannel(hs, args.chip)  # pushes volume/filter globals
        del ch  # only needed for its constructor side effect

        modes = [("instant", run_mode_a), ("scheduled", run_mode_b)]
        if args.order == "ba":
            modes.reverse()

        for label, fn in modes:
            print(f"\n=== MODE '{label}' for {args.minutes} minutes ===")
            tracker = PointerTracker(hs, label)
            trackers[label] = tracker
            fn(hs, tracker, args, patch, notes,
               time.perf_counter() + args.minutes * 60)
            print(tracker.summary())
            time.sleep(1.0)
    finally:
        try:
            block = _pad(build_reg_stream(
                note_off_pairs(args.chip, args.voice, patch)))
            hs.h.bulkWrite(hs4u.EP_OUT, block, timeout=hs4u.TIMEOUT)
        except Exception:
            pass
        hs.close()

    print("\n=== RESULTS ===")
    for label in ("instant", "scheduled"):
        if label in trackers:
            print(trackers[label].summary())
    if len(trackers) == 2:
        a = trackers["instant"]
        b = trackers["scheduled"]
        a_rate = a.mismatches / max(a.writes, 1)
        b_rate = b.mismatches / max(b.writes, 1)
        if a.mismatches >= 3 and b_rate < a_rate / 3:
            print("\n  Scheduled (Acid64-shaped) traffic shows a "
                  "substantially lower mismatch rate - supports adopting "
                  "a look-ahead scheduling mode in midi.py.")
        elif b.mismatches >= 3 and a_rate < b_rate / 3:
            print("\n  Instant traffic is CLEANER than scheduled - "
                  "opposite of the hypothesis; the engine-idle theory "
                  "is wrong.")
        else:
            print("\n  No decisive difference at this sample size - run "
                  "longer before concluding anything.")


if __name__ == "__main__":
    main()
