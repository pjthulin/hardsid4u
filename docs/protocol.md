# HardSID USB — protocol notes

Derived by static analysis of `hardsid.dll` v3.02 (2010-01-29, Hard Software / Téli Sándor & Simon White),
`hardsidusb.sys` / `hardsidusb.inf` (DriverVer 05/19/2009, 1.0.0.3), and `hardsid_usb.dll` as bundled with
ACID64 Console Player (UPX+LZMA packed; unpack params in §11), cross-checked against the GPL v3 Rust
source of `acid64c` (`src/player/hardsid_usb.rs`, `hardsid_usb_device.rs`).

Confidence is marked per item: **[confirmed]** = read directly out of the disassembly,
**[inferred]** = deduced from call sequences and needs a bus capture to pin down.

---

## 1. Device identity

| Device | VID | PID |
|---|---|---|
| HardSID 4U | 0x6581 | 0x8580 |
| HardSID UPlay | 0x6581 | 0x8581 |
| HardSID Uno | 0x6581 | 0x8582 |

**[confirmed]** The DLL identifies the model by substring-matching the device interface path
for `_8581` (→ UPlay, internal type 2) and `_8582` (→ Uno, type 3); anything else is type 1 (4U).

---

## 2. Kernel driver

`hardsidusb.sys` is a near-stock build of the Microsoft WDK **isousb** sample. The CodeView record
still carries the build path:

```
c:\winddk\3790.1830\src\wdm\usb\isousb\sys\objfre_wxp_x86\
```

Imports are only `USBD_ParseConfigurationDescriptorEx`, `USBD_CreateConfigurationRequestEx`,
`WmiSystemControl`/`WmiCompleteRequest`, and standard WDM. Registry knobs live under
`HKLM\SYSTEM\CurrentControlSet\Services\HARDSIDUSB\Parameters`: `frames`, `packets`, `startframe`,
`errordelay`, `threads`, `SSEnable` — the isochronous streaming parameters.

**Implication:** the driver carries no HardSID-specific logic. It is a pipe. Everything below is
payload, so WinUSB/libusb can replace it wholesale.

---

## 3. Opening the device (user mode)

**[confirmed]** Device interface GUID:

```
{D838B33A-F0BE-4765-996F-FBC3966EBFA7}
```

Sequence in the DLL:

1. `SetupDiGetClassDevsA(&GUID, NULL, NULL, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)`  (flags = 0x12)
2. `SetupDiEnumDeviceInterfaces` / `SetupDiGetDeviceInterfaceDetailA` → interface path
3. `CreateFileA(path)` — plain open, used only as a handle for the legacy IOCTL path
4. `CreateFileA(path + "\\PIPE00")` → **write pipe** (stored per device)
5. `CreateFileA(path + "\\PIPE01")` → **read pipe**

All opens use `GENERIC_READ|GENERIC_WRITE` (0xC0000000), share mode 3, `OPEN_EXISTING` (3).

`PIPE00`/`PIPE01` are the isousb sample's pipe-index naming; `PIPE02`/`PIPE03` strings are also
present in the binary but unused for these devices. When reimplementing over libusb you need the
descriptor dump to map pipe index → endpoint address.

Cross-process coordination: named mutex `HARDSID_DLL_MUTEX`, plus a shared PE section `.shr`
(0x1330 bytes) holding per-device state so several apps can see the same device table.

---

## 4. The command stream

**[confirmed]** The host builds an array of **16-bit little-endian words**:

```
word = (command_byte << 8) | data_byte
```

Buffer capacity: **256 words** in legacy/IOCTL mode, **1024 words** in USB mode (set at init from
the mode flag: `0x100` vs `0x400`). The buffer auto-flushes when full.

### Command byte map

| Command byte | Meaning | Confidence |
|---|---|---|
| `0x00`–`0x7F` | SID register write: `(chip << 5) \| reg`, chip 0–3, reg 0x00–0x1F | **[confirmed]** |
| `0xEE` | Delay, low byte — `cycles & 0xFF` | **[confirmed]** |
| `0xEF` | Delay, high byte — `cycles >> 8` (i.e. units of 256 cycles) | **[confirmed]** |
| `0xFF` (data `0xFF`) | Filler / NOP when padding to a 512-byte boundary — **and** an escape prefix (see below) | **[confirmed]** |
| `0xF0` | Control opcode, subcommand in the data byte | **[inferred]** |
| `0x80` | Appears in a mute/model-switch sequence | **[inferred]** |

