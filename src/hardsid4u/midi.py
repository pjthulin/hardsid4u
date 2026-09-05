#!/usr/bin/env python3
"""
midi.py — play the HardSID 4U live from a DAW or MIDI controller.

Opens a virtual CoreMIDI destination ("HardSID4U" by default). Select it as
a MIDI track's output in Ableton/Cubase, or point a controller at it.

Architecture note: every write in this module goes out as a single
immediate 512-byte block via write_regs_now(), the same real-time path
validated in midi_latency_probe.py (sub-millisecond bulkWrite(), ring
never backs up under rapid triggering — see docs/journey.md). This module
never uses hs4u.py's buffered, delay-scheduled flush()/HardSID4U.reg() —
that path is for pre-scheduled playback (SID-file style), and mixing it
with real-time events would reintroduce the ring-backlog risk the probes
were built to rule out. flush()/init() are only used once at startup to
arm the sockets.

Voice model
-----------
MIDI channels 1-4 map to chips (sockets) 0-3. Each channel gets 3-voice
polyphony, matching that socket's real SID voice count — there is no
cross-chip voice stealing, by design, since it would blur which physical
chip a channel controls.

Filter cutoff/resonance/volume are per-CHIP registers on real SID
hardware, not per-voice, so CC changes to them affect every note currently
sounding on that channel at once — this is a hardware constraint, not a
design choice. Waveform/ADSR/pulse-width are technically per-voice
registers, but this module treats them as a single "current patch" per
channel, applied to whichever voice is triggered and re-applied live to
all currently active voices on a CC change - the natural mental model for
a MIDI channel driving one socket.

CC map
------
    CC7   volume           CC71  resonance        CC79  sustain
    CC72  release           CC73  attack           CC74  filter cutoff
    CC75  decay             CC70  waveform (0-31 tri, 32-63 saw,
                                              64-95 pulse, 96-127 noise)
    CC20/CC52  pulse width MSB/LSB (14-bit MIDI convention, scaled to 12-bit)
    CC102 hard panic (DAW-mappable; CC123 also works from controllers that
                       can send Channel Mode Messages, which most DAWs won't)
    Pitch bend: +/-2 semitones by default (--bend-range), coalesced to
                ~66 writes/s - see ChipChannel.pitch_bend()

Terminal keys
-------------
    When it fails, work down this ladder and note which rung brings the
    sound back - each one names a different broken layer:

    D   dump the flight recorder (do this FIRST, before anything else)
    T   test tone + engine-execution check: is it really silent, and is
        the engine still executing the stream at all?
    X   SID-LEVEL hard reset (TEST bit + zeroed envelopes). The only rung
        that separates a wedged SID chip from a deaf HardSID.
    V   restore volume/filter only
    A   re-arm the sockets (no engine reset)
    R   full recovery (engine reset + re-arm), then auto-dumps again so
        the recovery's own writes are on the record
    SPACE  panic: silence everything and re-arm

Usage
-----
    uv run --extra midi python3 src/hardsid4u/midi.py
    uv run --extra midi python3 src/hardsid4u/midi.py --port-name "HS4U"

    # isolate specific sockets - e.g. testing a suspect chip with zero
    # writes ever reaching the others, including at startup:
    uv run --extra midi python3 src/hardsid4u/midi.py --chips 1,2
"""
import argparse
import collections
import datetime
import os
import signal
import sys
import threading
import time

try:
    import select
    import termios
    import tty
    _HAS_TTY_SUPPORT = True
except ImportError:
    _HAS_TTY_SUPPORT = False  # e.g. Windows - space-bar panic unavailable

# Prefer the package's own module, so `hardsid4u.midi` and
# `hardsid4u.hs4u` are the SAME module object. hs4u carries module-level
# state that matters - PAD_WORD in particular - and importing it twice
# under two names would silently give the two halves different settings.
# The fallback keeps this file runnable as a bare script.
try:
    from . import hs4u
except ImportError:  # noqa: E402
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hs4u  # noqa: E402

CC_VOLUME = 7
CC_RESONANCE = 71
CC_RELEASE = 72
CC_ATTACK = 73
CC_FILTER_CUTOFF = 74
CC_DECAY = 75
CC_WAVEFORM = 70
CC_PW_MSB = 20
CC_PW_LSB = 52
CC_ALL_NOTES_OFF = 123
CC_PANIC = 102  # most DAWs (Ableton included) won't send CC120-127 - those
                 # are reserved MIDI "Channel Mode Messages", not regular
                 # CCs, and get filtered before reaching a plugin/output.
                 # 102 is in the officially-undefined 102-119 block, freely
                 # mappable from a DAW. CC123 stays listened-for too, for
                 # hardware controllers/software that can send it directly.

DEFAULT_PATCH = dict(
    waveform=hs4u.PULSE, attack=0, decay=8, sustain=11, release=6,
    pulse_width=0x800, cutoff=1024, resonance=0, volume=15,
    filter_mode=hs4u.FILT_LP,
    filter_route=0x00,  # no voices routed through the filter by default -
                         # there's no filter-enable CC yet, so an engaged
                         # filter can only ever be turned off by luck
)


# `ff ff` is an ESCAPE PREFIX, not inert padding (protocol.md section 4,
# confirmed live in escape_prefix_test.py). The device consumes the word
# AFTER an escape as its payload, and `mm 00` means "set system mode to
# mm". Register writes only behave as documented in an acknowledged
# SIDPLAY mode, so an accidental mode-set makes the device go deaf to
# everything we send - which is exactly the hanging-note failure.
#
# This module's padding was arming that escape on every single block: the
# payload was always an odd number of words, so the filler run was always
# odd, so the final `ff ff` had no partner and swallowed the FIRST WORD OF
# THE NEXT BLOCK. Before the leading delay word that first word was a
# register write, and for pitch bend it was (freq_lo, 0x00) - command
# 0x00, data = a frequency byte a wheel sweep walks through all 256 values
# of. hs4u.pad_even() now guarantees an even filler run; the leading delay
# word means even a mis-framed escape would swallow `08 ee` rather than
# something shaped like a mode-set.
ENGINE_TOGGLE_PREFIX = b"\xff\xff\x01\x00"


def _build_block(pairs):
    """pairs: [(chip, reg, value), ...] -> one 512-byte immediate block.

    Every block opens with a delay word rather than a register write.
    That single wasted word (MIN_CYCLES, ~8 microseconds) is what makes
    the engine-stop pattern structurally impossible to emit:

    Blocks are padded to 512 bytes with FILLER, so every block ENDS in a
    run of `ff ff`. If a block then BEGAN with a register write, the byte
    stream across that boundary would read `... ff ff | <data> <cmd> ...`
    - and whenever that command byte is 0x00 with data 0x01, those four
    bytes are the engine toggle. With a leading delay word, a block
    always starts `08 ee`, so the boundary reads `ff ff 08 ee`, and any
    framing that lands inside the filler run reads `ff ff ff ff`. Neither
    can ever match.

    This is defence in depth: the framing this driver actually uses puts
    one 512-byte block per USB transfer, and a transfer starting with
    filler should not arise. But the device has been observed miscounting
    a transfer's length (the +512 `wr` anomaly, ~0.05-0.12% of writes),
    which means its idea of where blocks begin is not perfectly ours, and
    the consequence of being wrong once is a dead unit needing a power
    cycle. Eight microseconds is a very cheap insurance premium.
    """
    payload = hs4u.encode_delay(hs4u.MIN_CYCLES)
    for chip, reg, val in pairs:
        payload += hs4u.encode_reg(chip, reg, val)
        payload += hs4u.encode_delay(hs4u.MIN_CYCLES)
    payload = hs4u.pad_even(payload)
    assert not payload.startswith(ENGINE_TOGGLE_PREFIX)
    return payload


