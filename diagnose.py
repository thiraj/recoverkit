"""
diagnose.py - explain what the scanner thinks about one file, and why.

Read-only, like everything else here. Works on NTFS and exFAT:

    sudo python3 diagnose.py /dev/rdisk2s1 "Liyathambara.mp3"

It prints the two independent pieces of evidence the recovery score is built
from - the allocation bitmap and the file's own first bytes - separately, so
a wrong score can be traced to whichever one is lying.
"""

import struct
import sys

import diskio
import exfat
import ntfs
import signatures

ATTR_ATTRIBUTE_LIST = 0x20
FLAG_COMPRESSED = 0x0001
FLAG_ENCRYPTED = 0x4000
FLAG_SPARSE = 0x8000


def open_volume(disk):
    try:
        return ntfs.NtfsVolume(disk), "NTFS"
    except ValueError:
        pass
    return exfat.ExfatVolume(disk), "exFAT"


def describe_ntfs_attributes(rec):
    """Report the things ntfs.py does not parse."""
    notes = []
    attr_offset = struct.unpack_from("<H", rec, 0x14)[0]
    used = min(struct.unpack_from("<I", rec, 0x18)[0], len(rec))
    pos = attr_offset
    while pos + 8 <= used:
        atype = struct.unpack_from("<I", rec, pos)[0]
        if atype == 0xFFFFFFFF:
            break
        alen = struct.unpack_from("<I", rec, pos + 4)[0]
        if alen == 0 or pos + alen > len(rec):
            break
        flags = struct.unpack_from("<H", rec, pos + 0x0C)[0]
        if atype == ATTR_ATTRIBUTE_LIST:
            notes.append("has an $ATTRIBUTE_LIST - part of its cluster map "
                         "lives in records this version does not follow")
        if atype == ntfs.ATTR_DATA and rec[pos + 9] == 0:
            if flags & FLAG_COMPRESSED:
                notes.append("$DATA is COMPRESSED and is not unpacked here")
            if flags & FLAG_ENCRYPTED:
                notes.append("$DATA is ENCRYPTED")
            if flags & FLAG_SPARSE:
                notes.append("$DATA is sparse")
            if rec[pos + 8]:
                start_vcn = struct.unpack_from("<Q", rec, pos + 0x10)[0]
                if start_vcn:
                    notes.append(f"this record's $DATA starts at cluster "
                                 f"{start_vcn} of the file, not at zero")
        pos += alen
    return notes


def report_bitmap(volume):
    """
    The bitmap is half the score. If it did not load, everything reads 100%;
    if it loaded but disagrees with what the OS reports as free, it is being
    read wrongly and everything is suspect in the other direction.
    """
    print("Allocation bitmap")
    bitmap = volume._bitmap
    if bitmap is None:
        print("  NOT LOADED - the scorer treats every cluster as free, so")
        print("  every file reads 100%. Any score below is meaningless.")
        print()
        return
    bits = len(bitmap) * 8
    used = sum(bin(b).count("1") for b in bitmap)
    free = bits - used
    size = volume.cluster_size
    print(f"  loaded: {len(bitmap):,} bytes covering {bits:,} clusters")
    print(f"  volume has {volume.total_clusters:,} clusters of {size:,} bytes")
    print(f"  allocated {used:,} ({used * size / 1024**3:.2f} GB)")
    print(f"  free      {free:,} ({free * size / 1024**3:.2f} GB)")
    print("  ^ compare 'free' with what Finder shows. A wild disagreement")
    print("    means the bitmap is being read from the wrong place.")
    print()


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    device, wanted = sys.argv[1], sys.argv[2]

    disk = diskio.ReadOnlyDisk(device)
    volume, kind = open_volume(disk)
    print(f"{kind} volume   cluster size {volume.cluster_size:,}   "
          f"sector size {volume.sector_size}\n")

    files = volume.scan()
    report_bitmap(volume)

    found = [f for f in files if wanted.lower() in f.name.lower()]
    if not found:
        print(f"No deleted file matching {wanted!r} among {len(files)} found.")
        return 1

    for f in found:
        print(f"{f.path}\\{f.name}")
        print(f"  size              {f.size:,} bytes")
        print(f"  SCORE SHOWN       {f.chance}%")
        print(f"  content check     {f.content_check}"
              f"   (extension {f.extension!r}, "
              f"known={signatures.known_extension(f.extension)})")
        if getattr(f, "assumed_contiguous", False):
            print("  layout            GUESSED - the chain was gone, so the "
                  "score is capped at 50")
        print(f"  fragments         {len(f.runs)}   first run "
              f"{f.runs[0] if f.runs else 'none'}")

        mapped = sum(c for lcn, c in f.runs if lcn is not None)
        needed = -(-f.size // volume.cluster_size) if f.size else 0
        print(f"  clusters mapped   {mapped} of {needed} needed"
              + ("   <- INCOMPLETE" if mapped < needed else ""))

        # Exactly what the scorer sampled, and how it voted.
        if f.runs:
            free = checked = 0
            first_used = None
            for lcn, count in f.runs:
                if lcn is None:
                    continue
                for c in range(0, count, max(1, count // 64)):
                    checked += 1
                    if volume._cluster_free(lcn + c):
                        free += 1
                    elif first_used is None:
                        first_used = lcn + c
            print(f"  bitmap says       {free} of {checked} sampled clusters "
                  f"are free")
            if first_used is not None:
                print(f"  first cluster the bitmap calls IN USE: {first_used}")

        # The actual bytes where the file starts.
        if f.runs and f.runs[0][0] is not None:
            offset = (volume._cluster_offset(f.runs[0][0]) if kind == "exFAT"
                      else f.runs[0][0] * volume.cluster_size)
            head = disk.read(offset, 32)
            print(f"  first 16 bytes    {head[:16]!r}")
            print(f"  header verdict    "
                  f"{signatures.check(f.extension, head)}")

        if kind == "NTFS":
            rec = volume._read_record(f.record_no)
            for note in describe_ntfs_attributes(rec) if rec else []:
                print(f"  NOTE  {note}")
        print()

    disk.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