`HardSID_Write(dev, chip, reg, data)` rejects `reg >= 0x20`, then emits `(chip<<5)|reg` as the
command byte — the shift-by-5 is explicit in the code at `0x10004189`.

### Delay encoding

`HardSID_Delay(dev, cycles)` (cycles is 16-bit):

```
if cycles == 0:           nothing
elif cycles < 0x100:      emit(0xEE, cycles)
elif (cycles & 0xFF)==0:  emit(0xEF, cycles >> 8)
else:                     emit(0xEF, cycles >> 8); emit(0xEE, cycles & 0xFF)
```

### 0xF0 control subcommands

Observed data bytes in reset / lock / model-switch sequences: `0x00`, `0x02`, `0x04`, `0x06`,
`0x07`, `0x0A`. They are interleaved with delays of `0x1388` (5000), `0xEA60` (60000) and
`0x7530` (30000) cycles — consistent with a SID reset / power-settle routine. **[inferred]** —
worth confirming against a capture rather than trusting this table.

### System-mode handshake

**[confirmed]** `0xFFFF` is not only padding — it also acts as an **escape prefix**. The mode-set
routine at `0x10003df6` emits the word pair:

```
FF FF        escape
00 mm        mm = system mode
```

then polls the status block until `status[0x1E] == (mm | 0x80)`. The two modes are exposed by the
newer API as `SYS_MODE_SIDPLAY = 1` and `SYS_MODE_VST = 2` — so the low nibble of `status[0x1E]`
is the current mode and bit 7 is the acknowledge flag. This matters: the device must be put into
SIDPLAY mode before register writes behave as documented above.

---

## 5. Flushing / framing

**[confirmed]** On flush the host computes:

```
blocks = ((count*2 - 2) / 512) + 1
bytes  = blocks * 512
```

then either:

- **USB mode:** `WriteFile(pipe00_handle, buffer, bytes)` — always a multiple of **512 bytes**
- **Legacy mode:** `DeviceIoControl(handle, 0x220018, buffer, bytes, out, 4)`

Before flushing, the tail is padded with `0xFFFF` words so the payload lands on an even
512-byte block.

---

## 6. Flow control

**[confirmed]** `ReadFile(pipe01_handle, buf, 64)` returns a 64-byte status block. The DLL reads
these fields:

| Offset | Use |
|---|---|
| +0x00, +0x02, +0x04 | copied to internal state (unidentified) |
| +0x18 | busy / activity flag |
| +0x1A | ring **read** pointer |
| +0x1C | ring **write** pointer |
| +0x1E | device state; low nibble compared against a mode value, bit 7 set on success |

Free-space calculation, verbatim from the code:

```
a = status[0x1A]; b = status[0x1C];
if      (a < b)  free = a - b + 0x2000;
else if (a > b)  free = a - b;
else             free = 0x2000;
busy = (free < 0x1000) ? 2 : 1;
```

So the device holds a **0x2000 (8192) entry ring buffer** and reports both pointers back to the
host. `HardSID_Try_Write` returns 2 when `free < 0x1000`, meaning "would block, back off". This is
the mechanism you must reimplement to avoid the "delayed and garbled" failure mode.

**Independently confirmed** in the unpacked `hardsid_usb.dll` at offset `0x1fd0` and `0x3540` —
the same `add eax, 0x2000` / `cmp ax, 0x1000` / `adc bl, 1` sequence, producing state 1 (OK) or
2 (BUSY). Two unrelated implementations, same constants.

---

## 7. Exported API (33 ordinals, 26 named)

Modern API:

```
HardSID_Devices, HardSID_Version, HardSID_Reset, HardSID_Reset2,
HardSID_Lock, HardSID_Unlock, HardSID_Group,
HardSID_Write, HardSID_Try_Write, HardSID_Read,
HardSID_Delay, HardSID_Flush, HardSID_SoftFlush, HardSID_Sync,
HardSID_Mute, HardSID_MuteAll, HardSID_Filter
```

Legacy API (kept for old players):

```
InitHardSID_Mapper, GetHardSIDCount, GetDLLVersion,
WriteToHardSID, ReadFromHardSID,
MuteHardSID, MuteHardSIDAll, MuteHardSID_Line, SetDebug
```

