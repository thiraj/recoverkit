"""
ntfs.py - reads the NTFS Master File Table to find deleted files.

WHY THIS BEATS CARVING
----------------------
NTFS keeps a record for every file in the Master File Table (MFT): the name,
the folder it lived in, its size, its timestamps, and a map of exactly which
clusters on disk hold its data.

When you delete a file, Windows flips one bit in that record to say "not in
use". It does not erase the record and it does not erase the data. So if the
record is still there, we can read back the original filename, the full folder
path, the exact size, and the precise clusters - which means a byte-perfect
recovery even for fragmented files.

That record survives until Windows reuses it for a new file. On a drive that
hasn't been heavily written to, records can persist for years. This is why
files deleted "ages ago" are often still listed.

WHAT WE ALSO CHECK
------------------
NTFS keeps a bitmap of which clusters are currently in use ($Bitmap). We
compare each deleted file's clusters against it. If those clusters have since
been handed to another file, the data is gone even though the name survives -
so we show an honest recovery-chance percentage instead of pretending.

Everything here is read-only. This module never writes to the source device.
"""

import datetime
import struct

import signatures

ATTR_STANDARD_INFORMATION = 0x10
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80

FLAG_IN_USE = 0x0001
FLAG_DIRECTORY = 0x0002

NAMESPACE_DOS = 2  # the legacy 8.3 name - prefer any other


