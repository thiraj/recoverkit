"""
Builds real NTFS volume images in memory, with files already deleted.

WHY BUILD ONE INSTEAD OF USING mkntfs
-------------------------------------
The procedure in CLAUDE.md needs mkntfs and ntfs-3g, which means Linux, and
mounting a filesystem, which means root. That test exists too (see
test_integration_ntfs.py) and is the gold standard. But a recovery tool whose
tests only run on one OS, as root, is a tool whose parser silently rots.

So this module writes the on-disk structures directly: a boot sector, a Master
File Table with real records, data runs pointing at real clusters, and a
$Bitmap. The result is parsed by the same ntfs.py that reads a physical drive
- nothing is stubbed or mocked. That lets the suite check the two assertions
that matter on any machine, and lets us build cases a real filesystem will not
produce on demand: a badly fragmented file, a file whose clusters have since
been handed to something else, a resident file living inside its own record.

The images are minimal but valid where it counts. They are not a complete
NTFS implementation - there is no $LogFile, no index allocation, no $UpCase -
because ntfs.py does not read those.
"""

import datetime
import struct

ATTR_STANDARD_INFORMATION = 0x10
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80

FLAG_IN_USE = 0x0001
FLAG_DIRECTORY = 0x0002

NAMESPACE_WIN32 = 1

ROOT_RECORD = 5
FIRST_USER_RECORD = 16

EPOCH_1601 = datetime.datetime(1601, 1, 1)


def to_filetime(when):
    """Python datetime -> Windows FILETIME (100ns ticks since 1601)."""
    delta = when - EPOCH_1601
    ticks = (delta.days * 86400 + delta.seconds) * 10_000_000
    return ticks + delta.microseconds * 10


