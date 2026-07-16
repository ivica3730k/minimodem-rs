"""End-to-end round-trip with a fraction of the WAV file's bytes randomly mangled."""

from __future__ import annotations

import random
import shutil
import string
from pathlib import Path

import pytest

from minimodem_rs.main import main

WAV_HEADER_BYTES = 44


@pytest.mark.skipif(shutil.which("minimodem") is None, reason="minimodem binary not installed")
@pytest.mark.parametrize("corruption_rate", [0.05, 0.20])
def test_1000_random_chars_recovers_after_wav_byte_corruption(tmp_path: Path, corruption_rate: float) -> None:
    alphabet = (string.ascii_letters + string.digits + string.punctuation + " ").encode("ascii")
    rng = random.Random(42)
    message = bytes(rng.choices(alphabet, k=1000))

    message_file = tmp_path / "msg.bin"
    wav_file = tmp_path / "signal.wav"
    output_file = tmp_path / "out.bin"
    message_file.write_bytes(message)

    tx_exit = main(["tx", "--input", str(message_file), "--mm-file", str(wav_file), "1200"])
    assert tx_exit == 0

    _mangle_wav_bytes(wav_file, corruption_rate=corruption_rate, seed=1337)

    rx_exit = main(["rx", "--output", str(output_file), "--mm-file", str(wav_file), "1200"])
    assert rx_exit == 0

    assert output_file.read_bytes() == message


@pytest.mark.skipif(shutil.which("minimodem") is None, reason="minimodem binary not installed")
@pytest.mark.xfail(
    strict=True,
    reason="80% WAV byte corruption breaks minimodem's carrier lock, producing byte deletions that RS(24,16) cannot correct.",
)
def test_1000_random_chars_does_not_recover_at_80pct_wav_byte_corruption(tmp_path: Path) -> None:
    alphabet = (string.ascii_letters + string.digits + string.punctuation + " ").encode("ascii")
    rng = random.Random(42)
    message = bytes(rng.choices(alphabet, k=1000))

    message_file = tmp_path / "msg.bin"
    wav_file = tmp_path / "signal.wav"
    output_file = tmp_path / "out.bin"
    message_file.write_bytes(message)

    tx_exit = main(["tx", "--input", str(message_file), "--mm-file", str(wav_file), "1200"])
    assert tx_exit == 0

    _mangle_wav_bytes(wav_file, corruption_rate=0.80, seed=1337)

    rx_exit = main(["rx", "--output", str(output_file), "--mm-file", str(wav_file), "1200"])
    assert rx_exit == 0

    assert output_file.read_bytes() == message


def _mangle_wav_bytes(wav_path: Path, corruption_rate: float, seed: int) -> None:
    audio_bytes = bytearray(wav_path.read_bytes())
    corruption_rng = random.Random(seed)
    payload_offset = WAV_HEADER_BYTES
    corruptible_count = len(audio_bytes) - payload_offset
    corruption_count = int(corruptible_count * corruption_rate)
    corrupted_indexes = corruption_rng.sample(range(payload_offset, len(audio_bytes)), corruption_count)
    for byte_index in corrupted_indexes:
        audio_bytes[byte_index] ^= corruption_rng.randint(1, 255)
    wav_path.write_bytes(bytes(audio_bytes))
