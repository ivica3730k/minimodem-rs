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

---

# weaklink — 4-FSK streaming modem

The `weaklink` package is a standalone Python 4-FSK modem designed for HF SSB
and wider channels. It is **streaming**, same semantics as `minimodem-rs`
above: pipe arbitrary bytes in, get arbitrary bytes out, no packet awareness,
no length field on the wire. Callers add whatever framing / protocol they
want on top.

## Signal chain

```
stdin ──▶ RS(N,K)+CRC blocks ──▶ conv encode (K=7, r=1/2, per-block) ──▶
         interleave ──▶ 4-FSK CPFSK ──▶ [preamble][data]*sync_every[preamble]... ──▶ audio
                                                                                       │
                                                                                       ▼
stdout ◀── strip NUL pad ◀── RS decode per block ◀── soft Viterbi ◀── deinterleave ◀──
       ◀── preamble correlator (finds every sync boundary) ◀── non-coherent demod ◀──
```

RX slides a preamble correlator across the received signal, finds every sync
marker, decodes the data blocks between adjacent markers, and emits the
successfully-decoded payload bytes concatenated. Undecodable blocks are
silently dropped (same behaviour as minimodem-rs).

Baseline SNR performance measured in a 3 kHz reference bandwidth. The table
below is auto-generated by ``poetry run weaklink-benchmark`` — do not hand-edit
between the markers.

<!-- BENCHMARK RESULTS START -->

Streaming modem. Payload: 100 random-ASCII bytes. Reference bandwidth: 3 kHz.

