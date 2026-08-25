#!/usr/bin/env python3
"""
service.py - the recovery engines behind a line protocol.

WHY
---
Today the whole application runs as root: 1,385 lines of interface code hold
raw device access they have no use for. This is the half that actually needs
it - small, does one thing, and can be the only elevated part of a future
version whose window is not privileged at all. See PORTING.md.

It is also just useful. The engines become scriptable by anything that can
write a line of JSON.

THE PROTOCOL
------------
One JSON object per line, in and out. Requests on stdin:

    {"op": "volumes"}
    {"op": "scan", "device": "/dev/rdisk2s1", "mode": "undelete"}
    {"op": "scan", "device": "/dev/rdisk2s1", "mode": "carve",
     "types": ["jpg", "png"]}
    {"op": "recover", "ids": [3, 7], "dest": "/Users/me/Recovered"}
    {"op": "stop"}

Events on stdout:

    {"event": "ready", "version": "1.0"}
    {"event": "volumes", "volumes": [{"label": "...", "device": "..."}]}
    {"event": "progress", "done": 4096, "total": 3900000000}
    {"event": "batch", "files": [{"id": 0, "name": "IMG_0042.JPG", ...}]}
    {"event": "done", "found": 12}
    {"event": "recovered", "id": 3, "path": "...", "verdict": "intact"}
    {"event": "error", "message": "plain english, safe to show a user"}

`total` of 0 means the drive would not say how big it is - report bytes read
rather than inventing a percentage.

WHAT STAYS ON THIS SIDE
-----------------------
The same-drive check and the destination guard. A caller that can ask for an
arbitrary recovery path must not also be the thing deciding whether that path
is safe - that is the entire point of splitting the two apart.
"""

import json
import os
import sys
import threading

import carve
import diskio
import recovery
import verify
from exfat import ExfatVolume
from ntfs import NtfsVolume

VERSION = "1.0"
BATCH = 40


def _plain(value):
    """Make one field safe to put in JSON."""
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ", timespec="seconds")
    return value


def describe(index, found):
    """One recovered file, as the wire sees it."""
    return {
        "id": index,
        "name": found.name,
        "path": found.path or "",
        "size": found.size or 0,
        "chance": found.chance if found.chance is not None else 100,
        "extension": found.extension,
        "is_dir": bool(found.is_dir),
        "deleted_at": _plain(getattr(found, "deleted_at", None)),
        "created_at": _plain(getattr(found, "created_at", None)),
        "content_check": getattr(found, "content_check", None),
        "still_at": getattr(found, "still_at", None),
    }


