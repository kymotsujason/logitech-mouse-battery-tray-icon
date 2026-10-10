import errno
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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
        return subprocess.run([sys.executable, "-I", "-B", SCRIPT, *args], cwd=self.temp.name, capture_output=True)

    def assertExit(self, result, code):
        # a crash exits 1 as well, so a reject has to leave nothing on stderr
        self.assertEqual((result.returncode, result.stderr), (code, b""))

    def testTheHidppNodePasses(self):
        self.assertExit(self.runScript(self.fakeNode(C54F_HIDPP)), 0)

    def testAGenericDesktopNodeFails(self):
        self.assertExit(self.runScript(self.fakeNode(GENERIC_DESKTOP)), 1)

    def testAKeyboardCollectionAfterTheHidppReportsFails(self):
        self.assertExit(self.runScript(self.fakeNode(C54F_HIDPP + bytes.fromhex("05 07 09 06 a1 01 c0"))), 1)

    def testAMissingDescriptorFails(self):
        self.assertExit(self.runScript(self.fakeNode(None)), 1)

    def testADescriptorThatCantBeReadExitsTwoAndSaysWhy(self):
        # a descriptor that's there but can't be read leaves the node without the group, so udev's debug log gets the reason, unlike a reject
        node = self.fakeNode(None)
        os.makedirs(os.path.join(node, "device", "report_descriptor"))
        result = self.runScript(node)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"Couldn't read the report descriptor under " + os.fsencode(node) + b": [Errno 21] Is a directory", result.stderr)

    def testNoArgumentFails(self):
        self.assertExit(self.runScript(), 1)


class IsHidppStderrTests(unittest.TestCase):
    def testAnUnreadableDescriptorStillExitsTwoWhenStderrHasGoneAway(self):
        class BrokenStream(io.StringIO):
            def write(self, text):
                raise BrokenPipeError(errno.EPIPE, "Broken pipe")

        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "device", "report_descriptor"))
            sys.path.insert(0, os.path.join(REPO, "src"))
            try:
                import isHidpp
            finally:
                sys.path.pop(0)
            with mock.patch.object(sys, "stderr", BrokenStream()):
                self.assertEqual(isHidpp.main(["isHidpp.py", folder]), 2)


if (__name__ == "__main__"):
    unittest.main()
