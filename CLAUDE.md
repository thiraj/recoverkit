# RecoverKit

Free, open-source file recovery for Windows, macOS and Linux. Python 3.8+,
standard library only, Tkinter GUI. MIT licensed.

## Non-negotiable safety invariant

**The source drive must never be written to.** This is the single most
important property of this project. Anything that risks it is a critical bug,
not a trade-off.

Concretely:

- `diskio.py` is the ONLY module permitted to open a source device. Do not add
  device-opening code anywhere else.
- Devices are opened with `os.O_RDONLY` so the kernel enforces read-only, not
  our code. Never add a write path, a `w`/`r+` mode, or an ioctl that could
  mutate the device.
- `ReadOnlyDisk` intentionally exposes no write, truncate or seek-and-write
  method. Keep it that way.
- Files still marked in-use in the MFT are skipped entirely. Never read or
  recover a live file.
- Recovery output must never land on the drive being scanned. The check lives
  in `App._guard_destination` and `diskio.same_physical_drive`, and fails
  closed — if it can't determine the answer, it blocks. It resolves the
  destination to a *physical* drive: partition to whole disk, macOS
  `ntfs://diskNsM/` mount sources to the device, and APFS synthesized disks to
  the hardware underneath. Every one of those was a real miss at some point;
  `tests/test_destination_guard.py` keeps them fixed. If you touch this
  function, that file is not optional reading.
- Existing files in the destination are never overwritten; collisions become
  `name (2).ext`.

If you change anything in `diskio.py`, re-run the suite and update
`SAFETY.md`. `tests/test_diskio.py` asserts the descriptor is opened
`O_RDONLY`, that the kernel rejects a write to it, that no write-shaped method
exists on `ReadOnlyDisk`, and that `same_physical_drive` fails closed;
`tests/test_interface_parity.py` fails if any module other than `diskio.py`
opens a device, or if `diskio.py` grows a writable flag.

## Architecture

```
diskio.py   Read-only raw device access. Sector-aligned reads (Windows
            requires this on raw volume handles). Volume enumeration per OS,
            and the same-drive check that guards the recovery destination.
            On macOS, enumeration uses one `diskutil list -plist` call and
            never `diskutil info` — the latter blocks indefinitely on a slow
            or flaky removable device, which is precisely the hardware this
            tool is pointed at.
ntfs.py     NTFS Master File Table parser. The undelete engine: recovers
            real filenames, folder paths, sizes, timestamps, and cluster
            maps. Scores recoverability against the $Bitmap.
exfat.py    exFAT directory parser. The second undelete engine, for memory
            cards, phones and USB sticks. Same public interface as ntfs.py
            (ExfatVolume(disk) / .scan() / .read_file()) so app.py can use
            either without caring which. Scores against the allocation
            bitmap the same way.
carve.py    Signature scanner. The deep-scan engine: finds files by header/
            footer byte patterns. No filenames. Works on any filesystem.
signatures.py
            What each file type looks like at its first bytes, and the
            three-valued verdict (match / mismatch / blank / unknown) both
            undelete engines use to sanity-check a file before claiming it is
            recoverable.
app.py      Tkinter GUI. Scanning runs on a worker thread and communicates
            with the UI thread through a queue — never touch Tk widgets from
            the worker.
```

All engines are importable independently; keep them free of GUI imports.
`tests/test_interface_parity.py` fails if ntfs.py and exfat.py drift apart, or
if any module other than diskio.py starts opening devices.

## Design decisions worth preserving

- **Honesty over optimism.** The recovery-chance score comes from real
  evidence, not a flattering guess. Don't inflate it. Two independent sources
  feed it: the allocation bitmap, and the file's own first bytes
  (`signatures.py`). The bitmap alone is not enough — it answers "is this
  space spoken for *today*", which on a drive with history is a different
  question from "is my data still there". A header mismatch is hard evidence
  and overrides the bitmap; an unrecognised file type yields no verdict at
  all rather than a guess.

  The two sources disagree in both directions, and `_reconcile()` in each
  engine settles it:

  | Bitmap | Header | Verdict |
  |---|---|---|
  | free | matches | `match` — the ordinary good case |
  | free | doesn't | `mismatch`, score 0 — space was reused and released |
  | in use | matches, and a **live file starts at the same cluster** | `moved`, score 100 — it is in the Trash, tell them where |
  | in use | matches | `in_use`, score 50 — might be theirs, might not |

  The `moved` row is not a nicety. A file dragged to the Trash keeps its
  clusters and gains a second directory entry; the old one looks deleted and
  the bitmap correctly calls the space busy. Scoring that 0% tells someone
  their file is unrecoverable while it sits in their Trash, intact.
  Files whose data is gone are shown in red at a low score. On exFAT a deleted
  fragmented file has lost its FAT chain, so its layout is a guess: those
  carry `assumed_contiguous` and their score is capped at 50. Don't remove
  that cap to make results look better.