`HardSID_Version` / `GetDLLVersion` simply `return 0x0302`.

Mute is not a device command: the DLL keeps **shadow copies of all SID registers** per chip and
re-emits control registers 0x04 / 0x0B / 0x12 with the gate/waveform bits masked. Any replacement
host software needs the same shadow-register model.

---

## 8. Legacy PCI/ISA path (same DLL, not needed for USB)

Device names `\\.\HSID%04X` and `\\.\SidDev`; IOCTLs `0x22000C` (in 4 B / out 256 B),
`0x220010` (in 256 B / out 4 B), `0x220018` (bulk write). There is also a direct port-I/O path
using raw `out dx,al` / `in al,dx` against a base address read from the registry.

Registry state (shared with the old cards):

```
HKLM\Software\Hard Software\HardSID
HKLM\Software\Hard Software\HardSID\Devices\Device%d      (value: DeviceType)
HKLM\Software\Hard Software\HardSID\Group Settings\Device%d
HKLM\Software\Hard Software\HardSID\Single Settings\Device%d
```

---

## 9. The modern `hardsid_usb.dll` API

This is the API ACID64 actually calls (`acid64c/src/player/hardsid_usb.rs`, GPL v3). It is a
higher-level wrapper than the 2010 `hardsid.dll` and is the one to target for compatibility.

```c
bool  hardsid_usb_init(int sync, uint16_t sys_mode);   // sys_mode: 1 = SIDPLAY, 2 = VST
void  hardsid_usb_close(void);
uint8 hardsid_usb_getdevcount(void);
uint8 hardsid_usb_getdevicetype(uint8 dev_id);          // 1 = 4U, 2 = UPlay, 3 = Uno
uint8 hardsid_usb_getsidcount(uint8 dev_id);
uint8 hardsid_usb_write(uint8 dev_id, uint8 reg, uint8 data);
uint8 hardsid_usb_delay(uint8 dev_id, uint16 cycles);
uint8 hardsid_usb_flush(uint8 dev_id);
void  hardsid_usb_abortplay(uint8 dev_id);              // sync mode only
uint8 hardsid_write_buff(const uint8 *buf, int len);    // async mode only
uint32 hardsid_query_status(uint8 dev_id);              // e.g. errorpacketcount
const char* hardsid_usb_getlasterror(void);
```

Return states: `1 = OK`, `2 = BUSY`, `3 = ERROR`.

**[confirmed]** `reg` here carries the chip in the upper bits exactly as in §4: acid64c builds
`device_base_reg = sid_index * 0x20` and passes `(reg & 0x1f) | base_reg`, recovering the chip
with `sid_nr = reg >> 5`. Two independent code paths, same `(chip << 5) | reg` layout.

### Host-side scheduling rules worth copying

From `hardsid_usb_device.rs`:

- **Minimum 4 cycles between SID writes** (`HS_MIN_CYCLE_SID_WRITE = 4`). Shorter gaps must be
  padded, and the padding has to be accounted for against subsequent delays
  (`cycles_to_compensate`).
- On `BUSY`, push the write back onto the front of the FIFO, yield, and retry — never drop it.
  Sleep granularity used is 1 ms.
- Writes to a second chip on hardware that doesn't have one are redirected to **register `0x1E`**
  as a harmless dummy write rather than being dropped, so cycle accounting stays intact.
- PAL/NTSC clock correction is done host-side by rescaling voice frequency registers (0x00/0x01
  of each voice) before transmission, with an extra 4-cycle delay inserted when the high byte
  changes. The device itself has no clock adjustment.

---

## 10. Resolved: it is isochronous

The bundled `hardsid_usb.dll` is a Rust binary with **libusb 1.0 statically linked**. Its unpacked
string table settles the question:

```
WinUsb_WriteIsochPipeAsap      WinUsb_ReadIsochPipeAsap
WinUsb_RegisterIsochBuffer     WinUsb_UnregisterIsochBuffer
Allocation of IsochronousPacketsArray failed
usbdk_do_iso_transfer          winusbx_submit_iso_transfer
failed to enable ISO_ALWAYS_START_ASAP for endpoint %02X
```

Combined with the driver's `frames` / `packets` / `startframe` registry parameters and the
`errorpacketcount` status variable, the OUT path is **isochronous**, not bulk. The 512-byte
quantum in the legacy DLL is a host-side buffering artefact, not the wire framing.

