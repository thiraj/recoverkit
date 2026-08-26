# RecoverKit

[![tests](https://github.com/thiraj/recoverkit/actions/workflows/tests.yml/badge.svg)](https://github.com/thiraj/recoverkit/actions/workflows/tests.yml)

Free, open-source file recovery for Windows, macOS and Linux.

Recover deleted files — with their **original names and folders** where the
filesystem still remembers them. Search the results instead of scrolling
through thousands of `recovered_00001.jpg` files.

**Nothing is ever written to the drive you are recovering from.** See
[SAFETY.md](SAFETY.md) for exactly how that is guaranteed.

---

## Two modes

| Mode | What you get | Works on |
|---|---|---|
| **Undelete** | Real filenames, original folder paths, deletion dates, exact sizes, and an honest recovery-chance score. Handles fragmented files correctly. | **NTFS** — Windows drives and external disks. **exFAT** — larger SD cards, phones, most USB sticks. **FAT32** — camera cards and anything 32GB or under |
| **Deep scan** | Files rebuilt from their content signatures. No names or folders, and no way to tell whether a file is whole — scores are capped accordingly. | Any drive: APFS, HFS+, ext4, FAT32, SD cards, cameras |

Use **Undelete** first — RecoverKit picks the right engine for the drive on its
own. Fall back to **Deep scan** if the drive is neither NTFS nor exFAT, or the
file table is damaged.

## Install and run

Requires Python 3.8 or newer. No third-party packages.

```bash
git clone <your-repo-url>
cd recoverkit
```

**Windows** — open PowerShell *as Administrator*:
```powershell
python app.py
```

**macOS / Linux**:
```bash
sudo python3 app.py
```

Administrator/root is unavoidable: reading a drive below the filesystem level
is a privileged operation on every OS.

## How to use it

1. **Stop using the affected drive.** Every file written to it risks
   overwriting your deleted data permanently. If it's your system drive,
   consider shutting down and connecting it to another machine.
2. Pick the drive in the dropdown.
3. Pick a **recovery folder on a different drive**. The app refuses to
   continue otherwise.
4. Press **Start scan**.
5. Type in the **Search** box to filter by filename or folder. Click a column
   header to sort. Tick **Only likely recoverable** to hide the hopeless ones.
6. Select the files you want (Ctrl-click / Shift-click) and press
   **Recover selected**. Original folder structure is recreated for you.

## Reading the recovery chance

The score weighs two independent pieces of evidence: whether the file's
clusters are still free according to the volume's allocation bitmap, and
whether the data sitting there still looks like the kind of file the name
claims. The second matters more than it sounds — space on a well-used drive
gets handed out and given back repeatedly, so "not currently in use" does not
mean "still your data".

- **80–100%** (green) — the space is still free. Very likely intact.
- **40–79%** (amber) — partly reused. Expect a damaged file.
- **0–39%** (red) — the space belongs to other files now. The name survives,
  the data doesn't.

The **Condition** column says which check produced the verdict:

| Condition | Meaning |
|---|---|
| *looks intact* | The start of the data is still the right kind of file. |
| *content gone* | Something else has been written over it. It will not open. |
| *space is empty* | Nothing is left there at all. |
| *in the Trash - not deleted* | The file hasn't been deleted at all — it was moved. Open your Trash and drag it back; you don't need this tool for it. |
| *space reused - may work* | Something else is using that space, but the data still looks like your file. Worth trying, worth checking afterwards. |
| *(blank)* | We don't know this file type, so we offer no opinion. |

This is honest rather than optimistic. Commercial tools that promise 100%
on everything are guessing.

## Recovering files deleted long ago

Deleted-file records survive in the file table until Windows reuses them, which
can take **years** on a drive that isn't heavily written to. That is why old
deletions often still appear.

To maximise what you find:

- Scan the drive as soon as possible, and write nothing to it in the meantime.
- Run **Undelete** first, then **Deep scan** — they find different things, and
  deep scan can recover files whose table entry is long gone.
- Deep-scan an external drive as a whole device (`\\.\PhysicalDrive1`,
  `/dev/rdisk2`, `/dev/sdb`) rather than a single partition, to reach space
  outside the current partition layout.

### The SSD problem

If the drive is an SSD with TRIM enabled — which is nearly all modern internal
SSDs — deleted data is physically erased by the drive itself, usually within
seconds. **No software can recover it.** Recovery works best on mechanical hard
drives, USB sticks, SD cards and older external drives. This is a hardware
reality, not a limitation of this tool, and any product claiming otherwise is
selling you something.

## Building a double-clickable app

```bash
pip install pyinstaller

# Windows (prompts for Administrator automatically)
pyinstaller --onefile --noconsole --uac-admin --name RecoverKit app.py

# macOS
pyinstaller --onefile --windowed --name RecoverKit app.py

# Linux
pyinstaller --onefile --name recoverkit app.py
```

Two things to expect when distributing:

- **Windows SmartScreen** will warn users about an unsigned executable. A
  code-signing certificate (~£200/year) removes this.
- **Antivirus false positives** are common for tools that read raw disks.
  Submitting the binary to vendors for whitelisting helps.

## Running the tests

```bash
python3 -m unittest discover -v
```

No dependencies, no root, no disk images to download — the suite builds real
NTFS and exFAT volumes in memory and reads them back with the same parsers
that read a physical drive. Every test also re-checks that the image it
scanned is byte-for-byte unchanged afterwards.

Every push runs it on Windows, macOS and Linux across Python 3.8 to 3.13, and
runs the real-filesystem tests on Linux where `mkntfs` and `mkfs.exfat`
exist.

There is a second layer that formats and mounts a real filesystem with the
operating system's own tools. It needs `ntfs-3g` (Linux) or
`hdiutil`/`newfs_exfat` (macOS) and only runs on request:

```bash
RECOVERKIT_INTEGRATION=1 python3 -m unittest discover -v
```

## Project layout

```
diskio.py   Read-only disk access. The only module that touches a source drive.
ntfs.py     Master File Table parser — the NTFS undelete engine.
exfat.py    Directory-entry parser — the exFAT undelete engine.
carve.py    Signature scanner — the deep-scan engine.
app.py      The GUI.
tests/      The test suite. python3 -m unittest discover -v
```

Each engine is usable on its own if you want to build something else on top.
`ntfs.py` and `exfat.py` share an interface, so code that drives one drives
the other:

```python
import diskio, exfat

with diskio.ReadOnlyDisk("/dev/rdisk2") as disk:
    volume = exfat.ExfatVolume(disk)
    for found in volume.scan():
        print(found.chance, found.path, found.name, found.size)
        data = volume.read_file(found)
```

## Known limits

- Undelete covers NTFS and exFAT. APFS (Mac) and ext4 (Linux) undelete is not
  implemented — APFS in particular is copy-on-write and encrypted by default,
  which makes it effectively impossible.
- On FAT32, deleting a file overwrites the first letter of its short name
  and erases the record of where the file continued. Long filenames survive
  and are what you see; files stored in one piece come back exactly; anything
  longer is recovered on the assumption it was contiguous, and scored no
  higher than 50% because that is a guess.
- On exFAT, a deleted file that was stored in one piece comes back exactly.
  A deleted file that was *fragmented* has lost the record of where its later
  pieces went, so RecoverKit assumes it ran on contiguously and caps its score
  at 50%. Photos and videos straight off a camera are almost always in one
  piece; files edited in place on the card often are not.
- Very large drives take a long time to deep scan; undelete is much faster
  because it only reads the file table. Deep scan reads the drive roughly
  once — it does not hold what it finds in memory, so scanning a full card
  costs the same memory as scanning an empty one.
- Carved files with no end-marker (ZIP, DOCX, MP4) come out with harmless
  trailing junk.
- Compressed and encrypted NTFS files are not decoded.

## Alternatives worth knowing

**TestDisk / PhotoRec** (GPL) is the mature, battle-tested option and supports
far more file formats. If you just need your files back today, use it.
RecoverKit exists because PhotoRec is hard to use and can't search by filename.

## Licence

MIT — see [LICENSE](LICENSE).

Patches are welcome. Commits need a `Signed-off-by` line; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Disclaimer

Recovery is never guaranteed. This software is provided as-is with no warranty.
If the data is irreplaceable and this doesn't get it back, stop and consult a
professional data-recovery service before doing anything else to the drive.
