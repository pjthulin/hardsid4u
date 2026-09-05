#!/usr/bin/env python3
"""
midi_stress_via_coremidi.py - drive the REAL midi.py, through a REAL
CoreMIDI connection, with the gesture that kills the unit.

WHY THIS AND NOT bend_flood_test.py
-----------------------------------
bend_flood_test.py pushes the same register traffic and cannot kill the
device: 64,026 writes, 32 MB, four minutes, zero USB errors, zero short
writes, ring never above one block. Yet midi.py driven from Ableton dies
in seconds under notes+pitch-bend. So whatever the trigger is, it is NOT
the register traffic on its own - it is something about how midi.py
*produces* that traffic. The differences are all in the process:

  * the writes come from CoreMIDI's callback thread, not the main thread
  * a coalescing thread and (optionally) a health-monitor thread share
    the same libusb handle behind _IO_LOCK
  * MIDI arrives in bursts with real jitter, not on a tidy timer
  * messages queue up whenever a lock is held, then flush all at once

This script reproduces all of that by launching midi.py as a subprocess,
connecting to its virtual destination like a DAW would, and playing. Its
stdout is midi.py's own - bus faults, panics and recoveries appear inline.

LISTEN while it runs. The bus can look perfectly healthy while the audio
is not; that gap is the whole reason this investigation took so long.

Usage
-----
    # the failing gesture: held notes + a hard wheel sweep
    uv run --extra midi python3 tools/midi_stress_via_coremidi.py

    # A/B the suspected trigger: same run with bend disabled
    uv run --extra midi python3 tools/midi_stress_via_coremidi.py \
        --bend-rate 0

    # reinstate the old 2-second-lock health probe to test that theory
    uv run --extra midi python3 tools/midi_stress_via_coremidi.py \
        --health-interval 45
"""
import argparse
import math
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# midi.py lives in the package now, not beside this tool.
MIDI_PY = os.path.join(HERE, "..", "src", "hardsid4u", "midi.py")


def pump(proc, sink):
    for line in proc.stdout:
        sink.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--bend-rate", type=float, default=250.0,
                     help="pitch bend messages/second (0 = no bend, the "
                          "control arm of the A/B)")
    ap.add_argument("--note-rate", type=float, default=3.0)
    ap.add_argument("--chips", default="1,2,3,4",
                     help="passed straight through to midi.py")
    ap.add_argument("--health-interval", type=float, default=0.0,
                     help="passed through to midi.py (default 0 = off)")
    ap.add_argument("--port-name", default="HS4U-stress")
    args = ap.parse_args()

    try:
        import rtmidi
    except ImportError:
        print("python-rtmidi is required: uv sync --extra midi")
        sys.exit(1)

    cmd = [sys.executable, MIDI_PY,
           "--port-name", args.port_name,
           "--chips", args.chips,
           "--health-interval", str(args.health_interval)]
    print(f"launching: {' '.join(cmd)}\n")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, bufsize=1)
    lines = []
    threading.Thread(target=pump, args=(proc, lines), daemon=True).start()

    # Wait for the virtual destination to appear.
    out = None
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            print("\nmidi.py exited before opening its port.")
            sys.exit(1)
        probe = rtmidi.MidiOut()
        for i, name in enumerate(probe.get_ports()):
            if args.port_name in name:
                probe.open_port(i)
                out = probe
                break
        if out:
            break
        del probe
        time.sleep(0.3)
    if out is None:
        print(f"\ncould not find a MIDI destination named {args.port_name!r}")
        proc.terminate()
        sys.exit(1)

    channels = [int(c) - 1 for c in args.chips.split(",")]
    notes = [45, 52, 57, 60, 64, 67, 69, 72]
    print(f"\n=== connected. channels={[c + 1 for c in channels]} "
          f"notes={args.note_rate}/s bend={args.bend_rate}/s "
          f"for {args.minutes} min ===")
    print("=== LISTEN: ghost notes, hanging notes, silence ===\n")

    t_start = time.perf_counter()
    t_end = t_start + args.minutes * 60
    note_period = 1.0 / args.note_rate
    bend_period = 1.0 / args.bend_rate if args.bend_rate > 0 else 1e9
    next_note = t_start
    next_bend = t_start
    next_report = t_start + 30.0
    held = {c: [] for c in channels}
    sent_notes = sent_bends = 0
    i = 0
    try:
        while time.perf_counter() < t_end and proc.poll() is None:
            now = time.perf_counter()

            if now >= next_note:
                next_note += note_period
                for c in channels:
                    if len(held[c]) >= 3:
                        n = held[c].pop(0)
                        out.send_message([0x80 | c, n, 0])
                        sent_notes += 1
                    n = notes[i % len(notes)]
                    out.send_message([0x90 | c, n, 100])
                    held[c].append(n)
                    sent_notes += 1
                i += 1

            if now >= next_bend:
                next_bend += bend_period
                phase = math.sin((now - t_start) * 2 * math.pi * 1.5)
                v = int(8192 + phase * 8000)
                for c in channels:
                    out.send_message([0xE0 | c, v & 0x7F, (v >> 7) & 0x7F])
                    sent_bends += 1

            if now >= next_report:
                next_report += 30.0
                el = now - t_start
                print(f"  [stress {el:5.0f}s] notes={sent_notes} "
                      f"bends={sent_bends} "
                      f"({sent_bends / max(el, 1):.0f}/s)")

            slack = min(next_note, next_bend) - time.perf_counter()
            if slack > 0.0005:
                time.sleep(slack)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        for c in channels:
            for n in held[c]:
                try:
                    out.send_message([0x80 | c, n, 0])
                except Exception:
                    pass
        time.sleep(0.5)
        out.close_port()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    faults = [ln for ln in lines if "BUS FAULT" in ln or "WARNING" in ln
              or "midi error" in ln or "recover" in ln]
    print(f"\n=== RESULT after {time.perf_counter() - t_start:.0f}s ===")
    print(f"  MIDI sent : {sent_notes} notes, {sent_bends} bends")
    print(f"  midi.py reported {len(faults)} fault/recovery line(s)"
          f"{':' if faults else ' - none'}")
    for ln in faults[:40]:
        print("   ", ln.rstrip())


if __name__ == "__main__":
    main()
