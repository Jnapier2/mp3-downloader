# MP3 Downloader

MP3 Downloader is a guarded Windows command-line utility for converting the best available audio stream from one authorized HTTP(S) media URL into a validated MP3 file. It combines `yt-dlp` and an operator-supplied FFmpeg installation with explicit network, content, filesystem, and privacy boundaries.

## What it demonstrates

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
Copy-Item config.example.json config.json
.\run_mp3_downloader.bat
```

The launcher uses an existing environment and never installs, updates, or downloads dependencies silently.

For a non-download preflight:

```powershell
python mp3_downloader.py --url "https://example.org/authorized-media" --list-only
```

For one authorized download:

```powershell
python mp3_downloader.py --url "https://example.org/authorized-media" --easy
```

Run `python mp3_downloader.py --help` for the complete interface. Runtime output is written below `downloads/`, `logs/`, `state/`, and `temp/`; all are excluded from version control.

## Configuration

`config_default.json` documents the complete release defaults. Copy `config.example.json` to `config.json` for a concise starting point. On first run, omitted settings are filled with safe defaults. Notable controls include:

- `allow_private_networks: false`
- `allow_live_streams: false`
- `verify_ssl: true`
- `overwrite: false`
- `hide_completed_media: false`
- bounded file size, retry, concurrency, and queue-idle limits

## Support export

```powershell
python mp3_downloader.py --export-support
```

The resulting ZIP contains only a constrained status summary and redacted configuration snapshot. It never includes application logs, run history, queue records, media metadata, output filenames, source hosts, or uploader/title details. Review it before sharing. It also excludes media, partial downloads, databases, source archives, full URLs, configured local paths, and local dependency bundles.

## Verification

The deterministic test suite does not contact websites, download media, or require `yt-dlp`/FFmpeg:

```powershell
python -m compileall -q mp3_downloader.py tests
python -m unittest discover -s tests -v
```

The application's interactive `--self-test` is different: it checks the installed dependency lock and performs a short local FFmpeg/FFprobe conversion test.

## Security notes and limitations

- The initial submitted URL and a metadata-reported page URL are checked for local, private, link-local, multicast, reserved, and unspecified addresses by default.
- This preflight is **not an SSRF containment boundary**. `yt-dlp` can follow redirects and fetch manifests, subresources, or extracted media URLs that do not pass through this guard; DNS answers can also change after validation. Do not process untrusted URLs on a network that can reach sensitive services. Use a sandbox plus deny-by-default egress or an explicit destination allowlist.
- Extractor compatibility depends on upstream website behavior and the installed, pinned `yt-dlp` release.
- A support export is designed to reduce sensitive-data exposure, not to prove that every future log message is safe. Review the archive before sharing it.

See [SECURITY.md](SECURITY.md) for the reporting boundary and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for dependency ownership.

## Portfolio context

This public repository is a sanitized code-review artifact. Private release records, internal build records, handoff materials, deployment automation, binaries, downloaded media, and support bundles are intentionally excluded. The first-party source and documentation remain copyright-protected under [LICENSE.md](LICENSE.md).
