"""
recovery.py - writing a recovered file out safely.

Small, and deliberately shared. Two things now save recovered files - the
window and the headless service - and the rules about where a file may land
are not something to have two copies of. Every one of them exists because
getting it wrong destroys data:

  * Nothing already on disk is ever overwritten. A name that collides becomes
    `name (2).ext`.
  * A recovered folder path is treated as untrusted text. It came off a
    damaged filesystem, and `..\\..\\etc` is a perfectly legal thing to find
    in a deleted directory entry.
  * Nothing is written outside the folder the user chose.
  * What we write belongs to the person who ran the tool, not to root.

Nothing here touches a source drive; it only ever writes into the
destination.
"""

import os

# Characters no filesystem here will take, plus the ones that would let a
# recovered name escape the folder it is meant to land in.
BAD_CHARACTERS = '<>:"/\\|?*'

# Path values the engines use to mean "no folder", which must not become one.
NOT_A_FOLDER = ("\\", "/", "", "(no folder - carved)")


def safe_name(name):
    """Turn a recovered filename into one this filesystem will accept."""
    cleaned = "".join("_" if c in BAD_CHARACTERS or ord(c) < 32 else c
                      for c in name or "")
    # Trailing dots and spaces only: Windows will not accept them, while a
    # *leading* dot is a perfectly ordinary filename that people care about
    # getting back. Stripping both ends would quietly rename .gitignore.
    cleaned = cleaned.strip().rstrip(". ")
    if cleaned in ("", ".", ".."):
        return "recovered_file"
    return cleaned


def unique_path(path):
    """`name.ext` -> `name (2).ext`. Never returns a path that exists."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def folder_for(destination, recovered_path):
    """
    Where a recovered file's folder should be recreated.

    The folder name comes off the damaged drive, so every part of it is
    scrubbed and the result is confirmed to be inside the destination. A
    deleted directory entry saying `..` is not a reason to write into the
    parent of the folder the user picked.
    """
    destination = os.path.abspath(destination)
    if not recovered_path or recovered_path in NOT_A_FOLDER:
        return destination

    parts = [safe_name(part)
             for part in recovered_path.replace("\\", "/").split("/")
             if part and part not in (".", "..")]
    if not parts:
        return destination

    folder = os.path.abspath(os.path.join(destination, *parts))
    if folder != destination and not folder.startswith(
            destination.rstrip(os.sep) + os.sep):
        return destination                  # tried to climb out - refuse
    return folder


def invoking_user():
    """
    The (uid, gid) of whoever ran `sudo`, or None when that doesn't apply.

    Reading a raw device needs root, so on macOS and Linux this tool is run
    with sudo - and every file it saved came out owned by root. The files
    opened fine, but the person who recovered them could not rename, move or
    delete their own photographs without being asked for an admin password,
    because deleting a file needs write permission on the folder holding it.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None                        # not root, so nothing to hand over
    uid = os.environ.get("SUDO_UID")
    if not uid:
        return None                        # a genuine root session, not sudo
    try:
        return int(uid), int(os.environ.get("SUDO_GID") or -1)
    except ValueError:
        return None


def hand_to_user(path, owner=None):
    """Give one recovered file or folder back to the user who ran sudo."""
    owner = invoking_user() if owner is None else owner
    if not owner:
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError:
        # Ownership is a convenience. A recovered file that landed but is
        # owned by root is still a recovered file, and losing it over a
        # failed chown would be the worse outcome by far.
        pass


def make_folder(folder):
    """
    Create a folder, handing back every level we had to create.

    Only the levels this call creates - a folder that was already there
    belongs to whoever made it, and its ownership is none of our business.
    """
    owner = invoking_user()
    missing = []
    if owner:
        probe = os.path.abspath(folder)
        while not os.path.isdir(probe):
            missing.append(probe)
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
    os.makedirs(folder, exist_ok=True)
    for path in reversed(missing):
        hand_to_user(path, owner)


def write(destination, recovered_path, name, data):
    """
    Save one recovered file. Returns the path written.

    Creates the original folder structure underneath `destination`, never
    outside it, and never over the top of anything already there.
    """
    folder = folder_for(destination, recovered_path)
    make_folder(folder)
    target = unique_path(os.path.join(folder, safe_name(name)))
    with open(target, "wb") as out:
        out.write(data)
    hand_to_user(target)
    return target
