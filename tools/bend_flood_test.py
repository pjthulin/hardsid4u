#!/usr/bin/env python3
"""
bend_flood_test.py - reproduce the "play notes + move the pitch wheel =>
dead unit" failure without a DAW, with full instrumentation.

WHY THIS SHAPE
--------------
Pitch bend is the only MIDI gesture that turns a handful of writes per
second into hundreds: midi.py's ChipChannel.pitch_bend() issues one full
512-byte block per bend message, and a keyboard wheel emits 100-400 of
them per second. The user reports this triggers the failure within
seconds, every time, where ordinary playing takes 35-115s. That makes
"USB writes per second" the prime suspect variable - and this script
sweeps it.

Two hypotheses this is built to separate:

  H1 "flow control" - the ring fills and midi.py writes into it anyway
     (write_regs_now gives up after 50ms and sends regardless; and
     used=(wr-rd)&0x1FFF cannot represent a full ring, so 8192-full reads
     back as free=8192, i.e. an overrun is INVISIBLE to our own check).
     Signature: `used` climbing above one block before the failure.

  H2 "stream misalignment" - a short/failed bulkWrite leaves the device's
     512-byte word stream off by an odd number of bytes. From then on our
     DATA bytes are read as COMMAND bytes. That predicts exactly what was
     observed live and cannot easily be explained otherwise:
       * ghost notes on chips 3/4 while only chips 1/2 were being played
         (a command byte with bits 5-6 set can only come from a byte we
         only ever send as data),
       * hanging notes (stray gate-on writes with no matching gate-off),
       * permanent silence curable only by a power cycle (a stray write
         to register 0x1D/0x1E/0x1F DE-ARMS the socket - see
         docs/protocol.md section 17 - after which every register write is
         silently discarded).
     Signature: an exception or short write logged at the moment of
     failure, and/or `used` growing because the device is now executing
     garbage delay commands.

Every bulkWrite is wrapped so nothing can fail silently - note that
midi.py's own MidiToSid.__call__ swallows exceptions with a one-line
"[midi error]" print and keeps going, which is precisely how a
stream-corrupting partial write would go unnoticed mid-performance.

Listen to the analog output while this runs: report ghost notes, hanging
notes or silence, and the script's own log will say what the bus was
doing at that moment.

Usage
-----
    uv run python3 tools/bend_flood_test.py
    uv run python3 tools/bend_flood_test.py --bend-rate 400 --minutes 5
    uv run python3 tools/bend_flood_test.py --chips 0,1
"""
import argparse
import math
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


