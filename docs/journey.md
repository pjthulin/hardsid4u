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
3. **Vendor control requests on EP0 doing socket enable.** The capture showed
   only `GET_DESCRIPTOR` and `SET_CONFIGURATION`. There are no vendor control
   requests at all.

The pattern: **wire formats recovered from a stripped binary were reliable;
state machines and command semantics inferred from call sequences were not.**

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
