# How this was worked out

A record of the actual path, including the wrong turns. The failures are the
useful part: they show which kinds of inference held up against hardware and
which did not.

## What we started with

A HardSID 4U Studio Edition with two 6581s and two 8580s, its 2008 manuals,
the Windows driver (`hardsidusb.sys` / `.inf`), the VSTi (`hardsid4u.dll`),
and later the API DLL (`hardsid.dll` v3.02) and the ACID64 console player
source with its bundled `hardsid_usb.dll`.

## Phase 1 — static analysis

This went well and got a lot right.

- `hardsidusb.sys` still carried its build path:
  `c:\winddk\3790.1830\src\wdm\usb\isousb\sys\objfre_wxp_x86\`. It is a
  near-stock build of Microsoft's WDK **isousb** sample, so the driver adds
  almost no proprietary logic and could be replaced outright.
- `hardsid4u.dll` turned out to be a SynthEdit VSTi, not the API — a dead end
  for protocol work.
- `hardsid.dll` gave up the device interface GUID
  `{D838B33A-F0BE-4765-996F-FBC3966EBFA7}`, the `PIPE00`/`PIPE01` naming, the
  IOCTL codes, the 512-byte flush quantum, the `0xEE`/`0xEF` delay encoding,
  the `(chip << 5) | reg` command byte, and the ring-buffer free-space
  formula.
- `hardsid_usb.dll` was UPX-packed with LZMA. It unpacked with plain Python
  (`lzma.FORMAT_RAW`, `lc=3 lp=0 pb=0`, offset `0x402`) and proved to be Rust
  with libusb statically linked.

Everything in that list survived contact with hardware.

## Phase 2 — inference that failed

Three theories were derived from the same binaries and were wrong:

1. **A `0xF0` control-command family with a reset ritual.** Read out of call
   sequences. A proper disassembly of `HardSID_Reset` showed a plain loop
   zeroing registers `0x00`–`0x17`. No such commands exist.
2. **`FFFF` + `00 mm` as a system-mode escape sequence.** Sending it did not
   set a mode; it cleared a state bit and stopped the device.

   > **This entry is wrong, and it cost the project months — see Phase 9.**
   > The theory was right. The observation was right too: the state bit that
   > cleared was the *acknowledge* bit, which is exactly what a mode-set does
   > before the device acknowledges it. What failed was the interpretation —
   > "the device stopped" — and on that basis a correct finding was filed
   > under failures and never revisited. Everything needed to diagnose the
   > hanging-note failure was already on this page.
3. **Vendor control requests on EP0 doing socket enable.** The capture showed
   only `GET_DESCRIPTOR` and `SET_CONFIGURATION`. There are no vendor control
   requests at all.

The pattern: **wire formats recovered from a stripped binary were reliable;
state machines and command semantics inferred from call sequences were not.**

With hindsight, that pattern is half wrong too — theory 2 was a correct
semantic inference, discarded because the experiment that confirmed it was
misread. Two of three "failures" were real failures; one was a success we
threw away.

## Phase 3 — bring-up on hardware

Descriptors first, which immediately corrected two assumptions: there are four
endpoints, not two, and there is only one alt setting, so the usual
isochronous alt-setting dance does not apply.

Then a long stretch of bisection by ear. Confirmed on hardware:

- BULK OUT `0x02` is the command pipe; writes must be 512-byte multiples
- the ring is 8192 bytes at `0x2000`–`0x3FFF` and the DLL's free-space formula
  is correct
- command words are little-endian, data byte first — proved by a block of
  maximum-value delay pairs jamming the ring for minutes while filler drained
  instantly
- timing is delta-timed, not a fixed 8 kHz tick

And then: nothing made a sound, for hours.

## Phase 4 — the trap

We invented an engine-start step: a 4-byte `ff ff 01 00` short packet plus
filler blocks, alternated until `status[0x1E]` bit 7 came up.

It worked, in the sense that bit 7 set, the device consumed data, and the ring
pointers advanced. Every diagnostic reported a healthy running engine.

The sockets were never armed. The instrument said green while the thing under
test was dead.

This produced a run of contradictory results that consumed most of the
session. Byte-identical init streams appeared to behave differently depending
on whether they came from a file or a generator. Pass counts appeared to
matter, then not. A prefix in the capture appeared essential, then was shown
to contain no command the generated version lacked. Each explanation fitted
the data and each was wrong, because the real variable — whether the device
had been armed by a previous run of the vendor software — was invisible.

## Phase 5 — capture

Two captures settled everything that months of staring at disassembly would
not have.

The first, of ACID64 playing a tune, confirmed the entire command format at a
stroke and ruled out EP0 vendor requests.

The second was the decisive one: **ACID64 from a cold-started device**. Its
very first write is a 512-byte block —

```
ff ff 01 00  followed by 508 zero bytes
```

Not a short packet. Padded with zeros, not with `0xFF` filler. We had the four
magic bytes right and the framing wrong the whole time.

A third capture, of our own script before and after a power cycle, made the
trap visible: the runs that sang sent no start command at all, because ACID64
had already armed the device.

## What generalises

- Recover wire formats from binaries; get state machines from captures.
- A green diagnostic that you designed yourself is not evidence. Bit 7 said
  "running" because we chose to read it that way.
- When A/B results are inconsistent, suspect hidden state before suspecting
  the bytes. Byte-identical inputs behaving differently is not a paradox, it
  is a message.
- Capture earlier. The two captures took under an hour and answered questions
  that days of inference had not.

## Phase 6 — MIDI performance

With `hs4u.py` working and the protocol fully documented, the next question
was whether the device could be played live from a DAW rather than (or
before) building a SID-file player — the latter needs real 6502 emulation,
the former just needs a fast enough path from a MIDI event to a register
write, because the SID's own hardware ADSR already does the envelope.

### The transport question, settled early

`midi_latency_probe.py` measures a single immediate 512-byte block — one
note's freq+gate write, padded with `0xFF` filler — bypassing `hs4u.py`'s
buffered, delay-scheduled `flush()` entirely. That buffered path is built
for pre-scheduled playback (the whole timeline known in advance); a MIDI
performance needs the opposite, react *now*.

Over 200 note on/off pairs: median write latency 0.8ms, p95 under 1ms, and
the ring's free space held constant under sustained rapid triggering rather
than growing a backlog. The transport was never the risk.

### The acoustic latency trap

The real question was whether a fast `bulkWrite()` return means the chip
actually makes sound fast. `audible_latency_probe.py` records the analog
output on a mic and compares detected onsets against trigger timestamps to
find out.

First pass, MacBook's built-in mic: 0/30 detected — a threshold-tuning
problem, not a missing-signal one. Once tuned in, it reported a suspiciously
uniform **~100ms** across all 30 notes. The same test against an external
CM-15 USB mic gave a different, also internally consistent number:
**~65-70ms**.

Two different absolute numbers for the identical physical signal is the
tell. If the HardSID's own output had a fixed lag, both mics would have
agreed — they didn't, so each number was measuring the microphone, not the
device (the built-in mic runs real-time noise-suppression DSP; the USB mic
has its own ADC/USB round-trip). Neither number was trustworthy alone; the
*disagreement between them*, read alongside the sub-millisecond transport
result, was what actually ruled out the SID as the source of the delay.

This is Phase 4's trap one level up the stack: a measurement that looks
clean and internally consistent is not evidence, if the thing doing the
measuring hasn't itself been isolated from what's being measured. A fully
clean number would need a direct line-level cable into an audio interface
plus a calibrated loopback reference — not yet done.

### midi.py

`midi.py` maps MIDI channels 1-4 to chips 0-3 (3-voice polyphony per
channel, matching real hardware voice count), translates CCs to the SID
register map (waveform/ADSR/pulse-width per voice; filter/volume per chip —
shared across all notes on a channel, a hardware constraint, not a design
choice), and drives everything through the same immediate-block write path
the transport probe validated. It never touches `hs4u.py`'s buffered
`flush()`, which stays reserved for one-time socket arming and the
still-unbuilt SID-file player.

Confirmed live from Ableton Live: notes, CC (filter sweep), and pitch bend
all working, no 6502 emulation anywhere in the path.

### What generalises (continued)

- A measurement chain has to be validated against itself before its output
  can be trusted. Two independent listeners disagreeing is more informative
  than either one agreeing with what you expected to see.
- Real-time and pre-scheduled are different problems even on the same wire
  protocol. Conflating them — routing a live event through the ring behind
  an already-queued long delay — would reintroduce exactly the kind of
  hidden state that caused Phase 4.

## Phase 7 — the intermittent-failure investigation

Live play from Ableton kept failing in ways that got progressively harder to
explain: faint or absent volume, then ghost notes, then a hung note, then
total silence. Each fix looked sufficient until the next failure. This phase
is the record of chasing that, including the false trails — which, as in
Phase 4, are the useful part.

### The fixes that were real

Eight genuine bugs in `midi.py` were found and fixed along the way, each
confirmed on hardware: master volume never pushed at startup (register `0x18`
is untouched by `init()`); filter routing hard-wired on with no way off;
**no ring flow control on the real-time write path** (a chord could overrun
the ring, silently corrupting the device's word alignment — exactly the
"delayed and garbled" mode §6 of the protocol notes warns about); voice
stealing that didn't pulse gate-off first (so a stolen voice kept the old
envelope); an all-notes-off panic that only touched voices Python *thought*
were active; CC123 as the panic trigger (which Ableton, like most DAWs,
refuses to send — CC 120-127 are reserved Channel Mode Messages, so CC102 in
the undefined range was added); a panic with no debounce (one DAW "press"
sends the CC many times, each firing five writes, self-inflicting a burst);
and a panic that couldn't revive a genuinely halted engine.

### The theories that hardware killed

Every mechanism inferred from the symptoms was tested and, one by one,
eliminated with data — the same lesson as Phase 2, that symptom-shaped
inference is unreliable:

- Ring overrun under load. Two endurance runs (8 min heavy polyphony; 6 min
  with 40+ panic bursts) with a rigorous engine-timing probe firing
  throughout: zero failures. Free space sat at idle baseline at every
  observed failure — the ring was never congested when things broke.
- Wrap-boundary address, idle time alone, filler injection alone: three A/B
  tests, each isolating one variable, each 0% differential.
- **Failing 6581 hardware.** The strongest-looking theory — the two ageing
  6581s (chips 3/4) are the documented failure-prone ones, and the ghost
  notes did localise to them by both MIDI channel and isolated dry-out. But
  the user then hammered the whole unit for ten minutes with ACID64 playing
  SID files, chips 3/4 included, with zero failures. The silicon is fine.

### What survived, and the real signal

`pointer_slip_test.py` — a tight loop sending filler blocks and comparing the
device's reported write pointer against deterministic byte-counting — found a
real anomaly: **~0.12% of writes advance the pointer by exactly one extra
512-byte block**, as if the device counted one write as two, equally at and
away from the wrap boundary. On a separate long run the transport itself
failed outright (`LIBUSB_ERROR_IO` on a status read) while the device stayed
enumerated. Both are real and unexplained.

The obvious suspect was traffic *shape*: ACID64 streams large multi-block
writes full of real delta-timed content, so the device engine is continuously
busy and the ring stays stocked; our real-time path sends sparse single
512-byte mostly-filler blocks that drain instantly, leaving the engine idle
between events with a status read after every write. `lookahead_ab_test.py`
drove the same notes both ways for fifteen minutes each and counted pointer
mismatches. **The theory was wrong.** Instant 0.082%, scheduled 0.092% —
identical, both at the ~0.12% baseline. Keeping the engine busy changed
nothing. The +512 double-count is intrinsic to this HardSID/libusb/macOS
transport path, not something the host's traffic provokes — so it can't be
tuned away upstream, only detected and corrected for (which the host already
does, resyncing its expected pointer from `wr` after every write).

And the device is healthy: the corrected `stuck_note_diagnosis.py` produced
its low note, silence, and its high note exactly as a working unit should.
The anomaly is cosmetic under normal play. Which leaves the user-visible
live-Ableton failures only *partly* explained — several were our own, now
fixed, `midi.py` bugs; whether any genuine residual device fault remains is
unproven, and pinning it down would need the corrected correlation test
(WAV-logging, latency-compensated) running during an actual failure.

### What generalises (again — and it is about the tools this time)

- A diagnostic can manufacture the failure it claims to find.
  `stuck_note_diagnosis.py` reported "silence after note one, unrecoverable
  except by power cycle" — until review found the script's own recovery step
  zeroed the volume register and never restored it, guaranteeing the silence
  it then attributed to the device. The Phase 4 rule ("a green diagnostic you
  designed yourself is not evidence") has a mirror image: a red one isn't
  either.
- A detector is only as trustworthy as its calibration. The live stuck-note
  check compared a raw audio peak against an envelope-derived threshold, on
  an uncompensated capture stream — so it fired on noise-floor jitter and on
  the note's own hold phase, not on anything wrong with the device.
- Reproduce cheaply. The endurance and correlation runs cost tens of minutes
  each and mostly caught the tooling's own bugs. The pointer-slip loop found
  the one real anomaly in sixty seconds.

## Phase 8 — the off switch

The failure came back. Notes played, everything felt solved, and then: hanging
notes, silence, and once again nothing but the front-panel switch would fix it.
But this time there was a handle on it — *play a note, then move the pitch
wheel, and it dies within seconds, every time.*

Pitch bend is the only gesture that turns a handful of writes per second into
hundreds, so the obvious suspect was load. That died quickly. A standalone
repro pushing the identical register traffic — held notes plus a hard wheel
sweep at 300 messages/second — ran 64,026 writes and 32 MB across four minutes
with all four sockets armed: zero USB errors, zero short writes, the ring never
above one block of 8192. Driving the *real* `midi.py` through a real CoreMIDI
connection, 90,000 bend messages, was just as clean. The 8.5-second delay wall
the health probe injects turned out not to block anything either — `free` never
left 7680 under a flood.

The answer was not load at all. It was four bytes.

The engine start command is a 512-byte block beginning `ff ff 01 00`, and it is
a **toggle**: sent to a running engine, it stops it. A stopped engine
(`0x0001`) cannot be restarted — not by another start block (six attempts,
twelve seconds, state never moves), not by `setConfiguration`, not by a USB
`resetDevice`. Only the switch. Which is precisely the symptom, in the device's
own vocabulary.

Decompose those four bytes into the 16-bit words the protocol actually uses —
data byte first, then command byte:

```
ff ff   FILLER
01 00   write chip 0, register 0x00 (voice 0 frequency low) = 1
```

Neither half is exotic. Both are things the MIDI layer emits constantly. Every
block it builds is padded to 512 bytes with filler, so every block *ends* in
`ff ff`; and every block *began* with a register write. So the tempting
conclusion is that across a block boundary the stream read
`... ff ff | <data> <cmd> ...`, and that whenever that command byte was 0x00
with data 0x01 the instrument was spelling out its own off switch — with the
pitch wheel as the trigger, since a sweep walks `freq & 0xFF` through all 256
values many times a second.

**That conclusion is wrong, and the same test that found the trigger refutes
it.** One of the candidates was precisely this: an all-filler block, then a
block starting `01 00`, producing exactly that adjacency in the ring. Thirty
repeats, no stop. Two more variants of the same shape, no stop. Only the
candidate where all four bytes fell inside *one* 512-byte packet halted the
engine. The trigger is packet-position-based, not stream-adjacency-based.

Which leaves an honest gap. In the MIDI process every transfer is built by one
function, that function puts filler only at the tail, and so no packet it
produces can begin with `ff ff` at all. The kill pattern is real, reproducible,
and catastrophic — and there is still no demonstrated path from `midi.py` to
it. The pitch-bend correlation remains unexplained.

The fix is therefore defence in depth rather than a root-cause repair, and it
costs eight microseconds: every block now opens with a delay word, so blocks
start `08 ee`; a boundary reads `ff ff 08 ee`, and any framing that landed
inside a filler run would read `ff ff ff ff`. Neither can match, so even if the
device's idea of where packets begin ever diverges from the host's — and the
+512 `wr` miscount says its bookkeeping is not perfectly ours — the pattern
cannot form. A runtime guard in both write paths refuses to put a matching
block on the wire at all.

### What generalises

- **The device's control plane and its data plane share an encoding.** There is
  no escaping, no framing bit, nothing marking "this block is a command about
  the engine" versus "this block is music". Two innocuous words placed next to
  each other spell a privileged instruction. When a protocol has that shape,
  the dangerous patterns are not the ones you send deliberately — they are the
  ones your padding forms with your payload.
- **Finding a mechanism is not the same as finding the cause.** The kill
  pattern is proven; the route from this driver to it is not. It was tempting —
  and briefly written down as fact — to close the gap with a boundary-adjacency
  story that the test data already contradicted. The negative result is the
  load-bearing part of that experiment, and it was nearly discarded because the
  positive one was more satisfying.
- **A clean bus is not a working instrument.** The libusb log was spotless
  through every run — errors zero, short writes zero, ring healthy — because
  from the host's side nothing *was* wrong. It sent 512 bytes and the device
  accepted 512 bytes. The device simply obeyed them. Health metrics measure the
  transport, and the failure was in the meaning.
- **The cheap experiment beat every endurance run.** Weeks of thirty-minute
  soak tests, audio correlation, and pointer statistics produced eliminations.
  A script that sent five candidate byte patterns and checked one status bit
  found it on the fourth, in under a minute.

## Phase 9 — the padding was a command

Phase 8 ended honestly stuck: a proven hazard, no path from the driver to it, and
a pitch-bend correlation that made no sense. Then a forensic dump caught the
failure live, and it said something none of the theories allowed.

A note hung forever. Meanwhile: `state=0x0081` (healthy), ring draining, **zero**
pointer slips in 147 writes, no USB errors, MIDI perfectly balanced at 40 note-ons
and 40 note-offs, and the gate-off written to *every* voice — the very last write
in the log being voice 1's gate off. The device had accepted our bytes and ignored
them. Every layer we had instrumented was fine.

Then the user asked the question that ended the investigation: *we reverse
engineered this interface ourselves and wrote protocol.md — did we make a mistake
there?*

We had. Section 4, written months earlier and never acted on:

> `0xFFFF` is not only padding — it also acts as an **escape prefix**. The mode-set
> routine emits the word pair `FF FF` / `00 mm`, then polls until
> `status[0x1E] == (mm | 0x80)`. **The device must be put into SIDPLAY mode before
> register writes behave as documented.**

Two words on the wire change the device's system mode. And `0xFFFF` — the thing we
had been treating as inert filler and stuffing into the tail of *every single
block* — is the escape that arms it.

The arithmetic was merciless. Every block ended with an **odd** number of filler
words, for every possible payload length, because the payload was always an odd
number of words. An odd run leaves one `ff ff` with no partner: an unpaired escape,
whose payload is therefore the first word of the *next block* in the ring. That
word was a register write. For a pitch bend it was `(freq_lo, 0x00)` — command
`0x00`, data taken from a frequency low byte that a wheel sweep walks through all
256 values of, several times a second.

So the instrument was issuing mode-change commands out of the low byte of its own
pitch. In the wrong mode the device stops honouring register writes: the sounding
note hangs because no gate-off can reach it, new notes are silent, and the status
block reports perfect health throughout — because the engine genuinely is healthy.
It is doing exactly what it was told.

Decoding `status[0x1E]` properly — **low nibble = mode, bit 7 = acknowledged** —
retired three beliefs this project had been built on:

- There is no engine start command. The `ff ff 01 00` block is escape + *set mode
  1*. What we called `start_engine()` is a mode-set.
- `0x0001` never meant "the engine stopped". It means mode 1 was requested and
  never acknowledged.
- And that is why `start_engine()` could never revive it: re-requesting the mode
  you are already on does nothing. Every recovery attempt in this project — every
  one — was asking the device to enter a mode it thought it was already in.

The fix is `pad_even()`: one extra delay word so the filler run is always even.
Eight microseconds.

### What generalises

- **Read your own notes as specification, not as history.** The answer had been
  sitting in `protocol.md` §4 the entire time, in a section we wrote. It was
  ignored because it was filed under "system-mode handshake" — a feature we did
  not use — when it was really a constraint on padding, which we used constantly.
  A finding is only as useful as the place you put it.
- **In-band signalling has no safe filler.** When the control plane and the data
  plane share an encoding, "padding" is not a neutral act; it is speech. The bytes
  you emit to mean *nothing* are still bytes the device reads.
- **Parity is a protocol property.** Nothing was ever wrong with an individual
  block. The defect lived in a parity relationship between a block's tail and its
  successor's head — invisible to any check that examines one write at a time,
  which is every check we built.
- **A perfectly healthy status block is not evidence of a healthy device.** Every
  instrument we added measured the transport. The failure was in the meaning of
  the bytes, and the transport delivered them flawlessly.
- **The user's question was the intervention.** Six phases of instrumentation
  eliminated theories; one question about whether the map might be wrong found the
  bug. When the territory keeps contradicting the map, suspect the map.

### Postscript: we had already found it, in Phase 2

The worst part is on this page, near the top. Phase 2, "inference that failed",
item 2:

> **`FFFF` + `00 mm` as a system-mode escape sequence.** Sending it did not set a
> mode; it cleared a state bit and stopped the device.

The theory was correct. The experiment was correct. The *observation* was correct
and precise — the state bit that cleared was the acknowledge bit, which is exactly
what a mode-set looks like in the instant before the device acknowledges it.

Only the interpretation was wrong. "It stopped the device" became "the theory is
wrong", the finding was filed under failures, and the escape prefix was treated as
inert filler from that day forward — including in `pad_even()`'s absence, which is
the bug.

So the real lesson is not that we missed something. It is that we *found* it,
wrote it down, mislabelled it, and then spent the rest of the project building
increasingly sophisticated instruments to rediscover it. A result recorded under
the wrong heading is worse than one never recorded at all: it is actively
load-bearing in the wrong direction, and every later theory was built on top of
"the escape sequence does nothing."

Worth asking, of any list of dead ends: *which of these did I disprove, and which
did I merely fail to understand?*

## Phase 10 — the padding was the payload

Phase 9 found that `0xFFFF` is an escape prefix and fixed a real parity bug with
it. The failure continued anyway. What followed was four more mechanisms, all
wrong, and the thing that eventually worked was not a better theory — it was
building an instrument that could see the failure at all, and then calibrating
it.

### The instruments were pointed at the wrong quantity

Every stress test up to this point measured USB errors, short writes, ring
pointers and free space. **All of them stay perfectly healthy through this
failure.** `bend_flood_test`'s 64,026 "clean" writes and 32 MB proved nothing;
it may have stalled the engine two minutes in and carried on reporting a
spotless bus. This is Phase 4's trap for the third time: a green diagnostic you
designed yourself is not evidence, and it does not become evidence by getting
more elaborate.

The detector that finally worked measures whether delta-delays still take any
time — send a known delay, time until the ring is genuinely empty:

```
healthy   ~100ms for 100000 cycles      stalled   0-5ms
```

Calibrated against nominal delay across two decades before being trusted, which
immediately exposed that the *first* version was reading a fixed cost rather
than the delay, and that its first sample after `init()` was a race that
reported a healthy device as dead. That false alarm cost two needless power
cycles. Calibrate the instrument, then calibrate it again.

### What the detector eliminated

With a real detector, each experiment killed something:

- **Recovery, entirely.** Re-arm, mode bounces, `setConfiguration`, USB
  `resetDevice` — all left it at 4ms. And ACID64 itself cannot play on a
  stalled device. The wedge is device-level and the power switch is the only
  path.
- **Traffic shape.** ACID64-shaped delta-timed content stalled at 41, 64 and
  527 writes against instant's 82. The look-ahead scheduling idea was worthless.
- **Pitch bend**, which had been the most compelling clue for days — it fails
  identically without it.
- **The SID chips.** A `TEST`-bit write, which halts a voice's oscillator
  unconditionally, went out on the wire while the sound continued.
- **Lost bus timing.** Host-paced writes 25ms apart still produced clicks with
  no pitch.

### The answer

ACID64 does 1,200+ writes in ten minutes without a scratch; we stalled once per
~180 writes. That comparison had been sitting there for weeks, and it is not
compatible with a per-write hazard — I asserted one anyway. It is compatible
with a hazard that scales with *filler volume*:

```
a note-on block:  244 filler words of 256  =  95% padding
                  = 122 escape+payload pairs per block
                  = ~4,500 escape commands per second