def _bytes_unsigned(value):
    return max(1, (value.bit_length() + 7) // 8)


def _bytes_signed(value):
    for size in range(1, 9):
        low = -(1 << (size * 8 - 1))
        high = (1 << (size * 8 - 1)) - 1
        if low <= value <= high:
            return size
    return 8


def encode_runs(runs):
    """
    Encode [(lcn, count), ...] as an NTFS data-run list.

    Each run is a header byte packing the width of the two fields that follow,
    then the cluster count, then the *difference* from the previous run's
    start. lcn None writes a sparse run (offset width zero).
    """
    out = bytearray()
    previous = 0
    for lcn, count in runs:
        count_width = _bytes_unsigned(count)
        if lcn is None:
            out.append(count_width)
            out += count.to_bytes(count_width, "little")
            continue
        delta = lcn - previous
        offset_width = _bytes_signed(delta)
        out.append((offset_width << 4) | count_width)
        out += count.to_bytes(count_width, "little")
        out += delta.to_bytes(offset_width, "little", signed=True)
        previous = lcn
    out.append(0)          # end of run list
    return bytes(out)


def _pad8(data):
    return data + b"\x00" * (-len(data) % 8)


def _resident_attribute(atype, content):
    header = bytearray(0x18)
    struct.pack_into("<I", header, 0x00, atype)
    header[0x08] = 0                                  # resident
    header[0x09] = 0                                  # unnamed
    struct.pack_into("<H", header, 0x0A, 0x18)
    struct.pack_into("<I", header, 0x10, len(content))
    struct.pack_into("<H", header, 0x14, 0x18)
    body = bytearray(_pad8(bytes(header) + content))
    struct.pack_into("<I", body, 0x04, len(body))
    return bytes(body)


def _non_resident_attribute(atype, runs, real_size, cluster_size):
    run_list = encode_runs(runs)
    clusters = sum(count for _, count in runs)
    header = bytearray(0x40)
    struct.pack_into("<I", header, 0x00, atype)
    header[0x08] = 1                                  # non-resident
    header[0x09] = 0
    struct.pack_into("<H", header, 0x0A, 0x40)
    struct.pack_into("<Q", header, 0x10, 0)           # first VCN
    struct.pack_into("<Q", header, 0x18, max(0, clusters - 1))
    struct.pack_into("<H", header, 0x20, 0x40)        # run list offset
    struct.pack_into("<Q", header, 0x28, clusters * cluster_size)
    struct.pack_into("<Q", header, 0x30, real_size)   # real size
    struct.pack_into("<Q", header, 0x38, real_size)   # initialised size
    body = bytearray(_pad8(bytes(header) + run_list))
    struct.pack_into("<I", body, 0x04, len(body))
    return bytes(body)


def _standard_information(created, modified):
    content = bytearray(48)
    struct.pack_into("<Q", content, 0x00, to_filetime(created))
    struct.pack_into("<Q", content, 0x08, to_filetime(modified))
    struct.pack_into("<Q", content, 0x10, to_filetime(modified))
    struct.pack_into("<Q", content, 0x18, to_filetime(modified))
    return _resident_attribute(ATTR_STANDARD_INFORMATION, bytes(content))


def _file_name(name, parent, size, created, modified, is_dir):
    encoded = name.encode("utf-16-le")
    content = bytearray(0x42 + len(encoded))
    # The parent reference is 48 bits of record number plus 16 bits of
    # sequence number; ntfs.py masks the sequence off to rebuild folder paths.
    struct.pack_into("<Q", content, 0x00, parent | (1 << 48))
    struct.pack_into("<Q", content, 0x08, to_filetime(created))
    struct.pack_into("<Q", content, 0x10, to_filetime(modified))
    struct.pack_into("<Q", content, 0x18, to_filetime(modified))
    struct.pack_into("<Q", content, 0x20, to_filetime(modified))
    struct.pack_into("<Q", content, 0x28, size)
    struct.pack_into("<Q", content, 0x30, size)
    struct.pack_into("<I", content, 0x38, 0x10 if is_dir else 0x80)
    content[0x40] = len(name)
    content[0x41] = NAMESPACE_WIN32
    content[0x42:] = encoded
    return _resident_attribute(ATTR_FILE_NAME, bytes(content))


class _Entry:
    def __init__(self, record_no, name, parent, is_dir, deleted,
                 data=b"", runs=None, resident=None, created=None,
                 modified=None):
        self.record_no = record_no
        self.name = name
        self.parent = parent
        self.is_dir = is_dir
        self.deleted = deleted
        self.data = data
        self.runs = runs or []
        self.resident = resident
        self.created = created
        self.modified = modified


class NtfsImage:
    """
    An NTFS volume under construction.

    Clusters are handed out in order from `data_start`. `hole()` skips a few
    so the next file lands somewhere else, which is how the fragmented-file
    cases are built.
    """

    def __init__(self, size_mb=8, sector_size=512, sectors_per_cluster=8,
                 record_size=1024, record_count=64):
        self.sector_size = sector_size
        self.sectors_per_cluster = sectors_per_cluster
        self.cluster_size = sector_size * sectors_per_cluster
        self.record_size = record_size
        self.record_count = record_count

        self.total_bytes = size_mb * 1024 * 1024
        self.total_sectors = self.total_bytes // sector_size
        self.total_clusters = self.total_bytes // self.cluster_size

        self.mft_cluster = 32
        mft_bytes = record_count * record_size
        self.mft_clusters = max(1, -(-mft_bytes // self.cluster_size))

        self.bitmap_cluster = self.mft_cluster + self.mft_clusters
        bitmap_bytes = -(-self.total_clusters // 8)
        self.bitmap_clusters = max(1, -(-bitmap_bytes // self.cluster_size))

        self.data_start = self.bitmap_cluster + self.bitmap_clusters + 8
        self._next_cluster = self.data_start
        self._next_record = FIRST_USER_RECORD

        self.entries = {}
        self.allocated = set()

        # Metadata the filesystem itself owns is always in use.
        self._mark_used(0, self.data_start)

        self._add_root()

    # -- allocation ---------------------------------------------------------
    def _mark_used(self, start, count):
        for c in range(start, start + count):
            self.allocated.add(c)

    def _mark_free(self, start, count):
        for c in range(start, start + count):
            self.allocated.discard(c)

    def allocate(self, clusters):
        start = self._next_cluster
        self._next_cluster += clusters
        assert self._next_cluster < self.total_clusters, "image too small"
        return start

    def hole(self, clusters=2):
        """Reserve clusters for an imaginary other file, forcing a gap."""
        start = self.allocate(clusters)
        self._mark_used(start, clusters)
        return start

    # -- content ------------------------------------------------------------
    def _add_root(self):
        self.entries[ROOT_RECORD] = _Entry(
            ROOT_RECORD, ".", ROOT_RECORD, True, False,
            created=datetime.datetime(2024, 1, 1, 9, 0, 0),
            modified=datetime.datetime(2024, 1, 1, 9, 0, 0))

    def add_dir(self, name, parent=ROOT_RECORD):
        number = self._next_record
        self._next_record += 1
        self.entries[number] = _Entry(
            number, name, parent, True, False,
            created=datetime.datetime(2024, 2, 1, 10, 0, 0),
            modified=datetime.datetime(2024, 2, 1, 10, 0, 0))
        return number

    def add_file(self, name, data, parent=ROOT_RECORD, deleted=True,
                 resident=False, fragments=1, overwritten=False,
                 created=None, modified=None):
        """
        Place a file on the volume.

        deleted     - clear the in-use flag, as Windows does on delete, and
                      release its clusters in $Bitmap.
        resident    - store the content inside the MFT record (tiny files).
        fragments   - split the data into this many separate runs, with a gap
                      between each so the recovery has to stitch them back.
        overwritten - leave the clusters marked in use, as if another file
                      took the space. The recovery chance should drop.
        """
        number = self._next_record
        self._next_record += 1

        runs = []
        stored_resident = None

        if resident:
            assert len(data) <= 600, "resident data must fit in the record"
            stored_resident = data
        elif data:
            total_clusters = max(1, -(-len(data) // self.cluster_size))
            per_fragment = max(1, -(-total_clusters // fragments))
            placed = 0
            while placed < total_clusters:
                chunk = min(per_fragment, total_clusters - placed)
                start = self.allocate(chunk)
                runs.append((start, chunk))
                placed += chunk
                if placed < total_clusters:
                    self.hole(2)      # forces the next run to be elsewhere

            for start, count in runs:
                if deleted and not overwritten:
                    self._mark_free(start, count)
                else:
                    self._mark_used(start, count)

        self.entries[number] = _Entry(
            number, name, parent, False, deleted, data=data, runs=runs,
            resident=stored_resident,
            created=created or datetime.datetime(2024, 3, 1, 11, 30, 0),
            modified=modified or datetime.datetime(2024, 3, 2, 12, 45, 0))
        return number

    # -- record assembly ----------------------------------------------------
    def _record(self, number, flags, attributes):
        record = bytearray(self.record_size)
        usa_count = self.record_size // self.sector_size + 1
        usa_offset = 0x30
        attr_offset = 0x38

        record[0:4] = b"FILE"
        struct.pack_into("<H", record, 0x04, usa_offset)
        struct.pack_into("<H", record, 0x06, usa_count)
        struct.pack_into("<H", record, 0x10, 1)        # sequence number
        struct.pack_into("<H", record, 0x12, 1)        # hard link count
        struct.pack_into("<H", record, 0x14, attr_offset)
        struct.pack_into("<H", record, 0x16, flags)
        struct.pack_into("<I", record, 0x1C, self.record_size)
        struct.pack_into("<H", record, 0x28, 6)        # next attribute id
        struct.pack_into("<I", record, 0x2C, number)

        position = attr_offset
        for attribute in attributes:
            assert position + len(attribute) + 8 <= self.record_size, \
                f"record {number} overflowed"
            record[position:position + len(attribute)] = attribute
            position += len(attribute)

        struct.pack_into("<I", record, position, 0xFFFFFFFF)   # terminator
        struct.pack_into("<I", record, 0x18, position + 8)     # used size

        # NTFS replaces the last two bytes of every sector with an update
        # sequence number and stashes the originals in the update sequence
        # array. ntfs._apply_fixup puts them back; if we did not do this here,
        # the test would never exercise that path.
        usn = 0x0102
        for i in range(1, usa_count):
            tail = i * self.sector_size - 2
            slot = usa_offset + i * 2
            record[slot:slot + 2] = record[tail:tail + 2]
            struct.pack_into("<H", record, tail, usn)
        struct.pack_into("<H", record, usa_offset, usn)
        return bytes(record)

    def _boot_sector(self):
        boot = bytearray(self.sector_size)
        boot[0:3] = b"\xEB\x52\x90"
        boot[3:11] = b"NTFS    "
        struct.pack_into("<H", boot, 0x0B, self.sector_size)
        boot[0x0D] = self.sectors_per_cluster
        boot[0x15] = 0xF8                                   # fixed disk
        struct.pack_into("<H", boot, 0x18, 63)
        struct.pack_into("<H", boot, 0x1A, 255)
        struct.pack_into("<Q", boot, 0x28, self.total_sectors)
        struct.pack_into("<Q", boot, 0x30, self.mft_cluster)
        struct.pack_into("<Q", boot, 0x38, self.total_clusters - 8)
        # Negative means "2^-n bytes per record" - the usual encoding for the
        # standard 1024-byte record on a 4K-cluster volume.
        shift = self.record_size.bit_length() - 1
        struct.pack_into("<b", boot, 0x40, -shift)
        struct.pack_into("<b", boot, 0x44, -12)
        struct.pack_into("<Q", boot, 0x48, 0x1122334455667788)
        boot[510:512] = b"\x55\xAA"
        return bytes(boot)

    def build(self):
        image = bytearray(self.total_bytes)
        image[0:self.sector_size] = self._boot_sector()

        # File content into its clusters.
        for entry in self.entries.values():
            if not entry.runs:
                continue
            offset = 0
            for start, count in entry.runs:
                span = count * self.cluster_size
                chunk = entry.data[offset:offset + span]
                base = start * self.cluster_size
                image[base:base + len(chunk)] = chunk
                offset += span

        # $Bitmap content.
        bitmap = bytearray(-(-self.total_clusters // 8))
        for cluster in self.allocated:
            if cluster < self.total_clusters:
                bitmap[cluster >> 3] |= 1 << (cluster & 7)
        base = self.bitmap_cluster * self.cluster_size
        image[base:base + len(bitmap)] = bitmap

        # The MFT.
        records = {}
        records[0] = self._record(0, FLAG_IN_USE, [
            _standard_information(datetime.datetime(2024, 1, 1),
                                  datetime.datetime(2024, 1, 1)),
            _file_name("$MFT", ROOT_RECORD, self.record_count * self.record_size,
                       datetime.datetime(2024, 1, 1),
                       datetime.datetime(2024, 1, 1), False),
            _non_resident_attribute(
                ATTR_DATA, [(self.mft_cluster, self.mft_clusters)],
                self.record_count * self.record_size, self.cluster_size),
        ])
        records[6] = self._record(6, FLAG_IN_USE, [
            _standard_information(datetime.datetime(2024, 1, 1),
                                  datetime.datetime(2024, 1, 1)),
            _file_name("$Bitmap", ROOT_RECORD, len(bitmap),
                       datetime.datetime(2024, 1, 1),
                       datetime.datetime(2024, 1, 1), False),
            _non_resident_attribute(
                ATTR_DATA, [(self.bitmap_cluster, self.bitmap_clusters)],
                len(bitmap), self.cluster_size),
        ])

        for number, entry in self.entries.items():
            flags = 0 if entry.deleted else FLAG_IN_USE
            if entry.is_dir:
                flags |= FLAG_DIRECTORY
            attributes = [
                _standard_information(entry.created, entry.modified),
                _file_name(entry.name, entry.parent, len(entry.data),
                           entry.created, entry.modified, entry.is_dir),
            ]
            if not entry.is_dir:
                if entry.resident is not None:
                    attributes.append(
                        _resident_attribute(ATTR_DATA, entry.resident))
                elif entry.runs:
                    attributes.append(_non_resident_attribute(
                        ATTR_DATA, entry.runs, len(entry.data),
                        self.cluster_size))
                else:
                    attributes.append(_resident_attribute(ATTR_DATA, b""))
            records[number] = self._record(number, flags, attributes)

        base = self.mft_cluster * self.cluster_size
        for number, record in records.items():
            position = base + number * self.record_size
            image[position:position + self.record_size] = record

        return bytes(image)
