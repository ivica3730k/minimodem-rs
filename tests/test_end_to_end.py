"""End-to-end round-trip via minimodem's file I/O."""

from __future__ import annotations

import random
import shutil
import string
from pathlib import Path

import pytest

from minimodem_rs.main import main


@pytest.mark.skipif(shutil.which("minimodem") is None, reason="minimodem binary not installed")
def test_1000_random_chars_round_trip_through_wav_file(tmp_path: Path) -> None:
    alphabet = (string.ascii_letters + string.digits + string.punctuation + " ").encode("ascii")
    rng = random.Random(42)
    message = bytes(rng.choices(alphabet, k=1000))

    message_file = tmp_path / "msg.bin"
    wav_file = tmp_path / "signal.wav"
    output_file = tmp_path / "out.bin"
    message_file.write_bytes(message)

    tx_exit = main(["tx", "--input", str(message_file), "--mm-file", str(wav_file), "1200"])
    assert tx_exit == 0
    assert wav_file.exists() and wav_file.stat().st_size > 0

    rx_exit = main(["rx", "--output", str(output_file), "--mm-file", str(wav_file), "1200"])
    assert rx_exit == 0

    assert output_file.read_bytes() == message