The DLL supports both back ends: it contains the same interface GUID
`{D838B33A-F0BE-4765-996F-FBC3966EBFA7}` and the `PIPE00` / `PIPE01` names for the official
driver, alongside the full libusb/WinUSB stack for the Zadig route. `"Async is not supported"`
appears as an error string, so `hardsid_write_buff` only works on one of the two back ends.

### What still needs a device in hand

1. Endpoint addresses, interface number, alternate setting and `bInterval` — one
   `lsusb -v -d 6581:8580`.
2. Isochronous packet size and packets-per-transfer.
3. The `0xF0` subcommand table.
4. What `status[0x00..0x04]` carry.

---

## 11. Reproducing the unpack

`library/hardsid_usb.dll` is UPX-packed with **LZMA** (PackHeader at file offset `0x3E0`:
version 13, format 9 = PE i386, method 14 = LZMA, level 10, `u_len = 0x213AF`,
`c_len = 0xD0F0`). No UPX binary needed:

```python
import lzma
d = open('hardsid_usb.dll','rb').read()
dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW,
        filters=[{'id': lzma.FILTER_LZMA1, 'dict_size': 1<<24,
                  'lc': 3, 'lp': 0, 'pb': 0}])
open('unpacked.bin','wb').write(dec.decompress(d[0x402:0x402+0xD0F0+64], 0x213AF))
```

Output is exactly 136111 bytes. Note `pb = 0` (not the usual 2) and the 2-byte offset into the
UPX1 section. Code is still x86-filtered (UPX filter `0x49`, cto `0x0A`), which scrambles
call/jmp targets but leaves strings and data intact.

---

## 12. Licensing note

`acid64c` and its `hardsid_usb_device.rs` scheduling logic are **GPL v3**. `hardsid_usb.dll`
itself is closed source. If you want your own work under a permissive licence, treat the acid64c
source as specification-only and keep a clean-room separation between whoever reads it and
whoever writes the replacement.

---

## 13. Verified against hardware (supersedes earlier inferences)

Descriptor dump, HardSID 4U Studio Edition, full speed, vendor class 0xFF,
one interface, one alt setting, `bMaxPower` = 420 mA:

| EP | Dir | Type | wMaxPacketSize | bInterval |
|---|---|---|---|---|
| 0x81 | IN | BULK | 64 | - |
| 0x02 | OUT | BULK | 64 | - |
| 0x83 | IN | ISO | 64 | 1 |
| 0x04 | OUT | ISO | 512 | 1 |

**Confirmed by experiment:**

- **BULK OUT 0x02 is the command pipe.** Writes must be a multiple of
  **512 bytes**; 4-byte and 64-byte writes are silently discarded with no
  pointer movement.
- **The ring is 8192 bytes of address space, 0x2000–0x3FFF.** Pointers are
  byte addresses, granular to 256 bytes, and wrap 0x3E00 -> 0x2000.
- **Free space:** `used = (wr - rd) & 0x1FFF; free = 0x2000 - used`, with
  `rd` at +0x1A and `wr` at +0x1C. Matches the 2010 DLL's formula exactly.
- **Byte order is LITTLE-ENDIAN — data byte first, command byte second.**
  `cmd(c, d) = bytes((d, c))`. Proved by a block of `bytes((0xFF,0xEF))`
  stalling the ring: under the other reading that byte pair is `0xFF` filler
  and would have drained instantly.
- **Timing is DELTA-TIMED, not a fixed 8 kHz tick.** A block of maximum-value
  `0xEF`/`0xEE` pairs (128 pairs x 65535 cycles ~ 8.5 s per block) jams the
  ring for minutes, while pure `FFFF` filler drains faster than the host can
  write. `0xEE`/`0xEF` are real cycle delays; `FFFF` costs no time.
- **`state` at +0x1E starts at 0x0000 and becomes 0x0081 on the first block
  of data** — no handshake required. The device brings itself up.

**Withdrawn — earlier inferences that hardware contradicted:**

- ~~`FFFF` + `00 mm` is a system-mode escape sequence~~. Sending `ff ff 01 00`
  did not set a mode; it **cleared bit 7 of `state`** (0x0081 -> 0x0001) and
  the device produced no audio in that state thereafter. Bit 7 currently looks
  like an **enable**, not an acknowledgement. Recovery is a power cycle.