# Shared between the MIDI dispatch thread (write_regs_now, hard_panic) and
# the background health-monitor thread (both issue bulkWrite/bulkRead on
# the same libusb device handle) - serializes device I/O so the two never
# interleave transfers. Reentrant: hard_panic() holds it for its whole
# body while also calling write_regs_now(), which acquires it again.
_IO_LOCK = threading.RLock()

# Set from --trace-io: logs ring pointer state after EVERY write, not just
# periodic health probes. Built to catch a specific hypothesis - repeated
# sparse events (each padded to a full 512-byte block, mostly FILLER) wrap
# the 8192-byte ring far more often per unit of musical content than
# Acid64's efficiently-packed multi-block transfers ever do, and a
# reliable ~35-115s failure under a bare "one note/sec" loop is a strong,
# fast repro for whatever that difference triggers. See project memory /
# docs/journey.md for the story so far.
_TRACE_IO = False
_trace_n = 0


# Room required before any write, in bytes. This is the vendor's own
# threshold, not ours: docs/protocol.md section 6 quotes hardsid.dll
# verbatim - HardSID_Try_Write returns 2 ("would block, back off") when
# free < 0x1000, i.e. the reference implementation refuses to let the
# device hold more than HALF the ring. We used to require only 512, eight
# times more permissive, and there is a specific reason that is dangerous:
#
#   used = (wr - rd) & 0x1FFF
#
# cannot represent a full ring. At exactly 8192 bytes used, the mask
# yields 0 and our own state() reports free = 8192 - i.e. a COMPLETELY
# FULL ring is indistinguishable from a completely empty one. Overrun is
# invisible to the very check meant to prevent it, so the only safe
# strategy is to stay far away from the ambiguity. The vendor's 4096 does
# exactly that.
ROOM_REQUIRED = 0x1000

# Continuous controllers (pitch bend, filter sweeps) only ever need their
# LATEST value to reach the chip - intermediate positions are inaudible.
# A wheel emits 100-400 messages/second, and one 512-byte USB transfer per
# message was turning a musical gesture into the heaviest bus load this
# driver ever produces. Coalescing to ~66 writes/s is well above what a
# SID vibrato needs and cuts bend traffic 5x.
BEND_MIN_INTERVAL = 0.015


class BusBusy(Exception):
    """The ring had no room within the caller's budget. Not written."""


class Forensics:
    """Rolling flight recorder for the last few thousand bus events.

    Built because the failure cannot be reproduced from a script - two
    harnesses pushed 64,000 writes and 90,000 bend messages of the exact
    same traffic without a scratch - but happens readily under a real
    keyboard and a real pitch wheel. So the recording has to happen during
    live play, and it has to survive the moment of death: by the time a
    human notices silence and reaches for a key, the interesting writes
    are already seconds in the past. A ring buffer solves that; a watchdog
    that dumps the instant the engine's run bit clears solves it better.

    Cheap enough to leave on: one tuple append per write, no formatting
    until a dump is asked for.
    """

    def __init__(self, capacity=8000):
        self.events = collections.deque(maxlen=capacity)
        self.lock = threading.Lock()
        self.t0 = time.perf_counter()
        self.dumps = 0
        self.writes = 0
        self.mismatches = 0
        self.expected_wr = None  # predicted ring write pointer, resynced
                                  # from the device after every write

    def record(self, kind, detail):
        with self.lock:
            self.events.append((time.perf_counter() - self.t0, kind, detail))

    def dump(self, hs, reason, outdir=None):
        """Write the buffer to a file. Never raises - it runs in the
        failure path, where a second exception helps nobody."""
        self.dumps += 1
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"hs4u-forensics-{stamp}-{self.dumps}.log"
        path = os.path.join(outdir or os.getcwd(), name)
        try:
            rd, wr, st, free = hs.state()
            live = (f"rd={rd:#06x} wr={wr:#06x} state={st:#06x} free={free} "
                    f"running={bool(st & 0x80)}")
        except Exception as e:
            live = f"could not read device state: {type(e).__name__}: {e}"
        with self.lock:
            events = list(self.events)
        try:
            with open(path, "w") as f:
                f.write(f"HardSID 4U forensic dump\n")
                f.write(f"reason      : {reason}\n")
                f.write(f"time        : {datetime.datetime.now().isoformat()}\n")
                f.write(f"hs4u.py     : v{hs4u.VERSION}\n")
                f.write(f"device now  : {live}\n")
                f.write(f"writes      : {self.writes} "
                        f"({self.mismatches} pointer mismatches)\n")
                f.write(f"events held : {len(events)} "
                        f"(oldest {events[0][0]:.3f}s, "
                        f"newest {events[-1][0]:.3f}s)\n" if events
                        else "events held : 0\n")
                f.write("\n"
                        "kind: m=MIDI in  w=write  x=blocked/failed write  "
                        "s=state poll  e=note\n"
                        "-------------------------------------------------"
                        "---------------------------\n")
                for t, kind, detail in events:
                    f.write(f"{t:10.4f}  {kind}  {detail}\n")
            print(f"\n*** forensic dump written: {path}")
            print(f"    device state at dump: {live}")
        except Exception as e:
            print(f"\n*** forensic dump FAILED: {type(e).__name__}: {e}")
        return path


_FORENSICS = None  # set in main(); None disables recording entirely


