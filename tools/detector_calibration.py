#!/usr/bin/env python3
"""
detector_calibration.py - does engine_executing() measure what it claims?

Everything now rests on this detector, and it has never been calibrated.
That is exactly the mistake this project has made twice already: Phase 4's
"engine start" that set a status bit while the sockets stayed dead, and
the endurance runs that watched USB errors while the real failure sailed
past. A green instrument you designed yourself is not evidence.

The claim is that measure_delay_rate() times how long the device takes to
EXECUTE a delta-delay. If that is true, the measured time must grow with
the delay. There is already a bad sign that it does not:

    20000 cycles (20ms nominal)   -> 108ms measured
    200000 cycles (203ms nominal) -> 107ms measured

Identical. A ten-fold change in the delay produced no change at all, which
means the number is dominated by a fixed cost and is NOT a measure of
delay execution. It still separates healthy (107ms) from failed (4ms) by
25x, so it detects SOMETHING real - but what that something is has never
been established, and "the engine stopped executing" is an interpretation,
not an observation.

This sweeps the delay across two decades on a healthy device and reports
whether the measurement tracks it. Run it right after a power cycle.

    flat            -> the detector does not measure delay execution.
                       Whatever it distinguishes, the name and the story
                       around it are wrong and need rewriting.
    proportional    -> the detector is sound and the stall conclusions
                       stand.

Also sweeps how long the ring takes to FULLY drain (free back to maximum),
which is a much less ambiguous quantity, as a candidate replacement.

Usage
-----
    uv run python3 tools/detector_calibration.py
"""
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


def full_drain_time(hs, cycles, limit=6.0):
    """Time until the ring is genuinely empty again, not 'all but one block'.

    Uses the same test hs4u.drain() uses (free >= RING - BLOCK), which has
    an unambiguous meaning: the device has consumed everything we wrote.
    """
    payload = hs4u.pad_even(hs4u.encode_delay(cycles))
    hs._wait_room(hs4u.BLOCK * 2)
    while hs.state()[3] < hs4u.RING - hs4u.BLOCK:   # start from rest
        if not _settle(hs, 2.0):
            break
    t0 = time.perf_counter()
    hs.h.bulkWrite(hs4u.EP_OUT, payload, timeout=hs4u.TIMEOUT)
    while time.perf_counter() - t0 < limit:
        if hs.state()[3] >= hs4u.RING - hs4u.BLOCK:
            return time.perf_counter() - t0
        time.sleep(0.002)
    return None


def _settle(hs, limit):
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < limit:
        if hs.state()[3] >= hs4u.RING - hs4u.BLOCK:
            return True
        time.sleep(0.005)
    return False


def main():
    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    try:
        hs.init(chips=(0,))
        print("\ncycles      nominal   measure_delay_rate   full_drain")
        print("-" * 58)
        rows = []
        for cycles in (10000, 20000, 50000, 100000, 200000, 400000, 985248):
            nominal = cycles / hs4u.PAL_CLOCK
            _settle(hs, 3.0)
            r = hs4u.measure_delay_rate(hs, cycles=cycles, limit=6.0)
            m = r["measured_s"]
            _settle(hs, 3.0)
            d = full_drain_time(hs, cycles)
            rows.append((cycles, nominal, m, d))
            print(f"{cycles:8d}   {nominal * 1000:7.0f}ms   "
                  f"{'timeout' if m is None else f'{m * 1000:9.0f}ms':>18}   "
                  f"{'timeout' if d is None else f'{d * 1000:7.0f}ms'}")

        print()
        got = [(c, m, d) for c, n, m, d in rows if m is not None]
        if len(got) >= 2:
            lo, hi = got[0], got[-1]
            span = hi[0] / lo[0]
            mr = (hi[1] / lo[1]) if lo[1] else 0
            print(f"  delay increased {span:.0f}x across the sweep")
            print(f"  measure_delay_rate changed {mr:.2f}x")
            if mr < 2:
                print("  => FLAT. It does not measure delay execution.")
                print("     It detects something real (107ms vs 4ms) but the")
                print("     'engine stopped executing' story is unsupported;")
                print("     rewrite it around whatever full_drain shows.")
            else:
                print("  => tracks the delay; the detector is sound.")
        dr = [(c, d) for c, n, m, d in rows if d is not None]
        if len(dr) >= 2:
            span = dr[-1][0] / dr[0][0]
            ratio = (dr[-1][1] / dr[0][1]) if dr[0][1] else 0
            print(f"  full_drain changed {ratio:.2f}x over a {span:.0f}x "
                  f"delay increase"
                  f"{'  <- USE THIS ONE' if ratio >= 2 else ''}")
    finally:
        hs.close()


if __name__ == "__main__":
    main()
