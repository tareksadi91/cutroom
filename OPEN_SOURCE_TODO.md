# Open-source readiness checklist

Context handoff for work identified during holistic review on 2026-08-31.

Baseline at review time:

- Branch: `main`
- Commit: `e265645`
- Worktree clean and synced with `origin/main`
- `./cutroom check`: 104 tests passed
- No tracked media, large blobs, or live credentials found
- Repository still private

## 1. Release blocker: protect local HTTP mutations

- [x] Add protection against malicious websites sending requests to Cutroom on
      `127.0.0.1`.
- [x] Require a server-generated capability token for every state-changing HTTP
      request: `PUT /project` and all `POST` routes.
- [x] Validate `Host` against the bound loopback host and port — on READS too,
      which is what actually stops DNS rebinding.
- [x] Reject browser mutation requests whose `Origin` is not Cutroom's own origin.
      Keep a documented path for local agents that do not send an `Origin` header.
- [x] Require `Content-Type: application/json` for JSON mutation routes.
- [x] Cap request-body size and return `413` when exceeded (and refuse a
      negative or non-decimal `Content-Length`, and chunked framing).
- [x] Add regression tests covering hostile `Origin`, hostile `Host`, missing or
      wrong token, non-JSON content, oversized bodies, and valid browser/agent calls.

Evidence: `src/server.py:_body`, `do_PUT`, and `do_POST` currently accept a
cross-origin `text/plain` POST. A request carrying
`Origin: https://attacker.example` reached `/render` during review instead of
being rejected.

Done when: hostile requests fail before any route action; browser UI, CLI, and
documented agent workflow still work; all tests pass.

**DONE 2026-09-03** (`7a7714d`, `df7a201`). Re-measured: the original attack now
answers 403. An independent security review then found the gate covered writes
only — under DNS rebinding a page could READ the token, every absolute media
path, and the footage, without mutating anything — so the `Host` check runs on
every request. `/thumb` stays a GET (an `<img>` tag cannot carry a token) and is
guarded by `Sec-Fetch-Site`. Verified in a real browser, export included.

⚠️ RESIDUAL, ACCEPTED: a same-origin-looking simple GET from an old browser that
sends no `Sec-Fetch-Site` can still reach `/thumb`. It runs ffmpeg on media
already in the project and writes a jpg inside the project — bounded, no
disclosure, and closing it would mean the page could not show thumbnails.

## 2. Validate projects at their boundaries

- [x] Validate `cutroom new` arguments before creating any file.
- [x] Require positive fps.
- [x] Require `WIDTHxHEIGHT` with positive integer dimensions compatible with
      `yuv420p` output; use even dimensions unless renderer format changes.
- [x] Return concise argparse-style errors instead of Python tracebacks.
- [x] On `serve`, run project shape validation before binding or opening browser.
- [x] On `export`, fail cleanly for malformed hand-edited project documents.
- [x] Tighten `shape_problems()` so resolution values match renderer's real
      integer/even-dimension requirements.
- [x] Add tests for zero/negative fps, malformed resolution strings, float/odd/
      negative dimensions, `{}`, and partially malformed project documents.

Evidence: `src/server.py:create()` wrote unchecked settings; `serve()` checked
JSON syntax only; `export()` called `render_mod.load()`, which indexes
`project["fps"]` and every clip's `in`/`out` directly.

Done when: invalid project cannot be created or served, and every refusal names
the bad field without a traceback or partial project.

