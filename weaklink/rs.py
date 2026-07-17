"""Reed-Solomon block framer.

``data + [CRC-32] + parity`` block layout using the ``reedsolo`` library.
Each RS block carries ``data_bytes`` payload + optional CRC + ``parity_bytes``
correction bytes.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import reedsolo

CRC_BYTES = 4


@dataclass(frozen=True)
class BlockConfig:
    data_bytes: int
    parity_bytes: int
    crc_enabled: bool = True

    @property
    def block_size(self) -> int:
        crc_size = CRC_BYTES if self.crc_enabled else 0
        return self.data_bytes + crc_size + self.parity_bytes

    @property
    def rs_message_size(self) -> int:
        """The size of the RS message (data + optional CRC) before parity."""
        crc_size = CRC_BYTES if self.crc_enabled else 0
        return self.data_bytes + crc_size


class RSBlockCodec:
    def __init__(self, config: BlockConfig):
        self.config = config
        self._codec = reedsolo.RSCodec(config.parity_bytes)

    def encode(self, payload: bytes) -> bytes:
        if len(payload) != self.config.data_bytes:
            raise ValueError(f"payload must be exactly {self.config.data_bytes} bytes, got {len(payload)}")
        message = payload
        if self.config.crc_enabled:
            message = message + zlib.crc32(payload).to_bytes(CRC_BYTES, "big")
        return bytes(self._codec.encode(message))

    def try_decode(self, block: bytes) -> bytes | None:
        """Return the payload bytes on success, or None if the block is untrustworthy."""
        try:
            decoded = bytes(self._codec.decode(block)[0])
        except reedsolo.ReedSolomonError:
            return None
        if self.config.crc_enabled:
            if len(decoded) < CRC_BYTES:
                return None
            payload, crc = decoded[:-CRC_BYTES], decoded[-CRC_BYTES:]
            if int.from_bytes(crc, "big") != zlib.crc32(payload):
                return None
            return payload
        return decoded
