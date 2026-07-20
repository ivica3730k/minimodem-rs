"""rigctld PTT helper. ``hamlib_ptt(spec)`` is a context manager that
keys the radio on entry and releases on exit; ``spec=None`` is a no-op.
"""

from __future__ import annotations

import logging
import socket
import time
from contextlib import contextmanager
from typing import Iterator

from weaklink.modem.constants import (
    HAMLIB_DEFAULT_PORT,
    HAMLIB_PTT_LEAD_SECONDS,
    HAMLIB_PTT_TAIL_SECONDS,
)
from weaklink.modem.exceptions import ConfigError, PTTError

_log = logging.getLogger("weaklink.ptt")


def parse_endpoint(spec: str) -> tuple[str, int]:
    """``host``, ``host:port``, or ``:port`` -> (host, port). Bare host
    keeps the default port; bare ``:port`` keeps localhost."""
    host, sep, port_text = spec.partition(":")
    host = host or "localhost"
    if not sep or not port_text:
        return host, HAMLIB_DEFAULT_PORT
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError(f"invalid --hamlib-ptt port {port_text!r}") from exc
    return host, port


@contextmanager
def hamlib_ptt(spec: str | None) -> Iterator[None]:
    """Key PTT on entry, release on exit. ``spec=None`` -> no-op."""
    if spec is None:
        yield
        return

    host, port = parse_endpoint(spec)
    _log.debug("hamlib PTT: connecting to %s:%d", host, port)
    try:
        sock = socket.create_connection((host, port), timeout=5.0)
    except OSError as e:
        raise PTTError(f"rigctld connect {host}:{port} failed: {e}") from e
    try:
        try:
            sock.sendall(b"T 1\n")
        except OSError as e:
            raise PTTError(f"rigctld T 1 (key up) failed: {e}") from e
        _log.debug("hamlib PTT: keyed, waiting %.0f ms", HAMLIB_PTT_LEAD_SECONDS * 1000)
        time.sleep(HAMLIB_PTT_LEAD_SECONDS)
        yield
        _log.debug("hamlib PTT: holding tail %.0f ms", HAMLIB_PTT_TAIL_SECONDS * 1000)
        time.sleep(HAMLIB_PTT_TAIL_SECONDS)
    finally:
        try:
            sock.sendall(b"T 0\n")
            _log.debug("hamlib PTT: released")
        except OSError:
            _log.warning("hamlib PTT: release failed", exc_info=True)
        sock.close()
