"""4-FSK CPFSK modulator + non-coherent soft demodulator.

Continuous-phase for narrow spectrum, non-coherent I/Q magnitudes (~3 dB
worse than coherent but no carrier recovery). Gray-coded symbols so
adjacent-tone confusions cost one bit. Max-log-MAP soft output per bit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

BITS_PER_SYMBOL = 2
NUM_TONES = 4  # 2 ** BITS_PER_SYMBOL

# Uniform 4-FSK tone spacing. Total span = 3 * tone_spacing_hz.
UNIFORM_4_OFFSETS: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0)
_UNIFORM_4_MEAN: float = sum(UNIFORM_4_OFFSETS) / len(UNIFORM_4_OFFSETS)


@dataclass(frozen=True)
class WaveformConfig:
    baud: float = 300.0
    sample_rate: float = 18_000.0
    """Internal working rate. 18 kHz = 5 * LCM(45, 300, 1200), so
    samples_per_symbol comes out integer at every preset (400 / 60 /
    15) -- no rounding drift accumulating over long messages. Nyquist
    9 kHz > 4.1 kHz max tone with ~4.4 samples per cycle at the top
    tone. ~2.7x fewer samples than 48 kHz baseline for every DSP
    stage."""
    center_hz: float = 1_500.0
    """Centre of the 4-tone stack in the audio passband."""
    tone_spacing_hz: float = 300.0
    """Tone-to-tone spacing. The four tones sit at
    ``center_hz + (offset - 1.5) * tone_spacing_hz`` for offset in
    ``{0, 1, 2, 3}``. Total stack span is ``3 * tone_spacing_hz``."""
    amplitude: float = 0.25
    """Peak amplitude, well under 1.0 to leave headroom in WAV / audio devices."""

    tones_hz: tuple[float, ...] = field(init=False)

    MIN_TONE_HZ: float = 500.0
    """Guardrail: no tone allowed below this frequency. At high baud the
    default 1500 Hz center would push the lowest tone toward DC. If that would
    happen, we bump the center up so the lowest tone lands at MIN_TONE_HZ."""

    def __post_init__(self) -> None:
        relative_offsets = tuple(
            (offset - _UNIFORM_4_MEAN) * self.tone_spacing_hz for offset in UNIFORM_4_OFFSETS
        )
        raw_min = self.center_hz + min(relative_offsets)
        if raw_min < self.MIN_TONE_HZ:
            shift = self.MIN_TONE_HZ - raw_min
            object.__setattr__(self, "center_hz", self.center_hz + shift)
        tones = tuple(self.center_hz + off for off in relative_offsets)
        object.__setattr__(self, "tones_hz", tones)
        if self.samples_per_symbol < 8:
            raise ValueError("sample_rate / baud must be >= 8 samples per symbol")

    @property
    def samples_per_symbol(self) -> int:
        return int(round(self.sample_rate / self.baud))


# Gray code: symbol index i emits bits GRAY_TO_BITS[i]; a pair of bits
# (b1, b0) selects tone BITS_TO_GRAY[(b1 << 1) | b0].
GRAY_TO_BITS: tuple[tuple[int, int], ...] = ((0, 0), (0, 1), (1, 1), (1, 0))
BITS_TO_GRAY: tuple[int, ...] = (0, 1, 3, 2)


def bits_to_symbols(bits: bytes) -> np.ndarray:
    """Pack an even-length 0/1 bit stream into 4-FSK symbol indices."""
    if len(bits) % BITS_PER_SYMBOL != 0:
        raise ValueError(f"bit count {len(bits)} not a multiple of {BITS_PER_SYMBOL}")
    packed = np.frombuffer(bits, dtype=np.int8).reshape(-1, BITS_PER_SYMBOL)
    keys = (packed[:, 0] << 1) | packed[:, 1]
    lookup = np.asarray(BITS_TO_GRAY, dtype=np.int8)
    return lookup[keys]


def symbols_to_bits(symbols: np.ndarray) -> bytes:
    lookup = np.asarray(GRAY_TO_BITS, dtype=np.int8)
    bit_pairs = lookup[symbols]
    return bytes(bit_pairs.reshape(-1).tolist())


def modulate(symbols: np.ndarray, config: WaveformConfig) -> np.ndarray:
    """Continuous-phase FSK modulator. Returns float32 samples in [-A, A].

    Fully vectorised: per-symbol angular velocity is looked up, cumulative
    end-of-symbol phase is a cumsum, and the full sample matrix builds via
    NumPy broadcasting. Zero Python loop over symbols; equivalent to the
    old for-loop implementation to within float rounding.
    """
    samples_per_symbol = config.samples_per_symbol
    if len(symbols) == 0:
        return np.zeros(0, dtype=np.float32)
    dt = 1.0 / config.sample_rate
    omega = 2.0 * np.pi * np.asarray(config.tones_hz, dtype=np.float64)[
        np.asarray(symbols, dtype=np.int64)
    ] * dt
    # Phase at the START of each symbol = 0 for i=0, cumsum(omega*sps) shifted for i>=1.
    end_phases = np.cumsum(omega * samples_per_symbol)
    start_phases = np.empty_like(end_phases)
    start_phases[0] = 0.0
    start_phases[1:] = end_phases[:-1]
    # phases[i, n] = start_phases[i] + omega[i] * (n + 1), n = 0..sps-1.
    n_offsets = np.arange(1, samples_per_symbol + 1, dtype=np.float64)
    phases = start_phases[:, None] + omega[:, None] * n_offsets[None, :]
    return (config.amplitude * np.sin(phases).ravel()).astype(np.float32)


def demodulate_soft(samples: np.ndarray, config: WaveformConfig, *, frequency_offset_hz: float = 0.0) -> np.ndarray:
    """Non-coherent demod. Returns an ``(N, 4)`` array of squared magnitudes,
    one row per symbol, one column per tone.

    ``frequency_offset_hz`` shifts the reference tones — pass the estimate from
    :func:`estimate_frequency_offset` when TX and RX radios have imperfect LOs.
    """
    samples_per_symbol = config.samples_per_symbol
    num_symbols = len(samples) // samples_per_symbol
    if num_symbols == 0:
        return np.zeros((0, NUM_TONES), dtype=np.float64)
    trimmed = np.asarray(samples[: num_symbols * samples_per_symbol], dtype=np.float64)
    windowed = trimmed.reshape(num_symbols, samples_per_symbol)

    time_axis = np.arange(samples_per_symbol) / config.sample_rate
    magnitudes_sq = np.empty((num_symbols, NUM_TONES), dtype=np.float64)
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
    """FFT coarse-offset estimate via geometric-mean scoring on all 4 tones.

    Sum-of-magnitudes would let one dominant peak (common for
    zero-padded short payloads) drag the offset off; geometric mean
    requires energy at every candidate slot. Each bin smoothed ±10 Hz
    to absorb CPFSK spectral smearing. Step capped at 2 Hz.
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
    for offset in offsets:
        total_correct_energy = 0.0
        for position, symbol in enumerate(expected_symbols):
            freq = config.tones_hz[int(symbol)] + offset
            cos_wave = np.cos(2.0 * np.pi * freq * time_axis)
            sin_wave = np.sin(2.0 * np.pi * freq * time_axis)
            in_phase = windowed[position] @ cos_wave
            quadrature = windowed[position] @ sin_wave
            total_correct_energy += in_phase ** 2 + quadrature ** 2
        if total_correct_energy > best_score:
            best_score = total_correct_energy
            best_offset = float(offset)
    return best_offset


