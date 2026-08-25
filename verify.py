"""
verify.py - check whether a recovered file is actually whole.

WHY THE HEADER IS NOT ENOUGH
----------------------------
signatures.py looks at a file's first bytes and asks "is this still the kind
of file its name claims". That catches the common disaster - space reused, the
data gone - but it only ever sees the start.

A real recovery failed exactly in the gap that leaves. A 115MB video came back
with a perfectly valid MP4 header and passed as intact. Its `mdat` box said
the video was 420MB, its index was missing entirely, and no player would touch
it. The first 512 bytes were genuinely fine; the damage was a hundred
megabytes further in.

So this module walks the whole container. Every format here states its own
structure - box lengths, chunk lengths, an end-of-archive record - and that
internal bookkeeping is what tells us whether the bytes we have are all the
bytes there should be.

WHAT CAN AND CANNOT BE FIXED
----------------------------
Three outcomes, and only one of them is repair:

  * Too long. Carving cuts formats that have no end marker at an arbitrary
    size, so they arrive with junk on the end. The container states its true
    length, so we can trim to exactly the right byte. This is arithmetic, not
    guesswork, and `trim_copy` does it.

  * Too short. The data that exists is genuine but the file is incomplete -
    often missing the index a player needs. Rebuilding that index is a real
    project (see untrunc for MP4), deliberately not attempted here. We say
    precisely what is missing and stop.

  * Wrong. The bytes belong to something else now. Nothing fixes this and
    nothing here pretends to.

Nothing in this module writes to a source drive; it only ever reads files
that have already been recovered, and `trim_copy` writes a new file rather
than modifying the one it was given.
"""

import os
import struct
import zlib

# Verdicts.
INTACT = "intact"
TRAILING = "trailing"          # longer than it should be - trimmable
TRUNCATED = "truncated"        # shorter than it should be - not repairable here
DAMAGED = "damaged"            # structure breaks partway through
WRONG_FORMAT = "wrong_format"  # not this kind of file at all
UNKNOWN = "unknown"            # no validator for this type

REPAIRABLE = (TRAILING,)


class Report:
    """What we found, in a form the GUI and a human can both use."""

    __slots__ = ("verdict", "detail", "true_length", "actual_length", "kind")

    def __init__(self, verdict, detail, kind, actual_length,
                 true_length=None):
        self.verdict = verdict
        self.detail = detail
        self.kind = kind
        self.actual_length = actual_length
        self.true_length = true_length

    @property
    def repairable(self):
        return (self.verdict in REPAIRABLE
                and self.true_length is not None
                and 0 < self.true_length < self.actual_length)

    def __repr__(self):
        return f"<Report {self.kind} {self.verdict} {self.detail!r}>"


# ---------------------------------------------------------------------------
# Per-format walkers. Each returns (verdict, detail, true_length or None).
# ---------------------------------------------------------------------------

# JPEG markers that introduce a frame - the segment carrying the image's
# actual dimensions. A file without one is not a picture, whatever its first
# and last bytes say.
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_STANDALONE = {0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8}


