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

# weaklink — 4-FSK weak-signal modem

The `weaklink` package is a standalone Python 4-FSK modem designed for HF SSB
weak-signal work. It doesn't share code with the minimodem wrapper above; think
of it as the "next generation" transport once you've hit the minimodem cliff.

## Signal chain

```
payload bytes
  └─ RS(24,16)+CRC ──▶ conv encode (K=7, r=1/2) ──▶ interleave ──▶
     4-FSK symbols ──▶ [preamble][payload repeated N times] ──▶ CPFSK ──▶ audio
                                                                              │
                                                                              ▼
     ◀── RS decode ◀── soft Viterbi ◀── deinterleave ◀── soft magnitude combine ◀──
     ◀── preamble sync + freq-offset compensation ◀── non-coherent 4-FSK demod ◀──
```

Baseline SNR performance measured in a 3 kHz reference bandwidth. The table
below is auto-generated by ``poetry run weaklink-benchmark`` — do not hand-edit
between the markers.

<!-- BENCHMARK RESULTS START -->

Payload: 100 random-ASCII bytes unless noted. Reference bandwidth: 3 kHz.

| Baud | RS | Repeats | Throughput | Info rate | Our cliff | Shannon | Gap |
|---:|---|---:|---|---:|---:|---:|---:|
| 45 | RS(28,16) | 1&times; | 100 chars in 38.4 s | 20.8 bit/s | **-12 dB** | -23.2 dB | 11.2 dB |
| 45 | RS(44,32) | 1&times; | 100 chars in 35.6 s | 22.5 bit/s | **-12 dB** | -22.8 dB | 10.8 dB |
| 45 | RS(164,128) | 1&times; | 100 chars in 32.7 s | 24.4 bit/s | **-12 dB** | -22.5 dB | 10.5 dB |
| 45 | RS(28,16) | 2&times; | 100 chars in 75.4 s | 10.6 bit/s | **-14 dB** | -26.1 dB | 12.1 dB |
| 45 | RS(44,32) | 2&times; | 100 chars in 69.7 s | 11.5 bit/s | **-14 dB** | -25.8 dB | 11.8 dB |
| 45 | RS(164,128) | 2&times; | 100 chars in 64.0 s | 12.5 bit/s | **-14 dB** | -25.4 dB | 11.4 dB |
| 45 | RS(28,16) | 4&times; | 100 chars in 149.4 s | 5.4 bit/s | **-16 dB** | -29.1 dB | 13.1 dB |
| 45 | RS(44,32) | 4&times; | 100 chars in 138.0 s | 5.8 bit/s | **-16 dB** | -28.7 dB | 12.7 dB |
| 45 | RS(164,128) | 4&times; | 100 chars in 126.6 s | 6.3 bit/s | **-16 dB** | -28.4 dB | 12.4 dB |
| 100 | RS(28,16) | 1&times; | 100 chars in 17.3 s | 46.3 bit/s | **-8 dB** | -19.7 dB | 11.7 dB |
| 100 | RS(44,32) | 1&times; | 100 chars in 16.0 s | 50.0 bit/s | **-8 dB** | -19.3 dB | 11.3 dB |
| 100 | RS(164,128) | 1&times; | 100 chars in 14.7 s | 54.3 bit/s | **-9 dB** | -19.0 dB | 10.0 dB |
| 100 | RS(28,16) | 2&times; | 100 chars in 33.9 s | 23.6 bit/s | **-10 dB** | -22.6 dB | 12.6 dB |
| 100 | RS(44,32) | 2&times; | 100 chars in 31.4 s | 25.5 bit/s | **-9 dB** | -22.3 dB | 13.3 dB |
| 100 | RS(164,128) | 2&times; | 100 chars in 28.8 s | 27.8 bit/s | **-11 dB** | -21.9 dB | 10.9 dB |
| 100 | RS(28,16) | 4&times; | 100 chars in 67.2 s | 11.9 bit/s | **-12 dB** | -25.6 dB | 13.6 dB |
| 100 | RS(44,32) | 4&times; | 100 chars in 62.1 s | 12.9 bit/s | **-12 dB** | -25.3 dB | 13.3 dB |
| 100 | RS(164,128) | 4&times; | 100 chars in 57.0 s | 14.0 bit/s | **-13 dB** | -24.9 dB | 11.9 dB |
| 300 | RS(28,16) | 1&times; | 100 chars in 5.8 s | 138.9 bit/s | **-3 dB** | -14.9 dB | 11.9 dB |
| 300 | RS(44,32) | 1&times; | 100 chars in 5.3 s | 150.0 bit/s | **-3 dB** | -14.5 dB | 11.5 dB |
| 300 | RS(164,128) | 1&times; | 100 chars in 4.9 s | 163.0 bit/s | **-4 dB** | -14.2 dB | 10.2 dB |
| 300 | RS(28,16) | 2&times; | 100 chars in 11.3 s | 70.8 bit/s | **-6 dB** | -17.8 dB | 11.8 dB |
| 300 | RS(44,32) | 2&times; | 100 chars in 10.5 s | 76.5 bit/s | **-5 dB** | -17.5 dB | 12.5 dB |
| 300 | RS(164,128) | 2&times; | 100 chars in 9.6 s | 83.3 bit/s | **-6 dB** | -17.1 dB | 11.1 dB |
| 300 | RS(28,16) | 4&times; | 100 chars in 22.4 s | 35.7 bit/s | **-8 dB** | -20.8 dB | 12.8 dB |
| 300 | RS(44,32) | 4&times; | 100 chars in 20.7 s | 38.7 bit/s | **-7 dB** | -20.5 dB | 13.5 dB |
| 300 | RS(164,128) | 4&times; | 100 chars in 19.0 s | 42.1 bit/s | **-8 dB** | -20.1 dB | 12.1 dB |
| 1200 | RS(28,16) | 1&times; | 100 chars in 1.4 s | 555.6 bit/s | **+3 dB** | -8.6 dB | 11.6 dB |
| 1200 | RS(44,32) | 1&times; | 100 chars in 1.3 s | 600.0 bit/s | **+4 dB** | -8.3 dB | 12.3 dB |
| 1200 | RS(164,128) | 1&times; | 100 chars in 1.2 s | 652.2 bit/s | **+2 dB** | -7.9 dB | 9.9 dB |
| 1200 | RS(28,16) | 2&times; | 100 chars in 2.8 s | 283.0 bit/s | **+1 dB** | -11.7 dB | 12.7 dB |
| 1200 | RS(44,32) | 2&times; | 100 chars in 2.6 s | 306.1 bit/s | **+1 dB** | -11.3 dB | 12.3 dB |
| 1200 | RS(164,128) | 2&times; | 100 chars in 2.4 s | 333.3 bit/s | **+0 dB** | -11.0 dB | 11.0 dB |
| 1200 | RS(28,16) | 4&times; | 100 chars in 5.6 s | 142.9 bit/s | **-1 dB** | -14.7 dB | 13.7 dB |
| 1200 | RS(44,32) | 4&times; | 100 chars in 5.2 s | 154.6 bit/s | **-1 dB** | -14.4 dB | 13.4 dB |
| 1200 | RS(164,128) | 4&times; | 100 chars in 4.7 s | 168.5 bit/s | **-2 dB** | -14.0 dB | 12.0 dB |
| 45 | RS(28,16) | 6&times; | 15 chars in 35.6 s<br/><sub>fixed 15-byte payload, 6x repeat — SNR floor push</sub> | 3.4 bit/s | **-16 dB** | -31.1 dB | 15.1 dB |
| 45 | RS(28,16) | 4&times; | 20 chars in 46.9 s<br/><sub>20 B = 10 sensor reports via protocol codec (~99 B as ASCII)</sub> | 3.4 bit/s | **-16 dB** | -31.0 dB | 15.0 dB |

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