- ~~`0xF0` control subcommands with 5000/30000/60000-cycle delays~~. A proper
  disassembly of `HardSID_Reset` (0x10002390 -> `HardSID_Reset2` 0x10001ea0 ->
  0x100012d0) shows a plain loop writing 0 to registers 0x00-0x17, then one
  write of register 0x18 with the volume nibble, then a flush. There is no
  `0xF0` sequence and no enable ritual.

**Still open:**

- Meaning of `state` bit 7, and how to set it deliberately rather than by
  power-cycling.
- Whether register writes need anything beyond `(chip << 5) | reg` as the
  command byte — untested at a known-good `state = 0x0081`.
- The field at +0x16 (0x07D0 = 2000 when idle) and the counter at +0x18 that
  increments on iso writes (candidate: `errorpacketcount`).

---

## 14. THE START COMMAND (hardware-verified)

The single most important finding, and the one that unblocked everything.

**The endpoint carries two channels, distinguished by transfer length:**

| Transfer length | Meaning |
|---|---|
| exactly 512 bytes (or a multiple) | **stream data** — enters the 8 KB ring, consumed with delta timing |
| anything shorter | **out-of-band command** — acted on immediately, never enters the ring |

This is why short writes appeared to be "silently discarded" during early
probing: they do not move `rd`/`wr` because they are not ring data.

**Start / reset command — a 4-byte SHORT packet on BULK OUT 0x02:**

```
ff ff 01 00
```

Observed effect from a cold power-on (`state = 0x0000`, engine halted,
bulk blocks accepted but never consumed):

```
rd    0x2000 -> 0x3e00
wr    0x2200 -> 0x2000     <- ring pointers RESET
state 0x0000 -> 0x0081     <- bit 7 set, engine running
```

After this the device consumes stream data normally and `0xEE`/`0xEF` delays
take effect.

**Notes and cautions:**

- The command must be sent as its own short transfer. Padding it to 512 bytes
  turns it into stream data and it does nothing.
- Sending the same 4 bytes on the **ISO OUT endpoint 0x04** while running
  drove `state` from `0x0081` to `0x0001` and the device stopped producing
  audio. Treat it as a start/stop toggle, or as start-on-bulk / stop-on-iso.
  Recovery from the stopped state was a power cycle; whether the command can
  restart it in place is untested.
- `state` lives in RAM: a power cycle returns it to `0x0000`.
- Neither sustained polling of the status endpoint nor any isochronous
  activity starts the engine. Only this short packet did.

### Corrected bring-up sequence

```
1. claim interface 0            (no alt setting needed - only alt 0 exists)
2. bulkWrite(0x02, b'\xff\xff\x01\x00')      <- short packet, starts engine
3. verify status[0x1E] == 0x0081
4. stream 512-byte blocks on 0x02, throttled by
       used = (wr - rd) & 0x1FFF ; free = 0x2000 - used
5. command words are little-endian: bytes((data, cmd))
       cmd 0x00-0x7F : (chip << 5) | reg   SID register write
       cmd 0xEE      : delay, low byte of cycle count
       cmd 0xEF      : delay, high byte
       cmd 0xFF      : filler, costs no time, used to pad to 512
```

---

## 15. State of play, and the remaining blocker

### Solved and hardware-verified

- Endpoints and descriptors (§13)
- **BULK OUT 0x02** is the command pipe; **BULK IN 0x81** returns a 64-byte
  status block
- Stream data must be written in **512-byte units**; shorter transfers are
  treated as out-of-band and never enter the ring
- Ring is **8192 bytes at 0x2000–0x3FFF**;
  `used = (wr - rd) & 0x1FFF`, `free = 0x2000 - used`, `rd` at +0x1A,
  `wr` at +0x1C
- Command words are **little-endian: `bytes((data, cmd))`**
- Timing is **delta-timed**: `0xEE` = delay low byte, `0xEF` = delay high
  byte, `0xFF` = filler costing no time. Proven by a block of max-value delay
  pairs jamming the ring for minutes while filler drained instantly.
- The engine can be started, and `state` (+0x1E) bit 7 then reads set
  (`0x0081`). Once running, blocks are consumed and delays are honoured.

### Not solved

**Register writes do not produce sound.** With the engine confirmed running
and blocks confirmed consumed, a volume-register click train was silent at
every command base tried (0x00, 0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0) and a
full note was silent on all four sockets. The only audible event in any
session is a single click at engine start.

