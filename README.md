# AniDock

AniDock is a Python-based anime downloader focused on a robust **CLI workflow**, built around modular provider adapters and resumable batch downloads.

## Highlights

- Multi-provider search and streaming (`anineko`, `hianime`)
- Provider chain + fallback resolution per episode
- Episode queue input:
  - single (`12`)
  - range (`1-12`)
  - list (`1,7,20`)
  - mixed (`1-3,7,10-12`)
  - queue file (`.txt` / `.json`)
- Resume / retry / cleanup flows from manifest state
- Queue-wide preference application:
  - audio mode (`SUB`, `DUB`, `HSUB/RAW` where available)
  - subtitle output (`separate`, `mux`)
  - quality target
  - container policy (`auto`, `mp4`, `mkv`, `ts`)
- Parallel downloads (1-3 workers)
- Progress, queue status snapshots, and batch summary


## Requirements

- Python 3.10+
- `ffmpeg` recommended (required for mux/remux features)
- Python packages from `requirements.txt`

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python scraper.py
```

## Project Structure

```text
AniDock/
├── cli/                # CLI orchestration and prompts
├── core/               # Shared download engine, parsing, manifest, media, exceptions
├── providers/          # Provider implementations + registry
├── downloads/          # Output directory (created/used at runtime)
├── scraper.py          # CLI launcher
├── requirements.txt
└── README.md
```

## Queue File Formats

### TXT

Each line can be a number, range, or list:

```txt
1-5
7,9
12
```

### JSON

List form:

```json
[1, "4-6", 9]
```

Object form:

```json
{
  "episodes": [1, "4-6", 9]
}
```

## Manifest, Resume, and Cleanup

For each anime folder, AniDock writes a manifest:

```text
downloads/<Anime Name>/.<Anime Name>.download_manifest.json
```

From CLI queue setup you can:
- resume unfinished episodes
- retry failed episodes
- cleanup completed / partial / failed artifacts
- cleanup custom episode subsets

Cleanup also updates manifest state so re-downloads start fresh.

## Output Behavior

### Container policy
- `auto`: try mp4, then mkv, else keep ts
- `mp4`: try mp4 only, else keep ts
- `mkv`: try mkv only, else keep ts
- `ts`: skip remux and keep ts

### Subtitle output
- `separate`: save `.vtt` sidecar
- `mux`: embed subtitle track when supported

## Provider Notes

- Provider availability can vary by episode and mirror health.
- AniDock resolves streams using the selected provider chain and falls back when necessary.
- HiAnime endpoints can intermittently return 500 responses; fallback provider behavior is intentional in those cases.

## Troubleshooting

### Terminal input behaves oddly (`^M`, Enter issues)
```bash
stty sane && stty icrnl
```

### `ffmpeg` not found
Install ffmpeg and retry:

```bash
ffmpeg -version
```

### Episode failed but others worked
Usually source-side availability/server instability. Retry failed episodes from the queue setup menu.

## License

MIT License. See [`LICENSE`](./LICENSE).