Two subcommands, same shape as `minimodem-rs`. All framing options are
first-level; TX and RX must be launched with matching values (no on-wire
headers).

Simple loopback via WAV:

```bash
echo -n "hello over air" | poetry run weaklink-modem tx --wav /tmp/out.wav
poetry run weaklink-modem rx --wav /tmp/out.wav --length 14
```

Weak-signal preset (15 chars, 30 baud, RS + 3× repeat, ~28 s per packet,
survives down to −17 dB SNR and up to 1 kHz SSB LO error):

```bash
COMMON="--baud 30 --tone-spacing 30 --preamble-length 64 --payload-repeats 3 \
        --rs-data-bytes 16 --rs-parity-bytes 8"

echo -n "HELLO OM 73 DE!" | \
  poetry run weaklink-modem tx $COMMON --wav /tmp/weak.wav

poetry run weaklink-modem rx $COMMON --wav /tmp/weak.wav --length 15
```

Paragraph mode (~140 chars in 16 s, decodes to −8 dB SNR):

```bash
COMMON="--baud 100 --tone-spacing 100 --preamble-length 64 --payload-repeats 1 \
        --rs-data-bytes 150 --rs-parity-bytes 32"

echo -n "CQ CQ CQ this is a longer paragraph over weaklink..." | \
  poetry run weaklink-modem tx $COMMON --wav /tmp/para.wav

poetry run weaklink-modem rx $COMMON --wav /tmp/para.wav --length 142
```

