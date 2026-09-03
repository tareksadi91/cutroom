# cutroom

A local video cutting room for people who want to make films with agents, without handing their footage, timeline, or workflow to a walled garden.

Cutroom opens in a browser but runs on your own computer. Your project stays as a simple file you can inspect, copy, edit, and keep. You make decisions visually; an agent can make precise changes to the same cut or help build the workflow you wish existed.

No account. No subscription. No upload requirement. No proprietary project format.

> Your media stays yours. Cutroom never moves, deletes, or writes over source footage.

![Cutroom editing a film: the media bin down the left listing every source clip,
the program monitor at top right showing the shot under the playhead, and the
timeline across the bottom with clips laid out on lanes, a playhead, and
crossfades between shots.](docs/assets/cutroom-the-courier.png)

## What problem does it solve?

Most editing software assumes one of two things: you are already a professional editor, or you are happy to work inside somebody else's app, cloud, format, and limitations.

Cutroom takes a different approach. You can drag clips onto a timeline and make a normal cut yourself. If you are not an editing expert, you can also work with an agent locally:

- “Make this opening 20 seconds faster.”
- “Try three versions with a slower build.”
- “Put the strongest reaction shot after this line.”
- “Make a vertical version for social.”
- “Build me a button for the repetitive thing I keep doing.”

The agent is not locked into a vendor's idea of editing. It works with the same project you do. You can inspect every change, keep what works, and make the tool fit your film instead of fitting your film into somebody else's product.

## What Cutroom is good at

- Building a clean cut by dragging media onto a timeline
- Trimming, retiming, moving, cutting, and crossfading clips
- Letting an agent make structured changes to the same local project
- Keeping original media safe while you experiment
- Exporting a new versioned video when you are ready
- Adding your own post-processing tools and workflows

## What makes it different

### Your footage is not the product

Cutroom runs locally. It does not need to upload your footage or project to a platform before you can start.

You can use an agent with it, but that is your choice. If you use a hosted AI service, that service has its own privacy policy; Cutroom itself does not send your footage anywhere.

### Your originals stay untouched

Cutroom can read media, but it never moves, deletes, or overwrites source files.

Deleting a clip removes it from the timeline, not from your disk. Post-processing creates a new file instead of changing the original. Every edit is saved with history, so conflicting changes do not silently erase each other.

### The project is yours

A cut is one readable JSON file on your computer, not a proprietary cloud document.

That means you can:

- Back it up however you want
- Open it with your own tools
- Have an agent edit it carefully
- Build custom features around it
- Leave without losing access to your work

## Get Cutroom

### Easiest route: ask your coding agent

Send your agent this:

> Install Cutroom from https://github.com/tareksadi91/cutroom. Set up a new local project called `myfilm`, then open it in my browser. Do not move, rename, or delete any media.

### Manual route

1. On this page, click **Code** → **Download ZIP**.
2. Unzip it.
3. Open Terminal in the `cutroom` folder.
4. Make sure Python 3, ffmpeg, and ffprobe are installed.
5. Run:

```sh
./cutroom check
./cutroom new myfilm
./cutroom serve myfilm
```

A browser window opens with your empty cutting room. If `./cutroom` is not executable after downloading a ZIP, run `chmod +x cutroom` once and retry.

On macOS, use **Add media…** to choose files. On any system, you or an agent can add exact files with `./cutroom add`.

## Start a first project

```sh
./cutroom new myfilm
./cutroom serve myfilm
```

Then:

1. Click **Add media…**.
2. Choose clips.
3. Drag clips from the Media panel onto the timeline.
4. Drag clips to move them, or drag their edges to trim.
5. Press **Space** to play.
6. Click **export** when you want a rendered video.

Hover over controls in the interface for short explanations.

### Add media from the command line

```sh
./cutroom add myfilm /absolute/path/to/a.mp4 /absolute/path/to/b.mp4
./cutroom add myfilm --copy /absolute/path/to/clips/*.mp4
```

The page's **Add media…** picker always copies imports into the project. The command-line version normally references files where they already live; add `--copy` when you want a self-contained project copy.

## Work with an agent

Cutroom is designed so you and an agent can share one project without fighting over it.

You work visually in the browser. Your agent can make structured changes to the project, add media, create alternate cuts, or build small custom workflows. Cutroom protects against conflicting saves, preserves history, and does not let its own operations silently change your source footage.

Useful starting prompts:

> Read this Cutroom project and explain the current edit in plain English.

> Make a second version of this cut with a faster first 30 seconds. Keep the original version available.

> Add these clips to my Cutroom project and place them after the current opening.

> Build a small local post-pass that gives selected clips a softer, lower-saturation look.

## Make it your own

Cutroom is open source because editing workflows are personal.

Maybe you want a button for your preferred social format. Maybe you want an agent to assemble interview selects. Maybe your film needs a custom visual treatment, naming system, review workflow, or export rule.

You can change the tool. You can ask an agent to change it with you. You are not waiting for a platform roadmap.

## For technical collaborators

Requirements: Python 3, ffmpeg, and ffprobe. No package install, build step, or account required.

```sh
git clone https://github.com/tareksadi91/cutroom.git
cd cutroom
./cutroom check
```

Projects live in `~/cutroom-projects/`. The public commands are:

```sh
./cutroom new <project> [--fps 24] [--res 720x1280]
./cutroom add <project> [--copy] <absolute-path>...
./cutroom serve <project> [--port 8420] [--passes-dir <directory>]
./cutroom export <project>
./cutroom ls
./cutroom check
```

`serve --passes-dir` enables scripts that create derived media. It is off by default. See [SPEC.md](SPEC.md) for project format, local API constraints, post passes, and safety rules.

## Safety model

Cutroom intentionally does not scan, index, or watch media folders. A file enters only when you give Cutroom its exact path.

It also refuses to write outside `~/cutroom-projects/`, does not delete media or derived outputs, and does not follow a symlink out of a project directory. It updates only its own project file, atomically, after saving a snapshot of the previous state.

This is not a substitute for backups. It is a tool designed to make accidental damage less likely while you edit and experiment.

## License

Cutroom is licensed under the [GNU Affero General Public License v3.0](LICENSE).

If you improve it and let people use your modified version over a network, share the source with them too.
