# hardsid4u

Talk to a **HardSID 4U** from macOS (and Linux) over libusb, with no vendor
driver and no Windows — and play it live from a DAW.

The HardSID 4U is a USB box from 2008 that holds up to four real MOS 6581 /
8580 SID chips — the sound chip from the Commodore 64. It shipped with a
Windows-only driver, a Windows-only VSTi, and no published protocol. The
vendor (Hard Software, Hungary) is long gone.

This repository documents the USB protocol, provides a working Python
implementation, and ships a MIDI instrument so the hardware is playable from
Ableton, Cubase, or any MIDI controller. A power-cycled device plays a note on
the first run.

## Play a note

```bash
uv run --with libusb1 src/hardsid4u/hs4u.py --chip 0 --hz 440
```

```python
from hardsid4u import HardSID4U, PULSE, PAL_CLOCK

with HardSID4U() as hs:
    hs.init()                                  # arm the four sockets
    hs.volume(0, 15)
    hs.voice(0, 0, waveform=PULSE, attack=0, decay=9,
             sustain=12, release=8, pulse_width=0x400)
    hs.note_on(0, 0, 440)
    hs.delay(PAL_CLOCK)                        # one second, PAL cycles
    hs.note_off(0, 0)
    hs.flush()
```

## Play it from a DAW

```bash
uv sync --extra midi
uv run hardsid4u-midi
```

That opens a virtual CoreMIDI destination called **HardSID4U**. Select it as a
MIDI track's output and play.

- **MIDI channels 1–4 map to sockets 1–4**, three voices of polyphony each,
  matching the real SID voice count. There is no cross-chip voice stealing, so
  a channel always drives one physical chip.
- Sub-millisecond transport: a note-on is one 512-byte bulk write, measured at
  0.8 ms median. The SID's own hardware envelope does the ADSR, so no 6502
  emulation is involved.
- Waveform, ADSR, pulse width, filter cutoff/resonance and volume are on CCs —
  see the CC map in [`src/hardsid4u/midi.py`](src/hardsid4u/midi.py).
- Pitch bend is ±2 semitones by default, coalesced to ~66 writes/second.

Filter cutoff, resonance and volume are per-*chip* registers on real SID
hardware, not per-voice, so CC changes to them affect every note sounding on
that channel at once. That is the chip, not the software.

It has been endurance-tested with Beethoven's op. 27 no. 2 third movement on
repeat for six hours across two chips, with the engine still executing
correctly at the end.

---

## If you are implementing this protocol yourself, read this

**`0xFFFF` is not padding. It is an escape prefix.**

Every published description of this protocol — including earlier versions of
this repository — treats `0xFFFF` as inert filler that costs no time and pads
a block to 512 bytes. It does pad, and it does cost no time. It is also an
escape prefix, and the device consumes the word *after* it as an escape
payload:

```
ff ff        escape
mm 00        (data mm, command 0x00) -> set system mode to mm
```

A 512-byte block carrying a handful of register writes is ~95% padding. Pad it
with `0xFFFF` and you emit around 122 escape+payload pairs per block —
thousands of escape commands per second under live playing. Do that for long
enough and the device stops *executing* the command stream while continuing to
report perfect health: mode acknowledged, ring pointers advancing, `free`
returning to rest, no USB error, every byte accepted, and no sound. Only a
power cycle clears it, and the vendor's own player cannot play through it
either.

Measured, same traffic and rate:

| padding | result |
|---|---|
| `0xFFFF` filler | engine stalled after 82 and 94 writes |
| `0xEE 0x00` (zero-cycle delay) | 24,000 writes, 10 minutes, zero stalls |

`pad_even()` in this library pads with a zero-length delay: no time cost, but
an ordinary command rather than an escape. If you still pad with filler
anywhere, keep the run **even in length** — an odd run leaves an unpaired
escape that swallows the next word, and if that word is a register write of
the form `(data, 0x00)` the device silently changes system mode.

