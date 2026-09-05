#!/usr/bin/env python3
"""
escape_prefix_test.py - is 0xFFFF an ESCAPE PREFIX that our padding has
been accidentally arming this whole time?

THE MISTAKE
-----------
docs/protocol.md section 4, "System-mode handshake", says plainly:

    0xFFFF is not only padding - it also acts as an ESCAPE PREFIX. The
    mode-set routine emits the word pair:
        FF FF        escape
        00 mm        mm = system mode
    then polls the status block until status[0x1E] == (mm | 0x80). ...
    the low nibble of status[0x1E] is the current mode and bit 7 is the
    acknowledge flag. This matters: the device must be put into SIDPLAY
    mode before register writes behave as documented above.

We wrote that down and then treated 0xFFFF as inert padding everywhere.

It is not inert, and the arithmetic is unforgiving. midi._build_block()
emits an ODD number of words before padding, for every possible number of
register pairs, so the filler run is always ODD too. An odd run means the
LAST `ff ff` has no `ff ff` to pair with - it is an unpaired escape, and
the word that follows it is the FIRST WORD OF THE NEXT BLOCK.

Which makes the next block's opening word an escape payload. Before the
leading-delay-word change that word was a register write, and for a pitch
bend it was

    (freq_lo, 0x00)   ->  command 0x00, data = frequency low byte

exactly the `00 mm` mode-set form, with mm taken from a frequency byte
that a wheel sweep walks through all 256 values of, many times a second.

That predicts everything the forensic dumps show: the device stops
honouring register writes (wrong system mode), the note hangs because no
gate-off can reach the chip, the engine keeps running and the ring keeps
draining because the ENGINE is fine, and nothing short of a power cycle
restores it.

It also re-reads two earlier results correctly. In
engine_stop_trigger_test.py the all-filler block was 256 filler words -
EVEN, every escape paired, no effect. The block that did stop the device
was `ff ff 01 00` + filler: escape, then payload `01 00` = mode 1, which
left state at 0x0001 = mode 1 with the acknowledge bit CLEAR. We read
that as "the engine stopped". It never stopped. It was waiting for an
acknowledgement of a mode-set we had no idea we had sent.

WHAT THIS SCRIPT DOES
---------------------
Purely software - no ears needed. status[0x1E] reports the mode, so the
hypothesis is directly observable:

  1. baseline state
  2. send a deliberate escape pair for a few mode values and watch the
     state change (or not)
  3. send an ODD-filler block followed by a block whose first word is a
     register write, and see whether that register write is swallowed as
     a mode-set - the accidental case, reproduced on purpose
  4. try to restore SIDPLAY mode

Usage
-----
    uv run python3 tools/escape_prefix_test.py
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

ESC = hs4u.FILLER  # ff ff


def state_of(hs):
    raw = hs.status()
    st = raw[0x1E] | (raw[0x1F] << 8)
    return st, st & 0x0F, bool(st & 0x80)


def show(hs, label):
    st, mode, ack = state_of(hs)
    rd, wr, _st, free = hs.state()
    print(f"    {label:<44s} state={st:#06x} mode={mode} ack={ack} "
          f"free={free}")
    return st


def send(hs, payload, pad_with=ESC):
    """Send exactly one 512-byte block built from payload + padding."""
    block = payload + pad_with * ((hs4u.BLOCK - len(payload)) // 2)
    assert len(block) == hs4u.BLOCK, len(block)
    t0 = time.time()
    while hs.state()[3] < 0x1000 and time.time() - t0 < 2.0:
        time.sleep(0.002)
    hs.h.bulkWrite(hs4u.EP_OUT, block, timeout=hs4u.TIMEOUT)
    time.sleep(0.05)
    return block


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chip", type=int, default=0)
    args = ap.parse_args()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    try:
        hs.init(chips=(args.chip,))
        base = show(hs, "baseline (after init)")

        print("\n=== TEST 1: a deliberate escape pair, mode 2 (VST) ===")
        print("    wire: ff ff | 02 00   (escape, then cmd 0x00 data 0x02)")
        send(hs, ESC + hs4u.word(0x00, 0x02))
        show(hs, "after escape+mode2")
        time.sleep(0.3)
        show(hs, "  again 300ms later")

        print("\n=== TEST 2: escape pair back to mode 1 (SIDPLAY) ===")
        send(hs, ESC + hs4u.word(0x00, 0x01))
        show(hs, "after escape+mode1")

        print("\n=== TEST 3: a nonsense mode value (0x0B) ===")
        send(hs, ESC + hs4u.word(0x00, 0x0B))
        show(hs, "after escape+mode0x0b")
        send(hs, ESC + hs4u.word(0x00, 0x01))
        show(hs, "restored to mode 1?")

        print("\n=== TEST 4: THE ACCIDENTAL CASE ===")
        print("    Block A: one register write, then ODD filler (so the")
        print("             final ff ff is an unpaired escape).")
        print("    Block B: opens with a register write whose data byte is")
        print("             0x02 - i.e. wire bytes `02 00`, which as an")
        print("             escape payload reads as mode-set 2.")
        print("    If mode changes, our padding has been arming an escape")
        print("    with the next block's first word all along.")
        a = send(hs, hs4u.encode_delay(8) + hs4u.encode_reg(args.chip, 0x00, 0x40))
        fill = sum(1 for i in range(0, len(a), 2)
                   if a[i:i + 2] == ESC and i >= 8)
        print(f"    (block A trailing filler words: {fill}, "
              f"{'ODD' if fill % 2 else 'EVEN'})")
        show(hs, "after block A")
        send(hs, hs4u.encode_reg(args.chip, 0x00, 0x02))
        show(hs, "after block B (first word = 02 00)")

        print("\n=== TEST 5: same, but block A padded to an EVEN filler run ===")
        print("    Every escape is paired, so block B's first word should be")
        print("    executed as the register write it actually is.")
        send(hs, ESC + hs4u.word(0x00, 0x01))   # make sure we start at mode 1
        show(hs, "reset to mode 1")
        a = send(hs, hs4u.encode_delay(8)
                 + hs4u.encode_reg(args.chip, 0x00, 0x40)
                 + hs4u.encode_delay(8))        # one extra word -> even filler
        fill = sum(1 for i in range(0, len(a), 2)
                   if a[i:i + 2] == ESC and i >= 12)
        print(f"    (block A trailing filler words: {fill}, "
              f"{'ODD' if fill % 2 else 'EVEN'})")
        send(hs, hs4u.encode_reg(args.chip, 0x00, 0x02))
        show(hs, "after block B (should be UNCHANGED)")

        print("\n=== RESTORE ===")
        send(hs, ESC + hs4u.word(0x00, 0x01))
        final = show(hs, "final")
        print(f"\n  baseline was {base:#06x}, final is {final:#06x}")
    finally:
        try:
            hs.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
