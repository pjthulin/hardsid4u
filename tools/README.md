# Probe scripts

The bisection tools used while working the protocol out, kept for provenance.

**These are not good code and several encode assumptions now known to be
wrong.** `hs4u_m1_status.py` and `hs4u_m2_handshake.py` in particular send a
4-byte `ff ff 01 00` short packet that we mistakenly believed was the start
command. Do not use them as examples; use `src/hardsid4u/hs4u.py`.

| Script | Question it answered |
|---|---|
| `hs4u_m1_status.py` | Is the status block readable, and are the ring pointers sane? |
| `hs4u_m2_handshake.py` | Is there a mode handshake? (No.) |
| `hs4u_m2b_probe.py` | Which endpoint and payload shape moves the write pointer? |
| `hs4u_m3_note.py` | First attempt at an audible note. |
| `hs4u_m3b_timing.py` | Fixed 8 kHz tick, or delta timing? (Delta.) |
| `hs4u_m4_kick.py` | What starts the consume engine? (None of the guesses.) |
| `hs4u_m5_bisect.py` | Which earlier action had started it? |
| `hs4u_m6_probe.py` | Is the volume register reachable at any command base? |
| `hs4u_m7_note_after_init.py` | Is the init the missing piece? (Yes.) |
| `hs4u_replay.py` | Does replaying the capture verbatim reproduce the music? (Yes.) |
