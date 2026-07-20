"""All shared module-level constants. Single source of truth so tuning
knobs are grep-able and clashes are impossible.

Kept small on purpose: values that are truly private to one module
(e.g. an internal buffer size only ``codec.py`` uses) stay next to
their user. Anything imported from more than one module lives here.
"""

from __future__ import annotations

from pathlib import Path

# ---- Baud presets --------------------------------------------------------

# Per-baud presets. ``tone_spacing_hz`` widened at low bauds so the four
# tones spread across enough Hz to survive room modes and mic roll-off.
BAUD_PRESETS: dict[float, dict[str, float]] = {
    45.0:   dict(tone_spacing_hz=200.0, rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=4, sync_every_blocks=4),
    300.0:  dict(tone_spacing_hz=300.0, rs_data_bytes=16, rs_parity_bytes=8,  block_repeats=2, sync_every_blocks=4),
    1200.0: dict(tone_spacing_hz=1200.0, rs_data_bytes=16, rs_parity_bytes=8, block_repeats=2, sync_every_blocks=4),
}

# ---- Live TX pilot padding -----------------------------------------------

#: Pilot each side of live-tx: wakes the sink from IDLE (~50 ms) and
#: gives the coarse-offset FFT real N-FSK tone energy to lock onto.
LIVE_TX_PILOT_MIN_SECONDS: float = 0.2

#: Pilot must also exceed the preamble in symbol space so back-to-back
#: tx buffers keep > 2 * preamble_length between adjacent preambles.
#: Matters at low baud where 0.2 s is only ~9 symbols.
LIVE_TX_PILOT_MIN_SYMBOLS: int = 40

#: Floor on total live-tx duration. 1200-baud single-char is ~250 ms of
#: signal -- too short to give RX two clean poll windows. Pad to 1 s.
LIVE_TX_MIN_SECONDS: float = 1.0

# ---- Hamlib rigctld PTT --------------------------------------------------

#: rigctld TCP default; matches the ``--hamlib-ptt`` bare-flag default.
HAMLIB_DEFAULT_PORT: int = 4532

#: PTT-to-audio guard. Radios need a beat between key-up and first
#: sample or the leading pilot gets clipped by relay / AGC settling.
HAMLIB_PTT_LEAD_SECONDS: float = 0.1

#: Symmetric tail: hold PTT past the last sample so the trailing pilot
#: makes it onto the air before the relay drops.
HAMLIB_PTT_TAIL_SECONDS: float = 0.1

# ---- Live RX poll loop ---------------------------------------------------

#: Live-rx poll cadence.
LIVE_RX_POLL_MS: int = 100

#: Frequency of the audio peak / rms snapshot log line during live rx.
LIVE_RX_SNAPSHOT_EVERY_POLLS: int = 10

# ---- CLI -----------------------------------------------------------------

#: Where CLI diagnostics land by default (kept out of stdout).
DEFAULT_LOG_PATH: Path = Path("log.txt")
