# Porting the interface to Tauri

The Tkinter window has been taken about as far as it goes. Buttons,
checkboxes, the scrollbar and the dropdown are all drawn by hand on canvases
because the themed versions look like Windows 95. What cannot be done at all:
drop shadows, window blur, animation, rounded window corners, real icons,
control over kerning. Those come free in a browser engine and nowhere else.

This is the plan for replacing the shell. It is deliberately not a plan for
rewriting the recovery engines.

## The decision that matters

**Keep the engines in Python. Replace only the window.**

The repository is 2,712 lines of engine and 4,323 lines of tests. Almost all
of those tests exist to prove two things: that recovered files come back
byte-identical, and that the source drive is never written to. They build real
NTFS and exFAT volumes in memory and read them back with the same parsers that
read a physical drive.

Rewriting `ntfs.py`, `exfat.py` and `carve.py` in Rust throws all of that away
and starts the evidence from zero. The recovery logic is not the part that
looks dated, and there is no version of "the UI needs rounded corners" that
justifies re-proving the safety invariant from scratch.

So: Tauri provides the window, Python keeps doing the reading.

## The shape

Today everything runs as root:

```
sudo python3 app.py
└─ Tk window + engines, all privileged
```

The whole of `app.py` - 1,385 lines of interface code, the part that changes
most often - has raw device access it has no use for.

Proposed:

```
RecoverKit.app            Tauri shell, web frontend, NOT privileged
      │  JSON lines over stdin/stdout
      ▼
recoverkit-scan           small Python process, elevated
      └─ diskio · ntfs · exfat · carve · verify
```

The privileged half becomes a small program that does one thing. The half that
grows features never touches a disk. **This is a better security posture than
what ships today**, independently of how the window looks - and it is the real
argument for the port, more than the visual one.

## The protocol

`app.py` already speaks an event protocol; its worker thread pushes
`("batch", files)`, `("progress", done, total)`, `("done", None)` and
`("error", message)` onto a queue. Serialising that as newline-delimited JSON
is close to a mechanical change.

Requests in:

```json
{"op": "volumes"}
{"op": "scan", "device": "/dev/rdisk2s1", "mode": "undelete"}
{"op": "recover", "items": [12, 44], "dest": "/Users/x/Recovered"}
{"op": "stop"}
```

Events out:

```json
{"event": "progress", "done": 4096, "total": 3900000000}
{"event": "batch", "files": [{"name": "IMG_0042.JPG", "chance": 100, ...}]}
{"event": "done"}
{"event": "error", "message": "This drive isn't formatted in a way we can..."}
```

The destination guard and the same-drive check stay on the privileged side.
A UI that can ask for an arbitrary recovery path must not be the thing
deciding whether that path is safe.

## Phases

**1 · Headless protocol (no Rust needed).** Wrap the engines in a
`recoverkit-scan` command speaking the JSON above, with its own tests. The Tk
window can be switched to drive it, which proves the protocol carries
everything the interface needs before any of it is rewritten. Independently
useful: it makes the engines scriptable.

**2 · Elevation split.** Run the sidecar under the platform's privilege
mechanism while the caller stays unprivileged. This is the hardest phase and
the one most likely to need rework - see the risks below.

**3 · Tauri shell.** Frontend, IPC to the sidecar, and the window itself. The
visual work everyone is actually asking for. Comparatively easy once 1 and 2
hold.

**4 · Packaging.** PyInstaller the sidecar per platform, bundle it as a Tauri
`externalBin`, so users need no Python at all.

Phase 1 delivers value on its own and de-risks the rest. Phases 2 and 4 are
where the unknowns are.

## Risks, honestly

- **Elevation is per-platform and none of it is pleasant.** macOS wants a
  `SMJobBless` helper, which needs a Developer ID certificate; the unsigned
  alternative is an `osascript` password prompt, which works and looks like
  malware. Windows needs a UAC manifest on the sidecar. Linux needs a `pkexec`
  policy file. Expect this to take longer than the frontend.
- **Code signing and notarisation.** macOS will refuse to run an unsigned
  bundle that asks for a privileged helper. Apple Developer membership is
  £79/year. Without it, phase 2 is limited to the password-prompt route.
- **Losing "clone it and run it".** `python app.py` currently works on any
  machine with Python. A Tauri build does not. The Tk window may be worth
  keeping as the developer and emergency interface even after the port -
  that means maintaining two, which is a real cost and needs deciding rather
  than drifting into.
- **Rust is not installed on this machine.** Phases 3 and 4 need a toolchain
  that does not exist here yet; phase 1 needs nothing new.
- **The safety tests only cover Python.** They stay valid exactly as long as
  the engines stay Python. If any part of `diskio.py` is later moved to Rust,
  its safety tests move with it or the guarantee is unevidenced.

## Decisions taken

1. **The Tk window stays, as a fallback only.** It keeps the "copy the folder
   and run one command" property, which matters on a machine that is already
   broken. It is not the interface anyone is meant to use day to day, and it
   does not get new features - if a change is needed in both, the Tauri side
   is the one that gets the design attention.
2. **No Apple Developer account until the thing is finished.** macOS
   elevation therefore takes the password-prompt route for now, and
   distribution is unsigned. See the note below on what that costs.
3. **macOS is built first, Windows matters most.** Development happens on a
   Mac, so macOS is where anything can actually be tested. But Windows is
   where the need is - it is where NTFS lives, and NTFS undelete is this
   project's strongest feature. Design decisions get weighed for Windows even
   while the work happens on macOS.
4. **Free and public first, some earning plan later.** Which makes the
   licence worth thinking about *before* more of it is published, not after.

## What unsigned distribution actually costs

Downloadable-but-unsigned works. It is not free:

- **Windows** shows "Windows protected your PC" and hides the run button
  behind *More info*. For a tool that then asks for administrator rights and
  reads raw disks, that sequence looks exactly like malware to a cautious
  user. Signing certificates run £200-400/year and, since 2023, require
  hardware tokens.
- **macOS** refuses to open an unsigned bundle at all on first launch. The
  user has to right-click and choose Open, or go into System Settings and
  approve it by hand. Notarisation needs the £79/year account.
- **Antivirus false positives** are near-certain. A PyInstaller binary that
  opens raw devices matches several heuristics at once. README already warns
  about this; it will need submitting to vendors for whitelisting.

GitHub Releases is the better first home for downloads rather than a personal
site: it carries a trust signal, publishes checksums, and costs nothing.

## The Windows gap, concretely

Windows is the priority audience and currently gets the worse experience:

    macOS      TESTCARD — 3.7 GB exFAT, removable — /dev/rdisk2s1
    Windows    C:

`diskio.list_volumes` builds rich, plain-language labels on macOS and returns
bare drive letters on Windows. No volume name, no size, no filesystem, no
indication of which drive is removable. Everything the "which drive do I
pick" problem needed solving, solved on the platform that needs it least.

None of the Windows code paths have ever been run. The suite skips 13 tests
outside Windows.

**The cheapest fix for that is CI.** GitHub Actions runs Windows, macOS and
Linux for free on a public repository. Without it, "Windows first" means
shipping code nobody has executed.