def soft_bits_from_magnitudes(magnitudes_sq: np.ndarray) -> np.ndarray:
    """Turn per-symbol tone magnitudes into per-bit soft LLR-shaped values.

    Returns a 1-D array of length ``2 * num_symbols``, ordered ``[b1_0, b0_0, b1_1, b0_1, ...]``.

    Positive value → bit is more likely 0; negative → more likely 1. Magnitudes
    are unbounded; the Viterbi decoder scales them internally.
    """
    num_symbols = magnitudes_sq.shape[0]
    if num_symbols == 0:
        return np.zeros(0, dtype=np.float64)

    # Max-log-MAP per bit: LLR ≈ max(0-symbol likelihood) - max(1-symbol).
    bit_masks = np.asarray(GRAY_TO_BITS, dtype=np.int8)  # shape (4, 2)
    llrs = np.empty((num_symbols, BITS_PER_SYMBOL), dtype=np.float64)
    for bit_position in range(BITS_PER_SYMBOL):
        zero_symbols = np.where(bit_masks[:, bit_position] == 0)[0]
        one_symbols = np.where(bit_masks[:, bit_position] == 1)[0]
        max_zero = magnitudes_sq[:, zero_symbols].max(axis=1)
        max_one = magnitudes_sq[:, one_symbols].max(axis=1)
        llrs[:, bit_position] = max_zero - max_one
    return llrs.reshape(-1)
