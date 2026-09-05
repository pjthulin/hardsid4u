#!/usr/bin/env python3
"""
stuck_note_diagnosis.py - fast, tight reproduction + in-place recovery
investigation for the "note gets stuck immediately after arming" finding.

HISTORY - read before trusting old results from this script: its first
two runs appeared to show a catastrophic "silence after note 1, immune to
every software recovery, only a power cycle helps" failure. That
conclusion was an artifact of this script's own bug: the ATTEMPT-2
zero-sweep wrote 0x18 (master volume) = 0 and nothing afterwards restored
it (hs.init() deliberately never touches 0x18; note_on() doesn't either),
so the final confirmation note was guaranteed silent on perfectly healthy
hardware. Fixed by restoring chip globals after the sweep and after the
re-arm. The related live "stuck note" detections in
pointer_audio_correlation_test.py were also suspect (uncompensated audio
input latency). Whether any real stuck-note failure mode exists at all is
an OPEN question this script - now fixed - exists to answer.

This fires one note (~1.5s), then tries several in-place recovery actions
in sequence - repeating the same note-off, a full chip zero-sweep, a full
re-arm - printing device state at every step, ending with a fresh note to
confirm normal playback. Meant to be run interactively while listening to
the analog output; the script prints clearly when to listen. On healthy
hardware EVERY "listen" point after step 0 should be silent except the
final note, which should sound cleanly.

Usage
-----
    uv run python3 tools/stuck_note_diagnosis.py --chip 0
    uv run python3 tools/stuck_note_diagnosis.py --chip 0 --voice 1
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


def show_state(hs, label):
    rd, wr, st, free = hs.state()
    print(f"    [{label}] rd={rd:#06x} wr={wr:#06x} state={st:#06x} "
          f"free={free}  running={bool(st & 0x80)}")
    return rd, wr, st, free


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chip", type=int, default=0,
                     help="0=ch1/C1, 1=ch2/C2, 2=ch3/C3, 3=ch4/C4")
    ap.add_argument("--voice", type=int, default=0, choices=(0, 1, 2))
    ap.add_argument("--hz", type=float, default=440.0)
    ap.add_argument("--hold", type=float, default=0.15)
    ap.add_argument("--wait", type=float, default=1.5,
                     help="seconds to wait+listen after each step")
    args = ap.parse_args()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    ch = None
    try:
        print(f"\n=== ARM chip {args.chip} ===")
        hs.init(chips=(args.chip,))
        show_state(hs, "after init")

        ch = midi.ChipChannel(hs, args.chip)
        show_state(hs, "after ChipChannel (volume/filter defaults)")

        ch.patch["waveform"] = hs4u.PULSE
        ch.patch["attack"] = 0
        ch.patch["decay"] = 2
        ch.patch["sustain"] = 15
        ch.patch["release"] = 0

        # Two distinct pitches so the first note (A4=69) and the final
        # confirmation note (A5=81, an octave up) are unmistakably
        # different by ear - on healthy hardware you hear a LOW note, a
        # gap of silence, then a HIGH note.
        note = 69       # A4 - the step-0 note
        final_note = 81  # A5 - the final confirmation note
        print(f"\n=== STEP 0: fire note-on (chip {args.chip}, voice "
              f"{args.voice}), LOW note A4, hold {args.hold}s, then "
              f"note-off ===")
        ch.voices[args.voice].note = note  # ensure note_on targets this voice
        ch.note_on(note, 100)
        show_state(hs, "after note-on")
        time.sleep(args.hold)
        ch.note_off(note)
        show_state(hs, "after note-off")

        print(f"\n>>> LISTEN NOW for {args.wait}s - is it stuck on? <<<")
        time.sleep(args.wait)
        show_state(hs, "after wait")

        print("\n=== ATTEMPT 1: repeat the same note-off ===")
        ch.note_off(note)
        show_state(hs, "after repeat note-off")
        print(f">>> LISTEN NOW for {args.wait}s - did that fix it? <<<")
        time.sleep(args.wait)
        show_state(hs, "after wait")

        print("\n=== ATTEMPT 2: full chip zero-sweep (all registers "
              "0x00-0x18, bypassing note_off's targeted single write) ===")
        pairs = [(args.chip, r, 0) for r in range(0x19)]
        block = midi._build_block(pairs)
        hs.h.bulkWrite(hs4u.EP_OUT, block, timeout=hs4u.TIMEOUT)
        show_state(hs, "after zero-sweep")
        # The sweep just wrote 0x18 (master volume) = 0. Restore the chip
        # globals IMMEDIATELY - otherwise everything after this point,
        # including the final confirmation note, is guaranteed silent by
        # our own write, and the script "proves" an unrecoverable failure
        # that never existed. That exact omission invalidated this
        # script's first two runs: hs.init() deliberately never touches
        # 0x18 and note_on() doesn't either, so nothing downstream saves
        # you. midi.py's hard_panic() gets this right; mirror it.
        ch._write_chip_globals()
        show_state(hs, "after globals restore")
        print(f">>> LISTEN NOW for {args.wait}s - did that fix it? <<<")
        time.sleep(args.wait)
        show_state(hs, "after wait")

        print("\n=== ATTEMPT 3: full re-arm (hs.init() again, no power "
              "cycle) ===")
        hs.init(chips=(args.chip,))
        show_state(hs, "after re-arm")
        ch._write_chip_globals()  # init's probe sequence writes 0x15-0x17
                                   # and never restores 0x18 - same trap
        show_state(hs, "after globals restore")
        print(f">>> LISTEN NOW for {args.wait}s - did that fix it? <<<")
        time.sleep(args.wait)
        show_state(hs, "after wait")

        print("\n=== FINAL: fire a fresh note-on/off (HIGH note A5, an "
              "octave above step 0) to confirm normal playback works now "
              "===")
        ch.voices[args.voice].note = final_note
        ch.note_on(final_note, 100)
        time.sleep(args.hold)
        ch.note_off(final_note)
        show_state(hs, "after final note")
        print(f">>> LISTEN NOW for {args.wait}s - clean HIGH note, then "
              f"silence? <<<")
        time.sleep(args.wait)
        show_state(hs, "after wait")

        print("\n=== done - report what you heard at each LISTEN NOW "
              "point, in order ===")
    finally:
        if ch is not None:
            try:
                ch.all_notes_off()
            except Exception:
                pass
        hs.close()


if __name__ == "__main__":
    main()
