"""Reading capture metadata off a JPEG.

The EXIF here is assembled byte by byte rather than written with an imaging
library. Two reasons. The parser under test is itself hand-rolled, so feeding it a
known byte layout tests the thing that actually runs. And Pillow's EXIF *writer*
differs between versions — an earlier draft of this file passed against one Pillow
and failed against another, which would have made the suite depend on whichever
version a runner happened to install.

The case that matters is `test_a_stripped_photo…`: 83 photographs reached the
survey carrying a full-size but empty EXIF block, and nothing above the byte level
could see it — the files looked right by name, by size, and by eye.
"""
import pathlib
import struct
import tempfile
import unittest

from .context import plantdb

BE = ">"           # big-endian, the "MM" byte order
ASCII, RATIONAL, LONG = 2, 5, 4


def _rationals(value, parts=3):
    """A GPS coordinate as degrees/minutes/seconds, each an unsigned rational."""
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = round((v - d - m / 60) * 3600, 4)
    nums = [(d, 1), (m, 1), (int(s * 10000), 10000)][:parts]
    return b"".join(struct.pack(BE + "II", n, den) for n, den in nums)


def _ifd(entries, data_base, data):
    """One IFD. Values over 4 bytes go in the shared data area and are referenced."""
    out = struct.pack(BE + "H", len(entries))
    for tag, typ, count, payload in entries:
        if len(payload) <= 4:
            val = payload.ljust(4, b"\x00")
        else:
            val = struct.pack(BE + "I", data_base + len(data))
            data += payload
        out += struct.pack(BE + "HHI", tag, typ, count) + val
    return out + struct.pack(BE + "I", 0), data


def exif_block(lat=None, lon=None, taken=None):
    """A TIFF/EXIF block: what sits after the APP1 'Exif\\0\\0' header."""
    sub, gps = [], []
    if taken:
        stamp = taken.encode() + b"\x00"
        sub.append((0x9003, ASCII, len(stamp), stamp))          # DateTimeOriginal
    if lat is not None:
        gps += [(1, ASCII, 2, (b"N" if lat >= 0 else b"S") + b"\x00"),
                (2, RATIONAL, 3, _rationals(lat)),
                (3, ASCII, 2, (b"E" if lon >= 0 else b"W") + b"\x00"),
                (4, RATIONAL, 3, _rationals(lon))]

    ifd0 = []
    if sub:
        ifd0.append((0x8769, LONG, 1, b""))                     # ExifIFD pointer
    if gps:
        ifd0.append((0x8825, LONG, 1, b""))                     # GPSIFD pointer

    size = lambda n: 2 + 12 * n + 4
    ifd0_off = 8
    sub_off = ifd0_off + size(len(ifd0))
    gps_off = sub_off + (size(len(sub)) if sub else 0)
    data_base = gps_off + (size(len(gps)) if gps else 0)

    # Fill the pointer values now that the layout is known.
    ifd0 = [(t, ty, c, struct.pack(BE + "I", sub_off if t == 0x8769 else gps_off))
            for t, ty, c, _ in ifd0]

    data = b""
    b0, data = _ifd(ifd0, data_base, data)
    b1, data = (_ifd(sub, data_base, data) if sub else (b"", data))
    b2, data = (_ifd(gps, data_base, data) if gps else (b"", data))
    return b"MM" + struct.pack(BE + "HI", 42, ifd0_off) + b0 + b1 + b2 + data


def jpeg(path, exif=None):
    """A minimal JPEG. Only the segment structure matters to the parser."""
    out = b"\xff\xd8"
    if exif:
        payload = b"Exif\x00\x00" + exif
        out += b"\xff\xe1" + struct.pack(BE + "H", len(payload) + 2) + payload
    out += b"\xff\xd9"
    path.write_bytes(out)
    return path


class ReadingCaptureMetadata(unittest.TestCase):

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def test_reads_a_date_and_a_location(self):
        p = jpeg(self.dir / "good.jpg",
                 exif_block(lat=44.4496, lon=-68.8761, taken="2024:09:05 13:58:43"))
        e = plantdb.exif_of(p)
        self.assertTrue(e["taken"].startswith("2024-09-05"), e["taken"])
        self.assertAlmostEqual(float(e["lat"]), 44.4496, places=3)
        self.assertAlmostEqual(float(e["lon"]), -68.8761, places=3)

    def test_a_western_longitude_stays_negative(self):
        """Maine is west of Greenwich. A dropped sign puts every record in Asia."""
        p = jpeg(self.dir / "west.jpg",
                 exif_block(lat=44.45, lon=-68.87, taken="2024:09:05 13:58:43"))
        self.assertLess(float(plantdb.exif_of(p)["lon"]), 0)

    def test_a_stripped_photo_reports_nothing_rather_than_guessing(self):
        """The real failure. An export that leaves an EXIF block with no capture
        tags in it — and nothing may be invented to fill the gap, not from the
        file's modification time, not from anywhere."""
        p = jpeg(self.dir / "stripped.jpg", exif_block())
        self.assertEqual(plantdb.exif_of(p), {"taken": "", "lat": "", "lon": ""})

    def test_no_exif_segment_at_all(self):
        p = jpeg(self.dir / "bare.jpg")
        self.assertEqual(plantdb.exif_of(p), {"taken": "", "lat": "", "lon": ""})

    def test_a_date_without_a_location_is_reported_honestly(self):
        p = jpeg(self.dir / "nogps.jpg", exif_block(taken="2024:09:05 13:58:43"))
        e = plantdb.exif_of(p)
        self.assertTrue(e["taken"])
        self.assertEqual(e["lat"], "")

    def test_a_location_without_a_date(self):
        p = jpeg(self.dir / "nodate.jpg", exif_block(lat=44.45, lon=-68.87))
        e = plantdb.exif_of(p)
        self.assertEqual(e["taken"], "")
        self.assertTrue(e["lat"])

    def test_a_corrupt_file_does_not_raise(self):
        """Ingest has to survive whatever lands in the folder."""
        p = self.dir / "junk.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe1" + b"\x00" * 64)
        self.assertEqual(plantdb.exif_of(p)["lat"], "")


if __name__ == "__main__":
    unittest.main()
