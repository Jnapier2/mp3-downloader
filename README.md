# MP3 Downloader

[![CI](https://github.com/Jnapier2/mp3-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/Jnapier2/mp3-downloader/actions/workflows/ci.yml)

MP3 Downloader turns a single authorized media URL into a validated MP3 through a controlled Windows workflow. Metadata preflight, bounded recovery, duplicate reconciliation, and redacted support exports help limit repeated work and diagnostic-data disclosure; rights decisions remain with the operator.

A download begins only after metadata preflight, and it counts as complete only after output validation and duplicate reconciliation. A reachable URL or finished transfer alone is not treated as proof of a usable result.

## Retrieval safeguards

- Submitted HTTP(S) URL preflight with embedded-credential and private-address rejection.
- Metadata-only preflight before any media is written.
- Audio-only extraction with DRM, login, paywall, playlist, and live-stream restrictions.
- Bounded retries, adaptive fragment concurrency, queue isolation, and stall recovery.
- Filename containment, symlink/reparse-point checks, and output validation with FFprobe.
- SHA-256/SQLite duplicate reconciliation and visible output by default.
- Minimal support exports that omit logs, run history, queue details, media, full URLs, credentials, databases, and configured local paths.

## Responsible-use boundary

Use this software only for media you own or are authorized to save, such as your own uploads, public-domain material, or content carrying an applicable download license. A reachable, unlisted, or extractor-labeled `public` URL is not proof that downloading is permitted. The metadata preflight cannot verify ownership, publication status, or authorization. You are responsible for copyright, contract, platform terms, privacy, and local-law compliance.

This project does not bypass DRM, authentication, paywalls, anti-bot controls, or other access restrictions. It does not read browser cookies or profiles, and it does not bundle FFmpeg or downloaded media.

## Requirements

- Windows 10/11
- Python 3.11 or newer
- FFmpeg and FFprobe supplied by the operator and available either on `PATH`, in `ffmpeg\bin`, or through `ffmpeg_location` in `config.json`

## Quick start

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
.\run_mp3_downloader.bat
```

`run_mp3_downloader.bat` is the canonical Windows entrypoint. It resolves its own project root, prefers the local `.venv`, validates Python 3.11 or newer and the pinned imports, opens the normal interactive flow when no arguments are supplied, and forwards explicit CLI arguments to the same Python engine. It never installs, updates, or downloads dependencies silently.

For a non-download preflight:

```powershell
.\run_mp3_downloader.bat --url "https://example.org/authorized-media" --list-only
```

For one authorized download:

```powershell
.\run_mp3_downloader.bat --url "https://example.org/authorized-media" --easy
```

Run `.\run_mp3_downloader.bat --help` for the complete interface. Runtime output is written below `downloads/`, `logs/`, `state/`, and `temp/`; all are excluded from version control.

## Configuration

The no-argument launcher path copies `config.example.json` to `config.json` when no local configuration exists. Explicit read-only and maintenance arguments are forwarded before that compatibility copy. Missing settings use defaults defined in the application source. The local JSON file centralizes the operating boundary—network access, output behavior, recovery, capacity, and concurrency—so policy can be reviewed without changing code. Notable controls include:

- `allow_private_networks: false`
- `allow_live_streams: false`
- `verify_ssl: true`
- `overwrite: false`
- `hide_completed_media: false`
- bounded file size, retry, concurrency, and queue-idle limits

## Support export

```powershell
.\export_support.bat
```

`export_support.bat` is a thin compatibility redirect to `run_mp3_downloader.bat --export-support`; it does not duplicate interpreter, dependency, or configuration logic.

The ZIP contains a constrained status summary and redacted configuration snapshot. It excludes logs, run history, queue records, media and partial downloads, media metadata, output filenames, source hosts, full URLs, uploader or title details, databases, source archives, configured local paths, and local dependency bundles. Review it before sharing.

## Verification

The deterministic test suite does not contact websites, download media, or require FFmpeg:

```powershell
python -m compileall -q mp3_downloader.py tests
python -m unittest discover -s tests -v
```

GitHub Actions also launches the canonical BAT from an unrelated Windows working directory on Python 3.11 and 3.13. The application's interactive `--self-test` is different: it checks the installed dependency lock and performs a short local FFmpeg/FFprobe conversion test.

## Security notes and limitations

- The initial submitted URL and a metadata-reported page URL are checked for local, private, link-local, multicast, reserved, and unspecified addresses by default.
- This preflight is **not an SSRF containment boundary**. `yt-dlp` can follow redirects and fetch manifests, subresources, or extracted media URLs that do not pass through this guard; DNS answers can also change after validation. Do not process untrusted URLs on a network that can reach sensitive services. Use a sandbox plus deny-by-default egress or an explicit destination allowlist.
- Extractor compatibility depends on upstream website behavior and the installed, pinned `yt-dlp` release.
- A support export is designed to reduce sensitive-data exposure, not to prove that every future log message is safe. Review the archive before sharing it.

See [SECURITY.md](SECURITY.md) for the reporting boundary, [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for dependency ownership, and [LICENSE.md](LICENSE.md) for the source and documentation terms.

## Portfolio and rights

[Portfolio](https://jerry-napier-portfolio.netlify.app/) · [GitHub profile](https://github.com/Jnapier2)

Copyright © 2026 Gateway Information Group LLC. All rights reserved. Third-party components and services retain their own rights and terms.
