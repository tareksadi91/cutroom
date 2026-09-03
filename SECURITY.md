# Security Policy

## Supported versions

Cutroom does not yet cut numbered releases; `main` is the only supported
version. Fixes land there.

## Reporting a vulnerability

Please do not open a public GitHub issue for a security problem.

Instead, use GitHub's private reporting: on this repository, go to
**Security → Report a vulnerability** (or open a report directly at
`https://github.com/tareksadi91/cutroom/security/advisories/new`). This
opens a private advisory that only maintainers can see until it is
resolved.

Include:

- What you found and why it matters (what an attacker gains).
- Steps to reproduce, or a minimal example.
- Which version/commit you tested against.

## Scope

Cutroom is a local-first tool: a Python HTTP server bound to `127.0.0.1`,
meant to be reachable only from the machine it runs on. In scope:

- Anything that lets a remote or malicious web page read or change a
  project, media, or the filesystem through the local server (see
  `src/server.py`'s `writable()`, the `Host`/`Origin`/token checks, and the
  `⚠️ RESIDUAL` comments there for what is already a known, accepted
  tradeoff rather than a bug).
- A path that writes outside `~/cutroom-projects/`.
- A path that moves, deletes, or overwrites source media.

Out of scope: attacks that require running arbitrary code on the same
machine already (Cutroom does not attempt to defend against an
already-compromised local user), and issues in `ffmpeg`/`ffprobe`
themselves — report those upstream.
