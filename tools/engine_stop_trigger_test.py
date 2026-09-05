#!/usr/bin/env python3
"""
engine_stop_trigger_test.py - can ordinary register traffic accidentally
send the engine STOP command?

THE FINDING THIS TESTS
----------------------
The engine start command is a 512-byte block beginning `ff ff 01 00`, and
it is a TOGGLE: sent to a running engine it STOPS it. A stopped engine
(state 0x0001) is then unrecoverable in software - not by register
writes, not by setConfiguration, not by further start blocks (measured:
six attempts, twelve seconds, no change), and not reliably by a USB
resetDevice() either. Only the front-panel switch. That matches the
user's symptom: "hanging notes, only a power cycle can fix".

Decomposed into the 16-bit words this protocol uses (data byte first,
then command byte - see hs4u.word()):

    ff ff   -> data 0xFF, command 0xFF  = FILLER
    01 00   -> data 0x01, command 0x00  = write chip 0, register 0x00
                                          (voice 0 frequency low) = 1

Both halves are things midi.py emits constantly, which raises the obvious
question this script exists to answer: every block midi.py builds is
padded to 512 bytes with FILLER, so every block ENDS in `ff ff`, and a
block beginning with a chip-0 voice-0 frequency-low write begins `xx 00`.
In the ring - a continuous byte stream where one block's tail abuts the
next block's head - that adjacency spells `... ff ff | 01 00 ...`. Is
that enough?

RESULT: **no**. The adjacency candidates below do NOT stop the engine (30
repeats each, three variants). Only the candidate with all four bytes
inside ONE 512-byte packet stops it. The trigger is packet-position-
based, not stream-adjacency-based, and since midi.py's block builder puts
filler only at the tail, no packet it emits can begin with `ff ff`.

So the hazard is real and the negatives are the valuable part of this
test: they refute the boundary theory before someone re-derives it. The
pitch-bend correlation the user reports remains UNEXPLAINED.

RECOVERY: a trial that stops the engine leaves the device needing a
POWER CYCLE. reset_engine() is attempted but has not been observed to
work. Do not run this casually - it will cost you a trip to the switch.

Usage
-----
    uv run python3 tools/engine_stop_trigger_test.py
    uv run python3 tools/engine_stop_trigger_test.py --repeats 5
"""
import argparse
import os
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


def pad(payload):
    return payload + hs4u.FILLER * (((-len(payload)) % hs4u.BLOCK) // 2)


def filler_block():
    return hs4u.FILLER * (hs4u.BLOCK // 2)


def block_starting_with(word_bytes):
    """A 512-byte block whose first word is exactly word_bytes."""
    return pad(word_bytes)


def trial(hs, label, blocks, repeats):
    """Send `blocks` back to back `repeats` times; did the engine stop?"""
    if not hs.running():
        print(f"  [{label}] engine already stopped before trial - resetting")
        hs.reset_engine()
    for n in range(1, repeats + 1):
        for b in blocks:
            assert len(b) == hs4u.BLOCK
            # Room first, so this test can never be accused of overrunning.
            t0 = time.time()
            while hs.state()[3] < 0x1000 and time.time() - t0 < 2.0:
                time.sleep(0.002)
            hs.h.bulkWrite(hs4u.EP_OUT, b, timeout=hs4u.TIMEOUT)
        time.sleep(0.05)
        rd, wr, st, free = hs.state()
        if not (st & 0x80):
            print(f"  [{label}] *** ENGINE STOPPED after {n} repeat(s) "
                  f"*** state={st:#06x} rd={rd:#06x} wr={wr:#06x}")
            print(f"  [{label}] recovering via reset_engine()...")
            ok = hs.reset_engine()
            print(f"  [{label}] recovered: running={ok}")
            hs.init(chips=(0,))
            return True
    print(f"  [{label}] no stop after {repeats} repeats (state=0x0081)")
    return False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repeats", type=int, default=40,
                     help="how many times to send each candidate (default 40)")
    args = ap.parse_args()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    results = {}
    try:
        hs.init(chips=(0,))
        rd, wr, st, free = hs.state()
        print(f"start state={st:#06x} running={bool(st & 0x80)}\n")

        # The exact wire bytes of the real start/stop block, for reference:
        #   ff ff 01 00 00 00 00 00 ...
        candidates = [
            # (label, [blocks sent back to back])
            ("A filler block then a block starting 'write c0 r0 = 0x01'",
             [filler_block(),
              block_starting_with(hs4u.encode_reg(0, 0x00, 0x01))]),

            ("A filler block then a block starting 'write c0 r0 = 0x40'",
             [filler_block(),
              block_starting_with(hs4u.encode_reg(0, 0x00, 0x40))]),

            ("A filler block then a block starting 'write c1 r0 = 0x01'",
             [filler_block(),
              block_starting_with(hs4u.encode_reg(1, 0x00, 0x01))]),

            # NB: pad() pads with FILLER, not zeros - so this candidate is
            # `ff ff 01 00` followed by 508 bytes of 0xff. It STOPS the
            # engine, which together with the negatives above pins the
            # trigger to the first four bytes of a single 512-byte packet.
            ("One block: filler word, then 'write c0 r0 = 0x01', then filler",
             [pad(hs4u.FILLER + hs4u.encode_reg(0, 0x00, 0x01))]),

            # The strongest form: the literal start block payload, but as a
            # ZERO-padded block, which is what the real start block is.
            ("One block: ff ff 01 00 + 508 zero bytes (the literal start "
             "block)",
             [b"\xff\xff\x01\x00" + b"\x00" * (hs4u.BLOCK - 4)]),
        ]

        for label, blocks in candidates:
            print(f"--- {label} ---")
            results[label] = trial(hs, label, blocks, args.repeats)
            time.sleep(0.3)
            print()

    finally:
        try:
            if not hs.running():
                print("leaving device stopped - resetting first")
                hs.reset_engine()
                hs.init(chips=(0,))
        except Exception as e:
            print(f"final recovery failed: {e}")
        hs.close()

    print("=== SUMMARY ===")
    for label, stopped in results.items():
        print(f"  {'STOPPED ' if stopped else 'no effect'}  {label}")
    adjacency = [v for k, v in results.items() if k.startswith("A filler block")]
    if any(adjacency):
        print("\n  A block-BOUNDARY adjacency stops the engine. midi.py can\n"
              "  produce that during normal play, so this is the failure and\n"
              "  the fix is to make the pattern impossible to emit.")
    else:
        print("\n  Block-boundary adjacency does NOT stop the engine - only\n"
              "  all four bytes inside one 512-byte packet does. The trigger\n"
              "  is packet-position-based, and midi.py's block builder puts\n"
              "  filler only at the tail, so no packet it sends can begin\n"
              "  with ff ff. The hazard is real; the route from midi.py to\n"
              "  it is NOT established. Look elsewhere for the live trigger.")


if __name__ == "__main__":
    main()
