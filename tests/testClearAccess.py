import errno
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import clearAccess

C54F_HIDPP = bytes.fromhex("06 43 ff 0a 01 03 a1 01 85 10 95 06 75 08 15 00 26 ff 00 09 01 81 00 09 01 91 00 c0 06 43 ff 0a 02 03 a1 01 85 11 95 13 75 08 15 00 26 ff 00 09 02 81 00 09 02 91 00 c0")
GENERIC_DESKTOP = bytes.fromhex("05 01 09 02 a1 01 09 01 a1 00 95 10 75 01 15 00 25 01 05 09 19 01 29 10 81 02 95 02 75 10 16 00 80 26 ff 7f 05 01 09 30 09 31 81 06 95 01 75 08 15 80 25 7f 09 38 81 06 05 0c 0a 38 02 81 06 c0 95 05 75 08 15 00 26 ff 00 06 00 ff 09 f1 81 00 c0")
GROUP_ID = 975
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


class ClearAccessTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sysRoot = os.path.join(self.root, "sys")
        self.devRoot = os.path.join(self.root, "dev")
        self.udevRoot = os.path.join(self.root, "udev")
        os.makedirs(self.sysRoot)
        os.makedirs(self.devRoot)
        os.makedirs(self.udevRoot)
        self.removed = []
        self.owners = []
        self.failures = {}
        self.patches = [
            mock.patch.object(clearAccess.os, "removexattr", self.removeAttribute),
            mock.patch.object(clearAccess.os, "chown", self.changeOwner),
            mock.patch.object(clearAccess.grp, "getgrnam", return_value=mock.Mock(gr_gid=GROUP_ID)),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def removeAttribute(self, path, name):
        if (path in self.failures):
            raise self.failures[path]
        if (not os.path.exists(path)):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        self.removed.append((path, name))

    def changeOwner(self, path, uid, gid):
        self.owners.append((path, uid, gid))

    def addNode(self, name, hidId="0003:0000046D:0000C54F", driver="hid-generic", descriptor=C54F_HIDPP, present=True, number=None):
        device = os.path.join(self.sysRoot, name, "device")
        os.makedirs(device)
        if (number is not None):
            with open(os.path.join(self.sysRoot, name, "dev"), "w") as f:
                f.write(number + "\n")
        with open(os.path.join(device, "uevent"), "w") as f:
            f.write("DRIVER=" + driver + "\nHID_ID=" + hidId + "\n")
        with open(os.path.join(device, "report_descriptor"), "wb") as f:
            f.write(descriptor)
        path = os.path.join(self.devRoot, name)
        if (present):
            with open(path, "w") as f:
                f.write("")
            os.chmod(path, 0o666)
        return path

    def addUdevEntry(self, number, lines):
        with open(os.path.join(self.udevRoot, "c" + number), "w") as f:
            f.write("\n".join(lines) + "\n")

    def runClear(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = clearAccess.main(self.sysRoot, self.devRoot, self.udevRoot)
        return (code, output.getvalue())

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def testAMatchingNodeLosesItsAclAndGetsRootTheGroupAnd0660(self):
        path = self.addNode("hidraw4")
        self.assertEqual(self.runClear(), (0, "Closed access to " + path + "\n"))
        self.assertEqual((self.removed, self.owners, self.mode(path)), ([(path, "system.posix_acl_access")], [(path, 0, GROUP_ID)], 0o660))

    def testOtherNodesAreLeftAlone(self):
        paths = [self.addNode("hidraw1", hidId="0003:0000046E:0000C54F"), self.addNode("hidraw2", driver="hid-logitech-dj"), self.addNode("hidraw3", descriptor=GENERIC_DESKTOP)]
        self.assertEqual(self.runClear(), (0, ""))
        self.assertEqual((self.removed, self.owners, [self.mode(path) for path in paths]), ([], [], [0o666, 0o666, 0o666]))

    def testANodeWithNoAclStillGetsItsOwnerAndMode(self):
        for code in (errno.ENODATA, errno.EOPNOTSUPP):
            path = self.addNode("hidraw" + str(code))
            self.failures[path] = OSError(code, os.strerror(code))
        code, output = self.runClear()
        self.assertEqual((code, len(self.owners)), (0, 2))
        self.assertEqual(output.count("Closed access to "), 2)

    def testAGoneNodeIsSkippedQuietly(self):
        self.addNode("hidraw4", present=False)
        self.assertEqual(self.runClear(), (0, ""))
        self.assertEqual(self.owners, [])

    def testAnErrorOnOneNodeIsPrintedAndTheNextIsStillHandled(self):
        first = self.addNode("hidraw4")
        second = self.addNode("hidraw5")
        self.failures[first] = PermissionError(errno.EPERM, "Operation not permitted")
        code, output = self.runClear()
        self.assertEqual(code, 1)
        self.assertIn("Couldn't close access to " + first + ": [Errno 1] Operation not permitted\n", output)
        self.assertIn("Closed access to " + second + "\n", output)
        self.assertEqual(self.owners, [(second, 0, GROUP_ID)])

    def testAMissingGroupIsPrintedForEachNode(self):
        self.addNode("hidraw4")
        self.addNode("hidraw5")
        with mock.patch.object(clearAccess.grp, "getgrnam", side_effect=KeyError("getgrnam(): name not found: 'logitech-mouse-battery'")):
            code, output = self.runClear()
        self.assertEqual((code, output.count("Couldn't close access to "), self.owners), (1, 2, []))

    def testANodeAnotherRuleGivesTheSeatKeepsItsAclButGetsItsOwnerAndMode(self):
        path = self.addNode("hidraw4", number="243:4")
        self.addUdevEntry("243:4", ["E:DRIVER=hid-generic", "G:seat", "G:systemd", "G:uaccess", "Q:seat", "Q:systemd", "Q:uaccess", "V:1"])
        self.assertEqual(self.runClear(), (0, "Kept the seat's access to " + path + ", since another package's udev rule gives it\n"))
        self.assertEqual((self.removed, self.owners, self.mode(path)), ([], [(path, 0, GROUP_ID)], 0o660))

    def testATagTheNodeOnlyOnceHadDoesntKeepItsAcl(self):
        path = self.addNode("hidraw4", number="243:4")
        self.addUdevEntry("243:4", ["G:seat", "G:systemd", "G:uaccess", "Q:seat", "Q:systemd", "V:1"])
        self.assertEqual(self.runClear(), (0, "Closed access to " + path + "\n"))
        self.assertEqual((self.removed, self.owners), ([(path, "system.posix_acl_access")], [(path, 0, GROUP_ID)]))

    def testANodeWithNoUdevEntryIsClosed(self):
        path = self.addNode("hidraw4", number="243:4")
        self.assertEqual(self.runClear(), (0, "Closed access to " + path + "\n"))
        self.assertEqual(self.removed, [(path, "system.posix_acl_access")])

    def testANodeWithNoDevFileIsClosed(self):
        path = self.addNode("hidraw4")
        self.addUdevEntry("243:4", ["Q:uaccess", "V:1"])
        self.assertEqual(self.runClear(), (0, "Closed access to " + path + "\n"))
        self.assertEqual(self.removed, [(path, "system.posix_acl_access")])

    def testItImportsInIsolatedModeWithoutRunning(self):
        # -I leaves the script's folder off sys.path, so the module has to put it back for its hidpp import
        script = "import importlib.util; spec = importlib.util.spec_from_file_location('clearAccess', " + repr(os.path.join(SRC, "clearAccess.py")) + "); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); print(module.hidpp.__name__)"
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (0, "hidpp\n"), result.stderr)


if (__name__ == "__main__"):
    unittest.main()