**DONE 2026-09-03.** `create()` now runs the same `shape_problems()` every
other door into a project runs, before `mkdirs()` or `write_new()` touch disk
— so a rejected `cutroom new` leaves nothing on disk. `shape_problems()`
itself now requires resolution to be positive, even integers (yuv420p has no
center pixel to subsample on an odd dimension). `serve()` runs the same check
right after its existing JSON-parse check, before `Handler.project_name` is
set or the port is bound. `export()` — both the `/render` HTTP route and the
`cutroom export` CLI go through this one function — now shape-checks the
document itself instead of calling `render_mod.load()` (which canonicalises
and indexes fields directly); `render.py`'s own `--check`-adjacent standalone
CLI got the same fix for the same reason. `--res` values that fail `int()`
now raise `Refused` instead of a bare `ValueError` traceback. Six new tests
(`test_render.py`: shape_problems on a non-object, bad fps, bad resolution,
partially malformed docs; `test_server.py`: bad `new` args write nothing,
`serve` refuses before binding, `export` refuses cleanly both over HTTP and
via the CLI). Independent Codex review then found four more: a missing/
non-integer `version` passed shape_problems() and crashed `export_path()`'s
`f"{version:03d}"`; NaN/Infinity fps and clip fields passed the plain
`>0`/`isinstance` checks (`nan <= 0` is False) and crashed deep inside
`on_grid()`; the standalone `render.py` CLI's own `json.loads()` had no
`JSONDecodeError` handler; and that CLI claimed its output name — a
zero-byte file — before parsing or shape-checking the project at all. All
four fixed (`shape_problems()` now requires `math.isfinite()` and a
non-negative int version; the CLI parses and shape-checks before claiming
output) with 4 more regression tests. 142 tests green (was 129).

## 3. Reject unreadable media during import

- [ ] Make `add_media()` reject new files that `ffprobe` cannot identify as usable
      video.
- [ ] Show one friendly problem per rejected path.
- [ ] Preserve current behavior for media already in a project that later goes
      missing or offline.
- [ ] Test mixed batches containing valid, invalid, and already-added media.

Evidence: `src/server.py:add_media()` currently stores an entry even when
`probe()` returns no duration or dimensions; failure appears much later at export.

Done when: broken input fails at import, while moved/missing existing footage
still appears as OFFLINE without rewriting the project.

## 4. Document safe agent integration

- [x] Add `AGENTS.md` or `docs/agent-integration.md`.
- [x] State hard rule: agents use `PUT /project` or `edit_project()`, never write
      `<project>.json` directly.
- [x] Explain version guard, conflict response, snapshots, media allowlist, and
      retry/rebase behavior.
- [x] Include copy-paste examples for reading a project, adding media, making a
      safe edit, and handling `409`.
- [x] Explain capability-token flow introduced by task 1.
- [x] Link guide from README within two clicks.

Evidence: locking contract exists in `src/server.py` module comments and one line
of `SPEC.md`, but README's agent promises do not give agents enough operational
instructions to honor it.

Done when: a fresh coding agent can install Cutroom and edit one project safely
without reverse-engineering server code.

**DONE 2026-09-03.** `AGENTS.md` added at repo root, linked from README's
"Work with an agent" section. Covers both doors in (`edit_project()` in-process,
the HTTP API with the capability token), the version guard and how to rebase on
a 409 (the 409 body IS the live project, not an error object — verified against
`write_project()`/`_guarded_write()`), the `media` allowlist, and three worked
examples using the real function signatures (`add_media()`, `edit_project()`,
`write_project()`).

## 5. Make platform support honest

- [x] State clearly in README: macOS and Linux supported; Windows not yet
      supported.
- [x] Explain macOS has native `Add media...` picker; Linux uses CLI import.
- [x] Ensure installation prerequisites name Python 3, `ffmpeg`, and `ffprobe`.
- [x] Verify manual setup commands on both supported platforms through CI or a
      documented manual check.

Evidence: server imports `fcntl`; README currently says "On any system" for CLI
import, which can imply Windows support.

**DONE 2026-09-03.** README's "On any system" claim replaced with an explicit
per-platform note (macOS native picker vs. Linux CLI import) and a one-line
"Platform support: macOS and Linux. Windows is not supported yet." The CI
workflow added in §6 runs the documented `./cutroom check` on both platforms on
every push, which is the "verify through CI" half of the last item.

## 6. Add minimum public-repository infrastructure

