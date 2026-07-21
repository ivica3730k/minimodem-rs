"""N-FSK CPFSK modulator + non-coherent soft demodulator.

Continuous-phase for narrow spectrum, non-coherent I/Q magnitudes (~3 dB
worse than coherent but no carrier recovery). Gray-coded symbols so
adjacent-tone confusions cost one bit. Max-log-MAP soft output per bit.
Number of tones is configurable via ``WaveformConfig.num_tones`` -- 4
(default) or 8 for narrower-bandwidth-per-bit at ~1--2 dB SNR penalty.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field

import numpy as np

from weaklink.modem.exceptions import ConfigError, NyquistError

# Legacy module-level constants (still exposed for tests / callers that
# only use 4-FSK). Prefer ``config.num_tones`` / ``config.bits_per_symbol``.
BITS_PER_SYMBOL = 2
NUM_TONES = 4


def _num_symbols(num_tones: int) -> int:
    """Size of the symbol alphabet. For ``num_tones=1`` we run OOK
    (1 frequency, 2 symbols: silence + tone); otherwise symbols and
    tones are 1:1 as in standard N-FSK."""
    return 2 if num_tones == 1 else num_tones


def _bits_per_symbol(num_tones: int) -> int:
    return _num_symbols(num_tones).bit_length() - 1


@functools.lru_cache(maxsize=8)
def _gray_tables(num_tones: int) -> tuple[np.ndarray, np.ndarray]:
    """Binary-to-Gray tables sized for ``num_tones``. ``bits_to_symbol``
    maps a binary index → its Gray-coded symbol index; ``symbol_to_bits``
    is the inverse. For ``num_tones=1`` the tables are the 2-symbol
    identity (OOK: 1 bit ↔ 1 symbol)."""
    if num_tones != 1 and (num_tones < 2 or (num_tones & (num_tones - 1)) != 0):
        raise ConfigError(f"num_tones must be 1 or a power of 2 >= 2, got {num_tones}")
    n_syms = _num_symbols(num_tones)
    bits_per_symbol = n_syms.bit_length() - 1
    bits_to_symbol = np.empty(n_syms, dtype=np.int8)
    symbol_to_bits = np.empty((n_syms, bits_per_symbol), dtype=np.int8)
    for i in range(n_syms):
        gray = i ^ (i >> 1)
        bits_to_symbol[i] = gray
        # ``symbol_to_bits[gray]`` inverts the mapping: given the received
        # symbol ``gray``, recover the original ``i`` -- its bits are what
        # the transmitter packed. We index by ``gray`` and store bits of ``i``.
        for b in range(bits_per_symbol):
            symbol_to_bits[gray, b] = (i >> (bits_per_symbol - 1 - b)) & 1
    return bits_to_symbol, symbol_to_bits


@dataclass(frozen=True)
class WaveformConfig:
    baud: float = 300.0
    sample_rate: float = 18_000.0
    """Internal rate. 18 kHz = 5·LCM(45,300,1200) so samples_per_symbol
    is integer at every preset -- no rounding drift. Nyquist 9 kHz
    covers the 4.1 kHz top tone with ~4.4 samples per cycle."""
    center_hz: float = 1_500.0
    """Centre of the tone stack in the audio passband."""
    tone_spacing_hz: float = 300.0
    """Tone-to-tone spacing. Total spread = ``(num_tones - 1) * spacing``."""
    amplitude: float = 0.25
    """Peak amplitude, well under 1.0 to leave headroom in WAV / audio devices."""
    num_tones: int = 4
    """Number of tone frequencies. Standard N-FSK for values in
    {2, 4, 8, 16}: each tone is one symbol. ``num_tones=1`` selects
    OOK -- one carrier, symbol 0 = silence, symbol 1 = tone --
    which trades throughput for the narrowest bandwidth possible."""

    tones_hz: tuple[float, ...] = field(init=False)

    MIN_TONE_HZ: float = 500.0
    """Guardrail: no tone allowed below this frequency."""

    def __post_init__(self) -> None:
        if self.num_tones != 1 and (self.num_tones < 2 or (self.num_tones & (self.num_tones - 1)) != 0):
            raise ConfigError(f"num_tones must be 1 or a power of 2 >= 2, got {self.num_tones}")
        # OOK sits on a single carrier at ``center_hz``; N-FSK spreads
        # symmetrically around ``center_hz`` at ``tone_spacing_hz``.
        if self.num_tones == 1:
            relative_offsets: tuple[float, ...] = (0.0,)
        else:
            mean_offset = (self.num_tones - 1) / 2.0
            relative_offsets = tuple(
                (i - mean_offset) * self.tone_spacing_hz for i in range(self.num_tones)
            )
        raw_min = self.center_hz + min(relative_offsets)
        if raw_min < self.MIN_TONE_HZ:
            shift = self.MIN_TONE_HZ - raw_min
            object.__setattr__(self, "center_hz", self.center_hz + shift)
        tones = tuple(self.center_hz + off for off in relative_offsets)
        object.__setattr__(self, "tones_hz", tones)
        if self.samples_per_symbol < 8:
            raise ConfigError("sample_rate / baud must be >= 8 samples per symbol")
        nyquist = self.sample_rate / 2.0
        if max(tones) >= nyquist:
            raise NyquistError(
                f"top tone {max(tones):.0f} Hz exceeds Nyquist ({nyquist:.0f} Hz) -- "
                f"num_tones={self.num_tones} at {self.baud} baud needs more bandwidth "
                f"than the sample rate provides"
            )

    @property
    def samples_per_symbol(self) -> int:
        return int(round(self.sample_rate / self.baud))

    @property
    def num_symbols(self) -> int:
        """Symbol-alphabet size. Equals ``num_tones`` for standard N-FSK;
        equals 2 for OOK (``num_tones=1``)."""
        return _num_symbols(self.num_tones)

    @property
    def bits_per_symbol(self) -> int:
        return _bits_per_symbol(self.num_tones)


def bits_to_symbols(bits: bytes, num_tones: int = NUM_TONES) -> np.ndarray:
    """Pack a 0/1 bit stream into symbol indices."""
    bits_per_symbol = _bits_per_symbol(num_tones)
    if len(bits) % bits_per_symbol != 0:
        raise ValueError(f"bit count {len(bits)} not a multiple of {bits_per_symbol}")
    packed = np.frombuffer(bits, dtype=np.int8).reshape(-1, bits_per_symbol)
    # Big-endian bit packing: bit i contributes (bits_per_symbol - 1 - i).
    weights = 1 << np.arange(bits_per_symbol - 1, -1, -1, dtype=np.int64)
    keys = (packed.astype(np.int64) * weights).sum(axis=1)
    lookup, _ = _gray_tables(num_tones)
    return lookup[keys]


def symbols_to_bits(symbols: np.ndarray, num_tones: int = NUM_TONES) -> bytes:
    _, symbol_to_bits = _gray_tables(num_tones)
    return bytes(symbol_to_bits[symbols].reshape(-1).tolist())


def modulate(symbols: np.ndarray, config: WaveformConfig) -> np.ndarray:
    """Vectorised modulator: continuous-phase N-FSK for ``num_tones >= 2``;
    on-off keying for ``num_tones == 1`` (symbol 0 = silence, symbol 1 =
    continuous-phase sine at the carrier)."""
    samples_per_symbol = config.samples_per_symbol
    if len(symbols) == 0:
        return np.zeros(0, dtype=np.float32)
    dt = 1.0 / config.sample_rate
    symbols_int = np.asarray(symbols, dtype=np.int64)

    if config.num_tones == 1:
        # OOK. Keep the carrier phase running continuously (even during
        # off symbols) so on-symbols never produce a phase discontinuity
        # -- narrower spectrum, no glitches at bit boundaries.
        omega = 2.0 * np.pi * config.tones_hz[0] * dt
        n_offsets = np.arange(1, samples_per_symbol + 1, dtype=np.float64)
        num_symbols = symbols_int.shape[0]
        start_index = np.arange(num_symbols, dtype=np.int64) * samples_per_symbol
        phases = omega * (start_index[:, None] + n_offsets[None, :])
        carrier = np.sin(phases)
        gate = symbols_int.astype(np.float64)[:, None]
        return (config.amplitude * gate * carrier).ravel().astype(np.float32)

    # Standard N-FSK: per-symbol frequency + continuous phase.
    omega_per_symbol = 2.0 * np.pi * np.asarray(config.tones_hz, dtype=np.float64)[symbols_int] * dt
    # Phase at the START of each symbol = 0 for i=0, cumsum(omega*sps) shifted for i>=1.
    end_phases = np.cumsum(omega_per_symbol * samples_per_symbol)
    start_phases = np.empty_like(end_phases)
    start_phases[0] = 0.0
    start_phases[1:] = end_phases[:-1]
    # phases[i, n] = start_phases[i] + omega[i] * (n + 1), n = 0..sps-1.
    n_offsets = np.arange(1, samples_per_symbol + 1, dtype=np.float64)
    phases = start_phases[:, None] + omega_per_symbol[:, None] * n_offsets[None, :]
    return (config.amplitude * np.sin(phases).ravel()).astype(np.float32)


def demodulate_soft(samples: np.ndarray, config: WaveformConfig, *, frequency_offset_hz: float = 0.0) -> np.ndarray:
    """Non-coherent demod. Returns ``(N, num_tones)`` squared magnitudes.
    ``frequency_offset_hz`` shifts the reference tones."""
    samples_per_symbol = config.samples_per_symbol
    num_symbols = len(samples) // samples_per_symbol
    if num_symbols == 0:
        return np.zeros((0, config.num_tones), dtype=np.float64)
    trimmed = np.asarray(samples[: num_symbols * samples_per_symbol], dtype=np.float64)
    windowed = trimmed.reshape(num_symbols, samples_per_symbol)

    time_axis = np.arange(samples_per_symbol) / config.sample_rate
    magnitudes_sq = np.empty((num_symbols, config.num_tones), dtype=np.float64)
    for tone_index, freq in enumerate(config.tones_hz):
        adjusted_freq = freq + frequency_offset_hz
        cos_wave = np.cos(2.0 * np.pi * adjusted_freq * time_axis)
        sin_wave = np.sin(2.0 * np.pi * adjusted_freq * time_axis)
        in_phase = windowed @ cos_wave
        quadrature = windowed @ sin_wave
        magnitudes_sq[:, tone_index] = in_phase ** 2 + quadrature ** 2
    return magnitudes_sq


def estimate_coarse_frequency_offset(
    samples: np.ndarray,
    config: WaveformConfig,
    *,
    search_range_hz: float = 1500.0,
) -> float:
    """Coarse LO offset via FFT + geometric-mean scoring on the 4 tones.
    Geometric mean forces energy at every tone slot (sum-of-magnitudes
    lets one dominant peak drag the estimate). ±10 Hz bin smoothing
    absorbs CPFSK spectral smear; step 2 Hz.
    """
    if len(samples) == 0:
        return 0.0
    fft_length = len(samples)
    spectrum = np.abs(np.fft.rfft(np.asarray(samples, dtype=np.float64), n=fft_length))
    bin_hz = config.sample_rate / fft_length
    step_hz = max(bin_hz, 2.0)
    window_bins = max(1, int(round(10.0 / bin_hz)))  # ±10 Hz around each tone

    def _tone_energy(bin_index: int) -> float:
        lo = max(0, bin_index - window_bins)
        hi = min(len(spectrum), bin_index + window_bins + 1)
        if hi <= lo:
            return 0.0
        return float(spectrum[lo:hi].sum())

    offsets = np.arange(-search_range_hz, search_range_hz + step_hz, step_hz)
    best_offset = 0.0
    best_score = -np.inf
    for offset in offsets:
        log_score = 0.0
        for tone in config.tones_hz:
            bin_index = int(round((tone + offset) / bin_hz))
            e = _tone_energy(bin_index)
            log_score += np.log(e + 1e-12)  # avoid log(0)
        if log_score > best_score:
            best_score = log_score
            best_offset = float(offset)
    return best_offset


def estimate_frequency_offset(
    samples: np.ndarray,
    config: WaveformConfig,
    expected_symbols: np.ndarray,
    *,
    search_range_hz: float = 50.0,
    resolution_hz: float = 1.0,
    prior_offset_hz: float = 0.0,
) -> float:
    """Fine offset from a known symbol sequence (usually the preamble).

    Sweeps ``[prior_offset_hz ± search_range_hz]`` at ``resolution_hz`` and
    picks the offset maximising energy at the correct tone per symbol.
    Returns absolute offset in Hz.
    """
    samples_per_symbol = config.samples_per_symbol
    expected_length = len(expected_symbols) * samples_per_symbol
    if len(samples) < expected_length:
        return prior_offset_hz
    windowed = np.asarray(samples[:expected_length], dtype=np.float64).reshape(len(expected_symbols), samples_per_symbol)
    time_axis = np.arange(samples_per_symbol) / config.sample_rate

    offsets = np.arange(
        prior_offset_hz - search_range_hz,
        prior_offset_hz + search_range_hz + resolution_hz,
        resolution_hz,
    )
    best_offset = prior_offset_hz
    best_score = -np.inf
    is_ook = config.num_tones == 1
    for offset in offsets:
        total_correct_energy = 0.0
        for position, symbol in enumerate(expected_symbols):
            symbol_int = int(symbol)
            # OOK: only tone-symbols (== 1) contribute; silence-symbols
            # have no reference frequency to align to.
            if is_ook and symbol_int == 0:
                continue
            tone_index = 0 if is_ook else symbol_int
            freq = config.tones_hz[tone_index] + offset
            cos_wave = np.cos(2.0 * np.pi * freq * time_axis)
            sin_wave = np.sin(2.0 * np.pi * freq * time_axis)
            in_phase = windowed[position] @ cos_wave
            quadrature = windowed[position] @ sin_wave
            total_correct_energy += in_phase ** 2 + quadrature ** 2
        if total_correct_energy > best_score:
            best_score = total_correct_energy
            best_offset = float(offset)
    return best_offset


def soft_bits_from_magnitudes(magnitudes_sq: np.ndarray, num_tones: int = NUM_TONES) -> np.ndarray:
    """Turn per-symbol tone magnitudes into per-bit soft LLR-shaped values.

    Returns a 1-D array of length ``bits_per_symbol * num_symbols``.
    Positive → bit is more likely 0; negative → more likely 1.
    """
    num_symbols_seen = magnitudes_sq.shape[0]
    if num_symbols_seen == 0:
        return np.zeros(0, dtype=np.float64)

    if num_tones == 1:
        # OOK: single magnitude column. Threshold adaptively at the
        # per-frame median (a robust noise-floor estimate) and emit
        # LLR = threshold - magnitude -- positive for silence (bit 0),
        # negative for tone (bit 1).
        column = magnitudes_sq[:, 0]
        threshold = float(np.median(column))
        return (threshold - column).astype(np.float64)

    _, bit_masks = _gray_tables(num_tones)  # (num_tones, bits_per_symbol)
    bits_per_symbol = _bits_per_symbol(num_tones)
    llrs = np.empty((num_symbols_seen, bits_per_symbol), dtype=np.float64)
    for bit_position in range(bits_per_symbol):
        zero_symbols = np.where(bit_masks[:, bit_position] == 0)[0]
        one_symbols = np.where(bit_masks[:, bit_position] == 1)[0]
        max_zero = magnitudes_sq[:, zero_symbols].max(axis=1)
        max_one = magnitudes_sq[:, one_symbols].max(axis=1)
        llrs[:, bit_position] = max_zero - max_one
    return llrs.reshape(-1)
