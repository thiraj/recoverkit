# Safety design

The single biggest risk in data recovery isn't failing to find the file — it's
a tool that damages the drive while looking. RecoverKit is built so that the
source drive cannot be modified, and the guarantee doesn't rest on developers
remembering to be careful.

## 1. The source drive is opened read-only by the operating system

`diskio.ReadOnlyDisk` opens the device with `os.O_RDONLY` (plus `O_BINARY` on
Windows). A write attempt is rejected by the **kernel**, not by application
logic that could contain a bug.

Verified:

```
Write to source rejected by OS: OSError - Bad file descriptor
```

The object exposes `read`, `stream`, `size` and `close`. There is no write
method, no seek-and-write, no truncate.

## 2. Only one module can touch a source drive

`diskio.py` is the sole place in the codebase that opens a device. `ntfs.py`,
`exfat.py` and `carve.py` receive an already-read-only handle. There is no
second code path to audit, and a test fails if one ever appears.

## 3. Every test re-checks the source afterwards

The test suite hashes each disk image before a test opens it and again when
the test finishes. Any test that scanned, read or recovered from an image
fails if a single byte moved — the check lives in the shared base class, so it
applies to tests written later by people who never read this file.

```bash
python3 -m unittest discover -v
```

The same suite asserts that the descriptor really is `O_RDONLY` (it asks the
OS, via `fcntl`), that writing to it raises, that `ReadOnlyDisk` exposes no
method with a write-shaped name, and that no module except `diskio.py`
contains a call that opens anything.

## 4. Verified byte-for-byte on real volumes

Both undelete engines are checked against volumes formatted, written and
deleted by the operating system's own driver, not by us — `mkntfs`/`ntfs-3g`
for NTFS, `newfs_exfat` (macOS) or `mkfs.exfat` (Linux) for exFAT. The exFAT
engine returns camera-shaped files from a real macOS-written card image
byte-for-byte:

```
100%  DCIM\100CANON  IMG_0042.JPG   300005 bytes  -> MATCH
100%  DCIM\100CANON  IMG_0043.JPG   150003 bytes  -> MATCH
100%  \              receipt.txt         17 bytes  -> MATCH
```

For NTFS, a test volume was created, files were written into nested folders,
deleted normally, and then scanned:

```
before: 69dbf8cd87a0611e48ad7dce0ed9ba6a
after : 69dbf8cd87a0611e48ad7dce0ed9ba6a
SOURCE IMAGE UNCHANGED
```

Recovered files matched their originals exactly:

```
jpg  byte-perfect: True
xlsx byte-perfect: True
```

## 5. Live files are skipped entirely

Both undelete engines check the in-use flag on every record — the NTFS MFT
record header, the exFAT directory entry type byte — and ignore any file still
marked as active. Your current files are never listed, never read for
recovery, and never candidates for anything.

## 6. You cannot recover onto the drive you're scanning

This is the classic way people destroy their own data — the recovered files
land on the same drive and overwrite the deleted ones mid-recovery.

Before every scan and every recovery, RecoverKit works out which physical
drive the recovery folder is really on and refuses to proceed if it is the
drive being scanned. When it can't tell for certain, it assumes they match and
blocks. It also writes and deletes a small probe file first, so a permissions
problem surfaces before a long scan rather than after it.

"Really on" is doing some work in that sentence, and getting it wrong is how
this check fails silently:

- A partition and its disk are the same hardware. Scanning `/dev/disk4` and
  recovering to a folder on `/dev/disk4s1` is the same drive.
- macOS mounts NTFS and exFAT through a userspace filesystem, so the mount
  table reports the device as `ntfs://disk4s1/` rather than `/dev/disk4s1`.
  A check that only understands `/dev/` paths sees no device at all.
- APFS presents a container as a synthesized disk (`disk1`) backed by a real
  partition (`disk0s2`). Comparing those two names finds them different. They
  are one SSD.

All three are handled, and all three have tests built on captured mount tables
from real machines, in `tests/test_destination_guard.py`.

> **Fixed in this version.** The original check compared the destination's
> `st_dev` against the source device's `st_rdev`. On macOS those are unrelated
> numbers — a mount point's `st_dev` is a synthetic filesystem id — so the
> check answered "different drives" for *every* case, including recovering a
> USB stick onto itself. It happened to work on Linux. If you are running an
> older build, do not rely on this guard: check the destination yourself.

## 7. Nothing is overwritten in the destination either

Recovered files that would collide with an existing name are saved as
`filename (2).ext`. No existing file in your recovery folder is replaced.

## 8. Honest reporting

Recovery chance comes from comparing each file's clusters against the
volume's allocation bitmap — real evidence about whether the space has been
reused, not a marketing number. Files whose data is gone are shown in red with
a low score rather than being quietly presented as recoverable.

On exFAT, a deleted file that was fragmented has lost the chain describing
where its later pieces went. RecoverKit recovers what it can and caps the
score at 50% rather than presenting a guessed layout as a certainty.

The bitmap is not trusted on its own, either. It reports whether space is
spoken for *today* — on a drive that has been filled and emptied over years,
a cluster can be freed, reused and freed again, and the bitmap will call it
free while the bytes belong to nobody. So every file whose type we recognise
is also checked against its own first bytes: if a `.jpg` no longer starts like
a JPEG, the score is zero and the Condition column says **content gone**,
whatever the bitmap thinks.

> **Fixed in this version.** Before this check existed, a 211 MB video on a
> USB stick was listed at a confident **100%** and recovered without
> complaint. It was noise. The file table had been read correctly and the
> bitmap was telling the truth; the space had simply been used and released
> again in the years since the file was deleted, and nothing in the tool had
> looked at the actual bytes.

## What this cannot protect you from

- **A physically failing drive.** If it's clicking, grinding or disappearing
  from the system, every read shortens its life. Clone it first with
  `ddrescue`, then scan the clone.
- **Continuing to use the drive.** The moment you save, download or install
  anything on it, you may overwrite the data you're trying to recover. This is
  the most common cause of failed recovery by a wide margin.
- **TRIM on SSDs.** The drive has already erased the data at hardware level.
  Nothing running on the computer can bring it back.

## Reporting a safety issue

If you find any code path that could write to a source device, please open an
issue and mark it security-related. That's the one bug class in this project
that matters more than anything else.
