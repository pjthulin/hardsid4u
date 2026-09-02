#!/usr/bin/env python3
"""
hs4u.py — HardSID 4U over libusb. Reference implementation.

Every constant here is verified against hardware and against a USBPcap capture
of ACID64 Player Pro. See hardsid-usb-protocol-notes.md.

Protocol summary
----------------
Transport   BULK OUT 0x02 for commands, BULK IN 0x81 for a 64-byte status block.
            Writes must be a multiple of 512 bytes. The isochronous endpoints
            (0x83 / 0x04) are unused in this mode.

Word format Two bytes, DATA FIRST then COMMAND:
                cmd 0x00-0x7F   (chip << 5) | reg    register write
                cmd 0xEE        delay, low byte of cycle count
                cmd 0xEF        delay, high byte     (emitted BEFORE 0xEE)
                cmd 0xFF        filler, costs no time, pads to 512

Registers   0x00-0x18 are the SID's own registers.
            0x19-0x1F are HardSID DEVICE control registers, per socket.

Ring        8192 bytes of device address space, 0x2000-0x3FFF.
                rd = status[0x1A], wr = status[0x1C]
                used = (wr - rd) & 0x1FFF
                free = 0x2000 - used
            state = status[0x1E]; bit 7 set means the engine is running.

Init        Each socket must be armed before it will make a sound. The
            sequence writes ASCII 'S','I','D' to device registers 0x1D/0x1E/
            0x1F, then 'S','E','6', then three config values, then sets up
            0x19/0x1A/0x1F. Without it every register write is silently
            ignored - the SIDs stay dark.

Usage
-----
    with HardSID4U() as hs:
        hs.init()                       # arm all four sockets
        hs.reg(0, 0x18, 0x0F)           # volume
        hs.delay(8)
        hs.reg(0, 0x04, 0x11)           # gate on
        hs.delay(985248)                # one second, PAL
        hs.reg(0, 0x04, 0x10)           # gate off
        hs.flush()
"""
import os
import struct
import sys
import time

import usb1

VERSION = "1.1.0"

VID, PID = 0x6581, 0x8580
IFACE = 0
EP_IN, EP_OUT = 0x81, 0x02
BLOCK = 512
RING = 0x2000          # device ring buffer size, 0x2000-0x3FFF
TIMEOUT = 1000

PAL_CLOCK = 985248
NTSC_CLOCK = 1022730
PAL_FRAME = 19656          # cycles per single-speed frame
MIN_CYCLES = 8             # minimum gap between register writes

FILLER = b"\xff\xff"

# --- SID register map -------------------------------------------------------
# There are no "patches" on this device. It is a register-level pipe to four
# real SID chips: a patch is simply the set of register values you write.
# The 512-byte block is transport framing only and knows nothing about sound.
VOICE_BASE = (0x00, 0x07, 0x0E)        # voice 0, 1, 2

R_FREQ_LO, R_FREQ_HI = 0x00, 0x01      # + voice base
R_PW_LO, R_PW_HI = 0x02, 0x03          # + voice base; 12-bit, 0x800 = 50%
R_CONTROL = 0x04                       # + voice base
R_AD = 0x05                            # + voice base; attack << 4 | decay
R_SR = 0x06                            # + voice base; sustain << 4 | release

R_CUTOFF_LO, R_CUTOFF_HI = 0x15, 0x16  # global; 11-bit filter cutoff
R_RESON_FILT = 0x17                    # resonance << 4 | filter routing bits
R_MODE_VOL = 0x18                      # filter mode << 4 | volume

# Control register bits (R_CONTROL)
# NB: RING_MOD, not RING - RING is the ring-BUFFER size above, and naming
# this bit RING silently redefined it to 4 and broke all flow control.
GATE, SYNC, RING_MOD, TEST = 0x01, 0x02, 0x04, 0x08
TRIANGLE, SAWTOOTH, PULSE, NOISE = 0x10, 0x20, 0x40, 0x80

# Filter mode bits (high nibble of R_MODE_VOL)
FILT_LP, FILT_BP, FILT_HP, VOICE3_OFF = 0x10, 0x20, 0x40, 0x80

# Register sets used by the init, in the exact order the official software
# uses them. Order matters: reproduced verbatim from the capture.
_ZERO_REGS = [0x01, 0x00, 0x08, 0x07, 0x0F, 0x0E, 0x04, 0x05, 0x06,
              0x0B, 0x0C, 0x0D, 0x12, 0x13, 0x14]
