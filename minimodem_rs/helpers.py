"""Framing codec and minimodem passthrough helpers."""

from __future__ import annotations

import subprocess
import zlib
from dataclasses import dataclass
from typing import BinaryIO

import reedsolo

CRC_BYTES = 4
READ_CHUNK_BYTES = 64

# minimodem flags that take no value. Anything not listed here is assumed to
# take a value, so ``--mm-<flag> <value>`` consumes ``<value>``. This lets
# users write ``--mm-help`` without accidentally swallowing the BAUD_MODE
# positional. Users can always fall back to ``--mm-<flag>=<value>`` form.
MINIMODEM_BARE_FLAGS = frozenset(
    {
        # Direction (minimodem accepts long/short aliases; we forward whatever the user typed).
        "tx",
        "transmit",
        "write",
        "t",
        "rx",
        "receive",
        "read",
        "r",
        # Modulation options that take no value.
        "auto-carrier",
        "a",
        "inverted",
        "i",
        "ascii",
        "8",
        "7",
        "baudot",
        "5",
        "invert-start-stop",
        "quiet",
        "q",
        "version",
        "V",
        "float-samples",
        "rx-one",
        # ``--help``/``-h`` are not real minimodem options in 0.24, but minimodem prints its
        # usage on any unknown flag, so treating them as bare gives the user what they expect.
        "help",
        "h",
    }
)


@dataclass(frozen=True)
class FramingConfig:
    data_bytes: int
    parity_bytes: int
    sync_payload: bytes
    fec_enabled: bool
    crc_enabled: bool


class ReedSolomonFramer:
    """Blocks the byte stream into Reed-Solomon-protected frames.

    When ``fec_enabled`` is false the framer becomes a pure pass-through: TX
    writes bytes as-is, RX reads bytes as-is. When it is true, TX emits blocks
    of ``block_size`` bytes and periodically injects a sync block; RX aligns to
    the block grid by searching for a decodable frame.

    ``crc_enabled`` appends a CRC-32 of the payload inside the RS-protected
    region so RX can reject blocks that RS ``corrected'' into garbage.
    """

    def __init__(self, config: FramingConfig):
        self.config = config
        self._sync_payload = config.sync_payload[: config.data_bytes].ljust(config.data_bytes, b"\x00")
        self._reed_solomon = reedsolo.RSCodec(config.parity_bytes) if config.fec_enabled else None

    @property
    def data_bytes(self) -> int:
        return self.config.data_bytes

    @property
    def sync_payload(self) -> bytes:
        return self._sync_payload

    @property
    def block_size(self) -> int:
        crc_size = CRC_BYTES if self.config.crc_enabled else 0
        return self.config.data_bytes + crc_size + self.config.parity_bytes

    def encode_data_block(self, payload: bytes) -> bytes:
        if len(payload) != self.config.data_bytes:
            raise ValueError(f"payload must be exactly {self.config.data_bytes} bytes, got {len(payload)}")
        body = payload + zlib.crc32(payload).to_bytes(CRC_BYTES, "big") if self.config.crc_enabled else payload
        assert self._reed_solomon is not None
        return bytes(self._reed_solomon.encode(body))

    def encode_sync_block(self) -> bytes:
        return self.encode_data_block(self._sync_payload)

    def try_decode_block(self, block: bytes) -> bytes | None:
        """Return the decoded payload, or None if the block cannot be trusted."""
        assert self._reed_solomon is not None
        try:
            decoded = bytes(self._reed_solomon.decode(block)[0])
        except reedsolo.ReedSolomonError:
            return None
        if self.config.crc_enabled:
            if len(decoded) < CRC_BYTES:
                return None
            payload, received_crc = decoded[:-CRC_BYTES], decoded[-CRC_BYTES:]
            if int.from_bytes(received_crc, "big") != zlib.crc32(payload):
                return None
            return payload
        return decoded


