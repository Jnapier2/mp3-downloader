# Security Policy

## Supported version

Security review is focused on the current public branch and version 1.0.0. Older portfolio snapshots are not maintained.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting or security-advisory feature for this repository. Do not attach credentials, private URLs, downloaded media, browser data, local databases, or unreviewed support exports to a public issue.

Include the affected version, a minimal reproduction using synthetic or public-domain inputs, expected and observed behavior, and the security impact. Allow time for confirmation before public disclosure.

## Intended security boundary

MP3 Downloader accepts one authorized HTTP(S) media URL at a time. Its preflight rejects embedded credentials and rejects an initial submitted or metadata-reported page hostname when it resolves to a local, private, link-local, multicast, reserved, or unspecified address. Metadata such as `public`, `unlisted`, or missing availability is never treated as proof of authorization. The tool does not read browser cookies, automate login, bypass access controls, execute downloaded content, or interpolate a URL into a shell command.

The preflight is not a complete SSRF defense. The downloader library can follow redirects and request extracted media URLs, manifests, and subresources that are not passed back through this validator. A hostname can also change its DNS answer between validation and connection. Treat arbitrary URLs as untrusted network input: use a sandbox and deny-by-default egress or a destination allowlist, especially on networks that can reach internal services or cloud metadata endpoints.

Support exports deliberately omit application logs, run history, queue records, media metadata, and unrelated dependency names. They redact known sensitive configuration fields and configured local paths. Users must still inspect every archive before sharing it.