_PROBE_REGS = [0x02, 0x03, 0x04, 0x05, 0x06, 0x09, 0x0A, 0x0B, 0x0C, 0x0D,
               0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x19]


def word(c, d):
    """One command word on the wire: data byte first, then command byte."""
    return bytes((d & 0xFF, c & 0xFF))


def encode_reg(chip, r, d):
    return word(((chip & 3) << 5) | (r & 0x1F), d)


def encode_delay(cycles):
    out = b""
    while cycles > 0:
        n = min(cycles, 0xFFFF)
        if n >= 0x100:
            out += word(0xEF, n >> 8)
        if n & 0xFF:
            out += word(0xEE, n & 0xFF)
        cycles -= n
    return out


def chip_init_stream(chip):
    """Arm one socket. Byte-for-byte identical to what ACID64 sends
    (verified exactly for sockets 2 and 3 in the reference capture)."""
    s = b""
    for r in _ZERO_REGS:
        s += encode_delay(8) + encode_reg(chip, r, 0x00)
    for r in _PROBE_REGS:
        s += encode_delay(8) + encode_reg(chip, r, 0xFF)
        s += encode_delay(8) + encode_reg(chip, r, 0x08)
    s += encode_delay(50) + encode_reg(chip, 0x1E, 0x00)
    for r in _PROBE_REGS:
        s += encode_delay(8) + encode_reg(chip, r, 0x00)
    s += encode_delay(40000) + encode_reg(0, 0x1E, 0x00)

    # 'S' 'I' 'D' - the unlock knock
    s += encode_delay(8) + encode_reg(chip, 0x1D, 0x53)
    s += encode_delay(8) + encode_reg(chip, 0x1E, 0x49)
    s += encode_delay(8) + encode_reg(chip, 0x1F, 0x44)
    s += encode_delay(1000) + encode_reg(chip, 0x1E, 0x00)

    # 'S' 'E' '6' then 'S' 'I' 'D' again
    s += encode_delay(8) + encode_reg(chip, 0x1D, 0x53)
    s += encode_delay(8) + encode_reg(chip, 0x1E, 0x45)
    s += encode_delay(8) + encode_reg(chip, 0x1F, 0x36)
    s += encode_delay(8) + encode_reg(chip, 0x1D, 0x53)
    s += encode_delay(8) + encode_reg(chip, 0x1E, 0x49)
    s += encode_delay(8) + encode_reg(chip, 0x1F, 0x44)
    s += encode_delay(1000) + encode_reg(chip, 0x1E, 0x00)

    for v in (0x8A, 0x92, 0xC0):
        s += encode_delay(8) + encode_reg(chip, 0x1F, v)
        s += encode_delay(8) + encode_reg(chip, 0x1E, 0x45)
        s += encode_delay(1000) + encode_reg(chip, 0x1E, 0x00)

    s += encode_delay(8) + encode_reg(chip, 0x1D, 0x00)
    s += encode_delay(8) + encode_reg(chip, 0x1E, 0x00)
    s += encode_delay(8) + encode_reg(chip, 0x1F, 0x00)
    s += encode_delay(20000) + encode_reg(chip, 0x1E, 0x00)

    s += encode_delay(8) + encode_reg(chip, 0x19, 0x80)
    s += encode_delay(8) + encode_reg(chip, 0x1A, 0x65)
    s += encode_delay(8) + encode_reg(chip, 0x1F, 0x40)
    s += encode_delay(8) + encode_reg(chip, 0x19, 0x00)
    s += encode_delay(8) + encode_reg(chip, 0x1A, 0x00)
    return s


assert RING == 0x2000, (
    "RING has been redefined. It is the device ring-buffer size. Do not name "
    "anything else RING - the SID ring-modulation bit is RING_MOD."
)


def _default_capture():
    """Locate the reference capture whether run from the repo root, from
    src/hardsid4u/, or with the package installed."""
    here = os.path.dirname(os.path.abspath(__file__))
    for rel in ("hs4u_capture_writes.bin",
                os.path.join("..", "..", "captures", "hs4u_capture_writes.bin"),
                os.path.join("captures", "hs4u_capture_writes.bin")):
        cand = os.path.normpath(os.path.join(here, rel))
        if os.path.exists(cand):
            return cand
    cand = os.path.join(os.getcwd(), "captures", "hs4u_capture_writes.bin")
    if os.path.exists(cand):
        return cand
    return "hs4u_capture_writes.bin"