---

## How this was made, and by whom

Most of the reverse engineering in this repository was done by **Anthropic's
Claude**, working from files I supplied and running experiments against my own
hardware. I provided the device, the uploads, the USB captures, and the ears;
Claude did the binary analysis, formed and discarded the hypotheses, wrote the
probe scripts, and wrote the library and the documentation.

I am being explicit about this because the commit history would otherwise be
misleading, and because the process is genuinely interesting: the analytical
work was fast and mostly correct at the byte level, and it was also
confidently wrong several times in ways only real hardware could settle. Every
incorrect theory is recorded in [`docs/protocol.md`](docs/protocol.md)
alongside what replaced it.

The short version of the lesson: **static analysis of a stripped, packed binary
got the wire format exactly right and the state machine repeatedly wrong.**
What broke the deadlock was capturing the real driver on the wire.

The escape-prefix bug above is the sharper version of the same lesson. It was
found, written down, and then filed under "inference that failed" because the
*interpretation* of a correct experiment was wrong — and it cost months. See
[`docs/journey.md`](docs/journey.md), phases 2 and 9.

---

## Status

Working and verified on hardware:

- Enumeration, configuration, endpoint selection
- System mode handshake and socket arming ("init")
- Register writes to all four SID sockets
- Cycle-accurate delays
- Ring-buffer flow control
- A patch-level API: `voice()`, `note_on()`, `note_off()`, `volume()`
- A live MIDI instrument, endurance-tested for six hours

Known limits:

- **A wedged device needs the front-panel switch.** Once the engine stops
  executing, nothing in software recovers it — not re-arming, not a system
  mode bounce, not `setConfiguration`, not a USB `resetDevice`, and not the
  vendor's own player. Avoiding it is the fix; see the escape-prefix section.
- Isochronous mode (endpoints `0x83` / `0x04`) is unused, by this
  implementation and by the official software in SIDPLAY mode.
- No SID file player. See "Where next".

---

## Protocol summary

Full detail in [`docs/protocol.md`](docs/protocol.md). The essentials:

**Device:** VID `0x6581`, PID `0x8580` (`0x8581` = UPlay, `0x8582` = Uno).
Full speed, vendor class, one interface, one alt setting, 420 mA.

**Endpoints:**

| EP | Dir | Type | Size | Used for |
|---|---|---|---|---|
| 0x02 | OUT | BULK | 64 | command stream |
| 0x81 | IN | BULK | 64 | 64-byte status block |
| 0x04 | OUT | ISO | 512 | unused here |
| 0x83 | IN | ISO | 64 | unused here |

**Framing:** writes to EP 0x02 must be a multiple of **512 bytes**. Shorter
transfers are treated as out-of-band and ignored by the stream parser.

**Command words** are two bytes, **data byte first, then command byte**:

| Command byte | Meaning |
|---|---|
| `0x00`–`0x7F` | register write, `(chip << 5) \| reg` |
| `0xEE` | delay, low byte of cycle count |
| `0xEF` | delay, high byte (emitted *before* `0xEE`) |
| `0xFF` | **escape prefix** — consumes the following word. Also used as filler; see the warning above before you pad with it. |

Worked example: `4c ef 1e ee` = delay `0x4C1E` = 19,486 cycles ≈ one PAL frame.

**System mode** — `ff ff 01 00` padded to 512 bytes with zeros selects
SIDPLAY mode. This is commonly described as a "start command"; it is not. It
is escape + *set mode 1*, which is why sending it to a device already in mode
1 appears to behave as a toggle.

```
status[0x1E]  low nibble = current system mode (1 = SIDPLAY, 2 = VST)
              bit 7      = the device's acknowledgement of it
```

`0x0081` — mode 1, acknowledged — is the only healthy value. Bit 7 is **not**
"engine running"; reading it that way cost this project weeks. `0x0001` means
a mode was requested and never acknowledged.

