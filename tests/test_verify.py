"""
Tests for the structural check on recovered files.

The header check in signatures.py only ever sees the first 512 bytes. That is
enough to catch space that has been reused, and not nearly enough to catch a
file that starts perfectly and is wrong a hundred megabytes later - which is
what actually happened to a recovered video here: valid MP4 header, `mdat`
claiming 420MB, 115MB present, no index, and a clean "looks intact" verdict.

Every sample below is built rather than checked in, so the expected structure
is visible in the test itself.
"""

import os
import struct
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import verify
from tests.support import noise, sample_jpeg


# --- sample builders -------------------------------------------------------

def jpeg(payload=b"J" * 2000):
    return sample_jpeg(payload)


def png(payload=b"P" * 500):
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", payload)
            + chunk(b"IEND", b""))


def gif(frames=1):
    """A small but genuinely well-formed GIF, walked block by block."""
    out = bytearray(b"GIF89a")
    out += struct.pack("<HH", 1, 1)
    out += bytes([0x80, 0, 0])                 # global colour table, 2 entries
    out += b"\x00\x00\x00\xFF\xFF\xFF"
    for _ in range(frames):
        out += b"\x21\xF9\x04\x00\x00\x00\x00\x00"   # graphic control
        out += b"\x2C" + struct.pack("<HHHH", 0, 0, 1, 1) + b"\x00"
        out += b"\x02"                         # LZW minimum code size
        out += b"\x02\x4C\x01" + b"\x00"      # one sub-block, then terminator
    out += b"\x3B"
    return bytes(out)


def zip_archive(body=b"Z" * 1000, comment=b""):
    """Just enough of a ZIP for the end-of-archive record to be meaningful."""
    return (b"PK\x03\x04" + body + b"PK\x05\x06" + b"\x00" * 16
            + struct.pack("<H", len(comment)) + comment)


def mp4(payload=b"V" * 4000, with_index=True):
    def box(kind, body):
        return struct.pack(">I", len(body) + 8) + kind + body
    out = box(b"ftyp", b"isom" + b"\x00" * 8) + box(b"mdat", payload)
    if with_index:
        out += box(b"moov", b"\x00" * 64)
    return out


# --- the good case ---------------------------------------------------------

class IntactFilesTests(unittest.TestCase):
    def test_complete_files_are_recognised(self):
        for extension, data in (("jpg", jpeg()), ("png", png()),
                                ("zip", zip_archive()), ("mp4", mp4()),
                                ("pdf", b"%PDF-1.7\n" + b"x" * 500 + b"%%EOF"),
                                ("gif", gif())):
            report = verify.inspect_bytes(data, extension)
            self.assertEqual(report.verdict, verify.INTACT,
                             f"{extension}: {report.detail}")
            self.assertFalse(report.repairable)

    def test_a_trailing_newline_after_pdf_eof_is_not_junk(self):
        report = verify.inspect_bytes(b"%PDF-1.7\n" + b"x" * 100 + b"%%EOF\n",
                                      "pdf")
        self.assertEqual(report.verdict, verify.INTACT)


# --- too long: the repairable case ----------------------------------------

class TrailingDataTests(unittest.TestCase):
    """
    Carving cuts formats with no end marker at an arbitrary length, so they
    arrive with junk on the end. The container states its real length, so
    this one is arithmetic rather than guesswork.
    """

    def test_junk_after_the_real_end_is_spotted_and_measured(self):
        for extension, good in (("jpg", jpeg()), ("png", png()),
                                ("zip", zip_archive()), ("mp4", mp4()),
                                ("gif", gif())):
            data = good + noise(5000, seed=7)
            report = verify.inspect_bytes(data, extension)
            self.assertEqual(report.verdict, verify.TRAILING, extension)
            self.assertTrue(report.repairable, extension)
            self.assertEqual(report.true_length, len(good),
                             f"{extension}: wrong true length")

    def test_a_zip_comment_counts_as_part_of_the_archive(self):
        """The end record says how long its comment is; it is not junk."""
        data = zip_archive(comment=b"made by a real tool")
        report = verify.inspect_bytes(data, "zip")
        self.assertEqual(report.verdict, verify.INTACT)


# --- too short: the honest dead end ---------------------------------------

class TruncatedFilesTests(unittest.TestCase):
    def test_a_jpeg_with_no_end_marker(self):
        report = verify.inspect_bytes(jpeg()[:-2], "jpg")
        self.assertEqual(report.verdict, verify.TRUNCATED)
        self.assertFalse(report.repairable)

    def test_a_jpeg_shaped_lump_with_no_picture_in_it(self):
        """
        The real false positive: right first bytes, right last bytes, no
        frame header, no image. Three of these were recovered and reported
        as intact before the walker looked past the two ends.
        """
        report = verify.inspect_bytes(
            b"\xFF\xD8\xFF" + noise(3000, seed=41) + b"\xFF\xD9", "jpg")
        self.assertEqual(report.verdict, verify.WRONG_FORMAT)
        self.assertIn("no picture", report.detail)

    def test_a_zip_with_no_index(self):
        report = verify.inspect_bytes(b"PK\x03\x04" + b"Z" * 5000, "zip")
        self.assertEqual(report.verdict, verify.TRUNCATED)
        self.assertIn("index", report.detail)

    def test_a_png_cut_mid_chunk(self):
        report = verify.inspect_bytes(png()[:-20], "png")
        self.assertEqual(report.verdict, verify.TRUNCATED)

    def test_the_video_failure_that_started_this(self):
        """
        Valid header, `mdat` claiming far more than is present, no index.
        The header check calls this intact; this one must not.
        """
        header = struct.pack(">I", 32) + b"ftyp" + b"isom" + b"\x00" * 20
        mdat = struct.pack(">I", 420_000_000) + b"mdat"
        data = header + mdat + b"V" * 10_000

        self.assertEqual(verify.inspect_bytes(data, "mp4").verdict,
                         verify.TRUNCATED)
        detail = verify.inspect_bytes(data, "mp4").detail
        self.assertIn("index", detail)
        self.assertNotIn("mdat", detail)          # plain language, no jargon

    def test_a_complete_video_with_no_index_is_still_unplayable(self):
        report = verify.inspect_bytes(mp4(with_index=False), "mp4")
        self.assertEqual(report.verdict, verify.TRUNCATED)
        self.assertIn("index", report.detail)


