# Tools

Two generations of scripts, kept for different reasons.

Run them from the repository root. They import `hardsid4u` from the installed
package, falling back to `../src/hardsid4u` in a source tree:

```bash
uv run python3 tools/detector_calibration.py
uv run --extra audio python3 tools/audible_latency_probe.py
```

---

## Diagnostics (current, and worth using)

These are how the engine-stall failure was found. The important one is
`engine_executing()` in the library: **every conventional health metric — USB
errors, short writes, ring pointers, `free` — stays green through that
failure**, so anything that watches those alone will report a healthy bus
while the device silently plays nothing.

| Script | What it does |
|---|---|
| `detector_calibration.py` | Verifies the stall detector actually measures delay execution, by sweeping the delay across two decades. Run this before trusting any of the others. |
| `engine_stall_hunt.py` | Drives traffic while polling for the stall, so time-to-stall is a number. `--pad filler\|delay0` is the A/B that identified the cause. |
| `escape_prefix_test.py` | Demonstrates `0xFFFF` as an escape prefix: two words change the system mode, observable in `status[0x1E]`. |
| `engine_stop_trigger_test.py` | Which byte patterns halt the device, and — just as usefully — which do not. |
| `bend_flood_test.py` | Instrumented note+pitch-bend flood with full per-write bookkeeping. |
| `midi_stress_via_coremidi.py` | Drives the real `midi.py` through a real CoreMIDI connection, as a DAW would. |
| `pointer_slip_test.py` | Tracks predicted vs actual ring write pointer; finds the ~0.1% `+512` miscount. |
| `dearm_signature_test.py` | Whether a de-armed socket reproduces the hanging-note symptom, plus status-block diffing. |
| `stuck_note_diagnosis.py` | Fast repro and in-place recovery ladder for a stuck note. |
| `engine_health_probe.py` | Endurance run driving `midi.py` with raw MIDI, including panic bursts. |

**Caution:** several of these deliberately wedge the device, which costs a
power cycle. `engine_stop_trigger_test.py` and `engine_stall_hunt.py` say so
in their docstrings. Read before running.

## Acoustic verification (needs `--extra audio`)

The bus can look perfect while nothing comes out, so some questions can only
be answered with a microphone or a line input.

| Script | What it does |
|---|---|
| `audible_latency_probe.py` | Measures real acoustic latency by onset detection. |
| `midi_latency_probe.py` | Transport-only latency: how long a note-on takes to reach the wire. |
| `pointer_audio_correlation_test.py` | Correlates pointer anomalies against recorded audio. |
| `idle_gap_test.py`, `filler_injection_test.py`, `wrap_boundary_test.py`, `lookahead_ab_test.py` | A/B tests of eliminated theories — idle time, filler injection, ring wrap, and traffic shape. All came back negative; kept so nobody re-runs them expecting otherwise. |

---

## Bring-up probes (historical, provenance only)

The bisection tools used while working the protocol out.

**These are not good code and several encode assumptions now known to be
wrong.** `hs4u_m1_status.py` and `hs4u_m2_handshake.py` in particular send a
4-byte `ff ff 01 00` short packet that we mistakenly believed was the start
command. Do not use them as examples; use `src/hardsid4u/hs4u.py`.

| Script | Question it answered |
|---|---|
| `hs4u_m1_status.py` | Is the status block readable, and are the ring pointers sane? |
| `hs4u_m2_handshake.py` | Is there a mode handshake? (We concluded no. We were wrong — see `escape_prefix_test.py`.) |
| `hs4u_m2b_probe.py` | Which endpoint and payload shape moves the write pointer? |
| `hs4u_m3_note.py` | First attempt at an audible note. |
| `hs4u_m3b_timing.py` | Fixed 8 kHz tick, or delta timing? (Delta.) |
| `hs4u_m4_kick.py` | What starts the consume engine? (None of the guesses.) |
| `hs4u_m5_bisect.py` | Which earlier action had started it? |
| `hs4u_m6_probe.py` | Is the volume register reachable at any command base? |
| `hs4u_m7_note_after_init.py` | Is the init the missing piece? (Yes.) |
| `hs4u_replay.py` | Does replaying the capture verbatim reproduce the music? (Yes.) |