File pipe (arbitrary length, chunked into N RS blocks within a single packet;
each block is protected independently, decoded independently):

```bash
COMMON="--baud 100 --tone-spacing 100 --preamble-length 64 --payload-repeats 1 \
        --rs-data-bytes 32 --rs-parity-bytes 8"

poetry run weaklink-modem tx $COMMON --input long_message.txt --wav /tmp/file.wav

# RX needs to know the original payload length in bytes.
poetry run weaklink-modem rx $COMMON --output received.txt --wav /tmp/file.wav \
  --length $(stat -f%z long_message.txt)   # on macOS; use stat -c%s on Linux
```

Trade-off: RS block size (`--rs-data-bytes`) is per-block, so a big file with
small `rs_data_bytes` means many blocks per packet — good burst tolerance but
each block only has `parity_bytes/2` byte error correction. Bigger
`--rs-data-bytes` (up to 240) reduces block count and spreads error-correction
capacity across more bytes; useful when the channel is uniformly noisy rather
than bursty.

Live PulseAudio (default device on Linux; CoreAudio on macOS):

```bash
# TX plays the modulated audio out of the default audio device
poetry run weaklink-modem tx $COMMON < message.txt

# RX records for a given number of seconds
poetry run weaklink-modem rx $COMMON --record-seconds 30 --length 15
```

## CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--baud N` | `300` | Symbol rate. Every 10× drop buys ~10 dB of SNR budget. |
| `--tone-spacing HZ` | `--baud` | 4-FSK tone spacing. Match to baud for orthogonality. |
| `--sample-rate HZ` | `48000` | Audio sample rate. |
| `--preamble-length N` | `64` | Sync preamble in symbols. Longer = more robust sync at low SNR. |
| `--payload-repeats N` | `1` | Repeat encoded payload N times. RX averages magnitudes; ~3 dB per doubling. |
| `--rs-data-bytes N` | disabled | Enable Reed-Solomon outer with N data bytes per block. |
| `--rs-parity-bytes N` | `8` | RS parity bytes (corrects up to N/2 byte errors). |
| `--no-rs-crc` | CRC on | Strip the 4-byte payload CRC that RS uses to reject bogus decodes. |
| `--wav PATH` | live audio | Read/write a WAV file instead of the audio device. |
| `--length N` | required for RX | Expected payload length in bytes. |

## Handling a wide SSB LO offset

For very cold-start use where the two rigs might disagree on the dial
frequency by up to ~1 kHz, enable coarse offset search:

```python
from weaklink.modem.codec import ModemConfig
from weaklink.modem.waveform import WaveformConfig
config = ModemConfig(
    waveform=WaveformConfig(baud=30, tone_spacing_hz=30),
    coarse_frequency_search_hz=1500.0,  # FFT-based coarse pre-sync
    ...
)
```

Costs ~1 second of decode time. Not exposed on the CLI yet — set via the
library config.

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
