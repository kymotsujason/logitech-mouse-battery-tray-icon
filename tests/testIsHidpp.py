import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "src", "isHidpp.py")
C54F_HIDPP = bytes.fromhex("06 43 ff 0a 01 03 a1 01 85 10 95 06 75 08 15 00 26 ff 00 09 01 81 00 09 01 91 00 c0 06 43 ff 0a 02 03 a1 01 85 11 95 13 75 08 15 00 26 ff 00 09 02 81 00 09 02 91 00 c0")
GENERIC_DESKTOP = bytes.fromhex("05 01 09 02 a1 01 09 01 a1 00 95 10 75 01 15 00 25 01 05 09 19 01 29 10 81 02 95 02 75 10 16 00 80 26 ff 7f 05 01 09 30 09 31 81 06 95 01 75 08 15 80 25 7f 09 38 81 06 05 0c 0a 38 02 81 06 c0 95 05 75 08 15 00 26 ff 00 06 00 ff 09 f1 81 00 c0")


class IsHidppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def fakeNode(self, descriptor):
        # a hidraw node's device link leads two folders up, to the HID device that holds the descriptor
        device = os.path.join(self.temp.name, "0003:046D:C54F.0001")
        node = os.path.join(device, "hidraw", "hidraw0")
        os.makedirs(node)
        if (descriptor is not None):
            with open(os.path.join(device, "report_descriptor"), "wb") as f:
                f.write(descriptor)
        os.symlink("../..", os.path.join(node, "device"))
        return node

    def runScript(self, *args):
        # -I and a working folder away from the script match how udev runs it, which the sibling import has to survive
        return subprocess.run([sys.executable, "-I", "-B", SCRIPT, *args], cwd=self.temp.name, capture_output=True).returncode

    def testTheHidppNodePasses(self):
        self.assertEqual(self.runScript(self.fakeNode(C54F_HIDPP)), 0)

    def testAGenericDesktopNodeFails(self):
        self.assertEqual(self.runScript(self.fakeNode(GENERIC_DESKTOP)), 1)

    def testAMissingDescriptorFails(self):
        self.assertEqual(self.runScript(self.fakeNode(None)), 1)

    def testNoArgumentFails(self):
        self.assertEqual(self.runScript(), 1)


if (__name__ == "__main__"):
    unittest.main()
