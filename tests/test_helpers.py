"""Round-trip and passthrough tests for the framing helpers."""

from __future__ import annotations

import io

import pytest

from minimodem_rs.helpers import (
    FramingConfig,
    ReedSolomonFramer,
    build_minimodem_argv,
    split_minimodem_passthrough,
)
from minimodem_rs import main as main_module


def _make_framer(*, data_bytes: int = 16, parity_bytes: int = 8, crc_enabled: bool = False) -> ReedSolomonFramer:
    return ReedSolomonFramer(
        FramingConfig(
            data_bytes=data_bytes,
            parity_bytes=parity_bytes,
            sync_payload=b"ABCDEFGH",
            fec_enabled=True,
            crc_enabled=crc_enabled,
        )
    )


class TestSplitMinimodemPassthrough:
    def test_extracts_space_separated_pair(self) -> None:
        filtered, passthrough = split_minimodem_passthrough(["tx", "--mm-volume", "5", "1200"])
        assert filtered == ["tx", "1200"]
        assert passthrough == ["--volume", "5"]

    def test_extracts_equals_separated_pair(self) -> None:
        filtered, passthrough = split_minimodem_passthrough(["--mm-volume=5", "rx", "1200"])
        assert filtered == ["rx", "1200"]
        assert passthrough == ["--volume=5"]

    def test_extracts_bare_flag(self) -> None:
        filtered, passthrough = split_minimodem_passthrough(["tx", "--mm-auto-carrier", "--data-bytes", "8", "1200"])
        assert filtered == ["tx", "--data-bytes", "8", "1200"]
        assert passthrough == ["--auto-carrier"]

    def test_bare_flag_followed_by_another_option_stays_bare(self) -> None:
        filtered, passthrough = split_minimodem_passthrough(["--mm-auto-carrier", "--mm-volume", "5"])
        assert filtered == []
        assert passthrough == ["--auto-carrier", "--volume", "5"]

    def test_leaves_non_mm_args_untouched(self) -> None:
        filtered, passthrough = split_minimodem_passthrough(["tx", "--data-bytes", "16", "--parity-bytes", "8", "1200"])
        assert filtered == ["tx", "--data-bytes", "16", "--parity-bytes", "8", "1200"]
        assert passthrough == []


class TestBuildMinimodemArgv:
    def test_places_direction_flag_and_baud_mode(self) -> None:
        argv = build_minimodem_argv(direction="--tx", passthrough_args=["--volume", "5"], baud_mode="1200")
        assert argv == ["minimodem", "--tx", "--volume", "5", "1200"]

    def test_rejects_invalid_direction(self) -> None:
        with pytest.raises(ValueError):
            build_minimodem_argv(direction="tx", passthrough_args=[], baud_mode="1200")


class TestReedSolomonFramerRoundTrip:
    def test_encoded_block_size_matches_declared_size(self) -> None:
        framer = _make_framer()
        assert len(framer.encode_data_block(b"A" * 16)) == framer.block_size

    def test_encoded_block_size_matches_declared_size_with_crc(self) -> None:
        framer = _make_framer(crc_enabled=True)
        encoded = framer.encode_data_block(b"A" * 16)
        assert len(encoded) == framer.block_size == 16 + 4 + 8

    def test_data_block_round_trips(self) -> None:
        framer = _make_framer()
        payload = b"hello, minimode!"
        encoded = framer.encode_data_block(payload)
        assert framer.try_decode_block(encoded) == payload

    def test_data_block_round_trips_with_crc(self) -> None:
        framer = _make_framer(crc_enabled=True)
        payload = b"hello, minimode!"
        encoded = framer.encode_data_block(payload)
        assert framer.try_decode_block(encoded) == payload

    def test_sync_block_round_trips_to_padded_sync_payload(self) -> None:
        framer = _make_framer()
        encoded = framer.encode_sync_block()
        assert framer.try_decode_block(encoded) == framer.sync_payload

    def test_corrects_within_parity_budget(self) -> None:
        framer = _make_framer()
        payload = b"correct up to 4b"
        encoded = bytearray(framer.encode_data_block(payload))
        for corrupt_index in (0, 5, 10, 15):
            encoded[corrupt_index] ^= 0xFF
        assert framer.try_decode_block(bytes(encoded)) == payload

    def test_rejects_random_block_with_crc(self) -> None:
        framer = _make_framer(crc_enabled=True)
        assert framer.try_decode_block(b"\x00" * framer.block_size) is None

    def test_rejects_payload_of_wrong_length(self) -> None:
        framer = _make_framer()
        with pytest.raises(ValueError):
            framer.encode_data_block(b"short")


class TestReedSolomonFramerStreamRecovery:
    def test_shifted_stream_realigns_via_sync_block(self) -> None:
        framer = _make_framer()
        payload = b"aligned_payload!"
        stream = b"\xaa\xbb\xcc" + framer.encode_sync_block() + framer.encode_data_block(payload)
        recovered = _decode_stream(framer, stream)
        assert payload.rstrip(b"\x00") in recovered


class TestMinimodemEscapeHatch:
    def test_forwards_mm_help_directly_to_minimodem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_argv: list[list[str]] = []
        monkeypatch.setattr(main_module.subprocess, "call", lambda argv, *a, **k: captured_argv.append(argv) or 0)
        assert main_module.main(["--mm-help"]) == 0
        assert captured_argv == [["minimodem", "--help"]]

    def test_forwards_mm_version_directly_to_minimodem(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured_argv: list[list[str]] = []
        monkeypatch.setattr(main_module.subprocess, "call", lambda argv, *a, **k: captured_argv.append(argv) or 0)
        assert main_module.main(["--mm-version"]) == 0
        assert captured_argv == [["minimodem", "--version"]]

    def test_does_not_hijack_when_tx_subcommand_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # When a real subcommand is given the escape hatch must not fire; --mm-help is
        # forwarded alongside --tx and the baud mode via the normal tx code path.
        captured_argv: list[list[str]] = []

        def fake_run_tx(*, minimodem_argv, **_kwargs):
            captured_argv.append(minimodem_argv)
            return 0

        monkeypatch.setattr(
            main_module.subprocess,
            "call",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("escape hatch fired")),
        )
        monkeypatch.setattr(main_module, "run_tx", fake_run_tx)
        assert main_module.main(["tx", "--mm-help", "1200"]) == 0
        assert captured_argv == [["minimodem", "--tx", "--help", "1200"]]


def _decode_stream(framer: ReedSolomonFramer, stream: bytes) -> bytes:
    """Mimic the RX byte-shift resync loop for use in unit tests."""
    output = io.BytesIO()
    buffer = bytearray(stream)
    block_size = framer.block_size
    sync_payload = framer.sync_payload
    while len(buffer) >= block_size:
        candidate = bytes(buffer[:block_size])
        decoded = framer.try_decode_block(candidate)
        if decoded is None:
            del buffer[0]
            continue
        if decoded == sync_payload:
            del buffer[:block_size]
            continue
        output.write(decoded.rstrip(b"\x00"))
        del buffer[:block_size]
    return output.getvalue()
