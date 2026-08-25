"""
signatures.py - what a file of a given type looks like at its first bytes.

WHY THIS EXISTS
---------------
The recovery-chance score used to be based on one thing: whether the file's
clusters are still marked free in the volume's allocation bitmap. That answers
"has this space been handed to another file *right now*" - which is not the
same question as "does this space still hold my file".

On a drive with history, a cluster can be freed, reused, and freed again many
times over. The bitmap says free; the bytes are somebody else's. A 211MB video
recovered at a confident 100% opened as noise, and nothing in the tool had
noticed, because nothing in the tool had looked.

So we look. If a file is called holiday.jpg and the first bytes where its data
should be are not a JPEG header, that is hard evidence the content is gone -
evidence that outranks anything the bitmap has to say. It costs one 512-byte
read per file, and only for files whose extension we actually recognise.

WHAT THIS CANNOT TELL YOU
-------------------------
A matching header is not proof the whole file survived; the tail may still be
overwritten. It is only ever evidence in one direction: a mismatch means gone,
a match means "the start of it is still there". Unknown extensions get no
verdict at all rather than a flattering guess.
"""

# Extension -> alternative (offset, magic bytes) pairs. Any one matching is
# enough. Offsets are non-zero where the format's marker genuinely lives
# further in: MP4 and friends put `ftyp` after a four-byte box length, which
# varies between encoders, so matching at offset 4 is the reliable test.
FORMAT_MAGIC = {
    # Images
    "jpg":    ((0, b"\xFF\xD8\xFF"),),
    "jpeg":   ((0, b"\xFF\xD8\xFF"),),
    "png":    ((0, b"\x89PNG\r\n\x1a\n"),),
    "gif":    ((0, b"GIF87a"), (0, b"GIF89a")),
    "bmp":    ((0, b"BM"),),
    "tif":    ((0, b"II*\x00"), (0, b"MM\x00*")),
    "tiff":   ((0, b"II*\x00"), (0, b"MM\x00*")),
    "heic":   ((4, b"ftyp"),),
    "webp":   ((0, b"RIFF"),),
    "psd":    ((0, b"8BPS"),),

    # Camera raw
    "cr2":    ((0, b"II*\x00"),),
    "nef":    ((0, b"MM\x00*"), (0, b"II*\x00")),
    "arw":    ((0, b"II*\x00"),),
    "dng":    ((0, b"II*\x00"), (0, b"MM\x00*")),

    # Video and audio
    "mp4":    ((4, b"ftyp"),),
    "m4v":    ((4, b"ftyp"),),
    "m4a":    ((4, b"ftyp"),),
    "mov":    ((4, b"ftyp"), (4, b"moov"), (4, b"mdat"), (4, b"wide")),
    "3gp":    ((4, b"ftyp"),),
    "mkv":    ((0, b"\x1A\x45\xDF\xA3"),),
    "webm":   ((0, b"\x1A\x45\xDF\xA3"),),
    "avi":    ((0, b"RIFF"),),
    "wmv":    ((0, b"\x30\x26\xB2\x75"),),
    "wav":    ((0, b"RIFF"),),
    "flac":   ((0, b"fLaC"),),
    "ogg":    ((0, b"OggS"),),
    # An MP3 may open with an ID3 tag or straight into a frame header; the
    # frame sync is eleven set bits, so the second byte varies by version.
    "mp3":    ((0, b"ID3"), (0, b"\xFF\xFB"), (0, b"\xFF\xFA"),
               (0, b"\xFF\xF3"), (0, b"\xFF\xF2"), (0, b"\xFF\xE3")),

    # Documents and archives
    "pdf":    ((0, b"%PDF-"),),
    "zip":    ((0, b"PK\x03\x04"), (0, b"PK\x05\x06"), (0, b"PK\x07\x08")),
    "docx":   ((0, b"PK\x03\x04"),),
    "xlsx":   ((0, b"PK\x03\x04"),),
    "pptx":   ((0, b"PK\x03\x04"),),
    "odt":    ((0, b"PK\x03\x04"),),
    "doc":    ((0, b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"),),
    "xls":    ((0, b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"),),
    "ppt":    ((0, b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"),),
    "rtf":    ((0, b"{\\rtf"),),
    "gz":     ((0, b"\x1F\x8B"),),
    "7z":     ((0, b"7z\xBC\xAF\x27\x1C"),),
    "rar":    ((0, b"Rar!\x1A\x07"),),
    "sqlite": ((0, b"SQLite format 3\x00"),),
    "db":     ((0, b"SQLite format 3\x00"),),
    "exe":    ((0, b"MZ"),),
    "dll":    ((0, b"MZ"),),
}

# Verdicts. Deliberately three-valued plus a blank case: "we cannot tell" is a
# real answer here and must not be rounded up into good news.
MATCH = "match"
MISMATCH = "mismatch"
BLANK = "blank"
UNKNOWN = "unknown"
# The file's data is still where it was, and a file that is still alive on the
# drive occupies exactly those clusters - almost always because it was moved
# to the Trash rather than deleted. The user does not need us at all for this
# one; they need to be told where it went.
MOVED = "moved"
IN_USE = "in_use"

# How much of the file's start we need to judge it.
PROBE_BYTES = 512


def known_extension(extension):
    """True if we have something to compare this kind of file against."""
    return (extension or "").lower() in FORMAT_MAGIC


def check(extension, data):
    """
    Compare the first bytes of a file against what its extension promises.

    Returns MATCH, MISMATCH, BLANK or UNKNOWN. UNKNOWN is returned whenever we
    have no basis for an opinion - an unrecognised extension, or no data to
    look at - and callers must leave their score alone when they see it.
    """
    if not data:
        return UNKNOWN

    magics = FORMAT_MAGIC.get((extension or "").lower())
    if not magics:
        return UNKNOWN

    if not any(data):
        # Nothing was ever written here, or it has been wiped.
        return BLANK

    for offset, magic in magics:
        if data[offset:offset + len(magic)] == magic:
            return MATCH
    return MISMATCH


def explain(verdict, extension, still_at=None):
    """One plain sentence for the user. No jargon, no hex."""
    kind = (extension or "").lower()
    if verdict == MOVED:
        where = f" It is at: {still_at}" if still_at else ""
        return ("This file has not been deleted - it has been moved, most "
                "likely into the Trash." + where +
                " You can copy it straight back out; you don't need to "
                "recover anything.")
    if verdict == IN_USE:
        return ("Something else on the drive is using this space now, but "
                "the data still looks like this file. It may come back "
                "intact, or it may be someone else's file that happens to "
                "be the same kind. Worth trying, worth checking.")
    if verdict == MISMATCH:
        return (f"The data where this file used to be is not a {kind} file "
                f"any more - the space has been used by something else since. "
                f"Recovering it will not give you a working file.")
    if verdict == BLANK:
        return ("The space where this file used to be is empty. There is "
                "nothing left to recover.")
    if verdict == MATCH:
        return (f"The start of the data still looks like a {kind} file. The "
                f"rest may still be damaged - open it to check.")
    return ""
