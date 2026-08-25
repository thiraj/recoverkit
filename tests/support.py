"""
Shared test helpers.

The important one is `frozen_image`: it hashes an image file before a test
touches it and again afterwards, and fails if a single byte moved. Every test
that opens a source image goes through it, so the "never write to the source"
invariant is checked continuously rather than in one dedicated test.
"""

import hashlib
import os
import random
import sys
import tempfile
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import diskio  # noqa: E402


def noise(length, seed=0):
    """
    Deterministic filler bytes that cannot contain a file signature.

    Random filler looks tempting for carver tests and is a trap: 8MB of
    os.urandom contains a stray \xFF\xD8\xFF about half the time, and the
    test then fails for a reason that has nothing to do with the code. Every
    byte here is in 0x01-0x7E, which no signature in carve.SIGNATURES uses.
    """
    generator = random.Random(seed)
    return bytes(generator.randrange(0x01, 0x7F) for _ in range(length))


def md5(data):
    return hashlib.md5(data).hexdigest()


def md5_file(path):
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_temp_image(data, suffix=".img"):
    """Write `data` to a temp file and return its path. Caller deletes it."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


class ImageTestCase(unittest.TestCase):
    """
    Base class for tests that scan a disk image.

    Subclasses set `self.image_path` in setUp via `use_image`. The source hash
    is captured then and re-checked in tearDown.
    """

    def use_image(self, data_or_path, suffix=".img"):
        if isinstance(data_or_path, bytes):
            path = write_temp_image(data_or_path, suffix)
            self.addCleanup(os.unlink, path)
        else:
            path = data_or_path
        self.image_path = path
        # Bound to this particular image, so a test that opens a second one
        # still gets both checked.
        self.addCleanup(self.assert_unchanged, path, md5_file(path),
                        os.path.getsize(path))
        return path

    def assert_unchanged(self, path, expected_hash, expected_size):
        self.assertEqual(
            os.path.getsize(path), expected_size,
            f"{os.path.basename(path)} changed size during the test")
        self.assertEqual(
            md5_file(path), expected_hash,
            "THE SOURCE IMAGE WAS MODIFIED - this is the one bug class that "
            "must never happen")

    def disk(self, sector_size=512):
        """An open read-only handle, closed when the test finishes."""
        handle = diskio.ReadOnlyDisk(self.image_path, sector_size=sector_size)
        self.addCleanup(handle.close)
        return handle

    @contextmanager
    def open_disk(self, sector_size=512):
        disk = diskio.ReadOnlyDisk(self.image_path, sector_size=sector_size)
        try:
            yield disk
        finally:
            disk.close()


@contextmanager
def frozen_image(test, path):
    """Assert `path` is byte-identical before and after the block."""
    before = md5_file(path)
    size = os.path.getsize(path)
    yield
    test.assertEqual(os.path.getsize(path), size,
                     "the source image changed size")
    test.assertEqual(md5_file(path), before, "THE SOURCE IMAGE WAS MODIFIED")
