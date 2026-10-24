import io
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from ubertooth_harness import capturefile


def pcap(link, magic=0xA1B2C3D4, endian="<"):
    return struct.pack(endian + "IHHiIII", magic, 2, 4, 0, 0, 65535, link)


def pcapng(link, endian="<", shb_length=28):
    shb = struct.pack(endian + "IIIHHq", 0x0A0D0D0A, shb_length, 0x1A2B3C4D, 1, 0, -1)
    shb += bytes(shb_length - 28) + struct.pack(endian + "I", shb_length)
    idb = struct.pack(endian + "IIHHI", 1, 20, link, 0, 0) + struct.pack(endian + "I", 20)
    return shb + idb


class CaptureFileTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def run_main(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = capturefile.main([str(a) for a in argv])
        return code, out.getvalue().strip()

    def test_reads_pcap_link_types_in_either_byte_order(self):
        self.assertEqual(("pcap", 256), capturefile.read_header(self.write("a", pcap(256))))
        self.assertEqual(("pcap", 192), capturefile.read_header(self.write("b", pcap(192, endian=">"))))
        self.assertEqual(("pcap", 256), capturefile.read_header(self.write("c", pcap(256, 0xA1B23C4D))))

    def test_reads_the_first_pcapng_interface_link_type(self):
        self.assertEqual(("pcapng", 256), capturefile.read_header(self.write("a", pcapng(256))))
        self.assertEqual(("pcapng", 251), capturefile.read_header(self.write("b", pcapng(251, ">"))))

    def test_finds_the_interface_after_a_large_section_header(self):
        # libbtbb pads its section header block to 32 KiB (seen on hardware).
        path = self.write("big", pcapng(256, shb_length=0x8000))
        self.assertEqual(("pcapng", 256), capturefile.read_header(path))

    def test_rejects_empty_truncated_and_foreign_files(self):
        for name, data in (("empty", b""), ("short", pcap(256)[:10]), ("text", b"hello world\n")):
            with self.subTest(name), self.assertRaises(capturefile.CaptureFileError):
                capturefile.read_header(self.write(name, data))
        shb_only = pcapng(256)[:28]
        with self.assertRaises(capturefile.CaptureFileError):
            capturefile.read_header(self.write("no-idb", shb_only))

    def test_cli_prints_one_line_and_exit_status(self):
        path = self.write("x.pcap", pcap(192))
        self.assertEqual((0, "Capture format: pcap, link type: 192"), self.run_main(path))
        code, line = self.run_main(self.dir / "missing.pcap")
        self.assertEqual(1, code)
        self.assertTrue(line.startswith("Capture file absent: "))
        self.assertEqual(0, self.run_main("--absent", self.dir / "missing.pcap")[0])
        self.assertEqual(1, self.run_main("--absent", path)[0])
        self.assertEqual(1, self.run_main(self.write("bad", b"junk"))[0])


if __name__ == "__main__":
    unittest.main()