| Baud | RS | Sync every | Throughput | Info rate | Our cliff | Shannon | Gap |
|---:|---|---:|---|---:|---:|---:|---:|
| 45 | RS(28,16) | 2 | 100 chars in 43.4 s | 18.4 bit/s | **-10 dB** | -23.7 dB | 13.7 dB |
| 45 | RS(28,16) | 4 | 100 chars in 42.0 s | 19.1 bit/s | **-9 dB** | -23.6 dB | 14.6 dB |
| 45 | RS(28,16) | 8 | 100 chars in 41.3 s | 19.4 bit/s | **-10 dB** | -23.5 dB | 13.5 dB |
| 45 | RS(44,32) | 2 | 100 chars in 36.3 s | 22.1 bit/s | **-11 dB** | -22.9 dB | 11.9 dB |
| 45 | RS(44,32) | 4 | 100 chars in 35.6 s | 22.5 bit/s | **-10 dB** | -22.8 dB | 12.8 dB |
| 45 | RS(44,32) | 8 | 100 chars in 35.6 s | 22.5 bit/s | **-9 dB** | -22.8 dB | 13.8 dB |
| 45 | RS(164,128) | 2 | 100 chars in 32.7 s | 24.4 bit/s | **-9 dB** | -22.5 dB | 13.5 dB |
| 45 | RS(164,128) | 4 | 100 chars in 32.7 s | 24.4 bit/s | **-11 dB** | -22.5 dB | 11.5 dB |
| 45 | RS(164,128) | 8 | 100 chars in 32.7 s | 24.4 bit/s | **-12 dB** | -22.5 dB | 10.5 dB |
| 100 | RS(28,16) | 2 | 100 chars in 19.5 s | 41.0 bit/s | **-8 dB** | -20.2 dB | 12.2 dB |
| 100 | RS(28,16) | 4 | 100 chars in 18.9 s | 42.4 bit/s | **-8 dB** | -20.1 dB | 12.1 dB |
| 100 | RS(28,16) | 8 | 100 chars in 18.6 s | 43.1 bit/s | **-6 dB** | -20.0 dB | 14.0 dB |
| 100 | RS(44,32) | 2 | 100 chars in 16.3 s | 49.0 bit/s | **-8 dB** | -19.4 dB | 11.4 dB |
| 100 | RS(44,32) | 4 | 100 chars in 16.0 s | 50.0 bit/s | **-6 dB** | -19.3 dB | 13.3 dB |
| 100 | RS(44,32) | 8 | 100 chars in 16.0 s | 50.0 bit/s | **-7 dB** | -19.3 dB | 12.3 dB |
| 100 | RS(164,128) | 2 | 100 chars in 14.7 s | 54.3 bit/s | **-7 dB** | -19.0 dB | 12.0 dB |
| 100 | RS(164,128) | 4 | 100 chars in 14.7 s | 54.3 bit/s | **-6 dB** | -19.0 dB | 13.0 dB |
| 100 | RS(164,128) | 8 | 100 chars in 14.7 s | 54.3 bit/s | **-5 dB** | -19.0 dB | 14.0 dB |
| 300 | RS(28,16) | 2 | 100 chars in 6.5 s | 123.0 bit/s | **-3 dB** | -15.4 dB | 12.4 dB |
| 300 | RS(28,16) | 4 | 100 chars in 6.3 s | 127.1 bit/s | **+0 dB** | -15.3 dB | 15.3 dB |
| 300 | RS(28,16) | 8 | 100 chars in 6.2 s | 129.3 bit/s | **+3 dB** | -15.2 dB | 18.2 dB |
| 300 | RS(44,32) | 2 | 100 chars in 5.4 s | 147.1 bit/s | **-2 dB** | -14.6 dB | 12.6 dB |
| 300 | RS(44,32) | 4 | 100 chars in 5.3 s | 150.0 bit/s | **-2 dB** | -14.5 dB | 12.5 dB |
| 300 | RS(44,32) | 8 | 100 chars in 5.3 s | 150.0 bit/s | **-1 dB** | -14.5 dB | 13.5 dB |
| 300 | RS(164,128) | 2 | 100 chars in 4.9 s | 163.0 bit/s | **+0 dB** | -14.2 dB | 14.2 dB |
| 300 | RS(164,128) | 4 | 100 chars in 4.9 s | 163.0 bit/s | **-2 dB** | -14.2 dB | 12.2 dB |
| 300 | RS(164,128) | 8 | 100 chars in 4.9 s | 163.0 bit/s | **-3 dB** | -14.2 dB | 11.2 dB |
| 1200 | RS(28,16) | 2 | 100 chars in 1.6 s | 491.8 bit/s | **+6 dB** | -9.2 dB | 15.2 dB |
| 1200 | RS(28,16) | 4 | 100 chars in 1.6 s | 508.5 bit/s | **+5 dB** | -9.0 dB | 14.0 dB |
| 1200 | RS(28,16) | 8 | 100 chars in 1.5 s | 517.2 bit/s | **+7 dB** | -9.0 dB | 16.0 dB |
| 1200 | RS(44,32) | 2 | 100 chars in 1.4 s | 588.2 bit/s | **+6 dB** | -8.4 dB | 14.4 dB |
| 1200 | RS(44,32) | 4 | 100 chars in 1.3 s | 600.0 bit/s | **+5 dB** | -8.3 dB | 13.3 dB |
| 1200 | RS(44,32) | 8 | 100 chars in 1.3 s | 600.0 bit/s | **+4 dB** | -8.3 dB | 12.3 dB |
| 1200 | RS(164,128) | 2 | 100 chars in 1.2 s | 652.2 bit/s | **+6 dB** | -7.9 dB | 13.9 dB |
| 1200 | RS(164,128) | 4 | 100 chars in 1.2 s | 652.2 bit/s | **+7 dB** | -7.9 dB | 14.9 dB |
| 1200 | RS(164,128) | 8 | 100 chars in 1.2 s | 652.2 bit/s | **+8 dB** | -7.9 dB | 15.9 dB |

<!-- BENCHMARK RESULTS END -->

For reference, the Shannon limit at 30 bit/s in 3 kHz is −21.6 dB; at 300 bit/s
it's −11.6 dB. Ten times more information costs ~10 dB of SNR margin; that's
Shannon, not the modem.

