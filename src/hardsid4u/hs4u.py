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
                cmd 0xFF        FILLER - and an ESCAPE PREFIX. It costs no
                                time, but the device consumes the word AFTER
                                it as an escape payload, so filler runs must
                                have EVEN length. Use pad_even(), never pad
                                by hand. See protocol.md 4b.

Registers   0x00-0x18 are the SID's own registers.
            0x19-0x1F are HardSID DEVICE control registers, per socket.

Ring        8192 bytes of device address space, 0x2000-0x3FFF.
                rd = status[0x1A], wr = status[0x1C]
                used = (wr - rd) & 0x1FFF
                free = 0x2000 - used
            state = status[0x1E]: LOW NIBBLE = system mode, BIT 7 = the
            device's acknowledgement of it. 0x0081 (mode 1 SIDPLAY, acked)
            is the only healthy value. Bit 7 is not "engine running" -
            that reading cost this project weeks. See protocol.md 4b.

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

VERSION = "1.2.0"

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

# FILLER (0xFFFF) IS AN ESCAPE PREFIX, NOT INERT PADDING.
#
# docs/protocol.md section 4 documents this and we then ignored it for the
# entire project. The device reads `ff ff` as an escape and consumes the
# NEXT WORD as its payload:
#
#     ff ff        escape
#     mm 00        (data mm, command 0x00) -> set system mode to mm
#
# status[0x1E] low nibble is the current mode, bit 7 is the acknowledge
# flag. Verified live: sending `ff ff | 02 00` moved the device from
# 0x0081 to 0x0002 and then 0x0082 - a mode change caused by two words,
# with no register write anywhere.
#
# CONSEQUENCES, all confirmed:
#   * The "engine start block" (`ff ff 01 00` + padding) is not an engine
#     start at all. It is escape + "set mode 1 (SIDPLAY)".
#   * state 0x0001 does not mean "engine stopped". It means mode 1 was
#     requested and never acknowledged. Register writes are ignored in
#     that state, which is why it looked like a dead device.
#   * Re-sending mode 1 to a device already at mode 1 does nothing, which
#     is exactly why start_engine() could never revive it and why every
#     recovery attempt ended at the power switch. Recovery from a wedged
#     state remains UNRELIABLE - see recover_mode(). Prevention is the
#     fix; pad_even() is the fix.
#   * A filler RUN of ODD length leaves an unpaired escape that swallows
#     the following word. Padding must therefore always use an EVEN
#     number of filler words - see pad_even().
SYS_MODE_IDLE = 0      # not a playing mode; the reset rung
SYS_MODE_SIDPLAY = 1
SYS_MODE_VST = 2

# Escape + set-mode-1. Kept under the old name because code and docs refer
# to it; it is the byte pattern to never emit by accident.
ENGINE_TOGGLE = b"\xff\xff\x01\x00"


# What to pad blocks with. FILLER (0xFFFF) is the obvious choice and the
# one the vendor uses - but it is an ESCAPE PREFIX, and a 512-byte block
# carrying five register writes is 95% padding, so padding with it emits
# ~122 escape+payload pairs per block. At 37 writes/second that is ~4500
# escape commands per second.
#
# ACID64 packs 2048-byte transfers dense with real commands at 1-2 per
# second and emits almost no filler at all - and never stalls, across
# 1200+ writes in ten minutes. Our stall is one per ~180 writes. So the
# hazard was never per-write; it scales with FILLER VOLUME, which is the
# one axis on which we differ from ACID64 by three orders of magnitude.
#
# DELAY_ZERO (0xEE 0x00) is a zero-cycle delay: it costs no time, exactly
# like filler, but it is an ordinary command rather than an escape.
DELAY_ZERO = b"\x00\xee"

# RESULT - this is the fix. Measured A/B, same traffic, same rate:
#
#     pad with FILLER      stalled at 82 and 94 writes
#     pad with DELAY_ZERO  24,000 writes / 10 minutes / ZERO stalls
#
# The run with DELAY_ZERO ended because it reached its time limit, not
# because anything went wrong. Better than a 250x improvement, and it
# explains the ACID64 contrast that no other theory could: ACID64 packs
# dense 2048-byte transfers and emits almost no filler, so it never
# accumulated the escape traffic that kills us.
PAD_WORD = DELAY_ZERO


