"""
fat32.py - reads FAT32 directory entries to find deleted files.

WHY THIS ONE MATTERS MOST
-------------------------
FAT32 is what almost every SD card of 32GB or less is formatted with, and what
a great many cameras write regardless of card size. "I deleted the photos off
my camera card" is the most common thing anyone needs a recovery tool for, and
without this that person got a deep scan: thousands of files with no names.

WHAT SURVIVES A DELETE, AND WHAT DOES NOT
-----------------------------------------
FAT is older and cruder than NTFS, and it loses more:

  * **The first letter of the name is destroyed.** Deleting a file overwrites
    the first byte of its directory entry with 0xE5 to mark the slot free.
    That byte *was* the first character of the 8.3 name. It is gone, and no
    tool can invent it - anyone who shows you a complete short name for a
    deleted FAT file is guessing at it.

  * **Long names usually survive.** A file called `IMG_0042.JPG` is stored
    twice: as a cramped 8.3 name and as a chain of long-name entries holding
    the real one. Those entries lose their first byte too, but that byte is a
    sequence number, not a character - the name itself is intact, and it is
    what we show. So the first letter is only really lost for files that
    never had a long name.

  * **The cluster chain is wiped.** FAT keeps each file's layout as a linked
    list inside the FAT itself, and a delete zeroes every link. Only the
    starting cluster, held in the directory entry, survives. A file stored in
    one piece therefore comes back exactly; a fragmented one is guesswork
    after its first fragment, and we say so rather than pretending.

WHAT WE CHECK
-------------
FAT has no separate allocation bitmap - the FAT *is* the record of what is
free. A zero entry means the cluster is unused. We compare a deleted file's
clusters against it exactly as the other engines do, so the recovery-chance
figure rests on evidence.

Everything here is read-only. This module never writes to the source device.
"""

import datetime
import struct

import signatures

ATTR_READ_ONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_VOLUME_LABEL = 0x08
ATTR_DIRECTORY = 0x10
ATTR_LONG_NAME = 0x0F        # the marker for a long-name fragment

DELETED = 0xE5               # what a freed directory slot starts with
END_OF_DIRECTORY = 0x00
FIRST_CLUSTER = 2
ENTRY_SIZE = 32

# A cluster number at or above this ends the chain.
END_OF_CHAIN = 0x0FFFFFF8
BAD_CLUSTER = 0x0FFFFFF7
CLUSTER_MASK = 0x0FFFFFFF

# Where a long-name entry keeps its thirteen characters.
NAME_PARTS = ((1, 10), (14, 12), (28, 4))

MAX_CHAIN = 1 << 22
MAX_DIR_DEPTH = 64


def _fat_time(date, time_of_day, tenths=0):
    """
    Convert a packed DOS date and time to a datetime.

    Two-second resolution, year counted from 1980, and a separate tenths byte
    for creation times because two seconds was not quite enough even in 1980.
    """
    if not date:
        return None
    day = date & 0x1F
    month = (date >> 5) & 0x0F
    year = ((date >> 9) & 0x7F) + 1980
    second = (time_of_day & 0x1F) * 2
    minute = (time_of_day >> 5) & 0x3F
    hour = (time_of_day >> 11) & 0x1F
    try:
        value = datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None                  # a reused slot can hold anything
    if tenths:
        try:
            value += datetime.timedelta(milliseconds=min(tenths, 199) * 10)
        except (OverflowError, ValueError):
            pass
    return value


def _short_name(entry, first_letter="_"):
    """
    Rebuild an 8.3 name from a directory entry.

    `first_letter` is a stand-in: the real one was overwritten by the marker
    that says the slot is free. It is not recoverable and is not guessed at.
    """
    raw = bytes(entry[:11])
    if raw[0] in (DELETED, END_OF_DIRECTORY):
        raw = first_letter.encode("ascii", "replace")[:1] + raw[1:]
    elif raw[0] == 0x05:
        raw = b"\xE5" + raw[1:]      # a real leading 0xE5, escaped on disk
    stem = raw[:8].decode("cp437", "replace").rstrip()
    extension = raw[8:11].decode("cp437", "replace").rstrip()
    return f"{stem}.{extension}" if extension else stem