def _check_jpeg(data):
    """
    Walk the marker segments, not just the two ends.

    Checking only for the start and end markers was not enough, and this is
    exactly how it failed: a deep scan turned up three "photos" that began
    with FFD8FF, ended with FFD9, and contained no image at all - the header
    bytes happened to occur inside other data and an end marker happened to
    turn up later. They passed as intact and would not open in anything.

    A real JPEG carries a frame header giving its dimensions, and a
    start-of-scan marker introducing the compressed image. Both must be here.
    """
    if data[:3] != b"\xFF\xD8\xFF":
        return WRONG_FORMAT, "This does not start like a JPEG.", None

    pos = 2
    saw_frame = saw_scan = False
    while pos < len(data) - 1:
        if data[pos] != 0xFF:
            return (WRONG_FORMAT,
                    "This is not a JPEG - the file has the right first and "
                    "last bytes but no picture inside it.", None)
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1                            # fill bytes are allowed
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1

        if marker in _STANDALONE:
            continue
        if marker == 0xD9:                      # end of image
            break
        if pos + 2 > len(data):
            return TRUNCATED, "The end of this image is missing.", None
        length = struct.unpack_from(">H", data, pos)[0]
        if length < 2:
            return (WRONG_FORMAT,
                    "This is not a JPEG - its internal markers do not make "
                    "sense.", None)
        if marker in _SOF_MARKERS:
            saw_frame = True
        if marker == 0xDA:                      # start of scan
            saw_scan = True
            break
        pos += length

    if not saw_frame:
        return (WRONG_FORMAT,
                "This is not a JPEG - the file has the right first and last "
                "bytes but carries no picture.", None)
    if not saw_scan:
        return TRUNCATED, "This image has no picture data in it.", None

    end = data.rfind(b"\xFF\xD9")
    if end == -1:
        return (TRUNCATED,
                "The end of this image is missing. Most viewers will show "
                "the top part of it and stop there.", None)
    true_end = end + 2
    if true_end < len(data):
        return (TRAILING,
                f"The image itself ends after {true_end:,} bytes; the rest is "
                f"leftover data that isn't part of it.", true_end)
    return INTACT, "This image looks complete.", true_end