def write_regs_now(hs, pairs, backoff_budget=0.05, drop_if_busy=False):
    """Bypass hs.flush()/hs._buf entirely: one immediate bulk write.

    Checks ring free space first and backs off if the ring is nearly full.
    Skipping this - as the first version of this module did - lets a burst
    (a chord, heavy polyphony, a pitch-bend sweep) overrun the ring. The
    USB transport itself doesn't error when that happens; the device's own
    word alignment silently corrupts from that point on. This is the
    "delayed and garbled" failure mode docs/protocol.md section 6 warns
    about.

    What changed, and why it matters: this used to give up after
    backoff_budget and SEND ANYWAY, on the reasoning that a late write
    beats a stalled performance. That trade is wrong. A late note is a
    blemish; a write into a full ring desynchronises the device's 16-bit
    command stream, after which our DATA bytes are executed as COMMAND
    bytes - which can hit registers 0x1D/0x1E/0x1F and DE-ARM a socket, at
    which point every subsequent register write is silently discarded and
    only a device reset brings it back. That is the "only a power cycle
    fixes it" failure. So now we never write without room:

        drop_if_busy=True   (bend/CC - a stale controller value is
                             inaudible)  -> return False
        drop_if_busy=False  (notes - dropping a note-off would hang it)
                            -> raise BusBusy so the caller can escalate to
                               a real recovery instead of corrupting the
                               stream

    Returns True if the block went out.
    """
    if not pairs:
        return True
    block = _build_block(pairs)
    need = max(len(block), ROOM_REQUIRED)
    t0 = time.perf_counter()
    with _IO_LOCK:
        while hs.state()[3] < need:
            if time.perf_counter() - t0 > backoff_budget:
                rd, wr, st, free = hs.state()
                if _FORENSICS:
                    _FORENSICS.record("x", f"no room: free={free} need={need} "
                                           f"rd={rd:#06x} wr={wr:#06x} "
                                           f"state={st:#06x} "
                                           f"{'dropped' if drop_if_busy else 'RAISED'} "
                                           f"head={block[:16].hex(' ')}")
                if drop_if_busy:
                    return False
                raise BusBusy(
                    f"ring had {free} of {need} bytes free after "
                    f"{backoff_budget * 1000:.0f}ms (rd={rd:#06x} "
                    f"wr={wr:#06x} state={st:#06x})")
            time.sleep(0.001)
        # Last line of defence, checked on every transfer rather than
        # left to an assert (which -O would strip): a block matching the
        # engine toggle stops the device dead, and nothing short of the
        # power switch brings it back. Never put one on the wire by
        # accident.
        for off in range(0, len(block), hs4u.BLOCK):
            if block[off:off + 4] == ENGINE_TOGGLE_PREFIX:
                raise BusBusy(
                    f"refusing to send a block at offset {off} that begins "
                    f"with the engine stop pattern "
                    f"{ENGINE_TOGGLE_PREFIX.hex(' ')} - this would halt the "
                    f"device until it is power-cycled")
        try:
            sent = hs.h.bulkWrite(hs4u.EP_OUT, block, timeout=hs4u.TIMEOUT)
        except Exception as e:
            # A bulk transfer that fails may still have put an unknown
            # number of bytes on the wire. If that count is odd the
            # device's word stream is permanently misaligned. Never
            # swallow this - it is a candidate explanation for every
            # unexplained ghost note and de-armed socket in this project.
            if _FORENSICS:
                _FORENSICS.record("x", f"bulkWrite raised "
                                       f"{type(e).__name__}: {e}  "
                                       f"head={block[:16].hex(' ')}")
            raise BusBusy(f"bulkWrite failed: {type(e).__name__}: {e}") from e
        if _FORENSICS:
            # Recording the head bytes is the whole point: if the device
            # ever receives something it reads as the engine toggle, the
            # bytes we actually put on the wire in the moments before are
            # the only evidence that can prove or disprove it.
            _FORENSICS.writes += 1
            nblocks = len(block) // hs4u.BLOCK
            predicted = _FORENSICS.expected_wr
            if predicted is not None:
                predicted = (((predicted - 0x2000)
                              + hs4u.BLOCK * nblocks) % hs4u.RING) + 0x2000
            rd, wr, st, free = hs.state()
            slip = ""
            if predicted is not None and wr != predicted:
                _FORENSICS.mismatches += 1
                slip = (f" SLIP predicted={predicted:#06x} "
                        f"diff={wr - predicted:+d}")
            _FORENSICS.expected_wr = wr  # resync from truth after every write
            # Record the whole non-filler payload, not a 24-byte prefix.
            # The prefix hid the tail of every note-on block - including
            # the gate-ON write, which is the LAST pair note_on() emits -
            # so the one register that decides whether a note sounds was
            # invisible in every dump taken so far.
            body = block.rstrip(b"\xff")
            if len(body) % 2:
                body = block[:len(body) + 1]
            _FORENSICS.record("w", f"{len(block)}B({nblocks}blk) rd={rd:#06x} "
                                   f"wr={wr:#06x} free={free} state={st:#06x}"
                                   f"{slip} payload={body.hex(' ')}")
            if not (st & 0x80):
                _FORENSICS.record("e", "*** ENGINE RUN BIT CLEARED ***")
        if _TRACE_IO:
            global _trace_n
            _trace_n += 1
            rd, wr, st, free = hs.state()
            wrapped = wr < rd  # wr crossed 0x3E00 -> 0x2000 since last read
            print(f"  [trace {_trace_n:6d}] t={time.perf_counter():.3f} "
                  f"bytes={len(block)} rd={rd:#06x} wr={wr:#06x} "
                  f"free={free} state={st:#06x}"
                  f"{'  <<< WRAP' if wrapped else ''}")
    if sent != len(block):
        # A short write desyncs the ring's word alignment from this point
        # on with no other symptom. Treat it as the emergency it is.
        raise BusBusy(f"short bulkWrite: sent {sent}/{len(block)} bytes "
                      f"- command stream alignment is no longer trustworthy")
    return True


def midi_note_to_hz(note, bend_semitones=0.0):
    return 440.0 * (2.0 ** ((note - 69 + bend_semitones) / 12.0))


def waveform_from_cc(value):
    if value < 32:
        return hs4u.TRIANGLE
    if value < 64:
        return hs4u.SAWTOOTH
    if value < 96:
        return hs4u.PULSE
    return hs4u.NOISE


def scale7(value, maximum):
    return round(value / 127 * maximum)


class VoiceSlot:
    __slots__ = ("chip", "voice", "note", "state", "on_time")

    def __init__(self, chip, voice):
        self.chip = chip
        self.voice = voice
        self.note = -1
        self.state = "free"  # free | held | releasing
        self.on_time = 0.0