ACID64:           dense 2048-byte transfers, almost no filler
```

`0xFFFF` is not padding. It is the escape prefix, and we were transmitting it
by the thousand per second purely to fill space. `0xEE 0x00` — a zero-cycle
delay — costs exactly as much time as filler does, which is none, but it is an
ordinary command:

```
pad with FILLER      stalled at 82 and 94 writes
pad with DELAY_ZERO  24,000 writes / 10 minutes / zero stalls
```

The clean run ended on its timer, not on a failure. Confirmed by ear with an A
major arpeggio: the fix keeps the audio path entirely intact.

### What generalises

- **A comparison you cannot explain is a live clue, not background.** "ACID64
  never fails" was known from the very beginning and treated as context. It was
  the whole answer: the one axis where we differed by three orders of magnitude
  was the one that mattered, and it was measurable at any point.
- **Instrument the failure before theorising about it.** Four mechanisms were
  proposed and discarded while the only tool available reported health. One
  calibrated detector turned each subsequent experiment into a real
  elimination.
- **In a protocol where padding is a command, "empty" is a design decision.**
  There is no neutral filler in an in-band-signalled stream — only bytes you
  meant and bytes you did not think about, and the device cannot tell them
  apart.
- **The user's questions did more work than the theories.** "Did we make a
  mistake in protocol.md?" produced the escape-prefix finding. "Clicks, not
  notes" is what finally made us count the bytes in our own blocks instead of
  reasoning about them.
