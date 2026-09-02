#!/usr/bin/env python3
"""Three voices on one SID: an A minor chord, then an arpeggio.

    uv run --with libusb1 examples/chord.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "hardsid4u"))
from hs4u import HardSID4U, PAL_CLOCK, PULSE, SAWTOOTH   # noqa: E402

A, C, E = 440.0, 523.25, 659.25

with HardSID4U() as hs:
    hs.init()
    hs.volume(0, 15)
    for v in range(3):
        hs.voice(0, v, waveform=PULSE, attack=1, decay=8,
                 sustain=10, release=9, pulse_width=0x600)

    # held chord
    for v, hz in enumerate((A, C, E)):
        hs.note_on(0, v, hz)
    hs.delay(PAL_CLOCK * 2)
    for v in range(3):
        hs.note_off(0, v)
    hs.delay(PAL_CLOCK)

    # the C64 way of faking a chord: one voice, cycling fast
    hs.voice(0, 0, waveform=SAWTOOTH, attack=0, decay=6, sustain=12, release=6)
    for _ in range(24):
        for hz in (A, C, E):
            hs.note_on(0, 0, hz)
            hs.delay(PAL_CLOCK // 50)          # one PAL frame per step
    hs.note_off(0, 0)
    hs.delay(PAL_CLOCK)
    hs.flush()
    hs.drain()