class HardSID4U:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.ctx = None
        self.h = None
        self._buf = b""
        self._wave = {}

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self, set_config=True, hard_reset=False, start=True):
        self.ctx = usb1.USBContext()
        try:
            self.ctx.open()
        except (OSError, FileNotFoundError) as e:
            raise RuntimeError(
                "libusb-1.0 could not be loaded.\n"
                "  macOS:  brew install libusb\n"
                "  If Homebrew is in /usr/local (Intel Macs), the loader does\n"
                "  not search there by default:\n"
                '    export DYLD_LIBRARY_PATH="$(brew --prefix libusb)/lib:'
                '$DYLD_LIBRARY_PATH"\n'
                "  Linux:  install libusb-1.0-0 from your package manager\n"
                f"  Original error: {e}"
            ) from e
        self.h = self.ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if self.h is None:
            raise RuntimeError("HardSID 4U not found - powered on? switch to ON?")

        # SET_CONFIGURATION acts as a device RESET, and the device needs that
        # reset before its sockets can be armed. Issue it UNCONDITIONALLY.
        #
        # Do not guard it with `if getConfiguration() != 1` - after the first
        # run the configuration is already 1, the reset is skipped, the device
        # keeps stale state from the previous run and stays silent. That guard
        # was the source of a long run of intermittent, contradictory results.
        if hard_reset:
            try:
                self.h.resetDevice()
                time.sleep(0.2)
            except usb1.USBError as e:
                if self.verbose:
                    print(f"  resetDevice: {e} (continuing)")

        if set_config:
            try:
                self.h.setConfiguration(1)
                time.sleep(0.05)
            except usb1.USBError as e:
                if self.verbose:
                    print(f"  setConfiguration: {e} (continuing)")

        last = None
        for _ in range(3):
            try:
                self.h.claimInterface(IFACE)
                break
            except usb1.USBError as e:
                last = e
                try:
                    self.h.setConfiguration(1)
                except usb1.USBError:
                    pass
                time.sleep(0.1)
        else:
            raise RuntimeError(f"could not claim interface: {last}")

        # ACID64 never sends our engine-start packets. We invented that step
        # before understanding SET_CONFIGURATION, and the ff ff 01 00 short
        # packet was once seen to CLEAR state bit 7. It may be disarming the
        # device. start=False skips it entirely.
        if start:
            self.start_engine()
        else:
            rd, wr, st, free = self.state()
            print(f"  engine start SKIPPED (state={st:#06x} free={free})")
        return self

    def close(self):
        if self.h:
            try:
                self.h.releaseInterface(IFACE)
            except usb1.USBError:
                pass
            self.h.close()
            self.h = None
        if self.ctx:
            self.ctx.close()
            self.ctx = None

    # -- status ------------------------------------------------------------

    def status(self):
        return bytes(self.h.bulkRead(EP_IN, 64, timeout=TIMEOUT))

    def state(self):
        raw = self.status()
        rd, wr, st = struct.unpack_from("<HHH", raw, 0x1A)
        used = (wr - rd) & (RING - 1)
        return rd, wr, st, RING - used

    def running(self):
        return bool(self.state()[2] & 0x80)

    def start_engine(self, attempts=6, settle=2.0):
        """Start the device from cold.

        The start command is a full 512-BYTE BLOCK: ff ff 01 00 padded to 512
        with ZERO bytes (not 0xFF filler, not a short packet). A successful
        start also resets the ring pointers.

        CRITICAL: the start block is a TOGGLE. Sending it to a device that is
        already running STOPS it - state goes 0x0081 -> 0x0001 and stays
        there. So if bit 7 is set, send nothing at all, whatever the reported
        free space says. A v1.0.3 "improvement" that also required free space
        before returning early did exactly this and broke a working driver.
        """
        if self.running():
            return

        block = b"\xff\xff\x01\x00" + b"\x00" * (BLOCK - 4)
        rd = wr = st = free = 0
        for n in range(1, attempts + 1):
            self.h.bulkWrite(EP_OUT, block, timeout=TIMEOUT)
            t0 = time.time()
            while time.time() - t0 < settle:
                rd, wr, st, free = self.state()
                if st & 0x80:
                    if self.verbose:
                        print(f"  engine started on attempt {n}: "
                              f"state={st:#06x} free={free}")
                    return
                time.sleep(0.05)
            if self.verbose:
                print(f"  start attempt {n}: state={st:#06x} free={free} "
                      f"rd={rd:#06x} wr={wr:#06x}")
        raise RuntimeError(
            f"could not start engine after {attempts} attempts "
            f"(state={st:#06x}, free={free}, rd={rd:#06x}, wr={wr:#06x}).\n"
            f"  hs4u.py v{VERSION}\n"
            "  Power-cycle the HardSID (front switch off, wait a few seconds, "
            "on) and try again."
        )

    # -- command buffer ----------------------------------------------------

    def reg(self, chip, r, d):
        self._buf += encode_reg(chip, r, d)

    def delay(self, cycles):
        self._buf += encode_delay(cycles)

    def raw(self, data):
        self._buf += data

    def flush(self, wait=True):
        """Pad to a 512-byte multiple and send, respecting the ring."""
        if not self._buf:
            return
        payload = self._buf
        self._buf = b""
        payload += FILLER * (((-len(payload)) % BLOCK) // 2)
        for i in range(0, len(payload), BLOCK):
            self._wait_room(BLOCK * 2)
            self.h.bulkWrite(EP_OUT, payload[i:i + BLOCK], timeout=TIMEOUT)
            if self.verbose:
                rd, wr, st, free = self.state()
                print(f"  block  rd={rd:#06x} wr={wr:#06x} "
                      f"state={st:#06x} free={free}")

    def _wait_room(self, need, limit=30.0):
        t0 = time.time()
        while time.time() - t0 < limit:
            if self.state()[3] >= need:
                return
            time.sleep(0.001)
        rd, wr, st, free = self.state()
        msg = [
            f"timed out waiting for {need} bytes of ring space "
            f"(free={free}, rd={rd:#06x}, wr={wr:#06x}, state={st:#06x})",
            "The device is not consuming data.",
        ]
        if st & 0x80:
            msg += [
                "state bit 7 is set, so the engine reports itself running but",
                "the ring is not draining. Do NOT send a start block to fix",
                "this - it is a toggle and would stop the engine instead.",
            ]
        else:
            msg.append("state bit 7 is clear: the engine never started.")
        msg.append("Power-cycle the HardSID (front switch off, wait, on) "
                   "and run again.")
        raise RuntimeError("\n  ".join(msg))

    def drain(self, limit=15.0):
        """Block until the device has played everything buffered.

        If the engine is not running the ring never empties, so time out with
        a clear message rather than hanging."""
        t0 = time.time()
        while time.time() - t0 < limit:
            rd, wr, st, free = self.state()
            if free >= RING - BLOCK:
                return True
            time.sleep(0.02)
        rd, wr, st, free = self.state()
        print(f"  drain timed out after {limit}s: state={st:#06x} free={free}")
        if not (st & 0x80):
            print("  engine is NOT running (state bit 7 clear) - nothing is")
            print("  being consumed. The device was never started.")
        return False

    # -- high level --------------------------------------------------------

    def voice(self, chip, v, waveform=TRIANGLE, attack=0, decay=9,
              sustain=15, release=9, pulse_width=0x800):
        """Set up one voice's 'patch' - waveform and envelope. That is all a
        patch is on this hardware: register values. Then call note_on()."""
        b = VOICE_BASE[v]
        self.reg(chip, b + R_AD, ((attack & 0xF) << 4) | (decay & 0xF))
        self.delay(MIN_CYCLES)
        self.reg(chip, b + R_SR, ((sustain & 0xF) << 4) | (release & 0xF))
        self.delay(MIN_CYCLES)
        self.reg(chip, b + R_PW_LO, pulse_width & 0xFF)
        self.delay(MIN_CYCLES)
        self.reg(chip, b + R_PW_HI, (pulse_width >> 8) & 0x0F)
        self.delay(MIN_CYCLES)
        self._wave[(chip, v)] = waveform

    def note_on(self, chip, v, hz, clock=PAL_CLOCK):
        b = VOICE_BASE[v]
        f = freq_for_hz(hz, clock)
        self.reg(chip, b + R_FREQ_LO, f & 0xFF)
        self.delay(MIN_CYCLES)
        self.reg(chip, b + R_FREQ_HI, (f >> 8) & 0xFF)
        self.delay(MIN_CYCLES)
        self.reg(chip, b + R_CONTROL, self._wave.get((chip, v), TRIANGLE) | GATE)
        self.delay(MIN_CYCLES)

    def note_off(self, chip, v):
        b = VOICE_BASE[v]
        wave = self._wave.get((chip, v), TRIANGLE)
        self.reg(chip, b + R_CONTROL, wave & ~GATE)
        self.delay(MIN_CYCLES)

    def volume(self, chip, level=15, filter_mode=0):
        self.reg(chip, R_MODE_VOL, (filter_mode & 0xF0) | (level & 0x0F))
        self.delay(MIN_CYCLES)

    def init(self, chips=(0, 1, 2, 3), passes=1, warmup=0):
        """Arm the sockets. Without this, register writes make no sound.

        Structure matters, not just content. The reference capture sends:

            socket0, socket0, socket0, socket0(partial),   <- the "prefix"
            socket0, socket1, socket2, socket3, settle      <- the clean pass

        i.e. socket 0 is armed several times CONSECUTIVELY before any other
        socket is touched. Interleaving instead (0,1,2,3,0,1,2,3) does not
        work, which is why simply raising `passes` never helped.

        The 0:2048 prefix contains no command/data pair that the clean pass
        does not also contain - only filler - so repetition and ordering are
        the operative difference, not content.
        """
        if not self.running():
            print("  WARNING: engine not running (state bit 7 clear) - the")
            print("  init will be buffered but never executed.")

        first = chips[0] if chips else 0
        for _ in range(max(0, warmup)):
            self.raw(chip_init_stream(first))
        for _ in range(max(1, passes)):
            for c in chips:
                self.raw(chip_init_stream(c))
            self.raw(encode_delay(40000) + encode_reg(0, 0x1E, 0x00))
        self.flush()
        self.drain()

    def init_from_capture(self, path=None, start=0, end=4096):
        """Replay a slice of the reference capture's init verbatim.

        The full 0:4096 slice is known to work. The clean generated init is
        byte-identical to the 2048:3758 slice, yet fails - so something in
        the 0:2048 prefix is doing the real arming. Use start/end to bisect.
        """
        path = path or _default_capture()
        data = open(path, "rb").read()[start:end]
        self.raw(data)
        self.flush()
        self.drain()
        return len(data)

    def silence(self, chips=(0, 1, 2, 3)):
        for c in chips:
            for r in range(0x19):
                self.reg(c, r, 0x00)
                self.delay(MIN_CYCLES)
        self.flush()


def freq_for_hz(hz, clock=PAL_CLOCK):
    return int(round(hz * 16777216 / clock)) & 0xFFFF


def dump_status(repeat=3, interval=0.3):
    """Read the raw 64-byte status block and show every interpretation.
    Sends NOTHING to the device."""
    import usb1 as _u
    with _u.USBContext() as ctx:
        h = ctx.openByVendorIDAndProductID(VID, PID, skip_on_error=True)
        if h is None:
            print("device not found")
            return
        try:
            h.setConfiguration(1)
        except _u.USBError:
            pass
        h.claimInterface(IFACE)
        try:
            for i in range(repeat):
                raw = bytes(h.bulkRead(EP_IN, 64, timeout=1000))
                rd, wr, st = struct.unpack_from("<HHH", raw, 0x1A)
                used = (wr - rd) & (RING - 1)
                print(f"read {i+1}:")
                print(f"  raw[0:32] {raw[:32].hex(' ')}")
                print(f"  +0x1A rd={rd:#06x} ({rd})")
                print(f"  +0x1C wr={wr:#06x} ({wr})")
                print(f"  +0x1E st={st:#06x}  running={bool(st & 0x80)}")
                print(f"  used=(wr-rd)&0x1FFF = {used}   free=RING-used = "
                      f"{RING - used}")
                print(f"  reversed: (rd-wr)&0x1FFF = {(rd - wr) & (RING - 1)}")
                time.sleep(interval)
        finally:
            h.releaseInterface(IFACE)


def verify_init(path=None):
    """Prove the generated init is byte-identical to the reference capture.

    If this fails, the copy of hs4u.py or of the capture on this machine is
    not the one the analysis was done against, and every A/B result comparing
    'generated' with '--captured-init' is meaningless.
    """
    import hashlib
    path = path or _default_capture()
    gen = b"".join(chip_init_stream(c) for c in range(4))
    gen += encode_delay(40000) + encode_reg(0, 0x1E, 0x00)
    print(f"generated init : {len(gen)} bytes  sha256={hashlib.sha256(gen).hexdigest()[:16]}")
    try:
        cap = open(path, "rb").read()
    except OSError as e:
        print(f"capture file   : MISSING at {path}\n                 ({e})")
        return False
    print(f"capture file   : {len(cap)} bytes  sha256={hashlib.sha256(cap).hexdigest()[:16]}")
    for c in range(4):
        blk = chip_init_stream(c)
        print(f"  socket {c} block present in capture: {blk in cap}")
    at = cap.find(gen)
    print(f"  full generated init found in capture at offset: {at}")
    if at == 2048:
        print("  OK - matches the reference analysis exactly")
        return True
    print("  MISMATCH - this is why the A/B comparison is confusing")
    return False


def _demo():
    import argparse
    ap = argparse.ArgumentParser(description="play a note on the HardSID 4U")
    ap.add_argument("--chip", type=int, default=0)
    ap.add_argument("--hz", type=float, default=440.0)
    ap.add_argument("--seconds", type=float, default=1.5)
    ap.add_argument("--no-init", action="store_true")
    ap.add_argument("--init-passes", type=int, default=1,
                    help="full passes over all sockets (default 1)")
    ap.add_argument("--warmup", type=int, default=0,
                    help="consecutive arming attempts on the first socket "
                         "before the full pass (default 4, as in the capture)")
    ap.add_argument("--captured-init", action="store_true",
                    help="replay the reference capture's init verbatim "
                         "instead of generating it (A/B control)")
    ap.add_argument("--cap-start", type=int, default=0,
                    help="with --captured-init: first byte of the slice")
    ap.add_argument("--cap-end", type=int, default=4096,
                    help="with --captured-init: last byte of the slice")
    ap.add_argument("--hard-reset", action="store_true",
                    help="issue a full USB device reset before configuring")
    ap.add_argument("--no-start", action="store_true",
                    help="skip our engine-start step (ACID64 never does it)")
    ap.add_argument("--status", action="store_true",
                    help="dump the raw status block and exit (sends nothing)")
    ap.add_argument("--verify", action="store_true",
                    help="check the generated init against the capture and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    print(f"hs4u.py v{VERSION}")

    if args.status:
        dump_status()
        return

    if args.verify:
        verify_init()
        return

    hs = HardSID4U(verbose=args.verbose)
    hs.open(hard_reset=args.hard_reset, start=not args.no_start)
    try:
        rd, wr, st, free = hs.state()
        print(f"[link] state={st:#06x} free={free}")
        if free < RING - BLOCK:
            print(f"  NOTE: ring is not empty ({RING - free} bytes left over).")
            print("  Neither setConfiguration nor resetDevice clears this on")
            print("  macOS, so the device is carrying state from a previous")
            print("  run. Power-cycle the unit for a clean test.")
        if args.no_init:
            print("[init] SKIPPED")
        elif args.captured_init:
            n = hs.init_from_capture(start=args.cap_start, end=args.cap_end)
            print(f"[init] replayed capture bytes "
                  f"{args.cap_start}:{args.cap_end} ({n} bytes)")
        else:
            print(f"[init] generated, warmup={args.warmup} "
                  f"passes={args.init_passes}...")
            hs.init(passes=args.init_passes, warmup=args.warmup)
        f = freq_for_hz(args.hz)
        c = args.chip
        print(f"[note] chip {c}, {args.hz} Hz (freq={f:#06x}), {args.seconds}s")
        for r, v in ((0x18, 0x0F), (0x05, 0x11), (0x06, 0xF1),
                     (0x00, f & 0xFF), (0x01, f >> 8)):
            hs.reg(c, r, v)
            hs.delay(MIN_CYCLES)
        hs.reg(c, 0x04, 0x11)
        hs.delay(int(PAL_CLOCK * args.seconds))
        hs.reg(c, 0x04, 0x10)
        hs.delay(int(PAL_CLOCK * 0.4))
        hs.reg(c, 0x18, 0x00)
        hs.flush()
        hs.drain()
        print("[done]")
    finally:
        hs.close()


if __name__ == "__main__":
    _demo()