def _filetime(value):
    """Convert a Windows FILETIME (100ns ticks since 1601) to a datetime."""
    if not value:
        return None
    try:
        return (datetime.datetime(1601, 1, 1)
                + datetime.timedelta(microseconds=value // 10))
    except (OverflowError, OSError, ValueError):
        return None


def _apply_fixup(record, sector_size):
    """
    NTFS stores a copy of the last two bytes of each sector in an 'update
    sequence array' to detect torn writes. Put them back before parsing.
    """
    if len(record) < 8:
        return record
    usa_offset, usa_count = struct.unpack_from("<HH", record, 4)
    if usa_count == 0 or usa_offset + usa_count * 2 > len(record):
        return record

    rec = bytearray(record)
    for i in range(1, usa_count):
        pos = i * sector_size - 2
        src = usa_offset + i * 2
        if pos + 2 > len(rec) or src + 2 > len(rec):
            break
        rec[pos:pos + 2] = rec[src:src + 2]
    return bytes(rec)


def _parse_runs(data, offset):
    """
    Decode a data-run list into [(lcn, cluster_count), ...].
    lcn of None means a sparse run (a hole of zeroes).
    """
    runs = []
    lcn = 0
    i = offset
    while i < len(data):
        header = data[i]
        if header == 0:
            break
        len_size = header & 0x0F
        off_size = (header >> 4) & 0x0F
        i += 1
        if len_size == 0 or i + len_size + off_size > len(data):
            break

        count = int.from_bytes(data[i:i + len_size], "little")
        i += len_size

        if off_size == 0:
            runs.append((None, count))
        else:
            delta = int.from_bytes(data[i:i + off_size], "little", signed=True)
            i += off_size
            lcn += delta
            runs.append((lcn, count))

        if len(runs) > 8192:  # corrupt record guard
            break
    return runs


class DeletedFile:
    __slots__ = ("name", "path", "size", "deleted_at", "created_at",
                 "is_dir", "runs", "resident", "chance", "record_no",
                 "content_check", "still_at", "_in_use")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def extension(self):
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""


class NtfsVolume:
    """Parses an NTFS volume from a ReadOnlyDisk. Read-only throughout."""

    def __init__(self, disk):
        self.disk = disk
        boot = disk.read(0, 512)

        if boot[3:11] != b"NTFS    ":
            raise ValueError("This volume is not NTFS.")

        self.sector_size = struct.unpack_from("<H", boot, 0x0B)[0] or 512
        sectors_per_cluster = boot[0x0D] or 8
        self.cluster_size = self.sector_size * sectors_per_cluster
        self.total_clusters = struct.unpack_from("<Q", boot, 0x28)[0] // sectors_per_cluster

        disk.sector_size = self.sector_size

        mft_cluster = struct.unpack_from("<Q", boot, 0x30)[0]
        self.mft_offset = mft_cluster * self.cluster_size

        raw = struct.unpack_from("<b", boot, 0x40)[0]
        self.record_size = (raw * self.cluster_size if raw > 0 else 1 << (-raw))
        if not (256 <= self.record_size <= 65536):
            self.record_size = 1024

        self._mft_runs = None
        self._bitmap = None

    # -- MFT access ---------------------------------------------------------
    def _read_record(self, number):
        """Read MFT record `number`, following the MFT's own data runs."""
        if self._mft_runs is None:
            data = self.disk.read(self.mft_offset, self.record_size)
            return _apply_fixup(data, self.sector_size) if number == 0 else None

        target = number * self.record_size
        seen = 0
        for lcn, count in self._mft_runs:
            span = count * self.cluster_size
            if lcn is not None and seen <= target < seen + span:
                offset = lcn * self.cluster_size + (target - seen)
                data = self.disk.read(offset, self.record_size)
                if data[:4] != b"FILE":
                    return None
                return _apply_fixup(data, self.sector_size)
            seen += span
        return None

    def _bootstrap(self):
        """Read record 0 ($MFT) to learn where the rest of the MFT lives."""
        rec = self.disk.read(self.mft_offset, self.record_size)
        if rec[:4] != b"FILE":
            raise ValueError("Could not find the MFT - the volume may be "
                             "damaged or not NTFS.")
        rec = _apply_fixup(rec, self.sector_size)
        parsed = self._parse_record(rec, 0)
        if not parsed or not parsed.runs:
            raise ValueError("The MFT appears to be unreadable.")
        self._mft_runs = parsed.runs

        total = sum(c for _, c in self._mft_runs if _ is not None)
        self.record_count = (total * self.cluster_size) // self.record_size

    def _load_bitmap(self):
        """Record 6 is $Bitmap - which clusters are currently allocated."""
        try:
            rec = self._read_record(6)
            if not rec:
                return
            parsed = self._parse_record(rec, 6, want_data=True)
            if parsed and parsed.runs:
                chunks = []
                for lcn, count in parsed.runs:
                    if lcn is None:
                        chunks.append(b"\x00" * (count * self.cluster_size))
                    else:
                        chunks.append(self.disk.read(lcn * self.cluster_size,
                                                     count * self.cluster_size))
                self._bitmap = b"".join(chunks)
        except Exception:
            self._bitmap = None

    def _cluster_free(self, lcn):
        if self._bitmap is None:
            return True  # unknown - don't claim it's overwritten
        byte = lcn >> 3
        if byte >= len(self._bitmap):
            return True
        return not (self._bitmap[byte] >> (lcn & 7)) & 1

    # -- record parsing -----------------------------------------------------
    def _parse_record(self, rec, number, want_data=True):
        if len(rec) < 48 or rec[:4] != b"FILE":
            return None

        attr_offset = struct.unpack_from("<H", rec, 0x14)[0]
        flags = struct.unpack_from("<H", rec, 0x16)[0]
        used = struct.unpack_from("<I", rec, 0x18)[0]
        if used > len(rec):
            used = len(rec)

        name = None
        namespace = 99
        parent = None
        created = deleted = None
        size = 0
        runs = []
        resident = None

        pos = attr_offset
        while pos + 8 <= used:
            atype = struct.unpack_from("<I", rec, pos)[0]
            if atype == 0xFFFFFFFF:
                break
            alen = struct.unpack_from("<I", rec, pos + 4)[0]
            if alen == 0 or pos + alen > len(rec):
                break

            non_resident = rec[pos + 8]

            if atype == ATTR_STANDARD_INFORMATION and not non_resident:
                coff = struct.unpack_from("<H", rec, pos + 0x14)[0]
                base = pos + coff
                if base + 32 <= len(rec):
                    created = _filetime(struct.unpack_from("<Q", rec, base)[0])
                    deleted = _filetime(struct.unpack_from("<Q", rec, base + 8)[0])

            elif atype == ATTR_FILE_NAME and not non_resident:
                coff = struct.unpack_from("<H", rec, pos + 0x14)[0]
                base = pos + coff
                if base + 66 <= len(rec):
                    p = struct.unpack_from("<Q", rec, base)[0] & 0x0000FFFFFFFFFFFF
                    real = struct.unpack_from("<Q", rec, base + 0x30)[0]
                    nlen = rec[base + 0x40]
                    ns = rec[base + 0x41]
                    try:
                        candidate = rec[base + 0x42: base + 0x42 + nlen * 2] \
                            .decode("utf-16-le", errors="replace")
                    except Exception:
                        candidate = None
                    # Prefer the real long name over the legacy 8.3 name.
                    if candidate and (name is None or
                                      (namespace == NAMESPACE_DOS and ns != NAMESPACE_DOS)):
                        name, namespace, parent = candidate, ns, p
                        if real:
                            size = real

            elif atype == ATTR_DATA and want_data:
                name_len = rec[pos + 9]
                if name_len == 0:  # unnamed $DATA = the file's actual content
                    if non_resident:
                        real = struct.unpack_from("<Q", rec, pos + 0x30)[0]
                        if real:
                            size = real
                        run_off = struct.unpack_from("<H", rec, pos + 0x20)[0]
                        runs = _parse_runs(rec[pos:pos + alen], run_off)
                    else:
                        clen = struct.unpack_from("<I", rec, pos + 0x10)[0]
                        coff = struct.unpack_from("<H", rec, pos + 0x14)[0]
                        resident = rec[pos + coff: pos + coff + clen]
                        size = clen

            pos += alen

        if name is None:
            return None

        f = DeletedFile(
            name=name, path=None, size=size,
            deleted_at=deleted, created_at=created,
            is_dir=bool(flags & FLAG_DIRECTORY),
            runs=runs, resident=resident, chance=None, record_no=number,
            content_check=signatures.UNKNOWN, still_at=None,
        )
        f.path = parent  # temporarily holds the parent record number
        f._in_use = bool(flags & FLAG_IN_USE)
        return f

    # -- public scan --------------------------------------------------------
    def scan(self, progress=None, should_stop=None, include_dirs=False):
        """
        Walk the whole MFT and return a list of DeletedFile.
        `progress(done, total)` is called periodically.
        """
        self._bootstrap()
        self._load_bitmap()

        deleted = []
        dir_names = {}   # record -> (name, parent) so we can rebuild paths
        # first cluster -> a still-living file using it. A file moved to the
        # Recycle Bin keeps its clusters, so its old record looks deleted
        # while the data is perfectly safe.
        live_starts = {}
        total = self.record_count

        for number in range(total):
            if should_stop and should_stop():
                break
            if progress and number % 2000 == 0:
                progress(number, total)

            try:
                rec = self._read_record(number)
            except Exception:
                continue
            if not rec:
                continue

            parsed = self._parse_record(rec, number)
            if not parsed:
                continue

            if parsed.is_dir:
                dir_names[number] = (parsed.name, parsed.path)
                if not include_dirs:
                    continue

            if parsed._in_use:
                if parsed.runs and parsed.runs[0][0] is not None:
                    live_starts.setdefault(parsed.runs[0][0], parsed.name)
                continue  # still a live file - leave it well alone

            parsed.chance = self._estimate_chance(parsed)
            self._check_content(parsed)
            deleted.append(parsed)

        if progress:
            progress(total, total)

        for f in deleted:
            f.path = self._build_path(f.path, dir_names)

        self._reconcile(deleted, live_starts)
        return deleted

    def _reconcile(self, deleted, live_starts):
        """
        Settle the cases where the bitmap and the file's own bytes disagree.
        See exfat._reconcile - the reasoning is identical.
        """
        for f in deleted:
            if not f.runs or f.content_check != signatures.MATCH:
                continue
            start = f.runs[0][0]
            if start is None:
                continue
            if start in live_starts:
                f.content_check = signatures.MOVED
                f.still_at = live_starts[start]
                f.chance = 100
            elif f.chance is not None and f.chance < 40:
                f.content_check = signatures.IN_USE
                f.chance = 50

    def _check_content(self, f):
        """
        Look at the file's first bytes and see if they are still the kind of
        file its name claims.

        The allocation bitmap only knows whether the space is spoken for
        today. It cannot know that the space was handed out and given back
        twice since this file was deleted, and that the bytes sitting there
        now belong to nobody. The header can.

        A mismatch is hard evidence and overrides the bitmap's optimism. A
        match is not proof the whole file survived - only that its start did.
        Files whose extension we do not recognise are left alone entirely
        rather than guessed at, and cost no read at all.
        """
        if f.is_dir or not f.size:
            return
        if not signatures.known_extension(f.extension):
            return

        if f.resident is not None:
            head = f.resident[:signatures.PROBE_BYTES]
        elif f.runs and f.runs[0][0] is not None:
            try:
                head = self.disk.read(f.runs[0][0] * self.cluster_size,
                                      signatures.PROBE_BYTES)
            except Exception:
                return
        else:
            return

        f.content_check = signatures.check(f.extension, head)
        if f.content_check in (signatures.MISMATCH, signatures.BLANK):
            f.chance = 0

    def _estimate_chance(self, f):
        """Percentage of the file's clusters that are still marked free."""
        if f.resident is not None:
            return 100
        if not f.runs:
            return 0
        total = free = 0
        for lcn, count in f.runs:
            if lcn is None:
                continue
            # Sample large runs rather than checking millions of clusters.
            step = max(1, count // 64)
            for c in range(0, count, step):
                total += 1
                if self._cluster_free(lcn + c):
                    free += 1
        if total == 0:
            return 0
        return int(free * 100 / total)

    def _build_path(self, parent, dir_names, depth=0):
        parts = []
        while parent is not None and parent > 5 and depth < 64:
            entry = dir_names.get(parent)
            if not entry:
                parts.append("?")
                break
            parts.append(entry[0])
            parent = entry[1]
            depth += 1
        return "\\".join(reversed(parts)) or "\\"

    # -- recovery -----------------------------------------------------------
    def read_file(self, f):
        """Return the file's bytes. Reads only - writes nothing anywhere."""
        if f.resident is not None:
            return f.resident[:f.size] if f.size else f.resident

        chunks = []
        remaining = f.size or 0
        for lcn, count in f.runs:
            if remaining <= 0:
                break
            span = count * self.cluster_size
            take = min(span, remaining) if remaining else span
            if lcn is None:
                chunks.append(b"\x00" * take)
            else:
                chunks.append(self.disk.read(lcn * self.cluster_size, take))
            remaining -= take

        data = b"".join(chunks)
        return data[:f.size] if f.size else data
