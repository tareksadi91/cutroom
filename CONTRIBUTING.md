# Contributing

Cutroom is a small, dependency-free program on purpose. Contributions are
welcome, but the bar for adding a dependency, a config option, or an
abstraction is high — see the "Explicit non-goals" section of
[`SPEC.md`](SPEC.md).

## Setup

No install step. You need Python 3, `ffmpeg`, and `ffprobe` on your `PATH`.

```sh
git clone https://github.com/tareksadi91/cutroom.git
cd cutroom
./cutroom check
```

## Running the tests

```sh
./cutroom check
```

This runs `src/test_render.py` and `src/test_server.py` — bare `assert`
scripts, no pytest, no fixtures beyond what each test synthesises with
`ffmpeg` into a temporary directory. Every test cleans up after itself and
none of them touch real media or a real `~/cutroom-projects/`.

To run one test by name:

```sh
python3 src/test_server.py test_export_refuses_a_hand_edited_project_instead_of_crashing
```

(the argument is a substring match against test function names; run without
one to run the whole file).

## Safety invariants a change must not break

These are enforced by tests, not just convention — see `writable()` and the
comments around it in `src/server.py` if you are touching a write path:

- Cutroom never writes outside `~/cutroom-projects/`.
- Cutroom never moves, deletes, or overwrites source media.
- Every write to a project file is version-guarded and snapshotted first.
- A malformed project document (hand-edited, or from a bad request) is
  refused with a clear message — never a traceback.

If your change touches `server.py` or `render.py`, run `./cutroom check`
before opening a pull request, and add a test for the behavior you changed
or fixed. A bug fix with no regression test is easy to reintroduce.

## Pull requests

- Keep the diff focused on one change.
- Explain *why*, not just *what* — the codebase's own comments follow this
  convention and reviewers will ask "why" if a comment doesn't answer it.
- All tests must pass (CI runs `./cutroom check` on macOS and Linux).

## Reporting a security issue

Do not open a public issue. See [`SECURITY.md`](SECURITY.md).
