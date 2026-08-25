"""
Tests for the line protocol that puts the engines behind a process boundary.

This is phase 1 of the Tauri port (see PORTING.md). The point of doing it
first is that it proves the protocol carries everything an interface needs
before any interface is rewritten - and the checks that protect the user's
data have to survive the move, because a caller on the far side of a pipe is
not something to trust with them.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import service
from tests import exfat_image, ntfs_image
from tests.support import ImageTestCase, md5, sample_jpeg

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Driver:
    """Runs a Service in-process and records everything it emits."""

    def __init__(self):
        self.events = []
        self.service = service.Service(self.events.append)

    def send(self, **request):
        self.service.handle(json.dumps(request))
        return self

    def finish(self, timeout=30):
        worker = self.service.worker
        if worker:
            worker.join(timeout)
        return self

    def of(self, kind):
        return [e for e in self.events if e["event"] == kind]

    def one(self, kind):
        matching = self.of(kind)
        assert matching, f"no {kind!r} event in {[e['event'] for e in self.events]}"
        return matching[0]

    def files(self):
        return [f for batch in self.of("batch") for f in batch["files"]]


class RequestHandlingTests(unittest.TestCase):
    """A caller on the far side of a pipe can send anything at all."""

    def setUp(self):
        self.driver = Driver()
        self.addCleanup(self.driver.service.close)

    def test_it_answers_a_ping(self):
        self.driver.send(op="ping")
        self.assertEqual(self.driver.one("pong")["version"], service.VERSION)

    def test_broken_json_is_an_error_not_a_crash(self):
        self.driver.service.handle("{not json at all")
        self.assertIn("JSON", self.driver.one("error")["message"])

    def test_a_bare_value_is_rejected(self):
        self.driver.service.handle('"just a string"')
        self.assertTrue(self.driver.of("error"))

    def test_an_unknown_operation_says_so(self):
        self.driver.send(op="obliterate")
        self.assertIn("obliterate", self.driver.one("error")["message"])

    def test_blank_lines_are_ignored(self):
        self.driver.service.handle("")
        self.driver.service.handle("   \n")
        self.assertEqual(self.driver.events, [])

    def test_scanning_nothing_is_an_error(self):
        self.driver.send(op="scan")
        self.assertIn("drive", self.driver.one("error")["message"].lower())

    def test_a_missing_device_is_reported_in_plain_language(self):
        self.driver.send(op="scan", device="/dev/no-such-device").finish()
        message = self.driver.one("error")["message"]
        self.assertIn("not found", message.lower())
        self.assertTrue(self.driver.of("done"), "the scan never finished")

    def test_volumes_come_back_as_label_and_device(self):
        self.driver.send(op="volumes")
        for volume in self.driver.one("volumes")["volumes"]:
            self.assertIn("label", volume)
            self.assertIn("device", volume)


class ScanTests(ImageTestCase):
    def setUp(self):
        image = ntfs_image.NtfsImage()
        documents = image.add_dir("Documents")
        self.photo = sample_jpeg(b"P" * 40_000)
        image.add_file("holiday.jpg", self.photo, parent=documents,
                       deleted=True)
        image.add_file("notes.txt", b"a short note", deleted=True,
                       resident=True)
        image.add_file("keepme.txt", b"live" * 100, deleted=False)
        self.use_image(image.build(), suffix=".ntfs")

        self.driver = Driver()
        self.addCleanup(self.driver.service.close)

    def scan(self, **kw):
        self.driver.send(op="scan", device=self.image_path, **kw).finish()
        return self.driver

    def test_a_scan_reports_what_it_found(self):
        files = self.scan().files()
        names = {f["name"] for f in files}
        self.assertIn("holiday.jpg", names)
        self.assertNotIn("keepme.txt", names, "a live file was offered")

    def test_every_field_the_interface_needs_survives_the_wire(self):
        found = next(f for f in self.scan().files()
                     if f["name"] == "holiday.jpg")
        for field in ("id", "name", "path", "size", "chance", "extension",
                      "is_dir", "deleted_at", "created_at", "content_check"):
            self.assertIn(field, found)
        self.assertEqual(found["path"], "Documents")
        self.assertEqual(found["extension"], "jpg")

    def test_everything_sent_is_json(self):
        """A value that will not serialise takes the whole service down."""
        self.scan()
        json.dumps(self.driver.events)

    def test_dates_cross_the_wire_as_text(self):
        found = next(f for f in self.scan().files()
                     if f["name"] == "holiday.jpg")
        self.assertIsInstance(found["created_at"], str)

    def test_the_scan_finishes_even_with_nothing_to_find(self):
        self.use_image(b"\x00" * (1024 * 1024))
        self.driver.send(op="scan", device=self.image_path).finish()
        self.assertTrue(self.driver.of("error"))
        self.assertTrue(self.driver.of("done"))
        # Windows will not delete a file that is still open, and this test
        # swapped the image out from under a service that had the first one
        # held. Closing here rather than leaving it to cleanup, which runs in
        # the wrong order for a second image.
        self.driver.service.close()

    def test_two_scans_at_once_are_refused(self):
        self.driver.send(op="scan", device=self.image_path)
        self.driver.send(op="scan", device=self.image_path)
        self.driver.finish()
        self.assertTrue(any("already running" in e["message"]
                            for e in self.driver.of("error")))

    def test_stop_raises_the_flag(self):
        self.driver.send(op="stop")
        self.assertTrue(self.driver.service.stop_flag.is_set())

    def test_starting_a_scan_clears_a_stale_stop(self):
        """Otherwise one stop would poison every scan after it."""
        self.driver.send(op="stop")
        self.driver.send(op="scan", device=self.image_path).finish()
        self.assertFalse(self.driver.one("done")["stopped"])
        self.assertTrue(self.driver.files())

    def test_the_engine_is_given_a_way_to_be_interrupted(self):
        """
        Asserted by handing the service a stand-in engine, rather than by
        starting a real scan and racing it: a scan of a small image finishes
        in milliseconds, and a test that has to win a race is a test that
        will fail on someone else's machine.
        """
        seen = {}
        flag = self.driver.service.stop_flag

        class Interruptible:
            def __init__(self, disk):
                pass

            def scan(self, progress=None, should_stop=None):
                seen["asked"] = should_stop is not None
                flag.set()                      # as a user pressing Stop would
                seen["honoured"] = bool(should_stop and should_stop())
                return []

        original = service.NtfsVolume
        service.NtfsVolume = Interruptible
        self.addCleanup(setattr, service, "NtfsVolume", original)

        self.driver.send(op="scan", device=self.image_path).finish()

        self.assertTrue(seen.get("asked"), "the engine got no way to stop")
        self.assertTrue(seen.get("honoured"))
        self.assertTrue(self.driver.one("done")["stopped"])


class RecoverTests(ImageTestCase):
    def setUp(self):
        image = ntfs_image.NtfsImage()
        documents = image.add_dir("Documents")
        self.photo = sample_jpeg(b"P" * 40_000)
        image.add_file("holiday.jpg", self.photo, parent=documents,
                       deleted=True)
        self.use_image(image.build(), suffix=".ntfs")
        self.dest = tempfile.mkdtemp(prefix="recoverkit-service-")
        self.addCleanup(shutil.rmtree, self.dest, True)

        self.driver = Driver()
        self.addCleanup(self.driver.service.close)
        self.driver.send(op="scan", device=self.image_path).finish()

    def test_a_recovered_file_is_byte_identical(self):
        self.driver.send(op="recover", ids=[0], dest=self.dest)
        event = self.driver.one("recovered")
        self.assertTrue(event["path"])
        with open(event["path"], "rb") as fh:
            self.assertEqual(md5(fh.read()), md5(self.photo))

    def test_the_original_folder_is_recreated(self):
        self.driver.send(op="recover", ids=[0], dest=self.dest)
        self.assertTrue(os.path.exists(
            os.path.join(self.dest, "Documents", "holiday.jpg")))

    def test_it_says_whether_what_it_saved_is_whole(self):
        self.driver.send(op="recover", ids=[0], dest=self.dest)
        self.assertEqual(self.driver.one("recovered")["verdict"], "intact")

    def test_recovering_everything_is_the_default(self):
        self.driver.send(op="recover", dest=self.dest)
        self.assertGreaterEqual(self.driver.one("recovered_all")["saved"], 1)

    def test_an_id_that_does_not_exist_is_reported_not_fatal(self):
        self.driver.send(op="recover", ids=[999], dest=self.dest)
        self.assertTrue(self.driver.of("error"))
        self.assertTrue(self.driver.of("recovered_all"))

    def test_recovering_nowhere_is_an_error(self):
        self.driver.send(op="recover", ids=[0])
        self.assertIn("folder", self.driver.one("error")["message"].lower())

    def test_nothing_existing_is_overwritten(self):
        self.driver.send(op="recover", ids=[0], dest=self.dest)
        self.driver.send(op="recover", ids=[0], dest=self.dest)
        saved = [e["path"] for e in self.driver.of("recovered") if e["path"]]
        self.assertEqual(len(set(saved)), 2, "the second write reused a name")


class GuardTests(ImageTestCase):
    """
    The same-drive check stays on this side of the pipe. A caller that can
    name any folder must not also be the thing deciding whether that folder
    is safe.
    """

    def setUp(self):
        image = ntfs_image.NtfsImage()
        image.add_file("x.jpg", sample_jpeg(), deleted=True)
        self.use_image(image.build(), suffix=".ntfs")
        self.driver = Driver()
        self.addCleanup(self.driver.service.close)
        self.driver.send(op="scan", device=self.image_path).finish()

    def test_the_service_refuses_a_destination_on_the_source_drive(self):
        original = service.diskio.same_physical_drive
        service.diskio.same_physical_drive = lambda source, dest: True
        self.addCleanup(setattr, service.diskio, "same_physical_drive",
                        original)

        destination = tempfile.mkdtemp(prefix="recoverkit-guard-")
        self.addCleanup(shutil.rmtree, destination, True)
        self.driver.send(op="recover", ids=[0], dest=destination)

        self.assertTrue(self.driver.of("error"))
        self.assertFalse(self.driver.of("recovered"),
                         "a file was written despite the guard")
        self.assertEqual(os.listdir(destination), [])


class ProcessTests(unittest.TestCase):
    """The real thing: a separate process, spoken to over pipes."""

    def test_it_greets_and_answers_over_a_pipe(self):
        result = subprocess.run(
            [sys.executable, "service.py"], cwd=ROOT, text=True, timeout=60,
            input='{"op":"ping"}\n{"op":"volumes"}\n',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, 0, result.stderr)

        events = [json.loads(line) for line in result.stdout.splitlines()]
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds[0], "ready")
        self.assertIn("pong", kinds)
        self.assertIn("volumes", kinds)

    def test_nothing_but_json_is_ever_written_to_stdout(self):
        """
        A stray print would make the stream unparseable for whatever is
        reading it, and the failure would look like a protocol bug.
        """
        result = subprocess.run(
            [sys.executable, "service.py"], cwd=ROOT, text=True, timeout=60,
            input='{"op":"ping"}\n{"op":"nonsense"}\nnot json\n',
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for line in result.stdout.splitlines():
            json.loads(line)


if __name__ == "__main__":
    unittest.main()


class RecoveryPathTests(unittest.TestCase):
    """
    Where a recovered file is allowed to land.

    The folder name comes off a damaged filesystem, so it is untrusted text.
    A deleted directory entry saying `..\\..\\..` is a perfectly legal thing
    to find, and writing where it points would put files outside the folder
    the user chose.
    """

    def setUp(self):
        import recovery
        self.recovery = recovery
        self.dest = tempfile.mkdtemp(prefix="recoverkit-paths-")
        self.addCleanup(shutil.rmtree, self.dest, True)

    def test_an_ordinary_folder_is_recreated(self):
        folder = self.recovery.folder_for(self.dest, "Documents\\Reports")
        self.assertEqual(folder, os.path.join(self.dest, "Documents",
                                              "Reports"))

    def test_a_path_that_climbs_out_stays_inside(self):
        """
        The climbing parts are dropped rather than followed. Whatever is left
        is still recreated, so `..\\..\\etc` lands in `etc` inside the
        destination - which keeps the folder structure without ever leaving
        the folder the user picked.
        """
        inside = os.path.abspath(self.dest)
        for climb in ("..\\..\\etc", "../../..", "..", "\\..\\..\\Users"):
            folder = os.path.abspath(
                self.recovery.folder_for(self.dest, climb))
            self.assertTrue(folder == inside
                            or folder.startswith(inside + os.sep),
                            f"{climb!r} escaped to {folder}")

    def test_only_climbing_leaves_nothing_to_recreate(self):
        for climb in ("../../..", "..", "..\\.."):
            self.assertEqual(self.recovery.folder_for(self.dest, climb),
                             os.path.abspath(self.dest))

    def test_an_absolute_path_does_not_become_absolute(self):
        folder = self.recovery.folder_for(self.dest, "\\Windows\\System32")
        self.assertTrue(folder.startswith(os.path.abspath(self.dest)))

    def test_the_engines_no_folder_markers_mean_the_destination_itself(self):
        for empty in ("\\", "", None, "(no folder - carved)"):
            self.assertEqual(self.recovery.folder_for(self.dest, empty),
                             os.path.abspath(self.dest))

    def test_a_hostile_name_is_scrubbed(self):
        for name, expected in (("../../x.jpg", ".._.._x.jpg"),
                               ("a/b.jpg", "a_b.jpg"),
                               ("", "recovered_file"),
                               ("..", "recovered_file"),
                               ("...", "recovered_file"),
                               ("report.  ", "report")):
            self.assertEqual(self.recovery.safe_name(name), expected, name)

    def test_a_leading_dot_is_a_filename_not_an_attack(self):
        """Scrubbing both ends would quietly rename every dotfile."""
        self.assertEqual(self.recovery.safe_name(".gitignore"), ".gitignore")
        self.assertEqual(self.recovery.safe_name(".env.local"), ".env.local")

    def test_writing_stays_inside_the_destination(self):
        path = self.recovery.write(self.dest, "..\\..\\escape", "x.txt",
                                   b"data")
        self.assertTrue(os.path.abspath(path).startswith(
            os.path.abspath(self.dest)))
