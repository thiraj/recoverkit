"""
Tests for who owns a recovered file once it lands.

Reading a raw device needs root, so on macOS and Linux the tool runs under
sudo - and everything it wrote used to come out owned by root. Nothing was
lost, but the person who had just recovered their photographs could not then
delete or move them without an admin password, because removing a file needs
write permission on the folder that holds it. Handing the files back is the
difference between "recovered" and "recovered and usable".

Nothing here runs as root. The tests put the process where sudo would have
put it - euid 0 with SUDO_UID set - and record the chown that results, which
is the whole of what the code decides.
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import recovery

UNIX_ONLY = unittest.skipUnless(hasattr(os, "geteuid"),
                                "ownership is a Unix idea")


class FakeSudo:
    """Puts the process where `sudo python3 app.py` would have put it."""

    def under_sudo(self, uid="501", gid="20"):
        env = {"SUDO_UID": uid} if gid is None else {"SUDO_UID": uid,
                                                     "SUDO_GID": gid}
        patches = [mock.patch.object(os, "geteuid", lambda: 0),
                   mock.patch.dict(os.environ, env)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def record_chowns(self, fail=False):
        self.chowns = []

        def chown(path, uid, gid):
            self.chowns.append((path, uid, gid))
            if fail:
                raise PermissionError(1, "Operation not permitted")

        p = mock.patch.object(os, "chown", chown)
        p.start()
        self.addCleanup(p.stop)
        return self.chowns


@UNIX_ONLY
class InvokingUserTests(FakeSudo, unittest.TestCase):

    def test_an_ordinary_run_hands_nothing_over(self):
        """Not root: the files already belong to the person who made them."""
        with mock.patch.object(os, "geteuid", lambda: 501):
            self.assertIsNone(recovery.invoking_user())

    def test_sudo_names_the_user_underneath(self):
        self.under_sudo(uid="501", gid="20")
        self.assertEqual(recovery.invoking_user(), (501, 20))

    def test_a_real_root_session_is_left_alone(self):
        """
        Logged in as root rather than sudo-ed: there is no other user to hand
        the files to, and guessing one would be worse than doing nothing.
        """
        with mock.patch.object(os, "geteuid", lambda: 0), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(recovery.invoking_user())

    def test_a_nonsense_sudo_uid_is_ignored_rather_than_crashing(self):
        with mock.patch.object(os, "geteuid", lambda: 0), \
                mock.patch.dict(os.environ, {"SUDO_UID": "nobody"}):
            self.assertIsNone(recovery.invoking_user())


@UNIX_ONLY
class RecoveredFileOwnershipTests(FakeSudo, unittest.TestCase):

    def setUp(self):
        self.dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dest, ignore_errors=True)

    def test_a_recovered_file_is_given_to_the_user(self):
        self.under_sudo()
        chowns = self.record_chowns()
        path = recovery.write(self.dest, "", "holiday.jpg", b"data")
        self.assertIn((path, 501, 20), chowns)

    def test_the_folders_it_creates_are_given_to_the_user_too(self):
        """
        Deleting a file needs write permission on the folder holding it, so
        a user-owned file inside a root-owned folder is still undeletable.
        """
        self.under_sudo()
        chowns = self.record_chowns()
        path = recovery.write(self.dest, "DCIM\\100CANON", "IMG_0001.JPG",
                              b"data")
        handed = [c[0] for c in chowns]
        self.assertIn(os.path.join(self.dest, "DCIM"), handed)
        self.assertIn(os.path.join(self.dest, "DCIM", "100CANON"), handed)
        self.assertIn(path, handed)

    def test_a_folder_that_was_already_there_is_left_as_it_was(self):
        """Its ownership belongs to whoever made it, not to us."""
        self.under_sudo()
        existing = os.path.join(self.dest, "Photos")
        os.makedirs(existing)
        chowns = self.record_chowns()
        recovery.write(self.dest, "Photos", "a.jpg", b"data")
        self.assertNotIn(existing, [c[0] for c in chowns])
        self.assertNotIn(self.dest, [c[0] for c in chowns])

    def test_the_file_still_lands_when_the_chown_fails(self):
        """
        Ownership is a convenience; the recovered bytes are the point. A
        destination on exFAT or a network share may refuse the chown outright.
        """
        self.under_sudo()
        self.record_chowns(fail=True)
        path = recovery.write(self.dest, "", "holiday.jpg", b"data")
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), b"data")

    def test_nothing_is_chowned_when_not_running_under_sudo(self):
        chowns = self.record_chowns()
        with mock.patch.object(os, "geteuid", lambda: 501):
            recovery.write(self.dest, "Photos", "a.jpg", b"data")
        self.assertEqual(chowns, [])

    def test_make_folder_hands_over_the_whole_new_branch(self):
        self.under_sudo()
        chowns = self.record_chowns()
        deep = os.path.join(self.dest, "one", "two", "three")
        recovery.make_folder(deep)
        self.assertTrue(os.path.isdir(deep))
        handed = [c[0] for c in chowns]
        self.assertEqual(handed, [os.path.join(self.dest, "one"),
                                  os.path.join(self.dest, "one", "two"),
                                  deep])


@UNIX_ONLY
class TrimmedCopyOwnershipTests(FakeSudo, unittest.TestCase):
    """A trimmed copy is a recovered file too, and lands the same way."""

    def test_a_trimmed_copy_is_given_to_the_user(self):
        import verify
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        # A complete zip with junk stuck on the end - what a deep scan
        # produces for a format that never says how long it is.
        import zipfile
        source = os.path.join(folder, "recovered_docx_00001.docx")
        with zipfile.ZipFile(source, "w") as zf:
            zf.writestr("word/document.xml", "<w:document/>")
        with open(source, "ab") as fh:
            fh.write(b"\x00" * 4096)

        self.under_sudo()
        chowns = self.record_chowns()
        trimmed = verify.trim_copy(source)
        self.assertIsNotNone(trimmed, "the junk tail should be trimmable")
        self.assertIn((trimmed, 501, 20), chowns)


if __name__ == "__main__":
    unittest.main()