**Socket arming** — each socket must be armed before it makes a sound. A SID
has registers `0x00`–`0x18`; the command byte allots 32 per chip, so
`0x19`–`0x1F` are HardSID device control registers. The init writes ASCII
`'S' 'I' 'D'` (`0x53 0x49 0x44`) to registers `0x1D`/`0x1E`/`0x1F` — a magic
knock — plus a probe pattern and three configuration bytes. Generated by
`chip_init_stream()`, byte-identical to the official software.

**Flow control** — an 8192-byte ring at device address `0x2000`–`0x3FFF`:

```python
rd, wr = status[0x1A], status[0x1C]
used = (wr - rd) & 0x1FFF
free = 0x2000 - used
```

Note that this cannot represent a *full* ring: at 8192 bytes used the mask
yields 0 and `free` reads as 8192, indistinguishable from empty. The vendor
backs off at `free < 0x1000`, and so should you.

**There are no patches.** The device is a register-level pipe to real SID
chips; a patch is just the register values you write.

---

## Layout

```
src/hardsid4u/hs4u.py    the driver, and a demo entry point
src/hardsid4u/midi.py    the MIDI instrument (hardsid4u-midi)
docs/protocol.md         full protocol notes, including retracted theories
docs/journey.md          how it was worked out, and what went wrong
captures/                USB captures this was derived from
tools/                   probe scripts and diagnostics — see tools/README.md
examples/                small runnable examples
```

---

## Requirements

- Python 3.9+
- `libusb1` (the `usb1` package) — note **not** PyUSB, which lacks the needed
  transfer support
- libusb itself:

```bash
brew install libusb            # macOS
sudo apt install libusb-1.0-0  # Debian/Ubuntu
```

Optional extras:

```bash
uv sync --extra midi     # python-rtmidi, for hardsid4u-midi
uv sync --extra audio    # numpy + sounddevice, for the acoustic tools
```

The core driver depends on libusb alone, so it can be vendored for playback
without pulling anything else in.

On Intel Macs, Homebrew installs to `/usr/local`, which the Python loader does
not search. If you get `cannot find a suitable libusb-1.0`:

```bash
export DYLD_LIBRARY_PATH="$(brew --prefix libusb)/lib:$DYLD_LIBRARY_PATH"
```

No kernel extension, no driver signing, no Zadig. The device is vendor-class
so macOS does not claim it and libusb can talk to it directly.

On Linux you will want a udev rule granting access to `6581:8580`.

---

## Where next

A **compiled MIDI driver**, so the hardware appears as a normal MIDI device to
the whole system rather than needing a Python process. That belongs in its own
repository; this one stays the protocol reference and the Python
implementation.

If you want SID *file* playback, the most useful thing to build on this is a
**Network SID Device V4** daemon: implement that protocol on localhost over
this library and ACID64 and JSIDPlay2 will play SID files on real hardware
from macOS, with no 6502 emulation written locally. The specification ships
with the [ACID64 console player](https://github.com/WilfredC64/acid64c) source.
The alternative is `libsidplayfp` with a custom `sidemu` backend, which gets
you file parsing, sub-tunes, PAL/NTSC and song lengths for free.

---

## Legal

Reverse engineering for interoperability is explicitly permitted under
Article 6 of the EU Software Directive (2009/24/EC), implemented in Swedish
law as 26 g § upphovsrättslagen. This work was done to make hardware I own
usable on an operating system its discontinued vendor never supported.

No vendor binaries are redistributed here. The captures are recordings of my
own device on my own machine. No code was copied from ACID64 or any other
GPL-licensed project; its published API surface was read as documentation,
and this implementation was written independently.

"HardSID" is a trademark of its owner. This project is not affiliated with or
endorsed by Hard Software.

## Licence

MIT — see [LICENSE](LICENSE).