- **Search is the headline feature.** PhotoRec finds files but dumps
  thousands of `recovered_00001.jpg`. Filtering by filename and folder is the
  reason this project exists. Keep it fast and live.
- **No third-party dependencies.** Users are often mid-crisis on a broken
  machine. `python app.py` must just work. Do not add pip requirements
  without a strong reason.
- **Plain language in the UI.** The target user is not technical. No jargon
  in labels, errors or dialogs.

## Known limits (documented, not bugs)

- Undelete covers NTFS and exFAT. APFS is copy-on-write and encrypted by
  default, making undelete effectively impossible; ext4 undelete is not
  implemented. Mac/Linux internal drives get deep-scan mode only.
- exFAT can only recover a fragmented deleted file's first run for certain -
  the FAT chain is cleared on delete. The rest is assumed contiguous and
  flagged as such.
- SSDs with TRIM have physically erased the data. No software can recover it.
  Never imply otherwise in UI text or docs.
- Compressed and encrypted NTFS files are not decoded.
- Carved formats without a footer (ZIP, DOCX, MP4) include trailing junk.
- Carving cannot tell whether a file is whole, and a fragmented file carves
  into nonsense after its first piece however clean the header looked. Carved
  results are therefore never scored above 75, and never at 100. Don't
  "improve" that number.

## Testing

```bash
python3 -m unittest discover -v
```

Stdlib only, no root, no filesystem tools, a couple of seconds. Run it before
every commit.

Two assertions must always hold, and every test enforces both:

1. Recovered files are byte-identical to the originals.
2. The source image checksum is identical before and after a scan.

Assertion 2 is not left to a single test. `tests/support.ImageTestCase` hashes
the image in `setUp` and re-checks it in cleanup, so *any* test that opens a
source image fails if a byte moved.

### How the suite gets a volume to scan

`tests/ntfs_image.py` and `tests/exfat_image.py` build real volume images in
memory - boot sector, MFT records with data runs, allocation bitmap, exFAT
entry sets with valid checksums - and the real parsers read them back. Nothing
is mocked. This is what lets the suite run identically on Windows, macOS and
Linux, and it can produce cases a real filesystem won't make on demand: a file
split across five runs, a file whose clusters have since been reallocated, a
deleted folder with live files inside it, a corrupt entry set that must be
ignored rather than shown as a plausible filename.

When you add a case, add it to the builder rather than checking in a binary.

### The real-filesystem tests

The manual procedure this project used before the suite existed is automated
in `tests/test_integration_ntfs.py` and `tests/test_integration_exfat.py`:
format a volume with the OS's own tool, write files, delete them with a real
driver, unmount, scan the image, compare checksums. That is still the gold
standard - a real driver deletes files the way real drivers do, not the way we
assume they do.

Those tests format and mount a filesystem, so they only run on request:

```bash
RECOVERKIT_INTEGRATION=1 python3 -m unittest discover -v
```

They skip with a reason when the tooling is absent: NTFS needs `mkntfs` and
`ntfs-3g` (Linux); exFAT uses `mkfs.exfat` on Linux (as root) or
`hdiutil` + `newfs_exfat` on macOS. Run them on Linux before releasing a
change to either parser.

Two things about these tests are not obvious and cost real debugging time:

- **Unmount and remount between writing the files and deleting them.** `sync`
  is not enough. If the data is still in the buffer cache when the file is
  deleted, the OS drops those pages rather than writing them, and the image
  ends up with a perfectly good record pointing at clusters that were never
  filled in. The engine then looks broken when it isn't.
- **Assert the score's promise, not perfection.** A real OS writes its own
  housekeeping into the space a deleted file just released - macOS drops
  `.fseventsd` records straight into it within seconds. That file really is
  damaged, and saying so is the feature. The tests assert that every file
  scored 100% comes back byte-identical, and that at least one gets there.

## Roadmap

- Port the GUI to Tauri (Rust + web frontend) for a modern look. Keep
  `ntfs.py` / `exfat.py` / `carve.py` as the engines, or port them to Rust.
- FAT32 undelete — the last common camera/card format not covered. The
  directory-entry shape is different enough to need its own module, but
  `exfat.py` is the closer template.
- Preview pane (thumbnail for images, first page for PDFs) before recovering.
- Whole-disk scanning (`\\.\PhysicalDrive1`, `/dev/sdb`) to reach space
  outside the current partition layout.

## Style

- Standard library only. Python 3.8-compatible syntax.
- Comments explain *why*, especially for filesystem-format quirks — the NTFS
  spec is not obvious to a reader.
- User-facing strings are plain English, no jargon.