class ChipChannel:
    """Owns one chip's 3 voices and the 'patch' driving them."""

    def __init__(self, hs, chip, bend_range=2.0):
        self.hs = hs
        self.chip = chip
        self.bend_range = bend_range
        self.voices = [VoiceSlot(chip, v) for v in range(3)]
        self.patch = dict(DEFAULT_PATCH)
        self.bend_semitones = 0.0
        self._bend_dirty = False
        self._last_bend_write = 0.0
        self._bend_lock = threading.Lock()
        self._pw_msb = self.patch["pulse_width"] >> 5
        self._pw_lsb = 0
        # hs4u.py's chip_init_stream() never touches 0x18 (volume/filter
        # mode) - after a power cycle it reads 0 (silent) until something
        # writes it. Push the default patch immediately rather than waiting
        # for the first volume/cutoff/resonance CC, which may never come.
        self._write_chip_globals()

    # -- allocation ----------------------------------------------------

    def _allocate(self, note):
        for v in self.voices:
            if v.note == note and v.state in ("held", "releasing"):
                return v
        for v in self.voices:
            if v.state == "free":
                return v
        for v in self.voices:
            if v.state == "releasing":
                return v
        return min(self.voices, key=lambda v: v.on_time)

    def _find_held(self, note):
        for v in self.voices:
            if v.note == note and v.state == "held":
                return v
        return None

    # -- register building -----------------------------------------------

    def _freq_for(self, note):
        hz = midi_note_to_hz(note, self.bend_semitones)
        return hs4u.freq_for_hz(hz)

    def _voice_patch_pairs(self, slot, gate):
        b = hs4u.VOICE_BASE[slot.voice]
        p = self.patch
        ctrl = p["waveform"] | (hs4u.GATE if gate else 0)
        return [
            (self.chip, b + hs4u.R_AD, ((p["attack"] & 0xF) << 4) | (p["decay"] & 0xF)),
            (self.chip, b + hs4u.R_SR, ((p["sustain"] & 0xF) << 4) | (p["release"] & 0xF)),
            (self.chip, b + hs4u.R_PW_LO, p["pulse_width"] & 0xFF),
            (self.chip, b + hs4u.R_PW_HI, (p["pulse_width"] >> 8) & 0x0F),
            (self.chip, b + hs4u.R_CONTROL, ctrl),
        ]

    # -- events ----------------------------------------------------------

    def note_on(self, note, velocity):
        slot = self._allocate(note)
        # A voice being reused - whether retriggering the same note or
        # stealing one already sounding a different note - needs a
        # gate-off pulse first. GATE only retriggers the envelope on a
        # 0->1 transition; writing 1 while it's already 1 (the previous
        # note's gate, never released) is a no-op on real hardware, so a
        # stolen voice would silently keep the old envelope under new
        # frequency/ADSR registers instead of starting a fresh attack.
        pairs = []
        if slot.state != "free":
            b = hs4u.VOICE_BASE[slot.voice]
            pairs.append((self.chip, b + hs4u.R_CONTROL,
                          self.patch["waveform"] & ~hs4u.GATE))
        slot.note = note
        slot.state = "held"
        slot.on_time = time.perf_counter()
        freq = self._freq_for(note)
        b = hs4u.VOICE_BASE[slot.voice]
        pairs += [
            (self.chip, b + hs4u.R_FREQ_LO, freq & 0xFF),
            (self.chip, b + hs4u.R_FREQ_HI, (freq >> 8) & 0xFF),
        ]
        pairs += self._voice_patch_pairs(slot, gate=True)
        write_regs_now(self.hs, pairs)

    def note_off(self, note):
        slot = self._find_held(note)
        if slot is None:
            return
        slot.state = "releasing"
        b = hs4u.VOICE_BASE[slot.voice]
        ctrl = self.patch["waveform"] & ~hs4u.GATE
        write_regs_now(self.hs, [(self.chip, b + hs4u.R_CONTROL, ctrl)])

    def pitch_bend(self, value14):
        """Record the new wheel position; the register write is coalesced.

        bend_semitones updates immediately, so any note triggered between
        now and the next flush is still born at the correct pitch - only
        the retune of already-sounding voices is rate-limited. A wheel
        sweep used to cost one 512-byte USB transfer per MIDI message,
        making pitch bend by far the heaviest bus load this driver
        produces, and the user's most reliable trigger for the failure
        this whole investigation is about."""
        self.bend_semitones = ((value14 - 8192) / 8192.0) * self.bend_range
        self._bend_dirty = True
        self.flush_bend()

    def flush_bend(self, now=None):
        """Retune sounding voices to the current wheel position, at most
        once per BEND_MIN_INTERVAL.

        Called from two threads - the MIDI callback (fast path, so an
        isolated bend goes out with no added latency) and the coalescing
        thread (which guarantees the FINAL wheel position always lands;
        dropping the last message of a sweep would leave notes
        permanently detuned). Both the dirty flag and the rate check are
        therefore made under a lock and re-checked inside it. Without
        that, both threads pass the interval test on the same message and
        each send an identical block - doubling bend traffic, which is
        exactly the load this coalescing exists to remove. Caught in a
        forensic dump showing every bend message followed by two
        byte-identical writes."""
        with self._bend_lock:
            if not self._bend_dirty:
                return
            if now is None:
                now = time.perf_counter()
            if now - self._last_bend_write < BEND_MIN_INTERVAL:
                return
            pairs = []
            for v in self.voices:
                if v.state == "free" or v.note < 0:
                    continue
                freq = self._freq_for(v.note)
                b = hs4u.VOICE_BASE[v.voice]
                pairs += [
                    (self.chip, b + hs4u.R_FREQ_LO, freq & 0xFF),
                    (self.chip, b + hs4u.R_FREQ_HI, (freq >> 8) & 0xFF),
                ]
            # drop_if_busy: a bend that misses its slot is inaudible, and
            # the next flush carries the newer value anyway.
            if write_regs_now(self.hs, pairs, drop_if_busy=True):
                self._bend_dirty = False
                self._last_bend_write = now

    def _write_chip_globals(self):
        p = self.patch
        pairs = [
            (self.chip, hs4u.R_CUTOFF_LO, p["cutoff"] & 0x07),
            (self.chip, hs4u.R_CUTOFF_HI, (p["cutoff"] >> 3) & 0xFF),
            (self.chip, hs4u.R_RESON_FILT, ((p["resonance"] & 0xF) << 4) | p["filter_route"]),
            (self.chip, hs4u.R_MODE_VOL, (p["filter_mode"] & 0xF0) | (p["volume"] & 0x0F)),
        ]
        write_regs_now(self.hs, pairs)

    def _reapply_voice_patch(self):
        pairs = []
        for v in self.voices:
            if v.state == "free":
                continue
            pairs += self._voice_patch_pairs(v, gate=(v.state == "held"))
        write_regs_now(self.hs, pairs)

    def control_change(self, cc, value):
        p = self.patch
        if cc == CC_VOLUME:
            p["volume"] = scale7(value, 15)
            self._write_chip_globals()
        elif cc == CC_FILTER_CUTOFF:
            p["cutoff"] = scale7(value, 2047)
            self._write_chip_globals()
        elif cc == CC_RESONANCE:
            p["resonance"] = scale7(value, 15)
            self._write_chip_globals()
        elif cc == CC_ATTACK:
            p["attack"] = scale7(value, 15)
            self._reapply_voice_patch()
        elif cc == CC_DECAY:
            p["decay"] = scale7(value, 15)
            self._reapply_voice_patch()
        elif cc == CC_RELEASE:
            p["release"] = scale7(value, 15)
            self._reapply_voice_patch()
        elif cc == CC_PW_MSB:
            self._pw_msb = value
            p["pulse_width"] = ((self._pw_msb << 7) | self._pw_lsb) >> 2
            self._reapply_voice_patch()
        elif cc == CC_PW_LSB:
            self._pw_lsb = value
            p["pulse_width"] = ((self._pw_msb << 7) | self._pw_lsb) >> 2
            self._reapply_voice_patch()
        elif cc == CC_WAVEFORM:
            p["waveform"] = waveform_from_cc(value)
            self._reapply_voice_patch()
        # CC_ALL_NOTES_OFF is intercepted at the MidiToSid level as a hard
        # panic across all 4 chips - see MidiToSid.hard_panic(). It used to
        # be handled here as a chip-scoped all_notes_off(), which depends on
        # Python-side voice bookkeeping staying in sync with hardware - not
        # a safe assumption for an emergency stop.

    def all_notes_off(self):
        pairs = []
        for v in self.voices:
            if v.state != "free":
                b = hs4u.VOICE_BASE[v.voice]
                pairs.append((self.chip, b + hs4u.R_CONTROL,
                              self.patch["waveform"] & ~hs4u.GATE))
                v.state = "free"
                v.note = -1
        write_regs_now(self.hs, pairs)