class InstrumentedBus:
    """Wraps the device so no write can fail or short-write unnoticed."""

    def __init__(self, hs):
        self.hs = hs
        self.writes = 0
        self.bytes_out = 0
        self.errors = 0
        self.short_writes = 0
        self.mismatches = 0
        self.max_used = 0
        self.first_fault = None
        rd, wr, st, free = hs.state()
        self.expected_wr = wr

    def _note_fault(self, kind, detail):
        if self.first_fault is None:
            self.first_fault = (self.writes, time.perf_counter(), kind, detail)
        print(f"  *** {kind} at write #{self.writes}: {detail}")

    def write(self, block):
        """Send one 512-byte-multiple block, checking everything."""
        predicted = self.expected_wr
        for _ in range(len(block) // hs4u.BLOCK):
            predicted = ((predicted - 0x2000 + hs4u.BLOCK) % hs4u.RING) + 0x2000
        try:
            sent = self.hs.h.bulkWrite(hs4u.EP_OUT, block, timeout=hs4u.TIMEOUT)
        except Exception as e:
            self.errors += 1
            # A failed bulk transfer may still have put SOME bytes on the
            # wire. If that count is not a multiple of 2, the device's
            # 16-bit word stream is now permanently off by one byte and
            # every subsequent data byte is executed as a command.
            self._note_fault("USB ERROR", f"{type(e).__name__}: {e}")
            return
        self.writes += 1
        self.bytes_out += sent
        if sent != len(block):
            self.short_writes += 1
            self._note_fault("SHORT WRITE",
                             f"sent {sent}/{len(block)} bytes "
                             f"(parity {'ODD - stream misaligned' if sent % 2 else 'even'})")
        rd, wr, st, free = self.hs.state()
        used = hs4u.RING - free
        self.max_used = max(self.max_used, used)
        if wr != predicted:
            self.mismatches += 1
            if self.mismatches <= 20:
                print(f"  [ptr mismatch #{self.mismatches}] write #{self.writes} "
                      f"predicted={predicted:#06x} actual={wr:#06x} "
                      f"diff={wr - predicted:+d} used={used}")
        self.expected_wr = wr
        return rd, wr, st, free


def build_and_send(bus, pairs, backoff_budget=0.05):
    """midi.py's write_regs_now, but instrumented and honest about room."""
    if not pairs:
        return None
    block = midi._build_block(pairs)
    t0 = time.perf_counter()
    starved = False
    while bus.hs.state()[3] < len(block):
        if time.perf_counter() - t0 > backoff_budget:
            starved = True
            break
        time.sleep(0.001)
    if starved:
        bus._note_fault("RING STARVED",
                        "50ms backoff budget exhausted - midi.py would now "
                        "write into a ring with no room")
    return bus.write(block)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chips", default="0",
                     help="comma-separated chip indices 0-3 to actually PLAY "
                          "(default 0)")
    ap.add_argument("--arm", default=None,
                     help="comma-separated chip indices to ARM, if different "
                          "from --chips. THE DECISIVE TEST for H2 is "
                          "'--arm 0,1,2,3 --chips 0': every socket listening, "
                          "but not one byte deliberately addressed to 1-3. A "
                          "de-armed socket discards register writes silently "
                          "(protocol.md 17), so with only socket 0 armed a "
                          "misaligned stream CANNOT produce audible ghosts - "
                          "the absence of them proves nothing. Arm all four "
                          "and any sound from sockets 2-4 is proof: their "
                          "command bytes have bits 5-6 set, which this "
                          "program only ever emits as DATA.")
    ap.add_argument("--bend-rate", type=float, default=300.0,
                     help="pitch bend messages per second per chip "
                          "(default 300 - a hard wheel sweep)")
    ap.add_argument("--note-rate", type=float, default=2.0,
                     help="notes per second (default 2)")
    ap.add_argument("--minutes", type=float, default=5.0)
    ap.add_argument("--bend-range", type=float, default=2.0)
    ap.add_argument("--stop-on-fault", action="store_true",
                     help="stop the moment anything anomalous is logged, so "
                          "the device is left in the failed state for "
                          "inspection instead of being hammered further")
    args = ap.parse_args()

    chips = tuple(int(x) for x in args.chips.split(","))
    armed = tuple(int(x) for x in args.arm.split(",")) if args.arm else chips
    if not set(chips) <= set(armed):
        print("--chips must be a subset of --arm")
        sys.exit(1)
    print(f"hs4u.py v{hs4u.VERSION}")
    print(f"armed={list(armed)}  played={list(chips)}  "
          f"bend={args.bend_rate:.0f}/s  notes={args.note_rate:.0f}/s  "
          f"for {args.minutes:.0f} min")
    if set(armed) != set(chips):
        silent = sorted(set(armed) - set(chips))
        print(f"LISTEN ESPECIALLY to socket(s) {[c + 1 for c in silent]} - "
              f"this program addresses ZERO bytes to them.")
        print("Any sound at all from them is proof of stream misalignment.")
    print("LISTEN: report ghost notes, hanging notes, or silence.\n")

    hs = hs4u.HardSID4U(verbose=False)
    hs.open()
    try:
        hs.init(chips=armed)
        # Every ARMED socket needs its master volume (0x18) pushed once, or
        # a ghost note on it would be inaudible and the test would come
        # back falsely negative: hs.init() deliberately never writes 0x18,
        # so an armed-but-untouched socket sits at volume 0. This is the
        # only traffic the non-played sockets ever receive - four register
        # writes, before the flood starts, all with command bytes we emit
        # deliberately.
        for c in armed:
            if c not in chips:
                midi.ChipChannel(hs, c, bend_range=args.bend_range)
        channels = []
        for c in chips:
            ch = midi.ChipChannel(hs, c, bend_range=args.bend_range)
            ch.patch["waveform"] = hs4u.PULSE
            ch.patch["attack"] = 0
            ch.patch["decay"] = 6
            ch.patch["sustain"] = 12
            ch.patch["release"] = 4
            channels.append(ch)
        bus = InstrumentedBus(hs)

        # Route every ChipChannel write through the instrumented bus by
        # swapping out the module-level writer the channels call.
        midi.write_regs_now = lambda _hs, pairs, backoff_budget=0.05: \
            build_and_send(bus, pairs, backoff_budget)

        notes = [57, 60, 64, 67, 69, 72]
        t_start = time.perf_counter()
        t_end = t_start + args.minutes * 60
        bend_period = 1.0 / args.bend_rate
        note_period = 1.0 / args.note_rate
        next_bend = t_start
        next_note = t_start
        next_report = t_start + 15.0
        held = [[] for _ in chips]
        i = 0
        while True:
            now = time.perf_counter()
            if now >= t_end:
                break
            if args.stop_on_fault and bus.first_fault is not None:
                print("\n  --stop-on-fault: halting with the device in the "
                      "failed state.")
                break

            if now >= next_note:
                next_note += note_period
                for k, ch in enumerate(channels):
                    if len(held[k]) >= 3:
                        ch.note_off(held[k].pop(0))
                    n = notes[i % len(notes)]
                    ch.note_on(n, 100)
                    held[k].append(n)
                i += 1

            if now >= next_bend:
                next_bend += bend_period
                # a continuous wheel sweep, ~1.5 Hz, full range
                phase = math.sin((now - t_start) * 2 * math.pi * 1.5)
                value14 = int(8192 + phase * 8000)
                for ch in channels:
                    ch.pitch_bend(value14)

            if now >= next_report:
                next_report += 15.0
                rd, wr, st, free = hs.state()
                el = now - t_start
                print(f"  [{el:6.1f}s] writes={bus.writes:7d} "
                      f"({bus.writes / el:5.0f}/s) bytes={bus.bytes_out} "
                      f"errors={bus.errors} short={bus.short_writes} "
                      f"ptr_mismatch={bus.mismatches} max_used={bus.max_used} "
                      f"free={free} state={st:#06x}")

            slack = min(next_bend, next_note) - time.perf_counter()
            if slack > 0.0005:
                time.sleep(slack)

        el = time.perf_counter() - t_start
        print(f"\n=== RESULT after {el:.1f}s ===")
        print(f"  writes          : {bus.writes} ({bus.writes / el:.0f}/s)")
        print(f"  bytes out       : {bus.bytes_out} "
              f"(multiple of 512: {bus.bytes_out % 512 == 0})")
        print(f"  USB errors      : {bus.errors}")
        print(f"  short writes    : {bus.short_writes}")
        print(f"  ptr mismatches  : {bus.mismatches} "
              f"({bus.mismatches / max(bus.writes, 1) * 100:.4f}%)")
        print(f"  max ring used   : {bus.max_used} of {hs4u.RING} "
              f"(one block = {hs4u.BLOCK})")
        if bus.first_fault:
            n, t, kind, detail = bus.first_fault
            print(f"  FIRST FAULT     : {kind} at write #{n}, "
                  f"t={t - t_start:.1f}s - {detail}")
        else:
            print("  FIRST FAULT     : none - the bus stayed clean")
        if bus.max_used <= hs4u.BLOCK and bus.errors == 0 and bus.short_writes == 0:
            print("\n  No HOST-SIDE evidence of overrun or a failed transfer.\n"
                  "  That is weaker than it looks: 'sent == 512' only means\n"
                  "  the host stack believes it sent 512 bytes, and the +512\n"
                  "  pointer anomaly is standing proof that the DEVICE's own\n"
                  "  byte count can disagree with the host's. A device-side\n"
                  "  miscount produces zero libusb errors by construction.\n"
                  "  The canaries that matter are max_used climbing and what\n"
                  "  you HEARD - especially on any socket this run never\n"
                  "  addressed.")
    finally:
        try:
            for c in armed:
                pairs = [(c, r, 0) for r in range(0x19)]
                hs.h.bulkWrite(hs4u.EP_OUT, midi._build_block(pairs),
                               timeout=hs4u.TIMEOUT)
        except Exception:
            pass
        hs.close()


if __name__ == "__main__":
    main()