## Baud rate range

The same modem code runs from **45 baud upward** with no config changes other
than ``--baud`` (which auto-adjusts the tone spacing to match). How high you
can go depends on your radio's channel bandwidth — the 4-FSK stack occupies
roughly ``5 × baud`` Hz null-to-null:

| Channel | Usable baud (rough) |
|---|---:|
| Narrow SSB (2.4 kHz) | up to ~500 baud |
| Standard SSB (2.8 kHz) | up to ~600 baud |
| Wide / ESSB (5 kHz) | up to ~1000 baud |
| Narrow FM (~15 kHz) | up to ~3 kbaud |

Behaviour degrades gradually as sideband energy is clipped by the radio's
filter — nothing catastrophic, just some dB of margin lost. If you know the
channel is narrow, drop the baud; if you have wideband hardware, push higher.

Clock-drift tolerance: 100 ppm soundcard mismatch decodes fine at every tested
baud (45, 100, 300, 500, 700) for the short-message preset. Longer packets
(more than ~2000 symbols) may need drift correction, which is currently a
planned follow-up.

## Install

```bash
poetry install
```

Adds `numpy`, `soundfile` (WAV), and `sounddevice` (PulseAudio via PortAudio
on Linux) on top of the existing deps.

On Debian/Ubuntu you'll also want the system audio libraries:

```bash
sudo apt install libportaudio2 libsndfile1
```

## CLI: `weaklink-modem`

Same shape as `minimodem-rs`. Both sides launch with matching config; there
are no headers on the wire.

Simple loopback via WAV:

```bash
echo -n "hello over air" | poetry run weaklink-modem tx --wav /tmp/out.wav
poetry run weaklink-modem rx --wav /tmp/out.wav
```

Pipe an arbitrary-length file end to end:

```bash
poetry run weaklink-modem tx --input long_message.txt --wav /tmp/file.wav
poetry run weaklink-modem rx --output received.txt   --wav /tmp/file.wav
```

Live PulseAudio (default device on Linux; CoreAudio on macOS):

```bash
poetry run weaklink-modem tx < message.txt
poetry run weaklink-modem rx --record-seconds 30 > received.bin
```

Slower baud + more sync markers for a noisy HF channel:

```bash
COMMON="--baud 45 --sync-every 2"
poetry run weaklink-modem tx $COMMON --input msg.txt --wav /tmp/hf.wav
poetry run weaklink-modem rx $COMMON --wav /tmp/hf.wav
```

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--baud N` | `300` | Symbol rate. Every 10× drop buys ~10 dB of SNR budget. |
| `--tone-spacing HZ` | `--baud` | 4-FSK tone spacing. Match to baud for orthogonality. |
| `--sample-rate HZ` | `48000` | Audio sample rate. |
| `--rs-data-bytes N` | `16` | Reed-Solomon data bytes per block. |
| `--rs-parity-bytes N` | `8` | RS parity bytes (corrects up to N/2 byte errors per block). |
| `--no-rs-crc` | CRC on | Strip the 4-byte payload CRC that RS uses to reject bogus decodes. |
| `--sync-every N` | `4` | Preamble inserted every N data blocks. Smaller N = faster re-sync at low SNR, more overhead. |
| `--wav PATH` | live audio | Read/write a WAV file instead of the audio device. |
| `--record-seconds T` | — | RX-only: duration to record from the audio device when `--wav` is not set. |
| `--coarse-freq-search-hz N` | `0` | RX-only: enable FFT-based coarse LO-offset search up to ±N Hz. |

## Running the tests

Unit + integration tests:

```bash
poetry run pytest -q
```

Long SNR-sweep tests (marked `slow`) produce a printed table:

```bash
poetry run pytest -m slow -v -s
```

The suite runs on GitHub Actions; the `slow` marker is included in CI so the
SNR baselines are re-measured on every push.
