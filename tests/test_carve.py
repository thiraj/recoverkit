"""
Tests for the signature carver.

Carving is the fallback for volumes whose own records are gone, so the bar is
different: it cannot know filenames, but what it does hand back must be the
real bytes, and it must not invent files out of noise.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import carve
from tests.support import ImageTestCase, md5, noise


def jpeg(payload):
    return b"\xFF\xD8\xFF" + payload + b"\xFF\xD9"


def png(payload):
    return b"\x89PNG\r\n\x1a\n" + payload + b"IEND\xaeB`\x82"


class CarveTests(ImageTestCase):
    def setUp(self):
        # Two images buried in noise, each starting on a different alignment
        # so the carver cannot rely on tidy boundaries.
        self.photo = jpeg(b"J" * 5000)
        self.picture = png(b"P" * 9000)

        blob = bytearray(noise(3000, seed=1))
        blob += self.photo
        blob += noise(1234, seed=2)
        blob += self.picture
        blob += noise(2000, seed=3)
        self.blob = bytes(blob)
        self.use_image(self.blob)

    def carve(self, types, **kw):
        return list(carve.scan(self.disk(), types, **kw))

    def test_carved_files_are_byte_identical(self):
        found = self.carve(["jpg", "png"])
        by_ext = {f.ext: f for f in found}

        self.assertIn("jpg", by_ext)
        self.assertEqual(md5(by_ext["jpg"].data), md5(self.photo))

        self.assertIn("png", by_ext)
        self.assertEqual(md5(by_ext["png"].data), md5(self.picture))

    def test_source_image_is_unchanged_by_a_carve(self):
        self.carve(["jpg", "png", "pdf", "zip"])
        # tearDown re-checks the hash; this test states the intent.

    def test_offsets_point_at_the_real_data(self):
        found = self.carve(["jpg"])
        target = found[0]
        self.assertEqual(self.blob[target.offset:target.offset + 3],
                         b"\xFF\xD8\xFF")

    def test_carved_files_carry_no_folder_and_a_generated_name(self):
        target = self.carve(["jpg"])[0]
        self.assertTrue(target.name.startswith("recovered_jpg_"))
        self.assertTrue(target.name.endswith(".jpg"))
        self.assertEqual(target.path, "(no folder - carved)")
        self.assertFalse(target.is_dir)
        self.assertEqual(target.extension, "jpg")

    def test_unrequested_types_are_not_returned(self):
        found = self.carve(["png"])
        self.assertEqual([f.ext for f in found], ["png"])

    def test_an_unknown_type_yields_nothing_rather_than_failing(self):
        self.assertEqual(self.carve(["not-a-real-format"]), [])

    def test_stop_is_honoured(self):
        self.assertEqual(self.carve(["jpg", "png"], should_stop=lambda: True),
                         [])

    def test_progress_is_reported(self):
        seen = []
        self.carve(["jpg"], progress=lambda done, total: seen.append(done))
        self.assertTrue(seen)


class CarveAcrossChunkBoundaryTests(ImageTestCase):
    """
    The carver reads in 8MB chunks and carries the tail of each one forward.
    A header landing exactly on that seam is the case that breaks naive
    implementations.
    """

    def setUp(self):
        self.photo = jpeg(b"J" * 20_000)
        blob = bytearray(noise(carve.CHUNK - 2, seed=4))
        blob += self.photo
        blob += noise(1000, seed=5)
        self.use_image(bytes(blob))

    def test_a_file_straddling_a_chunk_boundary_is_still_found_whole(self):
        found = list(carve.scan(self.disk(), ["jpg"]))
        self.assertEqual(len(found), 1, "the straddling file was missed")
        self.assertEqual(md5(found[0].data), md5(self.photo))


class CarveNoiseTests(ImageTestCase):
    def setUp(self):
        # A header with no matching footer anywhere: a false positive.
        self.use_image(noise(2000, seed=6) + b"\xFF\xD8\xFF"
                       + noise(2000, seed=7))

    def test_a_header_with_no_footer_is_not_reported(self):
        found = list(carve.scan(self.disk(), ["jpg"]))
        self.assertEqual(found, [],
                         "a stray header was presented as a recovered file")


if __name__ == "__main__":
    unittest.main()


class CarveInterleavedReadTests(ImageTestCase):
    """
    The carver walks the whole device sequentially while extracting files it
    finds along the way, so a sequential read and a random read are
    interleaved on one descriptor the entire time.

    When both relied on the shared file position, the first extraction pulled
    the walk off course and everything afterwards was read from the wrong
    offset. Real files were skipped and pure noise was returned in their
    place - reported, as everything carved was, at 100%.
    """

    def setUp(self):
        self.first = jpeg(b"A" * 30_000)
        self.second = jpeg(b"B" * 30_000)
        # Far enough apart that the walk must cross a chunk boundary between
        # them, with an extraction happening in between.
        blob = (noise(1_000_000, seed=11) + self.first
                + noise(carve.CHUNK, seed=12) + self.second
                + noise(500_000, seed=13))
        self.use_image(bytes(blob))

    def test_files_after_the_first_are_still_found(self):
        found = list(carve.scan(self.disk(), ["jpg"]))
        self.assertEqual(len(found), 2,
                         "extracting one file knocked the scan off course and "
                         "later files were missed")

    def test_and_they_are_byte_identical(self):
        found = sorted(carve.scan(self.disk(), ["jpg"]),
                       key=lambda f: f.offset)
        self.assertEqual(md5(found[0].data), md5(self.first))
        self.assertEqual(md5(found[1].data), md5(self.second))

    def test_reported_offsets_point_at_the_real_data(self):
        for f in carve.scan(self.disk(), ["jpg"]):
            self.assertEqual(self.blob_at(f.offset, 3), b"\xFF\xD8\xFF")

    def blob_at(self, offset, length):
        with open(self.image_path, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)


class CarveHonestyTests(ImageTestCase):
    """
    Carving cannot know whether a file is whole. It used to claim 100% for
    everything it returned, which is the same flattering guess the undelete
    engines were fixed for.
    """

    def setUp(self):
        self.use_image(noise(2000, seed=21) + jpeg(b"J" * 4000)
                       + noise(2000, seed=22) + b"PK\x03\x04" + b"Z" * 5000)

    def test_nothing_carved_is_promised_at_100_percent(self):
        for f in carve.scan(self.disk(), ["jpg", "zip"]):
            self.assertLess(f.chance, 100,
                            f"{f.name} was carved and promised as certain")

    def test_a_format_with_an_end_marker_scores_higher_than_one_without(self):
        """
        A JPEG's footer says where it ends. A ZIP has none, so it is cut at an
        arbitrary length and carries trailing junk.
        """
        found = {f.ext: f for f in carve.scan(self.disk(), ["jpg", "zip"])}
        self.assertGreater(found["jpg"].chance, found["zip"].chance)
