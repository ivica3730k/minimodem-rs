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

Baseline SNR performance measured in a 3 kHz reference bandwidth:

| Config | Cliff SNR | Payload | Duration | Notes |
|---|---:|---|---:|---|
| 300 baud, no RS, no repeat | −3 dB | 21 bytes | ~1 s | modem baseline |
| 30 baud, no RS, no repeat | −15 dB | 21 bytes | ~5 s | slower symbols = more Es/N0 |
| 30 baud, RS(24,16), 3× repeat | **−17 dB** | 15 chars | ~28 s | weak-signal preset |
| 100 baud, RS(174,142), 1× repeat | **−8 dB** | ~140 chars | ~16 s | paragraph, single copy |
| 100 baud, RS(174,142), 2× repeat | **−10 dB** | ~140 chars | ~31 s | paragraph, 2× repeat |

For reference, the Shannon limit at 30 bit/s in 3 kHz is −21.6 dB; at 300 bit/s
it's −11.6 dB. LDPC in place of Viterbi would close another 2–4 dB of that
gap. Ten times more information (140 vs 15 chars) costs ~10 dB of SNR margin;
that's Shannon, not the modem.

## Baud rate range

The same modem code runs from **45 to 700 baud** in a 3 kHz SSB channel with
no config changes other than ``--baud`` (which auto-adjusts the tone spacing).
Above 700 baud the 4-FSK tone stack overflows the SSB passband — switch to a
wider channel or a lower-order modulation (BFSK) for that regime.

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