# --- wrong and damaged -----------------------------------------------------

class WrongAndDamagedTests(unittest.TestCase):
    def test_noise_named_as_a_real_format(self):
        for extension in ("jpg", "png", "zip", "mp4", "pdf", "gif"):
            report = verify.inspect_bytes(noise(20_000, seed=3), extension)
            self.assertEqual(report.verdict, verify.WRONG_FORMAT, extension)

    def test_a_png_with_a_corrupted_chunk(self):
        data = bytearray(png())
        data[30] ^= 0xFF                    # flip a byte inside IDAT
        report = verify.inspect_bytes(bytes(data), "png")
        self.assertEqual(report.verdict, verify.DAMAGED)

    def test_an_empty_file(self):
        self.assertEqual(verify.inspect_bytes(b"", "jpg").verdict,
                         verify.WRONG_FORMAT)

    def test_unknown_types_get_no_verdict(self):
        for extension in ("dat", "bin", "", None, "xyz"):
            self.assertEqual(verify.inspect_bytes(b"anything", extension).verdict,
                             verify.UNKNOWN)

    def test_a_validator_that_falls_over_yields_no_verdict(self):
        """A crash in a walker is not an opinion about the file."""
        original = verify._CHECKS["jpg"]
        verify._CHECKS["jpg"] = lambda data: 1 / 0
        self.addCleanup(verify._CHECKS.__setitem__, "jpg", original)
        self.assertEqual(verify.inspect_bytes(jpeg(), "jpg").verdict,
                         verify.UNKNOWN)


# --- trimming --------------------------------------------------------------

class TrimTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="recoverkit-trim-")
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.good = jpeg()
        self.path = os.path.join(self.dir, "photo.jpg")
        with open(self.path, "wb") as fh:
            fh.write(self.good + noise(9000, seed=5))

    def test_the_trimmed_copy_is_exactly_the_real_file(self):
        out = verify.trim_copy(self.path)
        self.assertIsNotNone(out)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), self.good)


    def test_the_trimmed_copy_now_validates(self):
        out = verify.trim_copy(self.path)
        self.assertEqual(verify.inspect_file(out).verdict, verify.INTACT)

    def test_the_original_is_never_modified(self):
        with open(self.path, "rb") as fh:
            before = fh.read()
        verify.trim_copy(self.path)
        with open(self.path, "rb") as fh:
            self.assertEqual(fh.read(), before,
                             "trimming altered the file it was given")

    def test_nothing_existing_is_ever_overwritten(self):
        first = verify.trim_copy(self.path)
        second = verify.trim_copy(self.path)
        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first))

    def test_a_file_that_needs_no_trimming_is_left_alone(self):
        path = os.path.join(self.dir, "fine.jpg")
        with open(path, "wb") as fh:
            fh.write(self.good)
        self.assertIsNone(verify.trim_copy(path))

    def test_a_truncated_file_is_not_silently_trimmed(self):
        path = os.path.join(self.dir, "short.jpg")
        with open(path, "wb") as fh:
            fh.write(b"\xFF\xD8\xFF\xE0" + b"J" * 3000)
        self.assertIsNone(verify.trim_copy(path),
                          "a file that is too short must not be 'repaired'")


class SummaryTests(unittest.TestCase):
    def test_unknown_types_do_not_pad_the_good_news(self):
        reports = [verify.inspect_bytes(jpeg(), "jpg"),
                   verify.inspect_bytes(b"whatever", "dat"),
                   verify.inspect_bytes(b"whatever", "bin")]
        text = verify.summarise(reports)
        self.assertIn("1 of 1", text)

    def test_it_says_when_nothing_could_be_checked(self):
        reports = [verify.inspect_bytes(b"x", "dat")]
        self.assertIn("None of these", verify.summarise(reports))

    def test_it_counts_the_three_outcomes_separately(self):
        reports = [verify.inspect_bytes(jpeg(), "jpg"),
                   verify.inspect_bytes(jpeg() + noise(500, seed=2), "jpg"),
                   verify.inspect_bytes(b"\xFF\xD8\xFF" + b"J" * 900, "jpg")]
        text = verify.summarise(reports)
        self.assertIn("trimmed", text)
        self.assertIn("incomplete or damaged", text)


if __name__ == "__main__":
    unittest.main()