class MidiToSid:
    """rtmidi callback: dispatches raw MIDI bytes to the right chip."""

    def __init__(self, hs, bend_range=2.0, verbose=False, enabled_chips=(0, 1, 2, 3)):
        self.hs = hs
        self.verbose = verbose
        # Disabled chips get a ChipChannel of None: no startup arming, no
        # dispatch, no hard_panic writes - zero bytes reach them, ever.
        # Built for isolating a suspect chip during hardware testing (see
        # project memory / docs/journey.md) without editing code each time.
        self.enabled_chips = set(enabled_chips)
        self.channels = [
            ChipChannel(hs, chip, bend_range) if chip in self.enabled_chips else None
            for chip in range(4)
        ]
        self._last_panic = 0.0
        self._last_recovery = 0.0
        self.bus_faults = 0
        # Coalescing thread for continuous controllers - see
        # ChipChannel.flush_bend(). Cheap: it wakes 200x/second and almost
        # always finds nothing dirty.
        self._stop = threading.Event()
        self._bend_thread = threading.Thread(
            target=self._bend_flush_loop, daemon=True, name="bend-flush")
        self._bend_thread.start()

    def _bend_flush_loop(self):
        while not self._stop.wait(0.005):
            now = time.perf_counter()
            for ch in self.channels:
                if ch is None or not ch._bend_dirty:
                    continue  # cheap pre-check; flush_bend re-checks safely
                try:
                    ch.flush_bend(now)
                except BusBusy as e:
                    self.on_bus_fault(e)
                except Exception as e:
                    print(f"  [bend flush error] {e}")

    def stop(self):
        self._stop.set()
        self._bend_thread.join(timeout=1.0)

    def on_bus_fault(self, exc):
        """A write could not be made safely. Recover rather than corrupt.

        Reaching here means the ring had no room, or the transfer failed
        or came up short - all states in which writing anyway risks
        knocking the device's 16-bit command stream off its word boundary.
        Recovery is rate-limited so a persistent fault logs once and gets
        one reset attempt, rather than hammering the device."""
        self.bus_faults += 1
        print(f"\n*** BUS FAULT #{self.bus_faults}: {exc}")
        now = time.perf_counter()
        if now - self._last_recovery < 2.0:
            print("    (recovery attempted recently - not repeating)")
            return
        self._last_recovery = now
        self.recover()

    def recover(self, reset_engine=True):
        """Full device recovery without touching the power switch.

        hard_panic() zeroes registers, which only helps if the device is
        still executing our stream correctly. When it isn't - a de-armed
        socket, or a command stream that has drifted off its word boundary
        - no sequence of register writes can help, because the device
        isn't reading them as register writes any more. The only thing
        that can is resetting the engine, which discards the ring and
        resets its pointers.

        Note what this fixes about the old code: hard_panic() called
        start_engine() only when running() was False, so on a device that
        reported itself running while misbehaving - the exact state the
        user kept hitting - it never attempted a reset at all. That is why
        panic never once brought the unit back."""
        with _IO_LOCK:
            if reset_engine:
                # Only touch the system mode if the engine is genuinely
                # not executing. A forced mode bounce on a HEALTHY device
                # wedges it - measured: it left a working unit at 0x0001,
                # needing the power switch. So diagnose first, and treat
                # the mode as a last resort rather than a routine step.
                executing, r = True, {}
                try:
                    executing, r = hs4u.engine_executing(self.hs)
                except Exception as e:
                    print(f"    [recover] execution check failed: {e}")
                ms = (r.get("measured_s") or 0) * 1000
                print(f"    [recover] engine executing: {executing} "
                      f"({ms:.0f}ms measured, healthy is ~107ms, "
                      f"a stopped engine returns ~4ms)")
                if not executing:
                    print("    [recover] engine is NOT executing - forcing "
                          "a system-mode bounce...")
                    try:
                        ok = self.hs.reset_engine()
                        print(f"    [recover] mode recovered: {ok}")
                    except Exception as e:
                        print(f"    [recover] engine reset FAILED: {e}")
                        print("    [recover] power-cycle the HardSID.")
                        return False
                else:
                    print("    [recover] engine is fine; not touching the "
                          "mode. Re-arming only.")
            print(f"    [recover] re-arming socket(s) "
                  f"{sorted(self.enabled_chips)}...")
            try:
                self.hs.init(chips=tuple(sorted(self.enabled_chips)))
            except Exception as e:
                print(f"    [recover] re-arm FAILED: {e}")
                return False
            for ch in self.channels:
                if ch is None:
                    continue
                for v in ch.voices:
                    v.state = "free"
                    v.note = -1
                ch._bend_dirty = False
                try:
                    ch._write_chip_globals()  # init() never restores 0x18
                except Exception as e:
                    print(f"    [recover] globals restore failed: {e}")
            if _FORENSICS:
                # init() flushes several blocks the recorder never sees.
                _FORENSICS.expected_wr = None
            # Verify rather than assume. "Recovered" previously meant
            # "we sent the recovery writes", which was true even when the
            # engine was still discarding every one of them.
            try:
                ok, r = hs4u.engine_executing(self.hs)
                ratio = r.get("ratio")
                print(f"    [recover] engine executing: {ok} "
                      f"(ratio {ratio if ratio is None else round(ratio, 3)})")
                if _FORENSICS:
                    _FORENSICS.record("e", f"recover: executing={ok} "
                                           f"ratio={ratio}")
                    _FORENSICS.expected_wr = None
                if not ok:
                    print("    [recover] STILL NOT EXECUTING - power-cycle "
                          "the HardSID.")
                    return False
            except Exception as e:
                print(f"    [recover] execution check failed: {e}")
            print("    [recover] done - play something.")
            return True

    def __call__(self, event, data=None):
        message, _delta = event
        if not message:
            return
        if _FORENSICS:
            _FORENSICS.record("m", bytes(message).hex(" "))
        status = message[0] & 0xF0
        midi_channel = message[0] & 0x0F
        if midi_channel > 3:
            return  # only channels 1-4 map to the 4 sockets
        ch = self.channels[midi_channel]
        if ch is None:
            return  # this chip is disabled via --chips
        try:
            if status == 0x90 and len(message) >= 3:
                note, vel = message[1], message[2]
                if vel > 0:
                    ch.note_on(note, vel)
                    if self.verbose:
                        print(f"  ch{midi_channel + 1} note on  {note:3d}")
                else:
                    ch.note_off(note)
                    if self.verbose:
                        print(f"  ch{midi_channel + 1} note off {note:3d}")
            elif status == 0x80 and len(message) >= 3:
                ch.note_off(message[1])
                if self.verbose:
                    print(f"  ch{midi_channel + 1} note off {message[1]:3d}")
            elif status == 0xB0 and len(message) >= 3:
                cc, value = message[1], message[2]
                if cc in (CC_PANIC, CC_ALL_NOTES_OFF):
                    now = time.perf_counter()
                    if now - self._last_panic < 0.3:
                        # A single "press" from a DAW/controller can send
                        # the same CC many times in a row (observed: 4-14
                        # repeats per press). hard_panic() does 5 separate
                        # immediate writes internally - back-to-back
                        # repeats can stack into exactly the kind of burst
                        # that overruns the ring, i.e. the panic button
                        # causing the next failure. Collapse repeats.
                        if self.verbose:
                            print("  [PANIC] debounced (repeat within 300ms)")
                    else:
                        self._last_panic = now
                        self.hard_panic()
                        rd, wr, st, free = self.hs.state()
                        print(f"  [PANIC] all 4 chips hard-silenced  "
                              f"state={st:#06x} free={free} rd={rd:#06x} wr={wr:#06x}")
                else:
                    ch.control_change(cc, value)
                    if self.verbose:
                        print(f"  ch{midi_channel + 1} cc {cc:3d} = {value:3d}")
            elif status == 0xE0 and len(message) >= 3:
                value14 = message[1] | (message[2] << 7)
                ch.pitch_bend(value14)
                if self.verbose:
                    print(f"  ch{midi_channel + 1} bend {value14:5d}")
        except BusBusy as e:
            # Never swallow this one. The write did NOT go out, precisely
            # so that the command stream stays intact - but that means the
            # device's idea of what is sounding is now stale, and a
            # dropped note-off would hang. Recover.
            self.on_bus_fault(e)
        except Exception as e:  # keep the callback thread alive mid-performance
            print(f"  [midi error] {type(e).__name__}: {e}")

    def hard_panic(self, reset_engine=False):
        """Zero every SID register on every chip immediately, regardless
        of tracked voice state. This is the real emergency stop -
        ChipChannel.all_notes_off() only touches voices Python *thinks*
        are active, which is exactly the kind of state that a corrupted
        ring can throw out of sync with hardware reality.

        reset_engine=True escalates to a full device reset (see
        recover()). That is what SPACE now does, because the plain
        register sweep provably could not fix the failures the user hit:
        if the device has stopped reading our stream as register writes,
        writing more registers cannot reach it. Reset first, ask
        questions later - the cost is a few hundred milliseconds and any
        sounding note, both of which you were losing anyway by reaching
        for the panic button.

        Holds _IO_LOCK for its whole body - this does several separate
        device transfers that need to be atomic with respect to the
        bend-coalescing and health-monitor threads, which share the same
        libusb device handle."""
        with _IO_LOCK:
            if reset_engine:
                return self.recover(reset_engine=True)

            pairs = [(chip, reg, 0) for chip in self.enabled_chips
                     for reg in range(0x19)]
            try:
                write_regs_now(self.hs, pairs)
            except BusBusy as e:
                # The sweep itself couldn't go out safely, so the device
                # is in the state only a reset clears. Escalate rather
                # than report a panic that did nothing.
                print(f"  [PANIC] register sweep blocked: {e}")
                return self.recover(reset_engine=True)
            for ch in self.channels:
                if ch is None:
                    continue
                for v in ch.voices:
                    v.state = "free"
                    v.note = -1
                ch._bend_dirty = False

            if not self.hs.running():
                print("  [PANIC] engine not running - escalating to reset")
                return self.recover(reset_engine=True)

            # Re-arm every socket. A de-armed socket silently discards
            # every register write (see docs/protocol.md section 17) -
            # which looks exactly like "ring/engine report perfectly
            # healthy, but not even this zero-sweep produced a click."
            # Uses hs4u.py's own buffered init() (paced, ring-space-
            # checked), not the real-time path - this is a recovery
            # action, not a performance event, so a few hundred ms is an
            # acceptable cost for actually being reliable.
            # NB: chip_init_stream() touches 0x15-0x17 as part of its
            # probe sequence, so this must run BEFORE the globals restore
            # below.
            print(f"  [PANIC] re-arming socket(s) "
                  f"{sorted(self.enabled_chips)}...")
            self.hs.init(chips=tuple(sorted(self.enabled_chips)))

            for ch in self.channels:
                if ch is None:
                    continue
                ch._write_chip_globals()  # the zero-sweep (and the
                                           # re-arm's own probe sequence)
                                           # also hit 0x15-0x18; restore
                                           # volume/filter
            return True

    def sid_hard_reset(self):
        """SID-LEVEL silence: TEST bit + zeroed envelopes on every voice.

        This is the rung that separates "the HardSID is deaf" from "the
        SID chip is wedged", and nothing else in the ladder can tell them
        apart.

        Setting bit 3 (TEST) of a voice's control register halts and
        resets that voice's oscillator and holds it at zero - it is the
        SID's own reset line for a voice, and it silences the voice
        regardless of what the envelope generator is doing. Zeroing
        attack/decay/sustain/release at the same time collapses the
        envelope.

        Why suspect the chip at all: every forensic dump says the bus is
        perfect - mode 1 acknowledged, ring draining, zero pointer slips,
        MIDI balanced, gate-off written to every voice - and yet a note
        sounds forever. One explanation left standing is the 6581/8580
        ADSR delay bug: the envelope generator's rate counter can wedge if
        the gate is retriggered at the wrong moment, and note_on() pulses
        gate low then high again only ~48 cycles later, which is precisely
        the condition real SID players avoid with a "hard restart" (zero
        the envelope and hold gate low for a whole frame before the new
        note). ACID64 plays real tunes, which all do this; we do not.

        If this rung silences a hanging note, the device was never deaf
        and the fix belongs in note_on(). If it does nothing, the chip is
        not the problem and the device really is discarding writes."""
        print("  [X] SID-level hard reset: TEST bit + zero envelopes")
        pairs = []
        for ch in self.channels:
            if ch is None:
                continue
            for v in ch.voices:
                b = hs4u.VOICE_BASE[v.voice]
                pairs += [
                    (ch.chip, b + hs4u.R_AD, 0x00),
                    (ch.chip, b + hs4u.R_SR, 0x00),
                    (ch.chip, b + hs4u.R_CONTROL, hs4u.TEST),
                ]
        try:
            write_regs_now(self.hs, pairs)
        except BusBusy as e:
            print(f"    {e}")
            return False
        time.sleep(0.05)
        print("  [X] TEST bit set - is the sound GONE?")
        time.sleep(1.2)
        # Release TEST so the voices can be used again.
        clear = []
        for ch in self.channels:
            if ch is None:
                continue
            for v in ch.voices:
                b = hs4u.VOICE_BASE[v.voice]
                clear.append((ch.chip, b + hs4u.R_CONTROL, 0x00))
                v.state = "free"
                v.note = -1
        try:
            write_regs_now(self.hs, clear)
        except BusBusy as e:
            print(f"    {e}")
        return True

    def test_tone(self, note=69, hold=0.35):
        """Play one short note on every enabled chip, independent of any
        DAW or keyboard.

        This is the measuring instrument for the recovery ladder. When the
        unit goes silent, the useful question is not "is it broken" but
        "which layer is broken", and each layer has a different fix:

            V then T sounds -> master volume (0x18) was lost
            A then T sounds -> the sockets were de-armed
            R then T sounds -> the engine or ring was wedged
            none of them, state 0x0081 -> something else entirely
            state 0x0001 -> the engine was stopped; power cycle

        Being able to ask that at the moment of failure, without touching
        the DAW, is worth more than any amount of after-the-fact
        reasoning about ring pointers."""
        rd, wr, st, free = self.hs.state()
        mode, ack = self.hs.system_mode()
        print(f"  [T] test tone on chip(s) {sorted(self.enabled_chips)}  "
              f"state={st:#06x} mode={mode} ack={ack} free={free}")
        # Is the engine still EXECUTING, or only accepting bytes? Every
        # dump so far shows rd trailing wr by exactly one block, which
        # looks identical whether the engine is idling healthily or has
        # stopped executing entirely. A delay probe distinguishes them:
        # it puts ~8.5s of pure delay in the ring and watches whether the
        # device actually takes that long to consume it.
        # Use the calibrated engine_executing() (100000 cycles, ~101ms)
        # and NOTHING heavier. This used to call measure_delay_rate() AND
        # delta_probe(); the latter injects 128 x 0xFFFF delays - about
        # 8.5 SECONDS of content - into a live performance's ring. During
        # the six-hour endurance run that made the ring-stall watchdog
        # fire three times on a perfectly healthy device, and each alert
        # wrote a 4.3MB dump. The probe was the stall it was reporting.
        #
        # measure_delay_rate() also still carries the status-read race
        # that full_drain_time() was fixed for, and it duly reported
        # "ratio 0.01x" - the stall signature - on a device that was fine.
        with _IO_LOCK:
            try:
                ok, r = hs4u.engine_executing(self.hs)
                ms = (r.get("measured_s") or 0) * 1000
                msg = (f"engine executing: {ok}  ({ms:.0f}ms measured, "
                       f"ratio {r.get('ratio') or 0:.2f}; healthy is ~1.0 "
                       f"idle or ~0.4-0.5 mid-performance, a stall ~0.01)")
                print(f"  [T] {msg}")
                if _FORENSICS:
                    _FORENSICS.record("e", msg)
                    _FORENSICS.expected_wr = None
                self._last_probe = time.perf_counter()
            except Exception as e:
                print(f"  [T] execution check failed: {type(e).__name__}: {e}")
        for ch in self.channels:
            if ch is None:
                continue
            try:
                ch.note_on(note, 100)
            except BusBusy as e:
                print(f"    chip {ch.chip}: {e}")
                continue
            time.sleep(hold)
            try:
                ch.note_off(note)
            except BusBusy as e:
                print(f"    chip {ch.chip}: {e}")
        print("  [T] done - did you hear it?")

    def all_notes_off(self):
        for ch in self.channels:
            if ch is not None:
                try:
                    ch.all_notes_off()
                except BusBusy as e:
                    print(f"  [all notes off] {e}")


