"""Streaming tx: encode_stream consumes bytes as they arrive, yields
audio incrementally, and the resulting stream decodes byte-perfect
even for payloads that were previously capped by the 1-byte
block_index limit."""

from __future__ import annotations

import numpy as np
import pytest

from weaklink.modem.codec import ModemConfig, decode, encode, encode_stream
from weaklink.modem.waveform import WaveformConfig

from ._streaming import pump_decode


def _config(baud: float = 1200.0) -> ModemConfig:
    return ModemConfig(
        waveform=WaveformConfig(baud=baud, tone_spacing_hz=baud),
        rs_data_bytes=16,
        rs_parity_bytes=8,
        block_repeats=1,
    )


def _stream_encode(payload: bytes, config: ModemConfig, chunk_size: int) -> np.ndarray:
    def chunks():
        for i in range(0, len(payload), chunk_size):
            yield payload[i : i + chunk_size]

    parts = list(encode_stream(chunks(), config))
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, dtype=np.float32)


def test_streaming_matches_batch_encode() -> None:
    """encode_stream chunked over the input yields the same audio as
    the batch encode() wrapper."""
    payload = b"".join(bytes([(i * 7 + 3) & 0xFF]) for i in range(1000))
    config = _config()
    batch = encode(payload, config)
    streamed = _stream_encode(payload, config, chunk_size=17)
    assert np.array_equal(batch, streamed)


@pytest.mark.parametrize("size", [500, 5_000, 20_000])
def test_stream_roundtrip_various_sizes(size: int) -> None:
    """Random payload, chunked at 100 B, decodes to the original bytes."""
    payload = np.random.default_rng(size).bytes(size)
    config = _config()
    audio = _stream_encode(payload, config, chunk_size=100)
    assert decode(audio, config) == payload


def test_repetitive_20kb_survives_spurious_correlator_peaks() -> None:
    """Highly repetitive payload used to fool the correlator into
    finding an extra peak mid-stream; the decoder must now recover by
    dropping the spurious peak instead of flushing the whole tail."""
    payload = (("a" * 500 + "\n" + "b" * 500 + "\n") * 20).encode()
    assert len(payload) == 20040
    config = _config()
    audio = _stream_encode(payload, config, chunk_size=100)
    assert decode(audio, config) == payload


def test_over_256_blocks_no_longer_raises() -> None:
    """Was capped at 256 blocks (1-byte index). 2-byte index lifts
    that; 500 blocks worth of data should just work."""
    config = _config()
    # payload_per_block = 16 - 3 = 13; 500 blocks = 6500 B
    payload = b"x" * 6500
    audio = _stream_encode(payload, config, chunk_size=250)
    assert decode(audio, config) == payload


# --- e2e streaming variants -------------------------------------------------


@pytest.mark.parametrize("size", [500, 5_000, 20_000])
def test_stream_roundtrip_e2e_streaming(size: int) -> None:
    payload = np.random.default_rng(size).bytes(size)
    config = _config()
    audio = _stream_encode(payload, config, chunk_size=100)
    assert pump_decode(audio, config) == payload


def test_repetitive_20kb_e2e_streaming() -> None:
    payload = (("a" * 500 + "\n" + "b" * 500 + "\n") * 20).encode()
    config = _config()
    audio = _stream_encode(payload, config, chunk_size=100)
    assert pump_decode(audio, config) == payload


def test_over_256_blocks_e2e_streaming() -> None:
    config = _config()
    payload = b"x" * 6500
    audio = _stream_encode(payload, config, chunk_size=250)
    assert pump_decode(audio, config) == payload
