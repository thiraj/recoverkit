"""
Builds real FAT32 volume images in memory, with files already deleted.

Same reasoning as ntfs_image.py and exfat_image.py: `mkfs.vfat` is not on
every machine and mounting needs root, so the structures are written directly
and read back by the real fat32.py.

What a FAT delete actually does, which is what this has to reproduce:

  * the first byte of every entry in the file's set - the long-name pieces
    and the 8.3 entry - becomes 0xE5. On the short entry that byte was the
    first character of the name, and it is gone for good.
  * every link in the file's FAT chain is set to zero, which both frees the
    clusters and destroys the record of where the file continued.
"""

import struct

ENTRY_SIZE = 32
FIRST_CLUSTER = 2
DELETED = 0xE5

ATTR_VOLUME_LABEL = 0x08
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_LONG_NAME = 0x0F

END_OF_CHAIN = 0x0FFFFFFF


def to_fat_date(when):
    return ((when.year - 1980) & 0x7F) << 9 | (when.month & 0x0F) << 5 \
        | (when.day & 0x1F)


def to_fat_time(when):
    return (when.hour & 0x1F) << 11 | (when.minute & 0x3F) << 5 \
        | (when.second // 2) & 0x1F


def short_name_bytes(name):
    """Squeeze a name into the 11-byte 8.3 field, as the format demands."""
    stem, _, extension = name.upper().partition(".")
    keep = "".join(c for c in stem if c.isalnum() or c in "_-")[:8]
    return (keep.ljust(8) + extension[:3].ljust(3)).encode("cp437", "replace")


def short_checksum(raw):
    """The checksum a long-name entry carries to tie it to its 8.3 entry."""
    total = 0
    for byte in raw:
        total = (((total & 1) << 7) + (total >> 1) + byte) & 0xFF
    return total


def build_entry_set(name, size, cluster, is_dir, deleted, created, written):
    """The long-name entries plus the 8.3 entry, in the order FAT stores them."""
    raw = short_name_bytes(name)
    checksum = short_checksum(raw)

    short = bytearray(ENTRY_SIZE)
    short[0:11] = raw
    short[0x0B] = ATTR_DIRECTORY if is_dir else ATTR_ARCHIVE
    short[0x0D] = 0
    struct.pack_into("<H", short, 0x0E, to_fat_time(created))
    struct.pack_into("<H", short, 0x10, to_fat_date(created))
    struct.pack_into("<H", short, 0x12, to_fat_date(written))
    struct.pack_into("<H", short, 0x14, (cluster >> 16) & 0xFFFF)
    struct.pack_into("<H", short, 0x16, to_fat_time(written))
    struct.pack_into("<H", short, 0x18, to_fat_date(written))
    struct.pack_into("<H", short, 0x1A, cluster & 0xFFFF)
    struct.pack_into("<I", short, 0x1C, 0 if is_dir else size)

    # Thirteen characters per entry, padded with a terminator and then 0xFFFF.
    encoded = name.encode("utf-16-le")
    pieces = [encoded[i:i + 26] for i in range(0, len(encoded), 26)] or [b""]
    longs = []
    for number, piece in enumerate(pieces, start=1):
        entry = bytearray(b"\xFF" * ENTRY_SIZE)
        entry[0] = number | (0x40 if number == len(pieces) else 0)
        entry[0x0B] = ATTR_LONG_NAME
        entry[0x0C] = 0
        entry[0x0D] = checksum
        struct.pack_into("<H", entry, 0x1A, 0)
        padded = piece + (b"\x00\x00" if len(piece) < 26 else b"")
        for at, length in ((1, 10), (14, 12), (28, 4)):
            take, padded = padded[:length], padded[length:]
            entry[at:at + len(take)] = take
        longs.append(entry)

    # Stored last piece first, so reading forward finds them in reverse.
    entries = bytearray()
    for entry in reversed(longs):
        entries += entry
    entries += short

    if deleted:
        for i in range(0, len(entries), ENTRY_SIZE):
            entries[i] = DELETED
    return bytes(entries)


class Fat32Image:
    """A FAT32 volume under construction."""

    def __init__(self, size_mb=64, sector_size=512, sectors_per_cluster=8,
                 reserved=32, fat_count=2):
        self.sector_size = sector_size
        self.sectors_per_cluster = sectors_per_cluster
        self.cluster_size = sector_size * sectors_per_cluster
        self.reserved = reserved
        self.fat_count = fat_count

        self.total_bytes = size_mb * 1024 * 1024
        self.total_sectors = self.total_bytes // sector_size

        clusters = (self.total_sectors - reserved) // sectors_per_cluster
        fat_bytes = (clusters + FIRST_CLUSTER) * 4
        self.fat_sectors = max(1, -(-fat_bytes // sector_size))
        self.data_sector = reserved + fat_count * self.fat_sectors
        self.total_clusters = ((self.total_sectors - self.data_sector)
                               // sectors_per_cluster)

        self.root_cluster = 2
        self._next = 3
        self.fat = {}
        self.content = {}
        self.dirs = {self.root_cluster: bytearray()}
        self.fat[self.root_cluster] = END_OF_CHAIN

    def allocate(self, count):
        start = self._next
        self._next += count
        assert self._next < FIRST_CLUSTER + self.total_clusters, "image too small"
        return start

    def hole(self, count=2):
        """Clusters belonging to an imaginary other file, forcing a gap."""
        start = self.allocate(count)
        for c in range(start, start + count):
            self.fat[c] = END_OF_CHAIN
        return start

    def add_dir(self, name, parent=None, deleted=False, created=None,
                modified=None):
        import datetime
        parent = self.root_cluster if parent is None else parent
        cluster = self.allocate(1)
        self.dirs[cluster] = bytearray()
        self.fat[cluster] = END_OF_CHAIN
        created = created or datetime.datetime(2024, 5, 1, 8, 0, 0)
        modified = modified or created
        self.dirs[parent] += build_entry_set(name, self.cluster_size, cluster,
                                             True, deleted, created, modified)
        return cluster

    def add_file(self, name, data, parent=None, deleted=True, fragments=1,
                 wipe_chain=True, overwritten=False, created=None,
                 modified=None):
        """
        deleted     - mark every entry in the set 0xE5 and free the clusters,
                      as a real delete does.
        fragments   - split the data, with gaps, so the FAT chain is needed.
        wipe_chain  - zero the file's FAT links, which is what a delete does
                      and what makes a fragmented file unrecoverable.
        overwritten - leave the clusters marked in use by something else.
        """
        import datetime
        parent = self.root_cluster if parent is None else parent
        created = created or datetime.datetime(2024, 6, 1, 14, 20, 0)
        modified = modified or datetime.datetime(2024, 6, 2, 15, 40, 0)

        if not data:
            self.dirs[parent] += build_entry_set(name, 0, 0, False, deleted,
                                                 created, modified)
            return []

        needed = max(1, -(-len(data) // self.cluster_size))
        per = max(1, -(-needed // fragments))
        runs, placed = [], 0
        while placed < needed:
            take = min(per, needed - placed)
            start = self.allocate(take)
            runs.append((start, take))
            placed += take
            if placed < needed:
                self.hole(2)

        offset = 0
        flat = []
        for start, count in runs:
            for i in range(count):
                self.content[start + i] = data[offset:offset + self.cluster_size]
                offset += self.cluster_size
                flat.append(start + i)

        for a, b in zip(flat, flat[1:]):
            self.fat[a] = b
        self.fat[flat[-1]] = END_OF_CHAIN

        if deleted:
            if overwritten:
                for c in flat:
                    self.fat[c] = END_OF_CHAIN     # taken by something else
            elif wipe_chain:
                for c in flat:
                    self.fat[c] = 0                # freed, and the links gone

        self.dirs[parent] += build_entry_set(name, len(data), flat[0], False,
                                             deleted, created, modified)
        return runs

    def _boot_sector(self):
        boot = bytearray(self.sector_size)
        boot[0:3] = b"\xEB\x58\x90"
        boot[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", boot, 0x0B, self.sector_size)
        boot[0x0D] = self.sectors_per_cluster
        struct.pack_into("<H", boot, 0x0E, self.reserved)
        boot[0x10] = self.fat_count
        struct.pack_into("<H", boot, 0x11, 0)          # zero: this is FAT32
        struct.pack_into("<H", boot, 0x13, 0)
        boot[0x15] = 0xF8
        struct.pack_into("<H", boot, 0x16, 0)
        struct.pack_into("<I", boot, 0x20, self.total_sectors)
        struct.pack_into("<I", boot, 0x24, self.fat_sectors)
        struct.pack_into("<I", boot, 0x2C, self.root_cluster)
        struct.pack_into("<H", boot, 0x30, 1)
        struct.pack_into("<H", boot, 0x32, 6)
        boot[0x42] = 0x29
        struct.pack_into("<I", boot, 0x43, 0x12345678)
        boot[0x47:0x52] = b"TESTCARD   "
        boot[0x52:0x5A] = b"FAT32   "
        boot[510:512] = b"\x55\xAA"
        return bytes(boot)

    def build(self):
        image = bytearray(self.total_bytes)
        image[0:self.sector_size] = self._boot_sector()

        label = bytearray(ENTRY_SIZE)
        label[0:11] = b"TESTCARD   "
        label[0x0B] = ATTR_VOLUME_LABEL
        self.dirs[self.root_cluster] = bytes(label) + \
            bytes(self.dirs[self.root_cluster])

        for cluster, entries in self.dirs.items():
            assert len(entries) <= self.cluster_size, \
                "test directory outgrew its single cluster"
            self.content[cluster] = bytes(entries).ljust(self.cluster_size,
                                                         b"\x00")

        fat = bytearray(self.fat_sectors * self.sector_size)
        struct.pack_into("<I", fat, 0, 0x0FFFFFF8)
        struct.pack_into("<I", fat, 4, 0x0FFFFFFF)
        for cluster, nxt in self.fat.items():
            if (cluster + 1) * 4 <= len(fat):
                struct.pack_into("<I", fat, cluster * 4, nxt)
        for copy in range(self.fat_count):
            base = (self.reserved + copy * self.fat_sectors) * self.sector_size
            image[base:base + len(fat)] = fat

        for cluster, data in self.content.items():
            offset = (self.data_sector * self.sector_size
                      + (cluster - FIRST_CLUSTER) * self.cluster_size)
            image[offset:offset + len(data)] = data

        return bytes(image)
