"""
Test suite for RecoverKit.

Run everything:

    python3 -m unittest discover -v

The tests are stdlib-only and hermetic by default: the NTFS and exFAT images
they scan are built byte by byte in Python (see ntfs_image.py and
exfat_image.py), so the suite runs the same on Windows, macOS and Linux with
no filesystem tools installed and no root.

The manual procedure in CLAUDE.md - build a real volume with mkntfs/ntfs-3g,
delete files, scan, compare checksums - is also automated, in
test_integration_ntfs.py and test_integration_exfat.py. Those tests need
external tools and mount a filesystem, so they only run when you ask:

    RECOVERKIT_INTEGRATION=1 python3 -m unittest discover -v

Otherwise they report as skipped, with the reason.
"""