def split_minimodem_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split ``--mm-<key>[=<val>|<val>]`` args out of ``argv``.

    Returns ``(filtered_argv, minimodem_passthrough_argv)``. The passthrough
    list is ready to be spliced into a ``minimodem`` argv (``--mm-foo bar``
    becomes ``--foo bar``, ``--mm-foo=bar`` becomes ``--foo=bar``, bare
    ``--mm-foo`` becomes ``--foo``).
    """
    filtered: list[str] = []
    passthrough: list[str] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument.startswith("--mm-"):
            if "=" in argument:
                key, value = argument.split("=", 1)
                passthrough.append(f"--{key[len('--mm-'):]}={value}")
            else:
                key = argument[len("--mm-") :]
                if key in MINIMODEM_BARE_FLAGS:
                    passthrough.append(f"--{key}")
                elif index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                    passthrough.extend([f"--{key}", argv[index + 1]])
                    index += 1
                else:
                    passthrough.append(f"--{key}")
        else:
            filtered.append(argument)
        index += 1
    return filtered, passthrough


def build_minimodem_argv(
    direction: str,
    passthrough_args: list[str],
    baud_mode: str,
    minimodem_binary: str = "minimodem",
) -> list[str]:
    """Assemble the full argv used to spawn ``minimodem``."""
    if direction not in ("--tx", "--rx"):
        raise ValueError(f"direction must be --tx or --rx, got {direction!r}")
    return [minimodem_binary, direction, *passthrough_args, baud_mode]


def run_tx(
    *,
    minimodem_argv: list[str],
    framer: ReedSolomonFramer,
    sync_every_blocks: int,
    input_stream: BinaryIO,
) -> int:
    """Read from ``input_stream``, frame it, and stream it into minimodem."""
    input_bytes = input_stream.read()
    with subprocess.Popen(minimodem_argv, stdin=subprocess.PIPE) as modem_process:
        assert modem_process.stdin is not None
        if framer.config.fec_enabled:
            block_bytes = framer.data_bytes
            remainder = len(input_bytes) % block_bytes
            if remainder:
                input_bytes = input_bytes + b"\x00" * (block_bytes - remainder)

            blocks_written = 0
            for offset in range(0, len(input_bytes), block_bytes):
                payload = input_bytes[offset : offset + block_bytes]
                modem_process.stdin.write(framer.encode_data_block(payload))
                blocks_written += 1
                if sync_every_blocks > 0 and blocks_written % sync_every_blocks == 0:
                    modem_process.stdin.write(framer.encode_sync_block())
        else:
            modem_process.stdin.write(input_bytes)
        modem_process.stdin.flush()
    return modem_process.returncode


def run_rx(
    *,
    minimodem_argv: list[str],
    framer: ReedSolomonFramer,
    output_stream: BinaryIO,
) -> int:
    """Read framed bytes from minimodem and write decoded payloads to ``output_stream``."""
    with subprocess.Popen(minimodem_argv, stdout=subprocess.PIPE) as modem_process:
        assert modem_process.stdout is not None
        if framer.config.fec_enabled:
            _rx_stream_with_fec(modem_process.stdout, framer, output_stream)
        else:
            while True:
                chunk = modem_process.stdout.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                output_stream.write(chunk)
                output_stream.flush()
    return modem_process.returncode


def _rx_stream_with_fec(
    modem_stdout: BinaryIO,
    framer: ReedSolomonFramer,
    output_stream: BinaryIO,
) -> None:
    buffer = bytearray()
    block_size = framer.block_size
    sync_payload = framer.sync_payload
    while True:
        chunk = modem_stdout.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        while len(buffer) >= block_size:
            candidate_block = bytes(buffer[:block_size])
            decoded = framer.try_decode_block(candidate_block)
            if decoded is None:
                del buffer[0]
                continue
            if decoded == sync_payload:
                del buffer[:block_size]
                continue
            output_stream.write(decoded.rstrip(b"\x00"))
            output_stream.flush()
            del buffer[:block_size]