def watchdog_loop(hs, dispatcher, stop_event, outdir, interval=0.05,
                   exec_interval=8.0, auto_recover=False):
    """Poll the device's run bit and dump the recorder the instant it dies.

    This is the piece that makes the failure investigable. The user cannot
    press a key fast enough: by the time silence registers and a hand
    moves, the writes that caused it have scrolled out of any reasonable
    buffer. A 20 Hz status read costs one 64-byte bulk read - about 0.8ms
    under the I/O lock - and catches the transition within 50ms of it
    happening.

    It also answers the one question that decides what to fix next:

        state 0x0001 -> the engine was STOPPED. Something on the wire was
                        read as the engine toggle. The bytes are in the
                        dump.
        state 0x0081 -> the engine is still running and the silence has
                        another cause entirely (de-armed sockets, master
                        volume, a wedged ring). Completely different bug.
    """
    was_running = True
    stalled_since = None
    last_exec_check = time.perf_counter()
    last_stall_dump = 0.0
    while not stop_event.wait(interval):
        try:
            with _IO_LOCK:
                rd, wr, st, free = hs.state()
        except Exception as e:
            if _FORENSICS:
                _FORENSICS.record("x", f"status read failed: "
                                       f"{type(e).__name__}: {e}")
            continue

        # The failure that actually happens does NOT clear the run bit -
        # the device keeps reporting mode 1 acknowledged while its engine
        # has stopped executing the stream. Only a delay measurement sees
        # it. Run one whenever nothing is sounding, so a performance is
        # never interrupted by the check itself.
        now = time.perf_counter()
        if (exec_interval > 0 and (now - last_exec_check) >= exec_interval
                and dispatcher is not None):
            idle = all(v.state == "free"
                       for ch in dispatcher.channels if ch is not None
                       for v in ch.voices)
            if idle:
                last_exec_check = now
                try:
                    with _IO_LOCK:
                        ok, r = hs4u.engine_executing(hs)
                except Exception as e:
                    ok, r = True, {"error": str(e)}
                if not ok:
                    ratio = r.get("ratio")
                    msg = (f"ENGINE STOPPED EXECUTING: "
                           f"{r.get('measured_s', 0) * 1000:.0f}ms measured "
                           f"vs {r.get('expected_s', 0) * 1000:.0f}ms "
                           f"expected (ratio "
                           f"{ratio if ratio is None else round(ratio, 3)})")
                    print(f"\n*** WATCHDOG: {msg}")
                    print("    This is the real failure. Register writes are "
                          "being discarded, not executed.")
                    if _FORENSICS:
                        _FORENSICS.record("e", f"WATCHDOG: {msg}")
                        _FORENSICS.expected_wr = None
                        _FORENSICS.dump(hs, "engine stopped executing", outdir)
                    if auto_recover:
                        print("    attempting forced recovery...")
                        dispatcher.recover(reset_engine=True)
                if _FORENSICS:
                    _FORENSICS.expected_wr = None

        running = bool(st & 0x80)
        if was_running and not running:
            print(f"\n*** WATCHDOG: engine run bit CLEARED "
                  f"(state={st:#06x} rd={rd:#06x} wr={wr:#06x} free={free})")
            print("    The engine has been STOPPED - this is the "
                  "power-cycle failure, caught live.")
            if _FORENSICS:
                _FORENSICS.record("e", f"WATCHDOG: run bit cleared, "
                                       f"state={st:#06x}")
                _FORENSICS.dump(hs, "engine run bit cleared", outdir)
        elif running and not was_running:
            print(f"\n*** WATCHDOG: engine run bit is set again "
                  f"(state={st:#06x})")
        was_running = running

        # A ring that stops draining while the engine claims to be running
        # is the other failure shape worth catching automatically.
        used = hs4u.RING - free
        # Our own probes legitimately park content in the ring, so ignore
        # the stall test for a while after one. Without this the watchdog
        # reports the probe it just issued as a fault.
        since_probe = time.perf_counter() - getattr(dispatcher, "_last_probe", 0.0)
        if running and used > 2 * hs4u.BLOCK and since_probe > 12.0:
            if stalled_since is None:
                stalled_since = time.perf_counter()
            elif time.perf_counter() - stalled_since > 2.0:
                print(f"\n*** WATCHDOG: ring not draining for 2s while the "
                      f"engine reports running (used={used})")
                if _FORENSICS:
                    _FORENSICS.record("e", f"WATCHDOG: ring stalled, "
                                           f"used={used}")
                    _FORENSICS.dump(hs, "ring stalled while running", outdir)
                stalled_since = None
        else:
            stalled_since = None


