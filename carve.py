"""
carve.py - signature-based recovery ("carving").

Used when the filesystem's own records are gone, or when the volume isn't
NTFS (APFS on Mac, ext4 on Linux, exFAT on cameras and SD cards).

It scans the raw drive for the byte patterns that mark the start and end of
known file formats. It cannot recover filenames - that information lived in
the filesystem - so results come out as recovered_00001.jpg and similar.

Read-only: it uses ReadOnlyDisk, which cannot write to the source.
"""

CHUNK = 8 * 1024 * 1024

# ext: (header, footer or None, max size)
SIGNATURES = {
    "jpg":  (b"\xFF\xD8\xFF",            b"\xFF\xD9",            30 * 1024**2),
    "png":  (b"\x89PNG\r\n\x1a\n",       b"IEND\xaeB`\x82",      60 * 1024**2),
    "gif":  (b"GIF89a",                  b"\x00\x3B",            20 * 1024**2),
    "bmp":  (b"BM",                      None,                    10 * 1024**2),
    "heic": (b"\x00\x00\x00\x18ftypheic", None,                   30 * 1024**2),
    "pdf":  (b"%PDF-",                   b"%%EOF",              200 * 1024**2),
    "zip":  (b"PK\x03\x04",              None,                  100 * 1024**2),
    "docx": (b"PK\x03\x04",              None,                  100 * 1024**2),
    "doc":  (b"\xD0\xCF\x11\xE0\xA1\xB1\x1a\xE1", None,          50 * 1024**2),
    "mp4":  (b"\x00\x00\x00\x18ftyp",    None,                  2000 * 1024**2),
    "mp3":  (b"ID3",                     None,                    50 * 1024**2),
    "sqlite": (b"SQLite format 3\x00",   None,                  200 * 1024**2),
}


class CarvedFile:
    __slots__ = ("name", "path", "size", "offset", "ext", "chance",
                 "deleted_at", "created_at", "is_dir", "data",
                 "content_check", "still_at")

    def __init__(self, name, ext, offset, size, data):
        self.name = name
        self.ext = ext
        self.offset = offset
        self.size = size
        self.data = data
        self.path = "(no folder - carved)"
        self.chance = 100
        self.deleted_at = None
        self.created_at = None
        self.is_dir = False
        # A carved file was found *by* its signature, so its header matches
        # by construction - there is nothing left to verify.
        self.content_check = "match"
        self.still_at = None

    @property
    def extension(self):
        return self.ext


def _extract(disk, start, footer, max_size):
    data = disk.read(start, max_size)
    if not data:
        return None
    if footer:
        end = data.find(footer)
        if end == -1:
            return None          # no end marker nearby - probably a false hit
        return data[:end + len(footer)]
    return data


def scan(disk, types, progress=None, should_stop=None, limit_bytes=None):
    """Yield CarvedFile objects as they're found."""
    sigs = {t: SIGNATURES[t] for t in types if t in SIGNATURES}
    if not sigs:
        return

    max_sig = max(max(len(h), len(f) if f else 0) for h, f, _ in sigs.values())
    total = limit_bytes or disk.size() or 0
    counters = {t: 0 for t in sigs}
    carry = b""
    base_of_carry = 0

    for offset, chunk in disk.stream(CHUNK):
        if should_stop and should_stop():
            return
        if limit_bytes and offset >= limit_bytes:
            return

        buf = carry + chunk
        base = base_of_carry if carry else offset

        for ext, (header, footer, max_size) in sigs.items():
            pos = 0
            while True:
                idx = buf.find(header, pos)
                if idx == -1:
                    break
                start = base + idx
                data = _extract(disk, start, footer, max_size)
                if data and len(data) > 512:
                    counters[ext] += 1
                    yield CarvedFile(
                        f"recovered_{ext}_{counters[ext]:05d}.{ext}",
                        ext, start, len(data), data)
                pos = idx + 1

        carry = buf[-max_sig:] if len(buf) >= max_sig else buf
        base_of_carry = offset + len(chunk) - len(carry)

        if progress and total:
            progress(offset, total)
