#!/usr/bin/env python3
"""
pointer_slip_test.py - characterize a ring-pointer anomaly: does the
device's own reported wr occasionally differ from what deterministic
byte-counting predicts, and if so, is it specific to the wrap boundary
(0x3E00 -> 0x2000) or can it happen at any block transition?

Found twice now during wrap_boundary_test.py runs: after sending a
computed number of pure-filler 512-byte blocks, the reported wr landed
exactly one block (0x200 bytes) further than arithmetic predicts - both
times while crossing the wrap boundary. That script treated this as fatal
(it needs an exact ring address to test against). This one doesn't - it
sends a continuous stream of filler blocks (no notes, no audio needed,
purely a USB/ring-pointer observation), tracks predicted vs actual wr
after EVERY single write, logs every mismatch with full context, then
resyncs and keeps going - so a rare event doesn't end the run, and we can
gather real statistics on how often and where this happens.

No audio interface needed for this one - it's a pure USB-level
observation, nothing needs to be heard.

Usage
-----
    uv run python3 tools/pointer_slip_test.py --minutes 10
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


def advance(wr, n=hs4u.BLOCK):
    """Where wr should land after advancing by n bytes, with wraparound
    at the ring's 0x2000-byte boundary (base address 0x2000)."""
    return ((wr - 0x2000 + n) % hs4u.RING) + 0x2000


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--settle-delay", type=float, default=0.0,
                     help="seconds to sleep after each write before "
                          "reading status back (default 0 - no delay, "
                          "maximum stress on any status-lag race)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()

    filler_block = hs4u.FILLER * (hs4u.BLOCK // 2)
    rd, wr, st, free = hs.state()
    expected_wr = wr
    print(f"[start] wr={wr:#06x} rd={rd:#06x} state={st:#06x}")

    n = 0
    mismatches = 0
    wrap_mismatches = 0
    nonwrap_mismatches = 0
    wrap_writes = 0
    t_start_run = time.perf_counter()
    t_end = t_start_run + args.minutes * 60

    try:
        while time.perf_counter() < t_end:
            n += 1
            t0 = time.perf_counter()
            while hs.state()[3] < len(filler_block):
                if time.perf_counter() - t0 > 0.5:
                    break
                time.sleep(0.001)

            crosses_wrap = (expected_wr + hs4u.BLOCK) >= 0x4000
            if crosses_wrap:
                wrap_writes += 1
            predicted_wr = advance(expected_wr)

            hs.h.bulkWrite(hs4u.EP_OUT, filler_block, timeout=hs4u.TIMEOUT)
            if args.settle_delay:
                time.sleep(args.settle_delay)
            rd, wr, st, free = hs.state()

            if wr != predicted_wr:
                mismatches += 1
                if crosses_wrap:
                    wrap_mismatches += 1
                else:
                    nonwrap_mismatches += 1
                print(f"  [MISMATCH #{mismatches}] write #{n}  "
                      f"predicted={predicted_wr:#06x} actual={wr:#06x}  "
                      f"diff={(wr - predicted_wr):+d}  "
                      f"{'WRAP' if crosses_wrap else 'non-wrap'}  "
                      f"rd={rd:#06x} state={st:#06x}  "
                      f"t={time.perf_counter() - t_start_run:.1f}s")
                expected_wr = wr  # resync so one mismatch doesn't cascade
            else:
                expected_wr = predicted_wr
                if args.verbose and n % 500 == 0:
                    print(f"  ...write #{n}, {mismatches} mismatches so "
                          f"far, t={time.perf_counter() - t_start_run:.1f}s")
    except KeyboardInterrupt:
        pass
    finally:
        hs.close()

    elapsed = time.perf_counter() - t_start_run
    print("\n=== SUMMARY ===")
    print(f"  total writes: {n}")
    print(f"  elapsed: {elapsed:.1f}s ({n / max(elapsed, 0.001):.1f} writes/s)")
    print(f"  mismatches: {mismatches} "
          f"({mismatches / max(n, 1) * 100:.4f}% of writes)")
    print(f"    at wrap boundary: {wrap_mismatches} / {wrap_writes} "
          f"wrap-crossing writes "
          f"({wrap_mismatches / max(wrap_writes, 1) * 100:.3f}%)")
    print(f"    NOT at wrap boundary: {nonwrap_mismatches} / "
          f"{n - wrap_writes} non-wrap writes "
          f"({nonwrap_mismatches / max(n - wrap_writes, 1) * 100:.4f}%)")
    if mismatches and wrap_writes:
        wrap_rate = wrap_mismatches / wrap_writes
        nonwrap_rate = nonwrap_mismatches / max(n - wrap_writes, 1)
        if wrap_rate > nonwrap_rate * 3:
            print("\n  Mismatches are heavily concentrated at the wrap "
                  "boundary - supports a wrap-specific device bug.")
        elif nonwrap_rate > wrap_rate * 3:
            print("\n  Mismatches are heavily concentrated AWAY from the "
                  "wrap boundary - this is not wrap-specific.")
        else:
            print("\n  Mismatch rate is similar at and away from the wrap "
                  "boundary - not specifically a wrap bug, something more "
                  "general (or too few samples yet).")


if __name__ == "__main__":
    main()
