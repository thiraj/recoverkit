"""
carve.py - signature-based recovery ("carving").

Used when the filesystem's own records are gone, or when the volume isn't
NTFS (APFS on Mac, ext4 on Linux, exFAT on cameras and SD cards).

It scans the raw drive for the byte patterns that mark the start and end of
known file formats. It cannot recover filenames - that information lived in
the filesystem - so results come out as recovered_00001.jpg and similar.

Read-only: it uses ReadOnlyDisk, which cannot write to the source.
"""

import signatures
import verify

CHUNK = 8 * 1024 * 1024

# How much of a candidate we look at before deciding it is worth listing.
# The parts that prove a file real - a JPEG's frame header, a PNG's IHDR -
# live near the front, so this does not need to be large.
PLAUSIBLE_PROBE = 64 * 1024

# How much of a candidate we are willing to read back to judge it. Deep scan
# is already the slow mode; handing someone a list of files that do not open
# is worse than taking longer to produce a shorter, true one.
VERDICT_CAP = 64 * 1024 * 1024

# ext: (header, footer or None, max size)
SIGNATURES = {
    "jpg":  (b"\xFF\xD8\xFF",            b"\xFF\xD9",            30 * 1024**2),
    "png":  (b"\x89PNG\r\n\x1a\n",       b"IEND\xaeB`\x82",      60 * 1024**2),
    "gif":  (b"GIF89a",                  b"\x00\x3B",            20 * 1024**2),
    "bmp":  (b"BM",                      None,                    10 * 1024**2),
    "heic": (b"ftypheic",                None,                    30 * 1024**2),
    "pdf":  (b"%PDF-",                   b"%%EOF",              200 * 1024**2),
    "zip":  (b"PK\x03\x04",              None,                  100 * 1024**2),
    "docx": (b"PK\x03\x04",              None,                  100 * 1024**2),
    "doc":  (b"\xD0\xCF\x11\xE0\xA1\xB1\x1a\xE1", None,          50 * 1024**2),
    "mp4":  (b"ftyp",                    None,                  2000 * 1024**2),
    "mp3":  (b"ID3",                     None,                    50 * 1024**2),
    "sqlite": (b"SQLite format 3\x00",   None,                  200 * 1024**2),
}


# Where a format's marker sits relative to the start of the file. Video
# containers put `ftyp` after a four-byte box length that varies between
# encoders, so searching for a fixed 24-byte first box - which is what this
# used to do - found only the small proportion of files that happened to have
# one, and silently missed the rest.
HEADER_OFFSET = {
    "mp4": 4,
    "heic": 4,
}


class CarvedFile:
    """
    One file found by its signature.

    Records where the file is, not what it contains. Holding the bytes meant a
    deep scan of a full card kept the whole card in memory - the undelete
    engines never did this; they keep a cluster map and read on demand, and so
    does this now. `read_file` fetches the content when it is actually needed.
    """

    __slots__ = ("name", "path", "size", "offset", "ext", "chance",
                 "deleted_at", "created_at", "is_dir",
                 "content_check", "still_at")

    def __init__(self, name, ext, offset, size):
        self.name = name
        self.ext = ext
        self.offset = offset
        self.size = size
        self.path = "(no folder - carved)"
        # Carving has no filesystem records to check against, so it cannot
        # know whether a file is whole - only that it starts and ends where a
        # file of this type should. Formats with an end marker are strong
        # evidence; formats without one are cut at an arbitrary length and
        # routinely carry trailing junk, and a fragmented file carves into
        # nonsense after its first piece however clean the header looked.
        # Saying 100% to all of that was never honest.
        self.chance = 75 if SIGNATURES.get(ext, (None, None, 0))[1] else 40
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


PROBE = 1024 * 1024      # how much we look at while hunting for the end

# A format with no end marker cannot tell us where it stops. Rather than
# swallow its theoretical maximum - two gigabytes, for video - we take a
# bounded amount and say the length is a guess.
UNKNOWN_LENGTH_CAP = 64 * 1024 * 1024

BOX_FORMATS = ("mp4", "m4v", "m4a", "mov", "3gp", "heic")


def _find_footer(disk, start, footer, max_size):
    """
    Length of the file at `start`, found by walking forward to its end marker.

    Reading the format's maximum size up front was the single most expensive
    thing this scanner did. A three-byte JPEG header turns up in random data
    roughly once per 16MB, and every one of those false hits pulled 30MB off
    the drive before being thrown away. Walking forward a megabyte at a time
    stops at the first end marker instead, which for a false hit is usually
    within a few tens of kilobytes.
    """
    overlap = len(footer) - 1
    carry = b""
    pos = 0
    while pos < max_size:
        chunk = disk.read(start + pos, min(PROBE, max_size - pos))
        if not chunk:
            return None
        buf = carry + chunk
        found = buf.find(footer)
        if found != -1:
            return pos - len(carry) + found + len(footer)
        carry = buf[-overlap:] if overlap else b""
        pos += len(chunk)
    return None


def _declared_length(disk, start, ext, max_size):
    """
    Ask a container how long it is, for formats that say so in their headers.

    An MP4 is a chain of boxes, each stating its own length, so the total can
    be had by reading sixteen bytes per box rather than by guessing. Formats
    that keep their index at the very end - zip and its descendants - cannot
    be asked this way.
    """
    if ext not in BOX_FORMATS:
        return None
    pos = 0
    boxes = 0
    while pos < max_size and boxes < 1024:
        head = disk.read(start + pos, 16)
        if len(head) < 8:
            break
        length = int.from_bytes(head[0:4], "big")
        kind = head[4:8]
        if not all(32 <= c < 127 for c in kind):
            break
        if length == 1:
            if len(head) < 16:
                break
            length = int.from_bytes(head[8:16], "big")
        if length < 8:
            break
        pos += length
        boxes += 1
    return pos or None