class Service:
    """
    Handles one request at a time, with scans running on a worker thread so
    a `stop` sent mid-scan is actually read.
    """

    def __init__(self, emit):
        self._emit = emit
        self._lock = threading.Lock()
        self.disk = None
        self.volume = None
        self.found = []
        self.stop_flag = threading.Event()
        self.worker = None

    # -- output -------------------------------------------------------------
    def emit(self, **event):
        """One writer, under a lock: a torn line is an unparseable line."""
        with self._lock:
            self._emit(event)

    def fail(self, message):
        self.emit(event="error", message=str(message))

    # -- input --------------------------------------------------------------
    def handle(self, line):
        line = line.strip()
        if not line:
            return
        try:
            request = json.loads(line)
        except ValueError:
            return self.fail("That request was not valid JSON.")
        if not isinstance(request, dict):
            return self.fail("A request must be a JSON object.")

        operation = request.get("op")
        handler = getattr(self, f"_op_{operation}", None) if operation else None
        if handler is None:
            return self.fail(f"Don't know how to {operation!r}.")
        try:
            handler(request)
        except Exception as problem:                # never take the service down
            self.fail(problem)

    # -- operations ---------------------------------------------------------
    def _op_ping(self, _request):
        self.emit(event="pong", version=VERSION)

    def _op_volumes(self, _request):
        diskio.refresh()
        self.emit(event="volumes",
                  volumes=[{"label": label, "device": device}
                           for label, device in diskio.list_volumes()])

    def _op_stop(self, _request):
        self.stop_flag.set()

    def _op_scan(self, request):
        if self.worker and self.worker.is_alive():
            return self.fail("A scan is already running.")
        device = request.get("device")
        if not device:
            return self.fail("No drive was given to scan.")

        self.found = []
        self.stop_flag.clear()
        self.worker = threading.Thread(
            target=self._scan, args=(device, request.get("mode", "undelete"),
                                     request.get("types") or []), daemon=True)
        self.worker.start()

    def _scan(self, device, mode, types):
        try:
            if self.disk is not None:
                self.disk.close()
            self.disk = diskio.ReadOnlyDisk(device)
            self.volume = None

            if mode == "carve":
                self._carve(types)
            else:
                self._undelete()
        except (PermissionError, FileNotFoundError, ValueError) as problem:
            self.fail(problem)
        except Exception as problem:
            self.fail(f"Unexpected problem: {problem}")
        finally:
            self.emit(event="done", found=len(self.found),
                      stopped=self.stop_flag.is_set())

    def _undelete(self):
        try:
            self.volume = NtfsVolume(self.disk)
        except ValueError:
            self.volume = ExfatVolume(self.disk)   # its message reaches the user
        results = self.volume.scan(
            progress=lambda done, total: self.emit(
                event="progress", done=done, total=total),
            should_stop=self.stop_flag.is_set)
        self._collect(results)

    def _carve(self, types):
        batch = []
        for found in carve.scan(
                self.disk, types,
                progress=lambda done, total: self.emit(
                    event="progress", done=done, total=total),
                should_stop=self.stop_flag.is_set):
            batch.append(found)
            if len(batch) >= BATCH:
                self._collect(batch)
                batch = []
        if batch:
            self._collect(batch)

    def _collect(self, results):
        """Give the results ids and send them on."""
        start = len(self.found)
        self.found.extend(results)
        self.emit(event="batch",
                  files=[describe(start + i, f) for i, f in enumerate(results)])

    def _op_recover(self, request):
        destination = request.get("dest")
        if not destination:
            return self.fail("No folder was given to recover into.")
        ids = request.get("ids")
        if ids is None:
            ids = list(range(len(self.found)))

        source = getattr(self.disk, "path", None)
        # The guard lives here, not in whatever asked. Writing recovered files
        # onto the drive being read is how people lose the data they came for.
        if source and diskio.same_physical_drive(source, destination):
            return self.fail(
                "That folder is on the drive being scanned. Recovering there "
                "would overwrite the very data you are trying to get back. "
                "Choose a folder on a different drive.")

        saved = 0
        for index in ids:
            if not isinstance(index, int) or not 0 <= index < len(self.found):
                self.fail(f"No file with id {index!r}.")
                continue
            found = self.found[index]
            if found.is_dir:
                continue
            try:
                data = (self.volume.read_file(found) if self.volume
                        else carve.read_file(self.disk, found))
                if not data:
                    self.emit(event="recovered", id=index, path=None,
                              verdict="empty")
                    continue
                path = recovery.write(destination, found.path, found.name, data)
                report = verify.inspect_file(path)
                self.emit(event="recovered", id=index, path=path,
                          verdict=report.verdict, detail=report.detail)
                saved += 1
            except Exception as problem:
                self.emit(event="recovered", id=index, path=None,
                          verdict="failed", detail=str(problem))
        self.emit(event="recovered_all", saved=saved, asked=len(ids))

    def close(self):
        self.stop_flag.set()
        if self.disk is not None:
            self.disk.close()
            self.disk = None


def main(stdin=None, stdout=None):
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def write(event):
        stdout.write(json.dumps(event) + "\n")
        stdout.flush()

    service = Service(write)
    service.emit(event="ready", version=VERSION)
    try:
        for line in stdin:
            service.handle(line)
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
