"""
Builds real exFAT volume images in memory, with files already deleted.

Same reasoning as ntfs_image.py: mkfs.exfat is not present everywhere and
mounting needs root, so the structures are written directly and parsed back by
the real exfat.py. See test_integration_exfat.py for the version that drives
the operating system's own exFAT driver.

An exFAT volume is simpler than NTFS: a boot sector, a FAT, and a heap of
clusters. Every file is an "entry set" of 32-byte directory entries - one file
entry, one stream entry, then name entries holding 15 UTF-16 characters each.
Deleting a file clears bit 7 of the type byte on each entry in the set and
releases its clusters in the allocation bitmap, which is what `deleted=True`
reproduces here.
"""

import datetime
import struct

ENTRY_SIZE = 32
FIRST_CLUSTER = 2
NAME_CHARS_PER_ENTRY = 15

ENTRY_BITMAP = 0x81
ENTRY_UPCASE = 0x82
ENTRY_LABEL = 0x83
ENTRY_FILE = 0x85
ENTRY_STREAM = 0xC0
ENTRY_NAME = 0xC1

ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20

FLAG_ALLOC_POSSIBLE = 0x01
FLAG_NO_FAT_CHAIN = 0x02

END_OF_CHAIN = 0xFFFFFFFF


def to_exfat_time(when):
    """datetime -> the packed DOS-style timestamp exFAT stores."""
    return (((when.year - 1980) & 0x7F) << 25
            | (when.month & 0x0F) << 21
            | (when.day & 0x1F) << 16
            | (when.hour & 0x1F) << 11
            | (when.minute & 0x3F) << 5
            | (when.second // 2) & 0x1F)


def set_checksum(entries):
    """The entry-set checksum, computed with the in-use bits still set."""
    checksum = 0
    for index, byte in enumerate(entries):
        if index in (2, 3):
            continue
        checksum = (((checksum << 15) | (checksum >> 1)) + byte) & 0xFFFF
    return checksum


def name_hash(name):
    """The up-cased name hash the stream entry carries. ASCII names only."""
    hashed = 0
    for char in name.upper():
        code = ord(char)
        for byte in (code & 0xFF, (code >> 8) & 0xFF):
            hashed = (((hashed << 15) | (hashed >> 1)) + byte) & 0xFFFF
    return hashed


def build_entry_set(name, size, first_cluster, contiguous, is_dir, deleted,
                    created, modified):
    """Assemble one file's directory entries, deleted or not."""
    encoded = name.encode("utf-16-le")
    name_entries = -(-len(name) // NAME_CHARS_PER_ENTRY) or 1
    secondary = 1 + name_entries

    head = bytearray(ENTRY_SIZE)
    head[0] = ENTRY_FILE
    head[1] = secondary
    struct.pack_into("<H", head, 4,
                     ATTR_DIRECTORY if is_dir else ATTR_ARCHIVE)
    struct.pack_into("<I", head, 8, to_exfat_time(created))
    struct.pack_into("<I", head, 12, to_exfat_time(modified))
    struct.pack_into("<I", head, 16, to_exfat_time(modified))

    stream = bytearray(ENTRY_SIZE)
    stream[0] = ENTRY_STREAM
    flags = 0
    if first_cluster:
        flags |= FLAG_ALLOC_POSSIBLE
        if contiguous:
            flags |= FLAG_NO_FAT_CHAIN
    stream[1] = flags
    stream[3] = len(name)
    struct.pack_into("<H", stream, 4, name_hash(name))
    struct.pack_into("<Q", stream, 8, size)          # valid data length
    struct.pack_into("<I", stream, 20, first_cluster)
    struct.pack_into("<Q", stream, 24, size)         # data length

    entries = bytearray(head + stream)
    for i in range(name_entries):
        chunk = bytearray(ENTRY_SIZE)
        chunk[0] = ENTRY_NAME
        piece = encoded[i * NAME_CHARS_PER_ENTRY * 2:
                        (i + 1) * NAME_CHARS_PER_ENTRY * 2]
        chunk[2:2 + len(piece)] = piece
        entries += chunk

    struct.pack_into("<H", entries, 2, set_checksum(entries))

    if deleted:
        # Deletion is exactly this: clear the in-use bit on every entry in the
        # set. Nothing else about the file is touched.
        for i in range(0, len(entries), ENTRY_SIZE):
            entries[i] &= 0x7F

    return bytes(entries)


class ExfatImage:
    """
    An exFAT volume under construction.

    Cluster 2 holds the allocation bitmap, cluster 3 a stub up-case table,
    cluster 4 the root directory; everything after that is handed out in
    order. `hole()` skips clusters so the next file lands elsewhere, which is
    how the fragmented cases are built.
    """

    def __init__(self, size_mb=8, sector_size=512, sectors_per_cluster=8):
        self.sector_size = sector_size
        self.sectors_per_cluster = sectors_per_cluster
        self.cluster_size = sector_size * sectors_per_cluster

        self.total_bytes = size_mb * 1024 * 1024
        self.total_sectors = self.total_bytes // sector_size

        self.fat_sector = 128
        self.heap_sector = 256
        self.heap_offset = self.heap_sector * sector_size
        self.total_clusters = (self.total_sectors - self.heap_sector) \
            // sectors_per_cluster

        fat_bytes = (self.total_clusters + FIRST_CLUSTER) * 4
        self.fat_sectors = max(1, -(-fat_bytes // sector_size))
        assert self.fat_sector + self.fat_sectors <= self.heap_sector

        self.bitmap_cluster = 2
        self.upcase_cluster = 3
        self.root_cluster = 4
        self._next_cluster = 5

        self.allocated = {2, 3, 4}
        self.fat = {}                      # cluster -> next cluster
        self.content = {}                  # cluster -> bytes
        self.dirs = {self.root_cluster: bytearray()}

    # -- allocation ---------------------------------------------------------
    def allocate(self, clusters):
        start = self._next_cluster
        self._next_cluster += clusters
        assert self._next_cluster < FIRST_CLUSTER + self.total_clusters, \
            "image too small"
        return start

    def hole(self, clusters=2):
        """Reserve clusters for an imaginary other file, forcing a gap."""
        start = self.allocate(clusters)
        for c in range(start, start + clusters):
            self.allocated.add(c)
        return start

    def _store(self, runs, data):
        offset = 0
        for start, count in runs:
            for i in range(count):
                span = self.cluster_size
                self.content[start + i] = data[offset:offset + span]
                offset += span

    # -- content ------------------------------------------------------------
    def add_dir(self, name, parent=None, deleted=False,
                created=None, modified=None):
        parent = self.root_cluster if parent is None else parent
        cluster = self.allocate(1)
        self.dirs[cluster] = bytearray()
        if not deleted:
            self.allocated.add(cluster)
        self.fat[cluster] = END_OF_CHAIN

        created = created or datetime.datetime(2024, 5, 1, 8, 0, 0)
        modified = modified or datetime.datetime(2024, 5, 1, 8, 0, 0)
        self.dirs[parent] += build_entry_set(
            name, self.cluster_size, cluster, True, True, deleted,
            created, modified)
        return cluster

    def add_file(self, name, data, parent=None, deleted=True, fragments=1,
                 wipe_chain=True, overwritten=False, created=None,
                 modified=None, reuse_runs=None):
        """
        Place a file on the volume.

        deleted     - clear the in-use bits and release the clusters, as a
                      real delete does.
        fragments   - split the data into this many runs with gaps between
                      them, so the file needs the FAT to be read back.
        wipe_chain  - when a fragmented file is deleted, also zero its FAT
                      entries. This is what real exFAT drivers do, and it is
                      the case where recovery has to guess.
        overwritten - leave the clusters marked in use, as if another file
                      took the space. The recovery chance should drop.
        reuse_runs  - point this entry at clusters another file already owns,
                      writing no data and touching no bitmap bits. This is
                      what a move to the Trash looks like: two directory
                      entries, one live and one marked deleted, describing
                      the same bytes.
        """
        parent = self.root_cluster if parent is None else parent
        created = created or datetime.datetime(2024, 6, 1, 14, 20, 0)
        modified = modified or datetime.datetime(2024, 6, 2, 15, 40, 0)

        if not data:
            self.dirs[parent] += build_entry_set(
                name, 0, 0, True, False, deleted, created, modified)
            return []

        if reuse_runs is not None:
            flat = [c for start, count in reuse_runs
                    for c in range(start, start + count)]
            self.dirs[parent] += build_entry_set(
                name, len(data), flat[0], len(reuse_runs) == 1, False,
                deleted, created, modified)
            return reuse_runs

        needed = max(1, -(-len(data) // self.cluster_size))
        per_fragment = max(1, -(-needed // fragments))
        runs = []
        placed = 0
        while placed < needed:
            chunk = min(per_fragment, needed - placed)
            start = self.allocate(chunk)
            runs.append((start, chunk))
            placed += chunk
            if placed < needed:
                self.hole(2)

        contiguous = len(runs) == 1
        self._store(runs, data)

        # Chain the clusters together in the FAT, in order.
        flat = [c for start, count in runs for c in range(start, start + count)]
        for a, b in zip(flat, flat[1:]):
            self.fat[a] = b
        self.fat[flat[-1]] = END_OF_CHAIN

        if deleted:
            if not overwritten:
                for cluster in flat:
                    self.allocated.discard(cluster)
            else:
                for cluster in flat:
                    self.allocated.add(cluster)
            if not contiguous and wipe_chain:
                for cluster in flat:
                    self.fat[cluster] = 0
        else:
            for cluster in flat:
                self.allocated.add(cluster)

        self.dirs[parent] += build_entry_set(
            name, len(data), flat[0], contiguous, False, deleted,
            created, modified)
        return runs

    # -- assembly -----------------------------------------------------------
    def _boot_sector(self):
        boot = bytearray(self.sector_size)
        boot[0:3] = b"\xEB\x76\x90"
        boot[3:11] = b"EXFAT   "
        struct.pack_into("<Q", boot, 0x40, 0)                   # partition off
        struct.pack_into("<Q", boot, 0x48, self.total_sectors)
        struct.pack_into("<I", boot, 0x50, self.fat_sector)
        struct.pack_into("<I", boot, 0x54, self.fat_sectors)
        struct.pack_into("<I", boot, 0x58, self.heap_sector)
        struct.pack_into("<I", boot, 0x5C, self.total_clusters)
        struct.pack_into("<I", boot, 0x60, self.root_cluster)
        struct.pack_into("<I", boot, 0x64, 0xDEADBEEF)          # serial
        struct.pack_into("<H", boot, 0x68, 0x0100)              # revision 1.00
        struct.pack_into("<H", boot, 0x6A, 0)                   # flags
        boot[0x6C] = self.sector_size.bit_length() - 1
        boot[0x6D] = self.sectors_per_cluster.bit_length() - 1
        boot[0x6E] = 1                                          # one FAT
        boot[0x6F] = 0x80
        boot[0x70] = 50                                         # percent used
        boot[510:512] = b"\x55\xAA"
        return bytes(boot)

    def build(self):
        image = bytearray(self.total_bytes)
        image[0:self.sector_size] = self._boot_sector()

        bitmap_bytes = -(-self.total_clusters // 8)

        # The allocation bitmap and the up-case stub are themselves files, and
        # the root directory names them the same way it names anything else.
        root = bytearray()
        label = bytearray(ENTRY_SIZE)
        label[0] = ENTRY_LABEL
        label[1] = 7
        label[2:2 + 14] = "TESTVOL".encode("utf-16-le")
        root += label

        bitmap_entry = bytearray(ENTRY_SIZE)
        bitmap_entry[0] = ENTRY_BITMAP
        struct.pack_into("<I", bitmap_entry, 20, self.bitmap_cluster)
        struct.pack_into("<Q", bitmap_entry, 24, bitmap_bytes)
        root += bitmap_entry

        upcase_entry = bytearray(ENTRY_SIZE)
        upcase_entry[0] = ENTRY_UPCASE
        struct.pack_into("<I", upcase_entry, 20, self.upcase_cluster)
        struct.pack_into("<Q", upcase_entry, 24, 128)
        root += upcase_entry

        root += self.dirs[self.root_cluster]
        self.dirs[self.root_cluster] = root

        # Directory clusters.
        for cluster, entries in self.dirs.items():
            assert len(entries) <= self.cluster_size, \
                "test directory outgrew its single cluster"
            padded = bytes(entries) + b"\x00" * (
                self.cluster_size - len(entries) % self.cluster_size)
            self.content[cluster] = padded[:self.cluster_size]

        # Allocation bitmap: one bit per cluster, set means in use.
        bitmap = bytearray(bitmap_bytes)
        for cluster in self.allocated:
            index = cluster - FIRST_CLUSTER
            if 0 <= index < self.total_clusters:
                bitmap[index >> 3] |= 1 << (index & 7)
        self.content[self.bitmap_cluster] = bytes(bitmap)

        # The FAT. Entries 0 and 1 are reserved by the format.
        fat = bytearray(self.fat_sectors * self.sector_size)
        struct.pack_into("<I", fat, 0, 0xFFFFFFF8)
        struct.pack_into("<I", fat, 4, 0xFFFFFFFF)
        for cluster, nxt in self.fat.items():
            if (cluster + 1) * 4 <= len(fat):
                struct.pack_into("<I", fat, cluster * 4, nxt)
        # Directories and metadata are one cluster each, ending their chain.
        for cluster in (self.bitmap_cluster, self.upcase_cluster,
                        self.root_cluster):
            struct.pack_into("<I", fat, cluster * 4, END_OF_CHAIN)
        base = self.fat_sector * self.sector_size
        image[base:base + len(fat)] = fat

        # Cluster contents.
        for cluster, data in self.content.items():
            offset = self.heap_offset + (cluster - FIRST_CLUSTER) * self.cluster_size
            image[offset:offset + len(data)] = data

        return bytes(image)
