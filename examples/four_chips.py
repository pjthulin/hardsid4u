#!/usr/bin/env python3
"""One note on each of the four sockets in turn, to check every SID.

    uv run --with libusb1 examples/four_chips.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "hardsid4u"))
from hs4u import VERSION, HardSID4U, PAL_CLOCK, TRIANGLE          # noqa: E402

print(f"hs4u v{VERSION}")

print(f"hs4u v{VERSION}")

with HardSID4U() as hs:
    hs.init()
    for chip in range(4):
        print(f"socket {chip}")
        hs.volume(chip, 15)
        hs.voice(chip, 0, waveform=TRIANGLE, attack=0, decay=9,
                 sustain=15, release=9)
        hs.note_on(chip, 0, 220 * (chip + 1))
        hs.delay(PAL_CLOCK)
        hs.note_off(chip, 0)
        hs.delay(PAL_CLOCK // 2)
        hs.volume(chip, 0)
        hs.flush()
        hs.drain()