**Engine start is not understood.** `ff ff 01 00` as a short packet started it
once, but only with data already buffered; on other occasions a second filler
block started it with no short packet at all; on a cold empty ring neither
works. Current workaround is to alternate filler blocks and the short packet
until bit 7 sets (see `hs4u_m6_probe.py: ensure_started`).

### The unexplored channel

**EP0 vendor control transfers.** Every experiment so far has used the bulk
and isochronous data endpoints only. A vendor control request is the natural
home for socket enable, per-socket SID type selection, or analogue output
un-mute — and it is exactly what is hardest to recover from UPX-filtered,
stripped code, which explains why two DLL disassemblies did not surface it.

**Do not blind-scan `bRequest` values.** The board has a firmware update mode
(the "Boot selector — short = update mode" jumper) and there is no published
firmware image to recover with.

### Recommended next step: capture

1. **Cheap first pass, macOS native.** Install Apple's Additional Tools for
   Xcode, then `sudo ifconfig XHC20 up`, and Wireshark gains a USB capture
   interface. Capture our own scripts to confirm how libusb frames the
   512-byte bulk writes on a 64-byte endpoint.
2. **The real capture.** Windows VM with the HardSID passed through, install
   `hardsidusb.sys` via the `.inf`, install `hardsid.dll` and the VSTi, play
   one note, capture with USBPcap.
3. **What to look for, in order:** any EP0 control transfers during device
   open; the first few hundred bytes written to EP 0x02; the first status
   reads from EP 0x81; whether the isochronous endpoints are used at all in
   SIDPLAY mode.

Given how much of the protocol is already pinned down, reading that capture
should be transcription rather than inference.

---

## 16. Confirmed by USB capture (ACID64 + WinUSB, Windows VM)

A USBPcap capture of ACID64 Player Pro playing a SID tune settles the format.
26,755 packets; the HardSID is device 2.

### Traffic profile

| Endpoint | Transfers | Notes |
|---|---|---|
| EP 0x81 BULK IN, 64 B | **13,037** reads over 7.76 s = **~1,679/s** | status polling |
| EP 0x02 BULK OUT | **11** writes, 22,528 bytes total | 4x512, 1x2048, 6x3072 |
| EP0 CONTROL | GET_DESCRIPTOR x2, SET_CONFIGURATION | **no vendor requests at all** |
| EP 0x83 / 0x04 (iso) | **unused** | not touched in SIDPLAY mode |

### Command format — fully confirmed

Every assumption we made was right:

- word = **data byte first, then command byte** (little-endian)
- `cmd 0x00-0x7F` = `(chip << 5) | reg`, and **nothing outside 0x00-0x7F,
  0xEE, 0xEF, 0xFF appears anywhere in 11,264 words**
- `0xEF` (delay high) is emitted **before** `0xEE` (delay low)
- worked example: `4c ef 1e ee` = delay 0x4C1E = 19,486 cycles ~ one PAL frame

Command byte histogram over the whole capture: 0x00-0x7F used throughout
(all four chips), `0xEE` x5318, `0xEF` x611, `0xFF` x10.

### There is no start command

The first write opens with two `0xFFFF` filler words and goes straight into
register writes. `0xFF` appears only 10 times in the entire capture, all as
leading padding. The `ff ff 01 00` short packet we found earlier is therefore
**not** how the official software starts the device.

`state` at +0x1E read `0x0081` for all 13,037 status reads — the same value we
reach with filler blocks.

### The init sequence

Writes 1-5 (4x512 + 1x2048 = 4096 bytes) precede any music. Shape:

```
filler, filler
delay 8, chip0 reg 0x00 = 0x00
delay 8, chip0 reg 0x08 = 0x00
delay 8, chip0 reg 0x07 = 0x00
...                                  (zero a specific register order)
delay 8, chip0 reg 0x02 = 0xff
delay 8, chip0 reg 0x02 = 0x08
delay 8, chip0 reg 0x03 = 0xff
delay 8, chip0 reg 0x03 = 0x08
...                                  (0xff/0x08 pairs across 0x02-0x06,
                                      0x09-0x0b, and onward, all four chips)
```

Note the delay comes **before** each register write here, and every write is
separated by at least 8 cycles. The `0xff` / `0x08` pairs look like SID
detection or chip configuration. This init is the main thing our own streams
were missing.

