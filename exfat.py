"""
exfat.py - reads exFAT directory entries to find deleted files.

WHY THIS MATTERS
----------------
exFAT is what SD cards, phones and cameras are formatted with, and that is
where recovery succeeds most often: a card is usually a one-off dump of
photos, not a busy system drive, so a deleted file's data is very often still
sitting there untouched.

Like NTFS, exFAT does not erase anything when you delete a file. Every file is
described by a small "entry set" in its folder: one file entry (attributes and
timestamps), one stream entry (size and starting cluster), and one or more
name entries. Deleting a file clears a single bit - bit 7, the "in use" bit -
in each of those entries. The name, the size, the timestamps and the starting
cluster all survive, so we can recover the real filename and the real folder
path, not just an anonymous carved blob.

WHERE exFAT IS WEAKER THAN NTFS
-------------------------------
NTFS stores a full map of every cluster a file used. exFAT only stores a
starting cluster, and relies on the FAT for files that are split into pieces.
When a file is deleted the FAT chain is usually cleared, so for a fragmented
file we know where the data starts but not where it continues.

We do not pretend otherwise:

  * A file marked contiguous (the common case on cameras and cards) is
    recovered exactly, start to finish.
  * A fragmented file whose FAT chain survived is followed properly.
  * A fragmented file whose chain is gone is recovered by assuming it ran
    contiguously - which is a guess. Those files carry `assumed_contiguous`
    and their recovery chance is capped, because the tail may well be wrong.

WHAT WE ALSO CHECK
------------------
exFAT keeps an allocation bitmap of which clusters are in use. We compare each
deleted file's clusters against it, exactly as the NTFS engine does, so the
recovery-chance figure is based on evidence rather than optimism.

Everything here is read-only. This module never writes to the source device.
It receives an already-read-only handle from diskio and has no other way to
reach the drive.
"""

import datetime
import struct

import signatures

# Directory entry type codes. Bit 7 is the "in use" flag: 0x85 is a live file
# entry, 0x05 is the very same entry after the file was deleted.
ENTRY_IN_USE = 0x80
ENTRY_BITMAP = 0x81
ENTRY_UPCASE = 0x82
ENTRY_LABEL = 0x83
ENTRY_FILE = 0x85
ENTRY_STREAM = 0xC0
ENTRY_NAME = 0xC1

ATTR_DIRECTORY = 0x10

# Stream extension flags.
FLAG_ALLOC_POSSIBLE = 0x01
FLAG_NO_FAT_CHAIN = 0x02   # set = the file is contiguous, ignore the FAT

END_OF_CHAIN = 0xFFFFFFFF
BAD_CLUSTER = 0xFFFFFFF7
FIRST_CLUSTER = 2          # cluster numbering in exFAT starts at 2

ENTRY_SIZE = 32
NAME_CHARS_PER_ENTRY = 15

# Guards against a corrupt volume sending us into a very long loop.
MAX_CHAIN = 1 << 22
MAX_DIR_DEPTH = 64


def _exfat_time(stamp, ten_ms=0):
    """
    Convert an exFAT timestamp to a datetime.

    exFAT inherited the packed DOS format: two-second resolution, year counted
    from 1980. A separate byte adds 0-199 units of 10ms for the odd second and
    the fraction. Returns a naive datetime, matching the NTFS engine.
    """
    if not stamp:
        return None
    second = (stamp & 0x1F) * 2
    minute = (stamp >> 5) & 0x3F
    hour = (stamp >> 11) & 0x1F
    day = (stamp >> 16) & 0x1F
    month = (stamp >> 21) & 0x0F
    year = ((stamp >> 25) & 0x7F) + 1980
    try:
        value = datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None          # a reused entry can hold nonsense - say nothing
    if ten_ms:
        try:
            value += datetime.timedelta(milliseconds=min(ten_ms, 199) * 10)
        except (OverflowError, ValueError):
            pass
    return value