def health_monitor_loop(hs, dispatcher, stop_event, interval=45.0):
    """Background thread: periodically checks whether the engine is
    genuinely honoring delta-timed delays, not just reporting itself as
    'running'. Built after two rigorous endurance tests (heavy polyphony,
    and separately heavy panic-button hammering - see project memory)
    failed to reproduce a real user-reported failure under scripted load,
    meaning it needs something only a real Ableton session produces. This
    catches it live, with the actual honored/not-honored evidence, the
    next time it happens - instead of inferring from ring-state snapshots
    that have repeatedly turned out to be insufficient on their own.

    The probe itself sends pure delay commands, no register writes, so it
    never interrupts currently-sounding notes. Device I/O is shared with
    the main dispatch thread via _IO_LOCK.
    """
    while not stop_event.wait(interval):
        with _IO_LOCK:
            result = hs4u.delta_probe(hs)
        if result["honored"]:
            continue

        print(f"\n*** HEALTH MONITOR: engine not honoring delays "
              f"(recovered after {result['recovered_after'] * 1000:.0f}ms, "
              f"should take ~8.5s) ***")
        print("    attempting recovery via engine reset...")
        dispatcher.recover(reset_engine=True)
        with _IO_LOCK:
            retest = hs4u.delta_probe(hs)
        if retest["honored"]:
            print("    RECOVERY WORKED - hard_panic()'s re-arm fixed the "
                  "delta-timing failure. This is the first confirmation "
                  "either way; worth remembering.")
        else:
            print("    RECOVERY FAILED - hard_panic() did not restore "
                  "delta timing. A power cycle is likely needed.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port-name", default="HardSID4U",
                     help="virtual MIDI destination name (default HardSID4U)")
    ap.add_argument("--bend-range", type=float, default=2.0,
                     help="pitch bend range in semitones (default 2.0)")
    ap.add_argument("--health-interval", type=float, default=0.0,
                     help="seconds between background engine-health probes, "
                          "0 to disable (default 0 = OFF). Off by default "
                          "now: the probe holds the device I/O lock for a "
                          "full 2 seconds, during which every note-on and "
                          "note-off blocks. On a live instrument that is "
                          "indistinguishable from a hanging note, so the "
                          "diagnostic was a plausible source of the symptom "
                          "it was built to catch. Enable it for endurance "
                          "testing, not for playing.")
    ap.add_argument("--chips", default="1,2,3,4",
                     help="comma-separated channel/socket numbers (1-4) to "
                          "enable; any not listed get ZERO writes ever, "
                          "including startup arming and panic (default: "
                          "all 4). For isolating a suspect chip during "
                          "hardware testing, e.g. --chips 1,2")
    ap.add_argument("--trace-io", action="store_true",
                     help="log ring pointer state (rd/wr/free/state) after "
                          "EVERY write, not just periodic health probes - "
                          "for correlating a failure with a specific write "
                          "or ring wraparound. Verbose; use for a targeted "
                          "repro, not routine play.")
    ap.add_argument("--forensics", type=int, default=8000,
                     help="size of the rolling flight recorder in events "
                          "(default 8000, 0 to disable). Records every MIDI "
                          "message in, every write with its head bytes and "
                          "ring pointers, and every blocked or failed "
                          "transfer. Dumped automatically the moment the "
                          "engine's run bit clears, or on demand with the D "
                          "key.")
    ap.add_argument("--forensics-dir", default=None,
                     help="where to write dump files (default: current "
                          "directory)")
    ap.add_argument("--watchdog-interval", type=float, default=0.05,
                     help="seconds between device run-bit polls (default "
                          "0.05 = 20Hz, 0 to disable). Cheap: one 64-byte "
                          "bulk read.")
    ap.add_argument("--exec-interval", type=float, default=8.0,
                     help="seconds between engine-execution checks (default "
                          "8, 0 to disable). Measures whether delta-delays "
                          "still take time - the ONLY check that detects the "
                          "real failure, since the device keeps reporting "
                          "mode 1 acknowledged with the ring draining "
                          "normally while discarding everything we send. "
                          "Only runs when no note is sounding.")
    ap.add_argument("--auto-recover", action="store_true",
                     help="on detecting a stopped engine, immediately attempt "
                          "the forced mode-bounce recovery instead of just "
                          "reporting it")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    global _TRACE_IO, _FORENSICS
    _TRACE_IO = args.trace_io
    if args.forensics > 0:
        _FORENSICS = Forensics(capacity=args.forensics)

    try:
        enabled_channels = sorted({int(x) for x in args.chips.split(",")})
    except ValueError:
        print(f"--chips must be a comma-separated list of 1-4, got: {args.chips!r}")
        sys.exit(1)
    if not enabled_channels or any(c < 1 or c > 4 for c in enabled_channels):
        print(f"--chips values must each be between 1 and 4, got: {args.chips!r}")
        sys.exit(1)
    enabled_chips = tuple(c - 1 for c in enabled_channels)

    try:
        import rtmidi
    except ImportError:
        print("python-rtmidi is required: uv sync --extra midi")
        sys.exit(1)

    def _handle_term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_term)

    print(f"hs4u.py v{hs4u.VERSION}")
    hs = hs4u.HardSID4U(verbose=args.verbose)
    hs.open()
    dispatcher = None
    midi_in = None
    monitor_thread = None
    monitor_stop = threading.Event()
    old_termios = None
    try:
        print(f"[init] arming socket(s) {list(enabled_channels)}...")
        hs.init(chips=enabled_chips)

        dispatcher = MidiToSid(hs, bend_range=args.bend_range,
                                verbose=args.verbose, enabled_chips=enabled_chips)
        midi_in = rtmidi.MidiIn()
        midi_in.ignore_types(sysex=True, timing=True, active_sense=True)
        midi_in.set_callback(dispatcher)
        midi_in.open_virtual_port(args.port_name)
        print(f'[midi] virtual destination "{args.port_name}" open.')
        print("       MIDI channels 1-4 -> sockets 0-3, 3-voice polyphony each.")
        if len(enabled_chips) < 4:
            disabled = sorted(set(range(1, 5)) - set(enabled_channels))
            print(f"       Channel(s) {disabled} DISABLED via --chips - "
                  f"zero writes will ever reach those sockets.")

        if args.health_interval > 0:
            monitor_thread = threading.Thread(
                target=health_monitor_loop,
                args=(hs, dispatcher, monitor_stop, args.health_interval),
                daemon=True, name="health-monitor")
            monitor_thread.start()
            print(f"[health] background engine probe every "
                  f"{args.health_interval:.0f}s")

        if args.watchdog_interval > 0:
            watchdog_thread = threading.Thread(
                target=watchdog_loop,
                args=(hs, dispatcher, monitor_stop, args.forensics_dir,
                      args.watchdog_interval, args.exec_interval,
                      args.auto_recover),
                daemon=True, name="watchdog")
            watchdog_thread.start()
            print(f"[watchdog] run-bit poll every "
                  f"{args.watchdog_interval * 1000:.0f}ms; auto-dumps the "
                  f"recorder the moment the engine stops")
        if _FORENSICS:
            print(f"[forensics] recording last {args.forensics} bus events "
                  f"-> {os.path.abspath(args.forensics_dir or os.getcwd())}")

        if _HAS_TTY_SUPPORT and sys.stdin.isatty():
            old_termios = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            print("       When it dies, work DOWN this ladder and note "
                  "which rung brings sound back:")
            print("         D = dump the flight recorder (do this FIRST)")
            print("         T = test tone + delta probe (is the engine even "
                  "executing?)")
            print("         X = SID-LEVEL reset: TEST bit + zero envelopes")
            print("             ^ the key rung: if this silences a hanging")
            print("               note, the chip was wedged, not the device")
            print("         V = restore volume/filter only, then test tone")
            print("         A = re-arm sockets (no engine reset), then test")
            print("         R = full recovery, then auto-dumps again")
            print("       SPACE = panic (silence + re-arm)")
            print("       Ctrl+C to stop.")
        else:
            print("       Ctrl+C to stop. (stdin isn't a terminal - "
                  "key commands unavailable; use CC102/123 instead.)")

        while True:
            if old_termios is not None:
                ready, _, _ = select.select([sys.stdin], [], [], 0.5)
                if not ready:
                    continue
                key = sys.stdin.read(1)
                if key == " ":
                    print("\n[space] panic triggered")
                    dispatcher.hard_panic()
                elif key in ("d", "D"):
                    if _FORENSICS:
                        print("\n[D] dumping flight recorder")
                        _FORENSICS.dump(hs, "manual (D key)",
                                        args.forensics_dir)
                    else:
                        print("\n[D] forensics disabled (--forensics 0)")
                elif key in ("t", "T"):
                    if _FORENSICS:
                        _FORENSICS.record("e", "LADDER: T (test tone)")
                    dispatcher.test_tone()
                elif key in ("x", "X"):
                    if _FORENSICS:
                        _FORENSICS.record("e", "LADDER: X (SID hard reset)")
                    # SID-level reset. Put this early in the ladder: it is
                    # the only rung that can tell a wedged chip from a
                    # deaf device.
                    dispatcher.sid_hard_reset()
                    dispatcher.test_tone()
                elif key in ("v", "V"):
                    # Rung 1 of the ladder: just re-push volume/filter.
                    print("\n[V] restoring chip globals (volume/filter) only")
                    if _FORENSICS:
                        _FORENSICS.record("e", "LADDER: V (restore globals)")
                    for c in dispatcher.channels:
                        if c is not None:
                            try:
                                c._write_chip_globals()
                            except BusBusy as e:
                                print(f"    {e}")
                    dispatcher.test_tone()
                elif key in ("a", "A"):
                    # Rung 2: re-arm the sockets, no engine reset.
                    print("\n[A] re-arming sockets (no engine reset)")
                    if _FORENSICS:
                        _FORENSICS.record("e", "LADDER: A (re-arm)")
                    dispatcher.recover(reset_engine=False)
                    dispatcher.test_tone()
                elif key in ("r", "R"):
                    # The power-switch replacement. Resets the engine,
                    # which discards the ring and resets its pointers -
                    # the only action that can clear a command stream
                    # that has drifted off its 16-bit word boundary, and
                    # the one thing the old panic never attempted because
                    # it gated start_engine() on running() being False.
                    print("\n[R] full recovery: engine reset + re-arm")
                    if _FORENSICS:
                        _FORENSICS.record("e", "LADDER: R (engine reset)")
                    dispatcher.recover(reset_engine=True)
                    dispatcher.test_tone()
                    if _FORENSICS:
                        # Dump AFTER the ladder so its writes are in the
                        # record. The first dump (D) captures the failure;
                        # this one captures what the recovery attempts
                        # actually put on the wire, which was missing from
                        # every dump so far.
                        _FORENSICS.dump(hs, "after recovery ladder",
                                        args.forensics_dir)
            else:
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if old_termios is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_termios)
        monitor_stop.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=2.0)
        if midi_in is not None:
            midi_in.close_port()
        if dispatcher is not None:
            dispatcher.stop()
            dispatcher.all_notes_off()
        hs.close()


if __name__ == "__main__":
    main()
