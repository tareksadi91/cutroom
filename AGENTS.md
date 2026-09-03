# Working on a Cutroom project as an agent

Cutroom is designed so a person in the browser and an agent can edit the same
project without clobbering each other. This is the contract that makes that
true. Read it before writing anything.

## The hard rule

**Never write `<project>.json` directly.** Always go through `PUT /project`
(HTTP) or `edit_project()` (in-process Python). Both take the same lock a
browser save takes, bump the version, and snapshot the state they replace.

Writing the file by hand skips all three. It can land between the server's
read and its own write and vanish without a trace, and it leaves no snapshot
to recover from. If you are tempted to `json.dump()` over the project file —
don't; use one of the two doors below instead.

## Two ways in

### In the same process: `edit_project()`

If your agent process can import `server.py` directly (the common case — a
coding agent working in this repo, or a script run alongside Cutroom), this
is the simplest door:

```python
import sys
sys.path.insert(0, "/path/to/cutroom/src")
import server

def mutate(project):
    project["clips"].pop(3)          # remove the 4th clip

status, payload = server.edit_project("myfilm", mutate)
if status != 200:
    print(payload["problems"])       # a validation refusal, not a crash
```

`mutate` receives the current project (a plain dict) inside the lock and may
change it in place, or return a new dict. `edit_project()` handles the version
bump, validation, the snapshot, and the atomic swap. It runs even if a
browser tab has the project open — the two queue for the same lock instead of
overwriting each other.

### Over HTTP: the same API the browser uses

If your agent talks to a running `cutroom serve` instance instead (a remote
agent, a different language, a shell script), use the HTTP API. Three things
every mutating request needs:

1. **The capability token.** Printed when the server starts:
   `token:  <token>   (send as X-Cutroom-Token to change anything)`.
   Send it as the `X-Cutroom-Token` header on every `PUT`/`POST`. Without it,
   every mutation is refused with 403 — this is what stops a malicious web
   page from driving your Cutroom server, not a real barrier between you and
   your own instance.
2. **`Content-Type: application/json`** on any request with a body.
3. **The current `version`.** Read it from `GET /project` before you write.

```sh
# read the current project
curl -s http://127.0.0.1:8420/project

# add media (absolute paths only — Cutroom never scans a folder for you)
curl -s -X POST http://127.0.0.1:8420/media \
  -H "Content-Type: application/json" -H "X-Cutroom-Token: $TOKEN" \
  -d '{"paths": ["/absolute/path/to/clip.mp4"]}'

# make an edit — version must match what GET /project just returned
curl -s -X PUT http://127.0.0.1:8420/project \
  -H "Content-Type: application/json" -H "X-Cutroom-Token: $TOKEN" \
  -d '{"version": 7, "clips": [...]}'
```

A `PUT` body is the fields you want to change (`clips`, `fps`, `resolution`,
...) plus the `version` you last read. `media` and `passes` in the body are
silently ignored — media only enters through `POST /media` or
`POST /media/pick`, never through a PUT, because a PUT you can shape however
you want must never be able to smuggle in `{"path": "/etc/passwd"}` and then
read it back.

## The version guard and what a 409 means

Every write compares the `version` you sent against the project's current
version. They must match exactly (an integer, not `7.0`).

- **Match:** your write lands, the version bumps by one, and the previous
  state is snapshotted to `.snapshots/` first.
- **Mismatch:** you get back `409` with the *entire current project* as the
  body — not an error message, the actual live document. Someone (the
  browser, another agent) wrote since you last read.

**On a 409, re-read the returned document, re-apply your intended change
against it, and PUT again with its version.** Do not retry with your old
version, and do not silently discard the other write — that is exactly the
lost-update bug the version guard exists to prevent. If your change and the
concurrent one touch different clips, this rebase-and-retry is usually a
no-op merge; if they touch the same field, decide which one should win before
retrying.

## What you can rely on

- A missing or offline source is never an error — a clip just renders as
  OFFLINE and blocks export, not editing. Don't "clean up" a project by
  removing clips whose media went missing.
- A malformed edit is refused with a list of problems, never a crash. If you
  get a 400 with `"problems"`, read them — they name the exact bad field.
- Nothing you do through these two doors can write outside
  `~/cutroom-projects/`, move or delete source media, or overwrite an
  existing derived file. Those are enforced in `server.py`, not just
  documented — see `SPEC.md` if you want the mechanism.

## Worked examples

**Read a project and describe it:**

```python
import json, pathlib
doc = json.loads((pathlib.Path.home() / "cutroom-projects" / "myfilm.json").read_text())
print(f"{len(doc['clips'])} clips, {doc['fps']}fps, v{doc['version']}")
```
(Reading directly is fine — only *writing* has to go through the doors above.)

**Add media, then place it after the current last clip:**

```python
status, payload = server.add_media("myfilm", ["/absolute/path/new_shot.mp4"])
mid = payload["added"][0]["mid"]

def mutate(p):
    last = max(p["clips"], key=lambda c: c["t"] + (c["out"] - c["in"]))
    end = last["t"] + (last["out"] - last["in"])
    p["clips"].append({"uid": "c99", "mid": mid, "lane": 0, "t": end,
                        "in": 0.0, "out": 2.0, "rate": 1.0, "label": "", "note": ""})

server.edit_project("myfilm", mutate)
```

**Handle a 409 by rebasing:**

```python
status, payload = server.write_project("myfilm", {"version": 7, "clips": new_clips})
if status == 409:
    current = payload  # the live project, not an error object
    rebased_clips = reapply_my_change(current["clips"])
    status, payload = server.write_project(
        "myfilm", {"version": current["version"], "clips": rebased_clips})
```

For the full project format, post-pass contract, and what each safety
invariant is defending against, see [`SPEC.md`](SPEC.md).
