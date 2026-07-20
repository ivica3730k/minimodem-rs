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
        payload, _errors = self.try_decode_with_stats(block)
        return payload

    def try_decode_with_stats(self, block: bytes) -> tuple[bytes | None, int]:
        """Return ``(payload, errors_corrected)``.

        ``errors_corrected`` is the number of byte-symbols the RS decoder had
        to fix. Zero means the block arrived clean; positive means the outer
        code intervened.
        """
        try:
            decoded_msg, _decoded_with_ecc, errata_positions = self._codec.decode(block)
            decoded = bytes(decoded_msg)
            errors_corrected = len(errata_positions)
        except reedsolo.ReedSolomonError:
            return None, 0
        if self.config.crc_enabled:
            if len(decoded) < CRC_BYTES:
                return None, errors_corrected
            payload, crc = decoded[:-CRC_BYTES], decoded[-CRC_BYTES:]
            if int.from_bytes(crc, "big") != zlib.crc32(payload):
                return None, errors_corrected
            return payload, errors_corrected
        return decoded, errors_corrected
