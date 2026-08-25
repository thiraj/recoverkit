"""
diskio.py - strictly read-only access to a raw disk or volume.

SAFETY DESIGN
-------------
This module is the ONLY place in the whole tool that touches a source drive,
and it is physically incapable of writing to one:

  * The device is opened with the OS read-only flag (O_RDONLY / "rb").
    A write would be rejected by the kernel, not just by our code.
  * The returned object exposes no write, seek-and-write, or truncate method.
  * Nothing else in the tool ever opens a source device.

Reads are sector-aligned internally because Windows rejects unaligned reads on
a raw volume handle. Callers can ask for any offset and length they like.
"""

import os
import re
import sys


class ReadOnlyDisk:
    """A read-only window onto a raw device. Never writes. Ever."""

    def __init__(self, path, sector_size=512):
        self.path = path
        self.sector_size = sector_size
        self._size = None

        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):          # Windows
            flags |= os.O_BINARY
        if hasattr(os, "O_NOATIME"):         # Linux: don't even update atime
            try:
                flags |= os.O_NOATIME
            except Exception:
                pass

        try:
            self._fd = os.open(path, flags)
        except PermissionError:
            raise PermissionError(
                "Permission denied opening the drive.\n\n"
                "Windows: run the app as Administrator.\n"
                "macOS/Linux: run with sudo."
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"Device not found: {path}")

    # -- geometry -----------------------------------------------------------
    def size(self):
        if self._size is None:
            try:
                self._size = os.lseek(self._fd, 0, os.SEEK_END)
            except OSError:
                self._size = 0
            finally:
                os.lseek(self._fd, 0, os.SEEK_SET)
        return self._size

    # -- reading ------------------------------------------------------------
    def read(self, offset, length):
        """Read `length` bytes from absolute `offset`. Handles alignment."""
        if length <= 0:
            return b""

        ss = self.sector_size
        start = (offset // ss) * ss
        skip = offset - start
        total = ((skip + length + ss - 1) // ss) * ss

        os.lseek(self._fd, start, os.SEEK_SET)
        buf = b""
        remaining = total
        while remaining > 0:
            piece = os.read(self._fd, min(remaining, 8 * 1024 * 1024))
            if not piece:
                break
            buf += piece
            remaining -= len(piece)

        return buf[skip:skip + length]

    def stream(self, chunk_size, start=0):
        """Yield (offset, bytes) sequentially. Used by the carver."""
        offset = start
        os.lseek(self._fd, start, os.SEEK_SET)
        while True:
            data = os.read(self._fd, chunk_size)
            if not data:
                return
            yield offset, data
            offset += len(data)

    def close(self):
        try:
            os.close(self._fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Device discovery
# ---------------------------------------------------------------------------

def device_path(identifier):
    """Turn a user-facing name into a raw device path for this OS."""
    if sys.platform == "win32":
        letter = identifier.rstrip("\\/").rstrip(":")
        return rf"\\.\{letter}:"
    return identifier


# ---------------------------------------------------------------------------
# macOS volume discovery
#
# One `diskutil list -plist` call, and only that call. `diskutil info` is not
# used anywhere: on a flaky or slow removable device - exactly the kind of
# device this tool exists for - it can block forever, which would freeze the
# window before the user ever sees it.
# ---------------------------------------------------------------------------

_DISKUTIL = "/usr/sbin/diskutil"
_DISKUTIL_TIMEOUT = 10

# APFS presents a synthesized disk (disk1) backed by a real one (disk0s2).
# Cached because it is consulted by the destination guard, and disks are not
# re-synthesized while a scan is running.
_physical_store_cache = None


def refresh():
    """
    Forget everything cached about the machine's disks.

    Removable drives are unplugged and replugged constantly, and macOS hands
    out a different identifier each time - the same stick can be disk4 one
    minute and disk5 the next. Anything holding a device path across a replug
    is holding a path to whatever is there now, which may be a different
    drive entirely.
    """
    global _physical_store_cache
    _physical_store_cache = None


def _diskutil_plist(args):
    """Run diskutil and parse its plist output. None on any failure."""
    try:
        import plistlib
        import subprocess
        result = subprocess.run([_DISKUTIL] + args, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                timeout=_DISKUTIL_TIMEOUT)
        if result.returncode != 0:
            return None
        return plistlib.loads(result.stdout)
    except Exception:
        return None


def physical_store_map(plist):
    """
    {synthesized disk: the real disk underneath it} for APFS containers.

    Without this, scanning /dev/rdisk0s2 (an APFS container on the internal
    SSD) and recovering to / (which the system reports as disk1) looks like
    two different drives. It is one drive, and recovering there would eat the
    data being recovered.
    """
    mapping = {}
    for entry in (plist or {}).get("AllDisksAndPartitions", []):
        stores = entry.get("APFSPhysicalStores") or []
        synthesized = entry.get("DeviceIdentifier")
        if not synthesized or not stores:
            continue
        backing = _whole_disk(stores[0].get("DeviceIdentifier"))
        if backing:
            mapping[synthesized] = backing
    return mapping


def _resolve_synthesized(disk):
    """Follow a synthesized disk down to the hardware it really lives on."""
    global _physical_store_cache
    if sys.platform != "darwin" or disk is None:
        return disk
    if _physical_store_cache is None:
        _physical_store_cache = physical_store_map(
            _diskutil_plist(["list", "-plist"])) or {}
    seen = set()
    while disk in _physical_store_cache and disk not in seen:
        seen.add(disk)
        disk = _physical_store_cache[disk]
    return disk


def human_size(byte_count):
    """4026531840 -> '3.8 GB'. Plain language, no jargon, no padding."""
    if not byte_count:
        return ""
    size = float(byte_count)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "bytes":
                return f"{int(size)} bytes"
            return f"{size:.1f} {unit}".replace(".0 ", " ")
        size /= 1024
    return ""


# Partition content types we can name. Anything not listed is left unnamed
# rather than guessed at - "Microsoft Basic Data" is exFAT or FAT32 and the
# difference matters, so we say nothing instead of saying the wrong thing.
_CONTENT_NAMES = {
    # "Windows_NTFS" is deliberately absent: it is MBR type 0x07, which means
    # NTFS *or* exFAT. When the volume is mounted we ask the filesystem; when
    # it is not, we say nothing rather than guess.
    "Apple_APFS": "APFS",
    "Apple_HFS": "Mac (HFS+)",
    "Apple_HFSX": "Mac (HFS+)",
    "Linux_Filesystem": "Linux",
}

# Volumes that are never what someone is trying to recover.
_HIDDEN_VOLUMES = {"Preboot", "Recovery", "VM", "Update", "xarts",
                   "iSCPreboot", "Hardware"}


def _describe(name, mount_point, size, removable, filesystem, device):
    """
    Build the label the user picks from the dropdown.

        Untitled - /Volumes/Untitled - 3.7 GB removable, NTFS [/dev/rdisk4s1]

    The name and mount point are what they recognise from Finder; the /dev
    path is kept on the end so anyone who does know what they are looking at
    can confirm they picked the right thing.
    """
    bits = []
    if size:
        bits.append(human_size(size))
    bits.append("removable" if removable else "internal")
    if filesystem:
        bits.append(filesystem)
    return (f"{name or 'Untitled'} - {mount_point or 'not mounted'} - "
            f"{', '.join(bits)} [{device}]")


def _macos_volumes():
    """
    Volumes worth offering, friendliest first.

    Removable drives come first because that is where recovery usually
    happens - cards, sticks and cameras.

    Two things are deliberately left out. APFS *containers* (the
    `Apple_APFS` partitions) are not volumes; the volumes inside them are
    listed individually, and offering both means two entries called
    "Untitled" that are really the same storage. And APFS volumes are listed
    without a size, because every volume in a container reports the whole
    container - showing four different volumes as "465.7 GB" each would be a
    confident lie.
    """
    plist = _diskutil_plist(["list", "-plist"])
    if not plist:
        return None

    external = _diskutil_plist(["list", "-plist", "external"]) or {}
    removable_disks = set(external.get("WholeDisks") or [])

    text = _read_mount_text()
    filesystems = parse_mount_filesystems(text) if text else {}

    volumes, whole_disks = [], []
    for entry in plist.get("AllDisksAndPartitions", []):
        disk_id = entry.get("DeviceIdentifier", "")
        backing = _resolve_synthesized(disk_id) or disk_id
        removable = disk_id in removable_disks or backing in removable_disks

        for part in (entry.get("Partitions") or []):
            ident = part.get("DeviceIdentifier")
            content = part.get("Content", "")
            if not ident or part.get("OSInternal"):
                continue
            if content in ("EFI", "Apple_APFS", "Apple_Boot", "Apple_KernelCoreDump"):
                continue
            device = f"/dev/r{ident}"
            volumes.append((
                not removable, (part.get("VolumeName") or "").lower(),
                _describe(part.get("VolumeName"), part.get("MountPoint"),
                          part.get("Size"), removable,
                          filesystems.get(part.get("MountPoint"))
                          or _CONTENT_NAMES.get(content, ""), device),
                device))

        for part in (entry.get("APFSVolumes") or []):
            ident = part.get("DeviceIdentifier")
            name = part.get("VolumeName")
            if not ident or part.get("OSInternal") or name in _HIDDEN_VOLUMES:
                continue
            # An APFS snapshot is another view of a volume already listed.
            if re.match(r"^disk\d+s\d+s\d+$", ident):
                continue
            device = f"/dev/r{ident}"
            volumes.append((
                not removable, (name or "").lower(),
                _describe(name, part.get("MountPoint"), None, removable,
                          filesystems.get(part.get("MountPoint")) or "APFS",
                          device),
                device))

        if entry.get("APFSPhysicalStores"):
            continue                                 # synthesized, not hardware
        if disk_id:
            device = f"/dev/r{disk_id}"
            whole_disks.append((
                not removable, disk_id,
                f"Whole drive - {human_size(entry.get('Size'))} "
                f"{'removable' if removable else 'internal'} [{device}]",
                device))

    volumes.sort()
    whole_disks.sort()
    return [(label, path) for _, _, label, path in volumes + whole_disks]


def _macos_volumes_fallback():
    """Raw device nodes, for when diskutil is unavailable or misbehaving."""
    out = []
    try:
        for name in sorted(os.listdir("/dev")):
            if name.startswith("rdisk") and name[5:].replace("s", "").isdigit():
                out.append((f"/dev/{name}", f"/dev/{name}"))
    except OSError:
        pass
    return out


def list_volumes():
    """
    Return [(display_name, device_path), ...] for volumes we might scan.
    Best-effort, no third-party dependencies.
    """
    out = []

    if sys.platform == "win32":
        import ctypes
        import string
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask >> i & 1:
                out.append((f"{letter}:", rf"\\.\{letter}:"))

    elif sys.platform == "darwin":
        # /dev/rdiskN is the raw (unbuffered) character device - much faster.
        out = _macos_volumes() or _macos_volumes_fallback()

    else:  # Linux
        try:
            with open("/proc/partitions") as fh:
                for line in fh.readlines()[2:]:
                    parts = line.split()
                    if len(parts) == 4:
                        dev = parts[3]
                        out.append((f"/dev/{dev}", f"/dev/{dev}"))
        except OSError:
            pass

    return out


# ---------------------------------------------------------------------------
# "Is the recovery folder on the drive we're reading?"
#
# Getting this wrong is the worst bug this tool can have: the recovered files
# land on the source and overwrite the deleted data mid-recovery. Everything
# below therefore fails closed - any question it cannot answer is answered
# "yes, same drive", which blocks the scan.
#
# The parsing is split into small pure functions so the whole thing can be
# tested against captured mount tables from several systems, rather than only
# against whatever happens to be plugged into the machine running the tests.
# ---------------------------------------------------------------------------

def _whole_disk(name):
    """
    Reduce a device name to the physical disk it belongs to.

    A partition is not a safe unit of comparison: /dev/disk4s1 and /dev/disk4
    are the same piece of hardware, and so are /dev/sda1 and /dev/sda. Returns
    None when the name is not one we recognise, which callers treat as "assume
    the worst".

        /dev/rdisk4s1  -> disk4        (macOS, raw character device)
        disk1s5s1      -> disk1        (macOS, APFS snapshot of a volume)
        /dev/sda1      -> sda          (Linux)
        /dev/nvme0n1p3 -> nvme0n1      (Linux NVMe)
        /dev/mmcblk0p1 -> mmcblk0      (Linux SD card)
    """
    if not name:
        return None
    name = name.strip()
    if name.startswith("/dev/"):
        name = name[5:]
    if "/" in name or not name:
        return None

    # macOS: optional leading r for the raw device, then diskN, then any
    # number of sNN partition/snapshot suffixes.
    match = re.match(r"^r?(disk\d+)(?:s\d+)*$", name)
    if match:
        return match.group(1)

    # Linux NVMe and SD/MMC put a p before the partition number.
    match = re.match(r"^(nvme\d+n\d+|mmcblk\d+|loop\d+)(?:p\d+)?$", name)
    if match:
        return match.group(1)

    # Linux SCSI/SATA/virtio/Xen: trailing digits are the partition. The
    # prefixes are listed explicitly - a looser pattern would happily turn
    # "tmpfs" into a disk called "tmpfs".
    match = re.match(r"^((?:sd|hd|vd|xvd)[a-z]+)\d*$", name)
    if match:
        return match.group(1)

    return None


def _device_from_mount_source(source):
    """
    Pull a device name out of the left-hand column of a mount table.

    Most entries are a plain /dev node, but macOS mounts NTFS and exFAT
    through a userspace filesystem and writes the device as a URL:

        /dev/disk1s5s1      -> disk1s5s1
        ntfs://disk4s1/     -> disk4s1
        exfat://disk5s1/    -> disk5s1
        devfs, map auto_home, tmpfs, //server/share  -> None
    """
    if not source:
        return None
    source = source.strip()
    if source.startswith("/dev/"):
        return source[5:]
    match = re.match(r"^[a-z0-9]+://([^/]+)/?$", source, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def parse_mount_table(text):
    """
    Turn mount-table text into [(mount_point, device_name), ...].

    Accepts both the `mount` output used on macOS:

        /dev/disk1s1 on /System/Volumes/Data (apfs, local, journaled)
        ntfs://disk4s1/ on /Volumes/Untitled (lifs, local, read-only)

    and the /proc/mounts format used on Linux:

        /dev/sda1 /mnt/photos ext4 rw,relatime 0 0

    Mount points containing spaces are why the macOS form is split on " on "
    and then on the final " (" rather than on whitespace.
    """
    entries = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue

        if " on " in line and line.rstrip().endswith(")"):
            source, _, rest = line.partition(" on ")
            point = rest.rsplit(" (", 1)[0]
        else:
            parts = line.split()
            if len(parts) < 2:
                continue
            source, point = parts[0], parts[1]
            # /proc/mounts escapes spaces and friends as octal.
            point = re.sub(r"\\([0-7]{3})",
                           lambda m: chr(int(m.group(1), 8)), point)

        device = _device_from_mount_source(source)
        if device and point.startswith("/"):
            entries.append((point, device))
    return entries


# What a mount table's filesystem token means in plain language. `lifs` is
# macOS's userspace filesystem host - it says nothing about the format, so the
# real answer comes from the device URL scheme instead.
_FS_NAMES = {
    "ntfs": "NTFS", "exfat": "exFAT", "msdos": "FAT32", "fat32": "FAT32",
    "vfat": "FAT32", "apfs": "APFS", "hfs": "Mac (HFS+)", "ext4": "Linux",
    "ext3": "Linux", "ext2": "Linux",
}


def parse_mount_filesystems(text):
    """
    {mount point: filesystem name} from mount-table text.

    The partition type byte cannot answer this: MBR type 0x07 is used for
    both NTFS and exFAT, so `diskutil` calls a freshly made exFAT card
    "Windows_NTFS". Only the mounted filesystem knows what it really is.

    macOS mounts NTFS and exFAT through `lifs`, which is a host rather than a
    format - but it writes the device as `ntfs://...` or `exfat://...`, and
    that scheme is the real answer.
    """
    found = {}
    for line in text.splitlines():
        if " on " not in line or not line.rstrip().endswith(")"):
            continue
        source, _, rest = line.partition(" on ")
        point, _, options = rest.rpartition(" (")
        token = options.split(",")[0].strip().rstrip(")").lower()

        scheme = re.match(r"^([a-z0-9]+)://", source.strip(), re.IGNORECASE)
        if scheme:
            token = scheme.group(1).lower()

        name = _FS_NAMES.get(token)
        if name and point.startswith("/"):
            found[point] = name
    return found


def _read_mount_text():
    """Raw mount-table text for this OS, or None."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/mounts") as fh:
                return fh.read()
        except OSError:
            return None
    try:
        import subprocess
        result = subprocess.run(["/sbin/mount"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        return None
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 \
        else None


def _read_mount_table():
    """The live mount table, or None if it cannot be read."""
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/mounts") as fh:
                return parse_mount_table(fh.read())
        except OSError:
            return None
    try:
        import subprocess
        result = subprocess.run(["/sbin/mount"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=10)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return parse_mount_table(result.stdout.decode("utf-8", "replace"))


def device_backing_path(path, mounts):
    """
    Which device holds `path`, according to `mounts`.

    The answer is the longest mount point that the path sits underneath -
    /Volumes/Untitled beats / for /Volumes/Untitled/Recovered.
    """
    if not mounts:
        return None
    path = os.path.abspath(path)
    best = None
    for point, device in mounts:
        if path == point or path.startswith(point.rstrip("/") + "/"):
            if best is None or len(point) > len(best[0]):
                best = (point, device)
    return best[1] if best else None


def same_physical_drive(source_path, dest_folder):
    """
    True if writing to dest_folder could land on the drive being scanned.

    Deliberately cautious - if we can't tell, we say yes and the scan is
    blocked. A false alarm costs the user one annoyed click; a miss costs
    them the files they were trying to rescue.
    """
    dest = os.path.abspath(dest_folder)

    if sys.platform == "win32":
        src_letter = source_path.rstrip("\\/").rstrip(":")[-1:].upper()
        dst_letter = dest[:1].upper()
        if not src_letter.isalpha() or not dst_letter.isalpha():
            return True                  # can't tell - assume the worst
        return src_letter == dst_letter

    # Scanning a disk image file rather than a device. Writing to the
    # filesystem that holds the image does not overwrite the image's own
    # contents, so this is allowed - it is how the test suite works.
    try:
        if os.path.isfile(source_path):
            return False
    except OSError:
        return True

    source_disk = _resolve_synthesized(_whole_disk(source_path))
    if source_disk is None:
        return True

    # No existence check: the mount table answers this by path prefix, so a
    # recovery folder that has not been created yet is still resolved to the
    # drive it *would* be created on. Walking up to an existing parent was
    # worse than useless - if the destination's own volume was unmounted or
    # renamed, the walk landed on / and cheerfully reported a different drive.
    dest_device = device_backing_path(dest, _read_mount_table())
    dest_disk = _resolve_synthesized(_whole_disk(dest_device))
    if dest_disk is None:
        return True

    return source_disk == dest_disk