- [x] Add GitHub Actions workflow running `./cutroom check` on macOS and Linux.
- [x] Add concise `CONTRIBUTING.md` with setup, test command, safety invariants,
      and pull-request expectations.
- [x] Add `SECURITY.md` with private vulnerability-reporting route and supported
      versions.
- [x] Update GitHub description to match new README positioning.
- [x] Add useful topics such as `video-editing`, `local-first`, `ffmpeg`,
      `agent-tools`, and `open-source`.
- [x] Decide whether homepage field should remain empty or point to demo/docs.
- [x] Keep repository private until release blockers and final QA are complete.

Done when: every pushed commit gets tested and a visitor can understand how to
contribute or report a security issue.

**DONE 2026-09-03.** `.github/workflows/check.yml` runs `./cutroom check` on
`ubuntu-latest` and `macos-latest` on every push/PR. `CONTRIBUTING.md` and
`SECURITY.md` added (the latter points to GitHub's private advisory flow —
`Security → Report a vulnerability` — never an email, since §9's history
cleanup is still open). GitHub description was already accurate; added the five
suggested topics via `gh repo edit`. Homepage left empty — no demo/docs site
exists yet, and pointing it at one prematurely would be a stale field, not a
positioning choice. Visibility unchanged: still **PRIVATE**, per the last
checkbox — §9 and §10 are not done.

## 7. Add screenshot to README

- [x] Preserve supplied screenshot inside repository at
      `docs/assets/cutroom-the-courier.png`.
- [x] Insert screenshot near top of README after opening description.
- [x] Add useful alt text describing media bin, program monitor, and timeline.
- [ ] Check GitHub rendering at desktop and narrow widths.
- [ ] Consider whether project name or film imagery exposes anything unwanted
      before repository becomes public.

## 8. Keep AGPL source access visible without restoring nav clutter

- [ ] Move or duplicate `Source code` link into a small, discoverable help/about
      surface or footer.
- [ ] Keep AGPL label out of top navigation.
- [ ] Confirm keyboard and touch users can reach source link without discovering
      an unmarked hover target.
- [ ] Preserve `rel="noopener"` on external link.

Evidence: link currently lives inside project-name tooltip in `src/ui.html`.
AGPL section 13 says modified network versions must "prominently offer" source.
This checklist is product hygiene, not legal advice.

## 9. Clean public-facing repository history and documents

- [ ] Decide whether personal email in commit `0f18e55` may remain public.
- [ ] If privacy matters, rewrite that author metadata before making repository
      public, then verify all refs. This is destructive and requires an explicit
      decision before execution.
- [ ] Reframe `NOTES.md` as historical engineering notes, move it under `docs/`,
      or omit it from public root.
- [ ] Remove or label stale claims in `NOTES.md`, including info-icon tooltips and
      old 77-test count.
- [ ] Run full-history credential scan with a dedicated scanner before public
      release. Regex review found no live secret, but no dedicated scanner was
      installed during review.

## 10. Final release gate

- [ ] Run `./cutroom check`; require all tests green.
- [ ] Run secret scan across current tree and git history.
- [ ] Confirm worktree contains no media, project JSON, renders, derived outputs,
      local paths, or private film notes.
- [ ] Test fresh ZIP download instructions from a temporary directory.
- [ ] Test fresh clone instructions from a temporary directory.
- [ ] Launch sample project and manually verify import, trim, move, cut, playback,
      history, conflict handling, and export.
- [ ] Verify README links, screenshot, license, platform note, security policy,
      contribution guide, and agent guide on GitHub.
- [ ] Confirm GitHub Actions passes on macOS and Linux.
- [ ] Only then change repository visibility to public.

## Explicit non-goals before first public release

- [ ] Do not split `server.py` or `ui.html` only because they are large.
- [ ] Do not optimize rendering without a measured bottleneck.
- [ ] Do not build installers, packaging, or hosted services before public usage
      shows demand.

Pressure test: broad cleanup can delay learning while increasing regression risk.
Security, truthful onboarding, agent safety, and reproducible CI survive that test;
architecture churn does not.