### Verified end to end

Replaying the 22,528 captured bytes verbatim over libusb on macOS reproduces
the music on real hardware. Transport, framing, flow control and command
format are all proven.

---

## 17. The init decoded — device control registers 0x19-0x1F

`--no-init` is silent; captured-init + our own note sings. The init is
mandatory, and reconstructing it generatively reproduces the capture
**byte-for-byte** for sockets 2 and 3 (socket 1's copy in the capture is
truncated at a 512-byte boundary, socket 0's is preceded by filler).

### The key insight

A SID has registers 0x00-0x18. The command byte allots **32** registers per
chip, so **0x19-0x1F are HardSID device control registers**, one set per
socket. The init writes ASCII to them:

```
reg 0x1D = 0x53   'S'
reg 0x1E = 0x49   'I'      <- "SID": the unlock knock
reg 0x1F = 0x44   'D'
```

Without this, the socket is never armed and every SID register write is
silently discarded — which is exactly the silence we chased for hours.

### Per-socket init sequence

Delay comes BEFORE each write. All delays in cycles.

```
for r in [01,00,08,07,0f,0e,04,05,06,0b,0c,0d,12,13,14]:
    delay 8; reg r = 0x00
for r in [02,03,04,05,06,09,0a,0b,0c,0d,10,11,12,13,14,15,16,17,19]:
    delay 8; reg r = 0xff
    delay 8; reg r = 0x08                  # probe / detect pattern
delay 50;    reg 0x1E = 0x00
for r in <same probe list>:
    delay 8; reg r = 0x00
delay 40000; reg 0x1E = 0x00               # note: chip 0's 0x1E, not this chip's

delay 8; reg 0x1D = 'S'
delay 8; reg 0x1E = 'I'
delay 8; reg 0x1F = 'D'
delay 1000; reg 0x1E = 0x00

delay 8; reg 0x1D = 'S'
delay 8; reg 0x1E = 'E'
delay 8; reg 0x1F = '6'
delay 8; reg 0x1D = 'S'
delay 8; reg 0x1E = 'I'
delay 8; reg 0x1F = 'D'
delay 1000; reg 0x1E = 0x00

for v in [0x8A, 0x92, 0xC0]:               # three config values
    delay 8; reg 0x1F = v
    delay 8; reg 0x1E = 0x45               # 'E'
    delay 1000; reg 0x1E = 0x00

delay 8; reg 0x1D = 0x00
delay 8; reg 0x1E = 0x00
delay 8; reg 0x1F = 0x00
delay 20000; reg 0x1E = 0x00

delay 8; reg 0x19 = 0x80
delay 8; reg 0x1A = 0x65
delay 8; reg 0x1F = 0x40
delay 8; reg 0x19 = 0x00
delay 8; reg 0x1A = 0x00
```

Repeat for each socket 0-3.

### Reference implementation

`hs4u.py` implements all of the above: open, engine start, generated init,
register/delay buffering, 512-byte framing, ring flow control and drain.
`python3 hs4u.py --chip 0 --hz 440` plays a note.

## 18. What is left

- The engine start remains the one soft spot: neither a filler block nor the
  `ff ff 01 00` short packet is reliable alone, so `start_engine()` alternates
  them. The official software sends no start command at all, which suggests
  the real condition is something about the very first transfer after
  SET_CONFIGURATION that we have not isolated.
- Meaning of the three config bytes 0x8A / 0x92 / 0xC0, and of 0x19 = 0x80 /
  0x1A = 0x65.
- Whether the probe pattern (0xff / 0x08 writes) is chip detection whose
  result can be read back from the status block.
- Player: the Network SID Device V4 protocol (spec in `docs/` of the acid64c
  source) implemented as a localhost daemon over `hs4u.py` would let ACID64
  and JSIDPlay2 drive this hardware from macOS with no 6502 emulation written
  locally.

---

## 19. Root cause of the "generated init does not work" saga

**It was a missing `SET_CONFIGURATION`, not the init content.**

On macOS the device is frequently left with no active configuration. The
symptom is `libusb_claim_interface` failing with `LIBUSB_ERROR_NOT_FOUND` on
the first run of a script and succeeding on the second. In the in-between
state the interface can be claimed and data accepted, but the sockets never
arm and nothing makes a sound.

The reference capture shows Windows issuing `SET_CONFIGURATION 1` immediately
before the init, which is exactly what was missing.

Fix, before claiming the interface:

```python
if handle.getConfiguration() != 1:
    handle.setConfiguration(1)
```

With that in place the **generated** init works: `hs4u.py` plays a 440 Hz
tone with no captured bytes involved.

### Theories this invalidates — and one it does NOT

Some hypotheses formed while chasing this were artefacts of the configuration
state. Recorded so nobody rebuilds on them:

- ~~The init must be delivered as one continuous burst without draining.~~
- ~~Something in the capture's first 2048 bytes performs the real arming.~~

**But repetition IS required.** With `SET_CONFIGURATION` fixed, tested on
hardware:

| init passes | result |
|---|---|
| 1 | **silent** |
| 3 | **plays** |

So a single pass of the 1710-byte sequence does not arm the sockets, and the
capture's repeated passes were not merely a buffer-flush artefact. The
minimum working count is still being narrowed; `hs4u.py` defaults to 3.

This is worth understanding eventually — most likely the `'S' 'I' 'D'` knock
needs the socket to already be in a particular state, which the first pass
establishes and the second acts on.

### Minimum working sequence

```
1. open device, SET_CONFIGURATION 1, claim interface 0
2. start engine (alternate filler blocks / ff ff 01 00 short packet
   until status[0x1E] bit 7 is set)
3. send the generated init: chip_init_stream(0..3) + delay(40000)
   + reg(chip 0, 0x1E, 0x00)
4. stream register writes and delays as 512-byte blocks, throttled on
   free = 0x2000 - ((wr - rd) & 0x1FFF)
```

---

## 20. SOLVED — the real start command

Confirmed by a cold-start capture of ACID64 and verified on hardware: a
power-cycled device now plays a note from `hs4u.py` on the first run, with a
single generated init pass, no captured bytes and no repetition.

**The start command is a full 512-byte block:**

```
ff ff 01 00  followed by 508 ZERO bytes
```

Not a short packet. Not padded with 0xFF filler. Padded with 0x00.

Our earlier hack — a 4-byte short packet plus 0xFF filler blocks — did set
`state` bit 7, so every diagnostic reported a healthy running engine that was
consuming data and advancing the ring pointers. But the sockets were never
armed, so nothing made a sound. That single fact explains the entire run of
contradictory results:

- after ACID64 had run, the device was genuinely armed and our init sang
- after a power cycle, it never was, and the identical init was silent
- which made every A/B look non-deterministic

### Corrected minimum sequence

```
1. open device, claim interface 0
2. write ONE 512-byte block: b"\xff\xff\x01\x00" + b"\x00" * 508
   verify status[0x1E] bit 7 is set
3. write the generated init once:
       chip_init_stream(0..3) + delay(40000) + reg(chip 0, 0x1E, 0x00)
4. stream register writes and delays in 512-byte blocks, throttled on
       free = 0x2000 - ((wr - rd) & 0x1FFF)
```

`--warmup` and `--init-passes` are retained as knobs but default to the
minimum: one warm-up of zero, one pass. The capture's repeated partial passes
were ACID64 flushing a partly-filled buffer, exactly as first suspected.

## 21. There are no patches

Worth stating plainly, because it is a natural question. The device is a
register-level pipe to four real SID chips. A "patch" is nothing more than the
register values written — waveform, ADSR, pulse width, filter. The 512-byte
block is transport framing and knows nothing about sound. The VSTi held its
patches host-side and rendered them to these same register writes at 8000 Hz,
which is what its manual means by a *replaceable synthesizer engine*: all the
synth intelligence lives in the host.

| Registers | Voice 1 | Voice 2 | Voice 3 |
|---|---|---|---|
| freq lo/hi | 0x00-0x01 | 0x07-0x08 | 0x0E-0x0F |
| pulse width lo/hi | 0x02-0x03 | 0x09-0x0A | 0x10-0x11 |
| control (waveform + gate) | 0x04 | 0x0B | 0x12 |
| attack/decay | 0x05 | 0x0C | 0x13 |
| sustain/release | 0x06 | 0x0D | 0x14 |

Global: `0x15`/`0x16` filter cutoff (11-bit), `0x17` resonance and filter
routing, `0x18` filter mode and volume.

`hs4u.py` v1.0.0 exposes this as `voice()`, `note_on()`, `note_off()` and
`volume()`, with named constants for the waveform and filter bits.
