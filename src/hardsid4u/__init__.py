from .hs4u import (  # noqa: F401
    HardSID4U, VERSION,
    chip_init_stream, encode_delay, encode_reg, freq_for_hz,
    PAL_CLOCK, NTSC_CLOCK, PAL_FRAME, MIN_CYCLES,
    VOICE_BASE, R_FREQ_LO, R_FREQ_HI, R_PW_LO, R_PW_HI,
    R_CONTROL, R_AD, R_SR, R_CUTOFF_LO, R_CUTOFF_HI,
    R_RESON_FILT, R_MODE_VOL,
    GATE, SYNC, RING, TEST, TRIANGLE, SAWTOOTH, PULSE, NOISE,
    FILT_LP, FILT_BP, FILT_HP, VOICE3_OFF,
)

__version__ = VERSION