def _check_png(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return WRONG_FORMAT, "This does not start like a PNG.", None
    pos = 8
    saw_header = saw_end = False
    while pos + 8 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        chunk_end = pos + 12 + length          # length + type + data + CRC
        if chunk_end > len(data):
            return (TRUNCATED,
                    f"This image stops partway through. It should carry on "
                    f"past {len(data):,} bytes.", None)
        if kind == b"IHDR":
            saw_header = True
        stored = struct.unpack_from(">I", data, chunk_end - 4)[0]
        if zlib.crc32(data[pos + 4:chunk_end - 4]) & 0xFFFFFFFF != stored:
            return (DAMAGED,
                    f"Part of this image is corrupted, {pos:,} bytes in. "
                    f"It may open but look wrong.", None)
        pos = chunk_end
        if kind == b"IEND":
            saw_end = True
            break
    if not saw_header:
        return DAMAGED, "This image has no header block.", None
    if not saw_end:
        return TRUNCATED, "The end of this image is missing.", None
    if pos < len(data):
        return (TRAILING,
                f"The image itself ends after {pos:,} bytes; the rest is "
                f"leftover data that isn't part of it.", pos)
    return INTACT, "This image looks complete.", pos


def _skip_sub_blocks(data, pos):
    """GIF payloads come as length-prefixed runs ending in a zero length."""
    while pos < len(data):
        size = data[pos]
        pos += 1
        if size == 0:
            return pos
        pos += size
    return None                                # ran off the end


def _check_gif(data):
    """
    Walk the block structure rather than hunting for the trailer byte.

    Searching backwards for 0x3B finds whatever junk happens to contain that
    byte, which is exactly the situation a carved file is in - the trailer has
    to be found by walking to it, not by looking for it.
    """
    if data[:6] not in (b"GIF87a", b"GIF89a"):
        return WRONG_FORMAT, "This does not start like a GIF.", None
    if len(data) < 13:
        return TRUNCATED, "This image is missing almost all of its data.", None

    packed = data[10]
    pos = 13
    if packed & 0x80:                          # global colour table
        pos += 3 * (1 << ((packed & 0x07) + 1))

    while pos < len(data):
        marker = data[pos]
        if marker == 0x3B:                     # trailer - the real end
            pos += 1
            if pos < len(data):
                return (TRAILING,
                        f"The image ends after {pos:,} bytes; the rest is "
                        f"leftover data that isn't part of it.", pos)
            return INTACT, "This image looks complete.", pos
        if marker == 0x21:                     # extension
            pos += 2
            pos = _skip_sub_blocks(data, pos)
        elif marker == 0x2C:                   # image descriptor
            if pos + 10 > len(data):
                return TRUNCATED, "The end of this image is missing.", None
            local = data[pos + 9]
            pos += 10
            if local & 0x80:                   # local colour table
                pos += 3 * (1 << ((local & 0x07) + 1))
            pos += 1                           # LZW minimum code size
            pos = _skip_sub_blocks(data, pos)
        else:
            return (DAMAGED,
                    f"The structure of this image breaks {pos:,} bytes in.",
                    None)
        if pos is None:
            return TRUNCATED, "The end of this image is missing.", None

    return TRUNCATED, "The end of this image is missing.", None


def _check_pdf(data):
    if data[:5] != b"%PDF-":
        return WRONG_FORMAT, "This does not start like a PDF.", None
    end = data.rfind(b"%%EOF")
    if end == -1:
        return (TRUNCATED,
                "The end of this document is missing. Some readers will open "
                "it and show fewer pages than it should have.", None)
    true_end = end + 5
    while true_end < len(data) and data[true_end] in b"\r\n":
        true_end += 1
    if true_end < len(data):
        return (TRAILING,
                f"The document ends after {true_end:,} bytes; the rest is "
                f"leftover data.", true_end)
    return INTACT, "This document looks complete.", true_end


def _check_zip(data):
    """
    A ZIP states its own end: the End of Central Directory record, at the
    back, with a comment length that says exactly how far it runs. Anything
    past that is carving overshoot.
    """
    if data[:4] not in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return WRONG_FORMAT, "This does not start like a zip archive.", None
    marker = data.rfind(b"PK\x05\x06")
    if marker == -1:
        return (TRUNCATED,
                "The index at the end of this archive is missing, so nothing "
                "can list what is inside it.", None)
    if marker + 22 > len(data):
        return TRUNCATED, "The index at the end of this archive is cut off.", None
    comment = struct.unpack_from("<H", data, marker + 20)[0]
    true_end = marker + 22 + comment
    if true_end > len(data):
        return TRUNCATED, "This archive is missing its last few bytes.", None
    if true_end < len(data):
        return (TRAILING,
                f"The archive ends after {true_end:,} bytes; the rest is "
                f"leftover data that isn't part of it.", true_end)
    return INTACT, "This archive looks complete.", true_end


def _check_mp4(data):
    """
    Walk the box structure. Two failures matter and they are different: a file
    that stops early, and a file that is all there but has no index.
    """
    if len(data) < 8 or data[4:8] != b"ftyp":
        return WRONG_FORMAT, "This does not start like a video file.", None

    pos = 0
    boxes = []
    while pos + 8 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        kind = data[pos + 4:pos + 8]
        readable = all(32 <= c < 127 for c in kind)

        if length == 1 and readable:
            if pos + 16 > len(data):
                return TRUNCATED, "This video stops partway through.", None
            length = struct.unpack_from(">Q", data, pos + 8)[0]
        elif length == 0 and readable:
            length = len(data) - pos          # runs to the end of the file

        if not readable or length < 8:
            # Junk where the next box should be. If we already have a
            # sensible file behind us this is carving overshoot and the file
            # ends here; if we do not, the thing is broken.
            if len(boxes) >= 2 and any(k == b"mdat" for k, _, _ in boxes):
                break
            return (DAMAGED,
                    f"The structure of this video breaks {pos:,} bytes in.",
                    None)

        boxes.append((kind, pos, length))
        pos += length

    names = {kind for kind, _, _ in boxes}
    has_index = b"moov" in names

    if pos > len(data):
        shortfall = pos - len(data)
        if not has_index:
            return (TRUNCATED,
                    f"This video is missing its last {shortfall:,} bytes, "
                    f"including the index that tells a player where each "
                    f"frame is. The picture data that survived is real, but "
                    f"no player will open it without that index.", None)
        return (TRUNCATED,
                f"This video is missing its last {shortfall:,} bytes. It may "
                f"play and stop early.", None)

    if not has_index:
        return (TRUNCATED,
                "This video has no index - the part that tells a player where "
                "each frame is. The picture data is here, but nothing will "
                "play it as it stands.", None)

    if pos < len(data):
        return (TRAILING,
                f"The video ends after {pos:,} bytes; the rest is leftover "
                f"data that isn't part of it.", pos)
    return INTACT, "This video looks complete.", pos


def _check_sqlite(data):
    if data[:16] != b"SQLite format 3\x00":
        return WRONG_FORMAT, "This does not start like a database.", None
    page = struct.unpack_from(">H", data, 16)[0]
    page = 65536 if page == 1 else page
    if page < 512 or page & (page - 1):
        return DAMAGED, "This database's header is corrupted.", None
    if len(data) % page:
        whole = (len(data) // page) * page
        return (TRAILING,
                f"The database ends after {whole:,} bytes; the rest is "
                f"leftover data.", whole)
    return INTACT, "This database looks complete.", len(data)


_CHECKS = {
    "jpg": _check_jpeg, "jpeg": _check_jpeg,
    "png": _check_png,
    "gif": _check_gif,
    "pdf": _check_pdf,
    "zip": _check_zip, "docx": _check_zip, "xlsx": _check_zip,
    "pptx": _check_zip, "odt": _check_zip,
    "mp4": _check_mp4, "m4v": _check_mp4, "m4a": _check_mp4,
    "mov": _check_mp4, "3gp": _check_mp4, "heic": _check_mp4,
    "sqlite": _check_sqlite, "db": _check_sqlite,
}


def can_check(extension):
    return (extension or "").lower().lstrip(".") in _CHECKS


def inspect_bytes(data, extension):
    """Structural verdict on a file already in memory."""
    kind = (extension or "").lower().lstrip(".")
    check = _CHECKS.get(kind)
    if check is None:
        return Report(UNKNOWN, "", kind, len(data))
    if not data:
        return Report(WRONG_FORMAT, "This file is empty.", kind, 0)
    try:
        verdict, detail, true_length = check(data)
    except Exception:
        # A validator falling over is not a verdict about the file.
        return Report(UNKNOWN, "", kind, len(data))
    return Report(verdict, detail, kind, len(data), true_length)


def plausible(head, extension):
    """
    Could the bytes at the start of this file really be one?

    Used by the deep scan to throw out false hits before they are ever
    listed. Only positive evidence counts against a file: a walker that runs
    out of data has learned nothing, and says so.
    """
    verdict = inspect_bytes(head, extension).verdict
    return verdict not in (WRONG_FORMAT, DAMAGED)


def inspect_file(path, extension=None):
    """Structural verdict on a recovered file on disk. Reads only."""
    if extension is None:
        extension = os.path.splitext(path)[1]
    with open(path, "rb") as fh:
        return inspect_bytes(fh.read(), extension)


def unique_path(path):
    """`name.ext` -> `name (2).ext`. Never returns an existing path."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def trim_copy(path, report=None, dest=None):
    """
    Write a copy cut to the file's own stated length.

    Returns the path written, or None if there was nothing to trim. The
    original is never modified and an existing file is never overwritten -
    same rule the recovery itself follows.
    """
    if report is None:
        report = inspect_file(path)
    if not report.repairable:
        return None

    if dest is None:
        stem, ext = os.path.splitext(path)
        dest = f"{stem} (trimmed){ext}"
    dest = unique_path(dest)

    remaining = report.true_length
    with open(path, "rb") as src, open(dest, "wb") as out:
        while remaining > 0:
            chunk = src.read(min(remaining, 1 << 20))
            if not chunk:
                break
            out.write(chunk)
            remaining -= len(chunk)
    return dest


def summarise(reports):
    """
    One line for a pile of files. Counts only what we actually checked, so
    unknown formats never pad the good news.
    """
    counted = [r for r in reports if r.verdict != UNKNOWN]
    if not counted:
        return "None of these file types can be checked."
    intact = sum(1 for r in counted if r.verdict == INTACT)
    fixable = sum(1 for r in counted if r.repairable)
    broken = len(counted) - intact - fixable
    parts = [f"{intact} of {len(counted)} checked files look complete"]
    if fixable:
        parts.append(f"{fixable} have extra data on the end and can be trimmed")
    if broken:
        parts.append(f"{broken} are incomplete or damaged")
    return "; ".join(parts) + "."
