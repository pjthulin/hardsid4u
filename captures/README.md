# Captures

USB traffic recorded with USBPcap in a Windows VM with the HardSID passed
through, bound to WinUSB via Zadig. These are recordings of my own device on
my own machine, kept as the evidence the protocol notes are derived from.

| File | What it shows |
|---|---|
| `01-acid64-playback.pcapng` | ACID64 Player Pro playing a SID tune. Confirms the whole command format. Began mid-session, so the device was already armed. |
| `02-acid64-cold-start.pcapng` | ACID64 driving a freshly power-cycled device. Contains the real start command. This is the one that solved it. |
| `03-our-script-before-after-power-cycle.pcapng` | Our own script run twice while the device was still armed by ACID64 (it worked), then once after a power cycle (it did not). Makes the failure mode visible. |
| `hs4u_capture_writes.bin` | Every byte written to EP 0x02 in capture 01, concatenated. |
| `hs4u_capture_writes.json` | Transfer boundaries and timestamps for the above. |

The `.bin` is no longer needed by the library — the init is generated — but it
is retained so `hs4u.py --verify` can prove the generated init is
byte-identical to what the official software sends.

## Reading them

```bash
tshark -r 02-acid64-cold-start.pcapng
```

Or use the small parser in `tools/` which needs no tshark.
