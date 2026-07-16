# minimodem-rs

A Python wrapper around [`minimodem`](http://www.whence.com/minimodem/) that
adds Reed-Solomon framing on top of the raw byte stream. `minimodem` handles
the RF-facing audio-frequency-shift-keying; this wrapper handles the framing
that makes the byte stream survive a noisy channel.

TX blocks the input into `--data-bytes` chunks, appends an optional CRC-32,
Reed-Solomon-encodes each block, and periodically injects a sync block so the
receiver can align. RX reverses that: it slides a byte-wide window across the
minimodem output until an RS-decodable block appears, drops sync blocks, and
writes payload bytes downstream.

## Signal chain

```
stdin ──▶ block ──▶ [CRC-32] ──▶ Reed-Solomon ──▶ [sync every N] ──▶ minimodem --tx ──▶ audio
                                                                                          │
                                                                                          ▼
stdout ◀── strip pad ◀── payload ◀── Reed-Solomon decode ◀── sliding window ◀── minimodem --rx ◀── audio
```

## Requirements

`minimodem` is a **system package** — install it from your distribution:

```bash
sudo apt install minimodem              # Debian/Ubuntu
brew install minimodem                  # macOS
```

Python 3.10+.

## Setup

```bash
poetry install
poetry run pre-commit install
```

## Run

Transmit stdin as 1200-baud AFSK with the default `data=16 / parity=8` framing:

```bash
echo "hello over the air" | poetry run minimodem-rs tx 1200
```

Receive it on another machine (or on a loopback audio device):

```bash
poetry run minimodem-rs rx 1200
```

Pass any minimodem option through with `--mm-<option>`. For example, set the
minimodem confidence threshold and volume:

```bash
poetry run minimodem-rs rx --mm-confidence 1.5 --mm-volume 1.0 1200
poetry run minimodem-rs tx --mm-volume 0.7 --mm-auto-carrier 1200
```

Both `--mm-key value` and `--mm-key=value` work, and bare `--mm-flag` is
forwarded as a bare flag. Well-known minimodem no-value flags (`--help`,
`--version`, `--quiet`, `--auto-carrier`, `--tx-carrier`, `--print-filter`,
`--ascii`, `--baudot`, `-8`/`-7`/`-5`, `--float-samples`, `--binary-output`,
`--invert-start-stop`, `--lut`, `--rx-once`) are recognised and won't
accidentally consume the following argument as a value. For any flag not in
that list, prefer the `--mm-key=value` form if the following token could be
ambiguous.

You can also skip the framing wrapper entirely to query minimodem itself.
When no `tx`/`rx` subcommand is given, `minimodem-rs` forwards its `--mm-*`
arguments straight to `minimodem` and exits with its return code:

```bash
minimodem-rs --mm-version       # forwarded to `minimodem --version`
minimodem-rs --mm-help          # minimodem 0.24 has no --help, but prints its
                                # usage on any unknown flag, so this still works
```

## Options

Framing/FEC options are first-level on both `tx` and `rx`:

| Flag | Default | Description |
|------|---------|-------------|
| `--data-bytes N` | `16` | Payload bytes per RS block. |
| `--parity-bytes N` | `8` | RS parity bytes per block. Corrects up to `N/2` byte errors. |
| `--sync-payload STR` | `ABCDEFGH` | Marker payload for the sync block; used by RX to hard-realign. |
| `--fec` / `--no-fec` | `--fec` | Enable/disable Reed-Solomon. With `--no-fec` the stream is raw bytes and the wrapper is a pure passthrough. |
| `--rs` / `--no-rs` | alias | Aliases for `--fec` / `--no-fec`. |
| `--crc` / `--no-crc` | `--no-crc` | Append a CRC-32 of the payload inside the RS-protected region, so RX rejects blocks that RS "corrected" into garbage. |

TX-only:

| Flag | Default | Description |
|------|---------|-------------|
| `--sync-every N` | `1` | Insert one sync block after every `N` data blocks. |
| `--input FILE` | stdin | Read bytes from a file instead of stdin. |

RX-only:

| Flag | Default | Description |
|------|---------|-------------|
| `--output FILE` | stdout | Write decoded bytes to a file instead of stdout. |

Positional (both tx and rx):

| Argument | Description |
|----------|-------------|
| `BAUD_MODE` | Passed to minimodem as its trailing positional (e.g. `1200`, `300`, `rtty`, `same`). |

Anything of the form `--mm-<key>[=<val>|<val>]` is forwarded to minimodem as
`--<key>[=<val>|<val>]`.

## Framing settings and matching sides

TX and RX must agree on `--data-bytes`, `--parity-bytes`, `--sync-payload`,
`--fec`/`--no-fec`, and `--crc`/`--no-crc`. If they disagree, RX will either
never align or will reject every block.

Rule of thumb: more parity = more error correction, less payload throughput.
With `--parity-bytes 8` you can correct up to 4 byte errors per block; with
`--parity-bytes 16` up to 8, and so on.

## Notes

- Block size is `data_bytes + parity_bytes` without `--crc`, and
  `data_bytes + 4 + parity_bytes` with `--crc`.
- The tail of each output block is stripped of trailing NULs, matching the
  zero-padding TX uses to fill the final block.
- With `--no-fec` (or `--no-rs`) the wrapper is a straight `minimodem` process
  with argument passthrough — useful for A/B'ing framed vs. unframed runs.