def pad_even(payload):
    """Pad to a 512-byte boundary with an EVEN number of filler words.

    Filler is an escape prefix, so an odd-length filler run ends with an
    unpaired escape that consumes the first word of whatever follows it in
    the ring - the next block's opening word. If that word is a register
    write of the form (data, 0x00) the device reads it as a mode-set and
    stops honouring register writes altogether.

    Every block this driver built had an odd filler run, for every
    possible payload length, because payloads were always an odd number of
    words. One extra delay word fixes it for good.
    """
    if (len(payload) // 2) % 2:
        payload += encode_delay(MIN_CYCLES)  # make the word count even
    payload += PAD_WORD * (((-len(payload)) % BLOCK) // 2)
    return payload


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

    def system_mode(self):
        """(mode, acknowledged) from status[0x1E].

        Low nibble is the current system mode (1 = SIDPLAY, 2 = VST), bit
        7 is the device's acknowledgement. Register writes only behave as
        documented in an ACKNOWLEDGED SIDPLAY mode - protocol.md section
        4. `running()` is really "mode acknowledged"; the name predates
        understanding the field.
        """
        st = self.state()[2]
        return st & 0x0F, bool(st & 0x80)

    def set_system_mode(self, mode, settle=1.0):
        """Escape + set mode, then wait for the acknowledgement.

        Two words: FILLER as the escape prefix, then (data=mode,
        command=0x00). Padded to a full block with an EVEN filler run so
        the block cannot itself arm another escape.
        """
        payload = pad_even(FILLER + word(0x00, mode))
        self._wait_room(BLOCK * 2)
        self.h.bulkWrite(EP_OUT, payload, timeout=TIMEOUT)
        t0 = time.time()
        while time.time() - t0 < settle:
            m, ack = self.system_mode()
            if m == mode and ack:
                return True
            time.sleep(0.02)
        return self.system_mode() == (mode, True)

    def recover_mode(self, target=SYS_MODE_SIDPLAY, attempts=4, settle=0.4,
                      force=False):
        """Bring a wedged device back to an acknowledged SIDPLAY mode.

        HONEST STATUS: this works sometimes and is not to be relied on.
        It recovered a device once (0x0001 -> 0x0082 -> 0x0081, in under a
        second) and has failed on every attempt since - through mode 2,
        through mode 0, with filler padding and with the vendor's zero
        padding, and with a USB resetDevice() as well. A device that has
        been sitting wedged appears to stop acknowledging mode changes
        altogether, and then only the front-panel switch helps.

        Why it is still here: the cost is a second, the failure mode is
        "nothing happens", and when it does work it saves a power cycle.
        But the real answer to this failure is not recovering from it - it
        is pad_even(), which stops us causing it.

        Does NOT re-arm the sockets; callers must follow with init() and
        restore anything init() does not (notably 0x18, master volume).
        """
        for n in range(attempts):
            mode, ack = self.system_mode()
            # force=True skips this early-out on the first pass. It has to
            # exist: the real failure leaves the device reporting mode 1
            # ACKNOWLEDGED while its engine has stopped executing the
            # stream entirely (measured: 200000 cycles of delay drained in
            # 4ms against a 107ms healthy baseline). Trusting the mode
            # field there made this function return True without sending
            # anything, which is why the R key was a no-op in every single
            # failure the user hit.
            if mode == target and ack and not (force and n == 0):
                return True
            # Bounce through mode 0 (idle), NOT through the other playing
            # mode. Measured: a device wedged at 0x0002 (mode 2 requested,
            # never acknowledged) ignores a mode-1 request and stays
            # unacknowledged forever; but from 0x0000 it acknowledges mode
            # 1 within 400ms, first try. Mode 0 is the reset rung of this
            # ladder - asking for a playing mode while already stuck on
            # one is what never worked.
            self.set_system_mode(SYS_MODE_IDLE, settle=1.0)
            time.sleep(settle)
            if self.set_system_mode(target, settle=1.5):
                time.sleep(settle)
                if self.system_mode() == (target, True):
                    return True
            if self.verbose:
                print(f"  recover_mode attempt {n + 1}: "
                      f"state now {self.state()[2]:#06x}")
        return self.system_mode() == (target, True)

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

    def reset_engine(self, settle=3.0, attempts=8):
        """Recover a wedged device.

        Tries the MODE BOUNCE first, which is what actually works.
        recover_mode() fixes the state this project spent its whole life
        power-cycling out of - 0x0001, meaning a mode was requested and
        never acknowledged, in which the device ignores every register
        write. Measured: 0x0001 -> 0x0082 -> 0x0081 in well under a
        second.

        Only if that fails do we fall back to re-enumerating over USB,
        which is slow, invalidates every handle, and has never actually
        been observed to help. It is kept as a last resort, not as the
        plan.

        Does NOT re-arm the sockets: a de-armed socket discards every
        register write until the 'S','I','D' knock is re-sent, so callers
        must follow this with init(), and then restore anything init()
        does not (notably 0x18, master volume).
        """
        # force=True: never trust the mode field here. See recover_mode().
        if self.recover_mode(force=True):
            return True
        if self.verbose:
            print("  mode bounce failed; falling back to USB resetDevice()")
        try:
            self.h.resetDevice()
        except usb1.USBError as e:
            if self.verbose:
                print(f"  resetDevice: {e} (continuing)")
        try:
            self.close()
        except Exception:
            pass

        # Give the device time to actually leave the bus before reopening.
        time.sleep(settle)
        last = None
        deadline = time.time() + attempts
        while time.time() < deadline:
            try:
                self.open(start=False)
                break
            except Exception as e:
                last = e
                time.sleep(0.5)
        else:
            raise RuntimeError(
                f"device did not come back after resetDevice(): {last}\n"
                "  Power-cycle the HardSID (front switch off, wait, on).")

        # Observed behaviour, worth knowing when reading logs: a device
        # that was RUNNING comes back from resetDevice() still running
        # (0x0081) - the reset is effectively a no-op for engine state,
        # and start_engine() below correctly does nothing. A device that
        # was STOPPED (0x0001) comes back cold (0x0000), and that is the
        # case this function exists for: start_engine() then revives it,
        # first try, where nothing else could.
        self.start_engine()
        return self.running()

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
        payload = pad_even(payload)
        for i in range(0, len(payload), BLOCK):
            block = payload[i:i + BLOCK]
            if block[:4] == ENGINE_TOGGLE:
                # This exact 512-byte prefix is the engine start/stop
                # toggle. Sent to a running engine it stops it, and a
                # stopped engine needs the front-panel switch - see
                # reset_engine(). Anything that builds one by accident is
                # a bug worth failing loudly on.
                raise RuntimeError(
                    f"block at payload offset {i} begins with the engine "
                    f"toggle {ENGINE_TOGGLE.hex(' ')}; refusing to send it")
            self._wait_room(BLOCK * 2)
            self.h.bulkWrite(EP_OUT, block, timeout=TIMEOUT)
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
    # Clamp, don't wrap: masking with 0xFFFF turned anything above ~C#7
    # into a wildly wrong low pitch instead of the highest note the chip
    # can make. Pitch bend at the top of the keyboard can cross that line.
    return max(0, min(int(round(hz * 16777216 / clock)), 0xFFFF))


def measure_delay_rate(hs, cycles=200000, limit=10.0, poll=0.01):
    """How long does the device ACTUALLY take to execute a known delay?

    Sends one block containing `cycles` of delta-delay and times how long
    the ring takes to drain it. At the PAL clock, 200000 cycles should be
    about 203ms. The ratio of measured to expected is the interesting
    number:

        ~0.5   MEASURED BASELINE on a healthy device (107ms for 200000
               cycles). It reads low because rd rests one block behind wr,
               so the drain test fires while the last block is still
               nominally outstanding. Compare against this, not against
               1.0.
        >>1.0  the engine is executing the stream far too SLOWLY, which
               would make every register write land late - a note-off
               arriving seconds after it was sent is indistinguishable
               from a hanging note, and a device that looks perfectly
               healthy on every pointer and status check is exactly what
               you would see
        <<1.0  delays are being ignored altogether

    Built because the forensic dumps show `rd` falling 2-5 blocks behind
    `wr` during a failure and staying there, where healthy playing keeps
    it at exactly 1 block in 336 of 340 writes. That is the signature of
    an engine executing too slowly, and nothing else we measure would
    catch it: the pointers still move, the mode is still acknowledged,
    and every byte we send is still accepted.
    """
    expected = cycles / PAL_CLOCK
    payload = pad_even(encode_delay(cycles))
    hs._wait_room(BLOCK * 2)
    rd0, wr0, st0, free0 = hs.state()
    t0 = time.perf_counter()
    hs.h.bulkWrite(EP_OUT, payload, timeout=TIMEOUT)
    wr_target = hs.state()[1]
    drained_at = None
    while time.perf_counter() - t0 < limit:
        rd, wr, st, free = hs.state()
        remaining = (wr_target - rd) & (RING - 1)
        if remaining <= BLOCK:
            drained_at = time.perf_counter() - t0
            break
        time.sleep(poll)
    return {
        "cycles": cycles,
        "expected_s": expected,
        "measured_s": drained_at,
        "ratio": (drained_at / expected) if drained_at else None,
        "timed_out": drained_at is None,
        "state": hs.state()[2],
    }


def full_drain_time(hs, cycles=100000, limit=4.0, settle=2.0):
    """Time for the device to fully consume a block carrying `cycles` of
    delta-delay - i.e. until `free` is back to maximum.

    Calibrated across two decades on a healthy device and it tracks the
    nominal delay almost exactly:

        nominal    10ms   20ms   51ms  101ms  203ms  406ms  1000ms
        measured    9ms   16ms   46ms   98ms  195ms  396ms   980ms

    That is what makes it trustworthy where measure_delay_rate is not.
    That one stops at "all but one block consumed", which lands at roughly
    half the nominal delay and was flat across the first decade of the
    sweep - it detects the failure, but it does not measure what its name
    claims. This does.
    """
    payload = pad_even(encode_delay(cycles))
    # Start from rest, or the measurement includes somebody else's backlog.
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < settle:
        if hs.state()[3] >= RING - BLOCK:
            break
        time.sleep(0.005)
    hs._wait_room(BLOCK * 2)
    t0 = time.perf_counter()
    hs.h.bulkWrite(EP_OUT, payload, timeout=TIMEOUT)
    # Wait until the block is actually VISIBLE in the ring before timing
    # the drain. The status read immediately after a bulkWrite can still
    # report the pre-write `free`, in which case the drain test below is
    # satisfied instantly and the function returns ~1ms for a perfectly
    # healthy device. That race made the very first measurement after
    # init() unreliable in both calibration runs, and it made the stall
    # hunt announce "Device is ALREADY stalled" on a freshly power-cycled
    # unit - costing two power cycles chasing a fault that was mine.
    seen = False
    while time.perf_counter() - t0 < 0.05:
        if hs.state()[3] < RING - BLOCK:
            seen = True
            break
        time.sleep(0.001)
    if not seen:
        # Either the device swallowed it faster than we could observe -
        # which is the stall signature - or we lost the race again. The
        # caller retries, so report it as "too fast to see" rather than
        # guessing.
        return 0.0
    while time.perf_counter() - t0 < limit:
        if hs.state()[3] >= RING - BLOCK:
            return time.perf_counter() - t0
        time.sleep(0.002)
    return None


def engine_executing(hs, cycles=100000, min_fraction=0.15):
    """Is the engine actually EXECUTING the stream, or just swallowing it?

    The only check that catches the real failure. Everything else reads
    healthy while the device is dead: mode 1 acknowledged, ring pointers
    advancing, `free` returning to rest, no USB error, every byte
    accepted. The one thing that changes is that delta-delays stop taking
    any time.

    Uses full_drain_time(), which is calibrated 1:1 against the nominal
    delay, and requires the device to have taken at least `min_fraction`
    of the time it should have.

    Measured ratios, which set the threshold:

        healthy, device idle          0.97 - 1.05
        healthy, mid-performance      0.36 - 0.52   (ring not at rest, so
                                                     the drain test is
                                                     satisfied earlier)
        stalled                       0.00 - 0.05

    min_fraction sits at 0.15, comfortably between the two populations.
    It was 0.30, which is close enough to a legitimate 0.36 to risk crying
    wolf at someone mid-performance - and a false alarm here costs a power
    cycle, so the asymmetry is worth respecting.
    """
    nominal = cycles / PAL_CLOCK
    # Measure up to three times and keep the LARGEST. A healthy device
    # occasionally reads near-zero because of the status-read race above;
    # a stalled one reads 1-5ms every single time. Taking the max makes a
    # single unlucky sample harmless while leaving the real failure
    # unmistakable - and a false "stalled" costs the user a power cycle,
    # so the asymmetry matters.
    best = 0.0
    for _ in range(3):
        m = full_drain_time(hs, cycles=cycles, limit=max(2.0, nominal * 8))
        if m is None:
            return True, {"cycles": cycles, "expected_s": nominal,
                          "measured_s": None, "ratio": None}
        best = max(best, m)
        if best >= nominal * min_fraction:
            break
    r = {"cycles": cycles, "expected_s": nominal, "measured_s": best,
         "ratio": (best / nominal) if best else 0.0}
    return best >= nominal * min_fraction, r


def delta_probe(hs, poll_interval=0.05, window=2.0, verbose=False):
    """One-shot health check: is the engine honoring delta-timed delays,
    or has 'running' (state bit 7) become a stale flag that no longer
    reflects reality?

    Sends one 512-byte block of maximum-length delay pairs (128 x 0xFFFF
    cycles ~ 8.4M cycles ~ 8.5s at PAL clock) and tracks whether the ring's
    read pointer reaches the exact end address of THIS block within a
    short window, or stays genuinely behind it.

    Anchors to wr_target (the ring address right after this block was
    appended), not to a free-space comparison against an earlier
    snapshot: this device's ring carries a persistent ~512-byte residual
    even at rest (ordinary trailing FILLER padding from whatever was last
    sent, which drains near-instantly but is still technically "in the
    ring" at whatever moment a status read lands), so a naive free-space
    comparison can be fooled by that unrelated content draining
    coincidentally during the measurement window - this cost two rounds
    of a false "engine is lying" conclusion before the anchoring above was
    worked out. See docs/journey.md / project memory for the story.

    Returns a dict with at least "honored" (bool). If not honored,
    "recovered_after" is how long (seconds) the block actually took to
    drain (should be ~8.5s if genuine). If honored, "remaining_at_end" is
    how many bytes of the block were still unconsumed at the end of the
    probe window.
    """
    rd0, wr0, st, free = hs.state()
    block = encode_delay(0xFFFF) * 128
    assert len(block) == BLOCK, len(block)
    if verbose:
        print(f"    block hex (first 16 bytes): {block[:16].hex(' ')}")
        print(f"    before send: rd={rd0:#06x} wr={wr0:#06x}")
    hs.h.bulkWrite(EP_OUT, block, timeout=TIMEOUT)
    rd1, wr_target, st, free = hs.state()
    if verbose:
        print(f"    after send:  rd={rd1:#06x} wr={wr_target:#06x}  "
              f"(my block ends at {wr_target:#06x})")

    t0 = time.time()
    while time.time() - t0 < window:
        rd, wr, st, free = hs.state()
        remaining = (wr_target - rd) & (RING - 1)
        if verbose:
            print(f"    t+{(time.time() - t0) * 1000:6.1f}ms  "
                  f"rd={rd:#06x} remaining_of_my_block={remaining} "
                  f"state={st:#06x}")
        if remaining == 0:
            return {
                "honored": False,
                "recovered_after": time.time() - t0,
                "state": st,
            }
        time.sleep(poll_interval)
    rd, wr, st, free = hs.state()
    return {
        "honored": True,
        "recovered_after": None,
        "state": st,
        "remaining_at_end": (wr_target - rd) & (RING - 1),
    }


def report_probe(label, result):
    if result["honored"]:
        print(f"  [PROBE {label}] HONORED - my block still had "
              f"{result['remaining_at_end']} bytes remaining at the end "
              f"of the window (engine genuinely still working through "
              f"the ~8.5s delay), state={result['state']:#06x}")
    else:
        print(f"  [PROBE {label}] *** NOT HONORED *** - this probe's "
              f"block fully drained after only "
              f"{result['recovered_after'] * 1000:.0f}ms "
              f"(should take ~8.5s if genuinely executing). "
              f"Engine status bit is LYING. state={result['state']:#06x}")


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