def _set_checksum(entries):
    """
    The 16-bit checksum an entry set carries over its own bytes.

    Bytes 2-3 of the first entry hold the checksum itself and are skipped.
    We use this to tell a genuine deleted file from random bytes left over in
    reused directory space - a wrong filename is worse than no filename.
    """
    checksum = 0
    for index, byte in enumerate(entries):
        if index == 2 or index == 3:
            continue
        checksum = (((checksum << 15) | (checksum >> 1)) + byte) & 0xFFFF
    return checksum


class DeletedFile:
    """
    One recoverable file.

    Deliberately the same shape as ntfs.DeletedFile so the GUI and any other
    caller can treat the two engines interchangeably. `assumed_contiguous` is
    the one addition: exFAT can lose track of a fragmented file's later
    clusters, and callers deserve to know when the tail is a guess.
    """

    __slots__ = ("name", "path", "size", "deleted_at", "created_at",
                 "is_dir", "runs", "resident", "chance", "record_no",
                 "assumed_contiguous", "content_check", "still_at",
                 "_in_use")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def extension(self):
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""


class ExfatVolume:
    """Parses an exFAT volume from a ReadOnlyDisk. Read-only throughout."""

    def __init__(self, disk):
        self.disk = disk
        boot = disk.read(0, 512)

        if len(boot) < 512 or boot[3:11] != b"EXFAT   ":
            raise ValueError("This volume is not exFAT.")

        sector_shift = boot[0x6C]
        cluster_shift = boot[0x6D]
        # A sane volume uses 512-4096 byte sectors and clusters up to 32MB.
        if not (9 <= sector_shift <= 12) or cluster_shift > 25:
            raise ValueError("This exFAT volume has an unreadable layout.")

        self.sector_size = 1 << sector_shift
        self.cluster_size = self.sector_size << cluster_shift

        self.fat_offset = struct.unpack_from("<I", boot, 0x50)[0] * self.sector_size
        self.fat_length = struct.unpack_from("<I", boot, 0x54)[0] * self.sector_size
        self.heap_offset = struct.unpack_from("<I", boot, 0x58)[0] * self.sector_size
        self.total_clusters = struct.unpack_from("<I", boot, 0x5C)[0]
        self.root_cluster = struct.unpack_from("<I", boot, 0x60)[0]

        if self.root_cluster < FIRST_CLUSTER or self.total_clusters == 0:
            raise ValueError("This exFAT volume has an unreadable layout.")

        disk.sector_size = self.sector_size

        self._bitmap = None
        self._fat_cache = {}
        self._fat_block = max(self.sector_size, 64 * 1024)

    # -- cluster arithmetic -------------------------------------------------
    def _cluster_offset(self, cluster):
        return self.heap_offset + (cluster - FIRST_CLUSTER) * self.cluster_size

    def _valid_cluster(self, cluster):
        return FIRST_CLUSTER <= cluster < FIRST_CLUSTER + self.total_clusters

    # -- the FAT ------------------------------------------------------------
    def _fat_entry(self, cluster):
        """Next cluster in the chain, or None if the entry is unusable."""
        position = cluster * 4
        if position + 4 > self.fat_length:
            return None
        block_no = position // self._fat_block
        block = self._fat_cache.get(block_no)
        if block is None:
            block = self.disk.read(self.fat_offset + block_no * self._fat_block,
                                   self._fat_block)
            # Keep the cache small; scans walk the FAT roughly in order.
            if len(self._fat_cache) > 64:
                self._fat_cache.clear()
            self._fat_cache[block_no] = block
        inside = position % self._fat_block
        if inside + 4 > len(block):
            return None
        return struct.unpack_from("<I", block, inside)[0]

    def _follow_chain(self, first, max_clusters=None):
        """
        Walk a FAT chain from `first`, returning [(cluster, count), ...] with
        consecutive clusters merged into runs so reads stay large.
        """
        runs = []
        cluster = first
        seen = set()
        while self._valid_cluster(cluster):
            if cluster in seen or len(seen) > MAX_CHAIN:
                break                      # corrupt loop - stop, don't hang
            seen.add(cluster)
            if runs and runs[-1][0] + runs[-1][1] == cluster:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((cluster, 1))
            if max_clusters and len(seen) >= max_clusters:
                break
            nxt = self._fat_entry(cluster)
            if nxt is None or nxt in (END_OF_CHAIN, BAD_CLUSTER, 0) or nxt == cluster:
                break
            cluster = nxt
        return runs

    def _contiguous_runs(self, first, count):
        if count <= 0 or not self._valid_cluster(first):
            return []
        last = FIRST_CLUSTER + self.total_clusters
        count = min(count, last - first)
        return [(first, count)]

    # -- the allocation bitmap ----------------------------------------------
    def _load_bitmap(self, entries):
        """
        The bitmap lives in a cluster chain named by an 0x81 entry in the root
        directory. One bit per cluster, set means in use.
        """
        for _, entry in entries:
            if entry[0] != ENTRY_BITMAP:
                continue
            flags = entry[1]
            if flags & 0x01:
                continue          # the second FAT's bitmap on a TexFAT volume
            first = struct.unpack_from("<I", entry, 20)[0]
            length = struct.unpack_from("<Q", entry, 24)[0]
            if not self._valid_cluster(first) or length == 0:
                return
            needed = (length + self.cluster_size - 1) // self.cluster_size
            chunks = []
            for cluster, count in self._follow_chain(first, needed):
                chunks.append(self.disk.read(self._cluster_offset(cluster),
                                             count * self.cluster_size))
            data = b"".join(chunks)
            if data:
                self._bitmap = data[:length]
            return

    def _cluster_free(self, cluster):
        if self._bitmap is None:
            return True          # unknown - don't claim it's overwritten
        index = cluster - FIRST_CLUSTER
        byte = index >> 3
        if byte >= len(self._bitmap):
            return True
        return not (self._bitmap[byte] >> (index & 7)) & 1

    # -- directory reading --------------------------------------------------
    def _read_directory(self, runs):
        """
        Return [(absolute_offset, entry_bytes), ...] for a whole directory.

        We keep going past the 0x00 "end of directory" marker rather than
        stopping there. Those trailing slots are not empty - they hold the
        entries of files deleted earlier, which is exactly what we came for.
        """
        entries = []
        for cluster, count in runs:
            base = self._cluster_offset(cluster)
            span = count * self.cluster_size
            data = self.disk.read(base, span)
            for pos in range(0, len(data) - ENTRY_SIZE + 1, ENTRY_SIZE):
                entries.append((base + pos, data[pos:pos + ENTRY_SIZE]))
        return entries

    # -- entry set parsing --------------------------------------------------
    def _parse_set(self, entries, index):
        """
        Parse the entry set starting at `entries[index]`.

        Returns (DeletedFile or None, entries_consumed). A file is described by
        a file entry followed by `secondary_count` more entries; we validate
        the set with its own checksum before trusting the name.
        """
        offset, head = entries[index]
        in_use = bool(head[0] & ENTRY_IN_USE)
        secondary = head[1]
        if secondary < 1 or index + secondary >= len(entries):
            return None, 1

        block = [entries[index + i][1] for i in range(secondary + 1)]

        # Every entry in one set sits back to back; a gap means the set was
        # partly overwritten and the metadata can't be trusted.
        for i in range(1, secondary + 1):
            if entries[index + i][0] != offset + i * ENTRY_SIZE:
                return None, 1
            kind = block[i][0] | ENTRY_IN_USE
            if kind not in (ENTRY_STREAM, ENTRY_NAME):
                return None, 1
            if bool(block[i][0] & ENTRY_IN_USE) != in_use:
                return None, 1   # half-deleted set - it has been reused

        # Re-assert the in-use bits before checksumming: deletion clears them,
        # and the stored checksum was computed while they were still set.
        restored = bytearray()
        for entry in block:
            piece = bytearray(entry)
            piece[0] |= ENTRY_IN_USE
            restored += piece
        stored = struct.unpack_from("<H", block[0], 2)[0]
        if _set_checksum(restored) != stored:
            return None, 1

        if (block[1][0] | ENTRY_IN_USE) != ENTRY_STREAM:
            return None, secondary + 1

        stream = block[1]
        stream_flags = stream[1]
        name_length = stream[3]
        data_length = struct.unpack_from("<Q", stream, 24)[0]
        first_cluster = struct.unpack_from("<I", stream, 20)[0]

        name_parts = []
        for entry in block[2:]:
            if (entry[0] | ENTRY_IN_USE) != ENTRY_NAME:
                continue
            name_parts.append(entry[2:2 + NAME_CHARS_PER_ENTRY * 2]
                              .decode("utf-16-le", errors="replace"))
        name = "".join(name_parts)[:name_length]
        if not name or "\x00" in name:
            return None, secondary + 1

        attributes = struct.unpack_from("<H", block[0], 4)[0]
        created = _exfat_time(struct.unpack_from("<I", block[0], 8)[0],
                              block[0][20])
        # exFAT has no delete timestamp; last-modified is the closest thing,
        # and it is what the NTFS engine reports in that column too.
        modified = _exfat_time(struct.unpack_from("<I", block[0], 12)[0],
                               block[0][21])

        is_dir = bool(attributes & ATTR_DIRECTORY)
        contiguous = bool(stream_flags & FLAG_NO_FAT_CHAIN)
        allocated = bool(stream_flags & FLAG_ALLOC_POSSIBLE)

        needed = ((data_length + self.cluster_size - 1) // self.cluster_size
                  if data_length else 0)
        runs = []
        assumed = False

        if allocated and self._valid_cluster(first_cluster) and needed:
            if contiguous:
                runs = self._contiguous_runs(first_cluster, needed)
            elif in_use:
                runs = self._follow_chain(first_cluster, needed)
            else:
                # Deleted and fragmented: the chain is normally wiped on
                # delete. Follow whatever survived, then fall back to
                # assuming the rest ran on contiguously - and say so.
                chain = self._follow_chain(first_cluster, needed)
                have = sum(count for _, count in chain)
                if have >= needed:
                    runs = chain
                else:
                    runs = self._contiguous_runs(first_cluster, needed)
                    assumed = True

        found = DeletedFile(
            name=name, path=None, size=data_length,
            deleted_at=modified, created_at=created,
            is_dir=is_dir, runs=runs, resident=None,
            chance=None, record_no=offset,
            assumed_contiguous=assumed,
            content_check=signatures.UNKNOWN, still_at=None,
        )
        found._in_use = in_use
        return found, secondary + 1

    # -- public scan --------------------------------------------------------
    def scan(self, progress=None, should_stop=None, include_dirs=False):
        """
        Walk every directory and return a list of DeletedFile.
        `progress(done, total)` is called periodically.
        """
        root_runs = self._follow_chain(self.root_cluster)
        if not root_runs:
            raise ValueError("Could not read the root folder - the volume may "
                             "be damaged or not exFAT.")

        root_entries = self._read_directory(root_runs)
        self._load_bitmap(root_entries)

        deleted = []
        # first cluster -> where a still-living file of that data sits. A
        # file moved to the Trash keeps its clusters and gets a new entry, so
        # its old entry looks deleted while the data is perfectly safe.
        live_starts = {}
        # (entries, path) queued breadth-first so paths are known as we go.
        pending = [(root_entries, "")]
        visited = {self.root_cluster}
        done = 0

        while pending:
            if should_stop and should_stop():
                break
            entries, path = pending.pop(0)

            index = 0
            while index < len(entries):
                if should_stop and should_stop():
                    break

                kind = entries[index][1][0]
                if (kind | ENTRY_IN_USE) != ENTRY_FILE:
                    index += 1
                    continue

                found, used = self._parse_set(entries, index)
                index += used
                if found is None:
                    continue

                found.path = path or "\\"

                if found.is_dir:
                    # Descend into live folders so we can name the path of the
                    # deleted files inside them. Deleted folders are followed
                    # too - their contents are often still intact.
                    child = found.runs[0][0] if found.runs else 0
                    if (child and child not in visited
                            and path.count("\\") < MAX_DIR_DEPTH):
                        visited.add(child)
                        sub = self._read_directory(found.runs)
                        name = (path + "\\" + found.name) if path else found.name
                        pending.append((sub, name))
                    if not include_dirs:
                        continue

                if found._in_use:
                    if found.runs:
                        live_starts.setdefault(
                            found.runs[0][0],
                            f"{found.path}\\{found.name}".replace("\\\\", "\\"))
                    continue     # still a live file - leave it well alone

                found.chance = self._estimate_chance(found)
                self._check_content(found)
                deleted.append(found)

            done += 1
            if progress:
                progress(done, done + len(pending))

        self._reconcile(deleted, live_starts)

        if progress:
            progress(1, 1)
        return deleted

    def _reconcile(self, deleted, live_starts):
        """
        Settle the cases where the bitmap and the file's own bytes disagree.

        The bitmap knows whether space is spoken for. The header knows whether
        the data is still the right kind of file. When they disagree, the
        answer is usually neither 0% nor 100% - and sometimes it is "this file
        was never deleted at all, it is in the Trash".
        """
        for f in deleted:
            if not f.runs or f.content_check != signatures.MATCH:
                continue
            start = f.runs[0][0]
            if start in live_starts:
                # A living file occupies exactly these clusters and the data
                # still matches this file's type: it was moved, not deleted.
                f.content_check = signatures.MOVED
                f.still_at = live_starts[start]
                f.chance = 100
            elif f.chance is not None and f.chance < 40:
                # Space is spoken for, but the data there still looks right.
                f.content_check = signatures.IN_USE
                f.chance = 50

    def _check_content(self, f):
        """
        Look at the file's first bytes and see if they are still the kind of
        file its name claims. See signatures.py for why the bitmap alone is
        not enough to answer that.

        Cards get this wrong more often than hard drives do, not less: a card
        is filled, emptied and refilled repeatedly, so space is handed back
        and forth constantly while the bitmap only ever reports today's state.
        """
        if f.is_dir or not f.size:
            return
        if not signatures.known_extension(f.extension):
            return
        if not f.runs:
            return

        try:
            head = self.disk.read(self._cluster_offset(f.runs[0][0]),
                                  signatures.PROBE_BYTES)
        except Exception:
            return

        f.content_check = signatures.check(f.extension, head)
        if f.content_check in (signatures.MISMATCH, signatures.BLANK):
            f.chance = 0

    def _estimate_chance(self, f):
        """Percentage of the file's clusters that are still marked free."""
        if f.size == 0:
            return 100
        if not f.runs:
            return 0
        total = free = 0
        for cluster, count in f.runs:
            # Sample large runs rather than checking millions of clusters.
            step = max(1, count // 64)
            for c in range(0, count, step):
                total += 1
                if self._cluster_free(cluster + c):
                    free += 1
        if total == 0:
            return 0
        chance = int(free * 100 / total)
        if f.assumed_contiguous:
            # We are guessing where the data continued. Even if every cluster
            # we guessed at is free, the file may come back scrambled.
            chance = min(chance, 50)
        return chance

    # -- recovery -----------------------------------------------------------
    def read_file(self, f):
        """Return the file's bytes. Reads only - writes nothing anywhere."""
        if f.resident is not None:
            return f.resident[:f.size] if f.size else f.resident

        chunks = []
        remaining = f.size or 0
        for cluster, count in f.runs:
            if remaining <= 0:
                break
            span = count * self.cluster_size
            take = min(span, remaining) if remaining else span
            chunks.append(self.disk.read(self._cluster_offset(cluster), take))
            remaining -= take

        data = b"".join(chunks)
        return data[:f.size] if f.size else data