def _long_name_part(entry):
    """The thirteen characters one long-name entry carries."""
    chunk = b"".join(bytes(entry[at:at + length]) for at, length in NAME_PARTS)
    text = chunk.decode("utf-16-le", "replace")
    end = text.find("\x00")
    return text[:end] if end >= 0 else text


class DeletedFile:
    """
    One recoverable file. The same shape as the other engines' results, so
    the window and the service can treat every filesystem alike.

    `assumed_contiguous` means the cluster chain was gone and the layout is a
    guess. `first_letter_lost` means no long name survived, so the name shown
    starts with a stand-in character.
    """

    __slots__ = ("name", "path", "size", "deleted_at", "created_at",
                 "is_dir", "runs", "resident", "chance", "record_no",
                 "assumed_contiguous", "first_letter_lost", "content_check",
                 "still_at", "_in_use")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def extension(self):
        return self.name.rsplit(".", 1)[-1].lower() if "." in self.name else ""


class Fat32Volume:
    """Parses a FAT32 volume from a ReadOnlyDisk. Read-only throughout."""

    def __init__(self, disk):
        self.disk = disk
        boot = disk.read(0, 512)
        if len(boot) < 512 or boot[510:512] != b"\x55\xAA":
            raise ValueError("This volume is not FAT32.")

        self.sector_size = struct.unpack_from("<H", boot, 0x0B)[0]
        sectors_per_cluster = boot[0x0D]
        reserved = struct.unpack_from("<H", boot, 0x0E)[0]
        fat_count = boot[0x10]
        root_entries = struct.unpack_from("<H", boot, 0x11)[0]
        fat_sectors = struct.unpack_from("<I", boot, 0x24)[0]

        if (self.sector_size not in (512, 1024, 2048, 4096)
                or sectors_per_cluster == 0
                or sectors_per_cluster & (sectors_per_cluster - 1)
                or not reserved or not fat_count or not fat_sectors
                or root_entries != 0):
            # root_entries is zero only on FAT32; FAT12 and FAT16 put a
            # fixed-size root directory here and are a different format.
            raise ValueError("This volume is not FAT32.")

        self.cluster_size = self.sector_size * sectors_per_cluster
        self.fat_offset = reserved * self.sector_size
        self.fat_length = fat_sectors * self.sector_size
        self.data_offset = ((reserved + fat_count * fat_sectors)
                            * self.sector_size)
        self.root_cluster = struct.unpack_from("<I", boot, 0x2C)[0]

        total_sectors = (struct.unpack_from("<I", boot, 0x20)[0]
                         or struct.unpack_from("<H", boot, 0x13)[0])
        data_sectors = max(0, total_sectors - reserved
                           - fat_count * fat_sectors)
        self.total_clusters = data_sectors // sectors_per_cluster

        if self.root_cluster < FIRST_CLUSTER or not self.total_clusters:
            raise ValueError("This FAT32 volume has an unreadable layout.")

        disk.sector_size = self.sector_size
        self._fat_cache = {}
        self._fat_block = max(self.sector_size, 64 * 1024)

    # -- cluster arithmetic -------------------------------------------------
    def _cluster_offset(self, cluster):
        return self.data_offset + (cluster - FIRST_CLUSTER) * self.cluster_size

    def _valid_cluster(self, cluster):
        return FIRST_CLUSTER <= cluster < FIRST_CLUSTER + self.total_clusters

    # -- the FAT ------------------------------------------------------------
    def _fat_entry(self, cluster):
        """The FAT's 28-bit entry for a cluster, or None if unreadable."""
        position = cluster * 4
        if position + 4 > self.fat_length:
            return None
        block_no = position // self._fat_block
        block = self._fat_cache.get(block_no)
        if block is None:
            block = self.disk.read(self.fat_offset + block_no * self._fat_block,
                                   self._fat_block)
            if len(self._fat_cache) > 64:
                self._fat_cache.clear()
            self._fat_cache[block_no] = block
        inside = position % self._fat_block
        if inside + 4 > len(block):
            return None
        return struct.unpack_from("<I", block, inside)[0] & CLUSTER_MASK

    def _cluster_free(self, cluster):
        """
        FAT has no separate bitmap - a zero entry *is* the record that the
        cluster is unused. Unknown counts as free rather than as overwritten.
        """
        entry = self._fat_entry(cluster)
        return True if entry is None else entry == 0

    def _follow_chain(self, first, max_clusters=None):
        """Walk a chain, merging consecutive clusters so reads stay large."""
        runs = []
        cluster = first
        seen = set()
        while self._valid_cluster(cluster):
            if cluster in seen or len(seen) > MAX_CHAIN:
                break
            seen.add(cluster)
            if runs and runs[-1][0] + runs[-1][1] == cluster:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((cluster, 1))
            if max_clusters and len(seen) >= max_clusters:
                break
            nxt = self._fat_entry(cluster)
            if nxt is None or nxt == 0 or nxt >= END_OF_CHAIN \
                    or nxt == BAD_CLUSTER or nxt == cluster:
                break
            cluster = nxt
        return runs

    def _contiguous_runs(self, first, count):
        if count <= 0 or not self._valid_cluster(first):
            return []
        last = FIRST_CLUSTER + self.total_clusters
        return [(first, min(count, last - first))]

    # -- directories --------------------------------------------------------
    def _read_directory(self, runs):
        """
        Every 32-byte slot in a directory, with its absolute offset.

        Read right to the end rather than stopping at the first end-of-
        directory marker: the slots past it hold the entries of files deleted
        earlier, which is the whole point of being here.
        """
        entries = []
        for cluster, count in runs:
            base = self._cluster_offset(cluster)
            data = self.disk.read(base, count * self.cluster_size)
            for pos in range(0, len(data) - ENTRY_SIZE + 1, ENTRY_SIZE):
                entries.append((base + pos, data[pos:pos + ENTRY_SIZE]))
        return entries

    def _name_before(self, entries, index):
        """
        Reassemble the long name from the entries in front of a short one.

        Long-name fragments are stored backwards - last piece first - so
        walking backwards from the short entry reads them in order. Deleting
        the file overwrote each fragment's sequence number, but not the
        characters, so the name itself is still all there.
        """
        parts = []
        at = index - 1
        while at >= 0 and len(parts) < 20:
            offset, entry = entries[at]
            if entry[0x0B] != ATTR_LONG_NAME:
                break
            if offset != entries[index][0] - (index - at) * ENTRY_SIZE:
                break                        # a gap: not part of this set
            parts.append(_long_name_part(entry))
            at -= 1
        return "".join(parts)

    def _parse(self, entries, index):
        """One directory slot, as a file. Returns None for anything else."""
        offset, entry = entries[index]
        marker = entry[0]
        if marker == END_OF_DIRECTORY:
            return None
        attributes = entry[0x0B]
        if attributes == ATTR_LONG_NAME or attributes & ATTR_VOLUME_LABEL:
            return None

        short = _short_name(entry)
        if short in (".", "..", "_", "_."):
            return None                      # the self and parent links

        long_name = self._name_before(entries, index)
        name = long_name or short
        if not name or "\x00" in name:
            return None

        size = struct.unpack_from("<I", entry, 0x1C)[0]
        cluster = (struct.unpack_from("<H", entry, 0x14)[0] << 16
                   | struct.unpack_from("<H", entry, 0x1A)[0])
        in_use = marker != DELETED
        is_dir = bool(attributes & ATTR_DIRECTORY)

        created = _fat_time(struct.unpack_from("<H", entry, 0x10)[0],
                            struct.unpack_from("<H", entry, 0x0E)[0],
                            entry[0x0D])
        # FAT has no delete timestamp; last-write is the closest thing, and
        # it is what the other engines report in that column too.
        written = _fat_time(struct.unpack_from("<H", entry, 0x18)[0],
                            struct.unpack_from("<H", entry, 0x16)[0])

        needed = ((size + self.cluster_size - 1) // self.cluster_size
                  if size else 0)
        runs = []
        assumed = False
        if self._valid_cluster(cluster) and (needed or is_dir):
            wanted = needed or 1
            if in_use:
                runs = self._follow_chain(cluster, wanted)
            else:
                # Deleting a file zeroes its chain. Follow whatever is left,
                # then assume the rest ran on contiguously - and record that
                # the tail is a guess.
                chain = self._follow_chain(cluster, wanted)
                if sum(count for _, count in chain) >= wanted:
                    runs = chain
                else:
                    runs = self._contiguous_runs(cluster, wanted)
                    # A file that fits in one cluster has no continuation to
                    # guess at - the starting cluster is the whole of it.
                    # Only a longer file is really being assumed about.
                    assumed = wanted > 1

        found = DeletedFile(
            name=name, path=None, size=size, deleted_at=written,
            created_at=created, is_dir=is_dir, runs=runs, resident=None,
            chance=None, record_no=offset, assumed_contiguous=assumed,
            first_letter_lost=not in_use and not long_name,
            content_check=signatures.UNKNOWN, still_at=None)
        found._in_use = in_use
        return found

    # -- public scan --------------------------------------------------------
    def scan(self, progress=None, should_stop=None, include_dirs=False):
        """Walk every directory and return a list of DeletedFile."""
        root = self._read_directory(self._follow_chain(self.root_cluster))
        if not root:
            raise ValueError("Could not read the root folder - the volume may "
                             "be damaged or not FAT32.")

        deleted = []
        live_starts = {}
        pending = [(root, "")]
        visited = {self.root_cluster}
        done = 0

        while pending:
            if should_stop and should_stop():
                break
            entries, path = pending.pop(0)

            for index in range(len(entries)):
                if should_stop and should_stop():
                    break
                found = self._parse(entries, index)
                if found is None:
                    continue
                found.path = path or "\\"

                if found.is_dir:
                    child = found.runs[0][0] if found.runs else 0
                    if (child and child not in visited
                            and path.count("\\") < MAX_DIR_DEPTH):
                        visited.add(child)
                        pending.append((self._read_directory(found.runs),
                                        (path + "\\" + found.name) if path
                                        else found.name))
                    if not include_dirs:
                        continue

                if found._in_use:
                    if found.runs:
                        live_starts.setdefault(
                            found.runs[0][0],
                            f"{found.path}\\{found.name}".replace("\\\\", "\\"))
                    continue          # still a live file - leave it well alone

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

    def _estimate_chance(self, f):
        """Percentage of the file's clusters the FAT still calls unused."""
        if f.size == 0:
            return 100
        if not f.runs:
            return 0
        total = free = 0
        for cluster, count in f.runs:
            step = max(1, count // 64)
            for c in range(0, count, step):
                total += 1
                if self._cluster_free(cluster + c):
                    free += 1
        if total == 0:
            return 0
        chance = int(free * 100 / total)
        if f.assumed_contiguous:
            # Guessing where the data continued. Even if every cluster we
            # guessed at is free, the file may come back scrambled.
            chance = min(chance, 50)
        return chance

    def _check_content(self, f):
        """See signatures.py - the FAT alone cannot tell whose bytes those are."""
        if f.is_dir or not f.size or not f.runs:
            return
        if not signatures.known_extension(f.extension):
            return
        try:
            head = self.disk.read(self._cluster_offset(f.runs[0][0]),
                                  signatures.PROBE_BYTES)
        except Exception:
            return
        f.content_check = signatures.check(f.extension, head)
        if f.content_check in (signatures.MISMATCH, signatures.BLANK):
            f.chance = 0

    def _reconcile(self, deleted, live_starts):
        """See exfat._reconcile - the reasoning is identical."""
        for f in deleted:
            if not f.runs or f.content_check != signatures.MATCH:
                continue
            start = f.runs[0][0]
            if start in live_starts:
                f.content_check = signatures.MOVED
                f.still_at = live_starts[start]
                f.chance = 100
            elif f.chance is not None and f.chance < 40:
                f.content_check = signatures.IN_USE
                f.chance = 50

    # -- recovery -----------------------------------------------------------
    def read_file(self, f):
        """Return the file's bytes. Reads only - writes nothing anywhere."""
        chunks = []
        remaining = f.size or 0
        for cluster, count in f.runs:
            if remaining <= 0:
                break
            take = min(count * self.cluster_size, remaining)
            chunks.append(self.disk.read(self._cluster_offset(cluster), take))
            remaining -= take
        data = b"".join(chunks)
        return data[:f.size] if f.size else data