def _measure(disk, start, ext, footer, max_size, head=b""):
    """How long is the file at `start`? None means "not a real hit"."""
    if footer:
        # Start looking past the format's metadata. A photograph carries a
        # complete little JPEG in its EXIF - the thumbnail - and searching
        # from byte zero finds *its* end marker, cutting the photograph down
        # to its own thumbnail.
        begin = verify.payload_offset(head, ext) or 0
        if begin >= max_size:
            begin = 0
        length = _find_footer(disk, start + begin, footer, max_size - begin)
        return None if length is None else begin + length

    declared = _declared_length(disk, start, ext, max_size)
    if declared:
        return min(declared, max_size)

    # Nothing states the length. Take a bounded slice rather than the
    # format's theoretical maximum, and let the trailing junk be trimmed
    # afterwards by verify.py.
    tail = disk.read(start, 1)
    if not tail:
        return None
    return min(max_size, UNKNOWN_LENGTH_CAP)


# Several formats share one signature. `PK\x03\x04` is a zip, and a .docx is
# a zip too - so a plain archive carved while looking for documents came out
# named .docx and would not open in Word, despite the data being perfect.
_ZIP_FAMILY = {"zip", "docx", "xlsx", "pptx", "odt"}


def _real_extension(ext, head):
    """
    Refine the extension using what is actually inside the file.

    Only ever narrows a guess we already had, and only on evidence: an Office
    document names [Content_Types].xml near the front, an OpenDocument names
    its mimetype. Anything else keeps the honest, general name.
    """
    if ext not in _ZIP_FAMILY:
        return ext
    if b"[Content_Types].xml" in head:
        if b"word/" in head:
            return "docx"
        if b"xl/" in head:
            return "xlsx"
        if b"ppt/" in head:
            return "pptx"
        return "docx"
    if head[30:38] == b"mimetype":
        return "odt"
    return "zip"


# What the whole extracted file turned out to be, and what to say about it.
# Nothing here is a guess: every number comes from reading the bytes back and
# walking the format's own structure.
_VERDICTS = {
    verify.INTACT: (signatures.MATCH, 90),
    verify.TRAILING: (signatures.MATCH, 75),    # trimmable, see verify.py
    verify.TRUNCATED: (signatures.MISMATCH, 15),
    verify.DAMAGED: (signatures.MISMATCH, 10),
    verify.UNKNOWN: (signatures.UNKNOWN, 40),   # no validator for this type
}


def _judge(disk, start, size, ext):
    """
    Read the candidate back and see what it actually is.

    Carved results used to carry a fixed score - 75 for anything with an end
    marker - and a hardcoded "looks intact". Both were invented. A file cut
    at the wrong place scored exactly the same as a perfect one, which is how
    a deep scan came to hand back a list of files that would not open, every
    one of them labelled as fine.

    Returns (verdict, chance), or (None, None) for something that is not a
    file at all and should never be listed.
    """
    if not verify.can_check(ext):
        return signatures.UNKNOWN, 40
    data = disk.read(start, min(size, VERDICT_CAP))
    if not data:
        return None, None
    report = verify.inspect_bytes(data, ext)
    if report.verdict == verify.WRONG_FORMAT:
        return None, None
    return _VERDICTS.get(report.verdict, (signatures.UNKNOWN, 40))


def read_file(disk, found):
    """Fetch a carved file's bytes. Read-only, like everything else here."""
    return disk.read(found.offset, found.size)


def scan(disk, types, progress=None, should_stop=None, limit_bytes=None):
    """Yield CarvedFile objects as they're found."""
    sigs = {t: SIGNATURES[t] for t in types if t in SIGNATURES}
    if not sigs:
        return

    max_sig = max(max(len(h), len(f) if f else 0) for h, f, _ in sigs.values())
    total = limit_bytes or disk.size() or 0
    counters = {t: 0 for t in sigs}
    # How far each type has already been carved to. A photograph contains a
    # complete little JPEG in its EXIF, so scanning finds the thumbnail as
    # well as the photograph and hands back two files for every picture - the
    # second one a 160x120 version nobody asked for. Anything starting inside
    # a file we have already produced is part of that file.
    carved_to = {t: 0 for t in sigs}
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
                start = base + idx - HEADER_OFFSET.get(ext, 0)
                if start < 0 or start < carved_to[ext]:
                    pos = idx + 1
                    continue
                head = disk.read(start, PLAUSIBLE_PROBE)
                if not verify.plausible(head, ext):
                    pos = idx + 1
                    continue

                size = _measure(disk, start, ext, footer, max_size, head)
                if not size or size <= 512:
                    pos = idx + 1
                    continue

                real = _real_extension(ext, head)
                verdict, chance = _judge(disk, start, size, real)
                if verdict is None:
                    pos = idx + 1
                    continue            # not a file, whatever it looked like

                counters[ext] += 1
                carved_to[ext] = start + size
                found = CarvedFile(
                    f"recovered_{ext}_{counters[ext]:05d}.{real}",
                    real, start, size)
                found.chance = chance
                found.content_check = verdict
                yield found
                pos = idx + 1

        carry = buf[-max_sig:] if len(buf) >= max_sig else buf
        base_of_carry = offset + len(chunk) - len(carry)

        if progress:
            # `total` is 0 when the drive will not say how big it is. Pass it
            # through as 0 rather than inventing a denominator - the caller
            # can report bytes scanned instead of a percentage, which is
            # honest, where a bar pinned at 100% for an hour is not.
            progress(offset + len(chunk), total)
