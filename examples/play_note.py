#!/usr/bin/env python3
"""Simplest possible example: one note on socket 0.

    uv run --with libusb1 examples/play_note.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "hardsid4u"))
from hs4u import VERSION, HardSID4U, PAL_CLOCK, TRIANGLE          # noqa: E402

print(f"hs4u v{VERSION}")

with HardSID4U() as hs:
    hs.init()
    hs.volume(0, 15)
    hs.voice(0, 0, waveform=TRIANGLE, attack=0, decay=9, sustain=15, release=9)
    hs.note_on(0, 0, 440)
    hs.delay(PAL_CLOCK)
    hs.note_off(0, 0)
    hs.delay(PAL_CLOCK // 2)
    hs.flush()
    hs.drain()
