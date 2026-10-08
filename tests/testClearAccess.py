import errno
import io
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import types
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
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.sysRoot = os.path.join(self.root, "sys")
        self.devRoot = os.path.join(self.root, "dev")
        self.udevRoot = os.path.join(self.root, "udev")
        os.makedirs(self.sysRoot)
        os.makedirs(self.devRoot)
        os.makedirs(self.udevRoot)
        self.removed = []
        self.owners = []
        self.failures = {}
        self.devices = {}
        self.realFstat = os.fstat
        self.patches = [
            mock.patch.object(clearAccess.os, "fstat", self.fakeFstat),
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

    # an unprivileged test can't make a character device, so a regular file stands in for one by its inode
    def fakeFstat(self, fd):
        info = self.realFstat(fd)
        number = self.devices.get((info.st_dev, info.st_ino))
        if (number is None):
            return info
        major, minor = number.split(":")
        return types.SimpleNamespace(st_mode=stat.S_IFCHR | stat.S_IMODE(info.st_mode), st_rdev=os.makedev(int(major), int(minor)), st_dev=info.st_dev, st_ino=info.st_ino)

    def makeDevice(self, path, number):
        with open(path, "w") as f:
            f.write("")
        os.chmod(path, 0o666)
        info = os.stat(path)
        self.devices[(info.st_dev, info.st_ino)] = number

    def removeAttribute(self, path, name):
        if (not os.path.exists(path)):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        path = os.path.realpath(path)
        if (path in self.failures):
            raise self.failures[path]
        self.removed.append((path, name))

    def changeOwner(self, path, uid, gid):
        if (not os.path.exists(path)):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        self.owners.append((os.path.realpath(path), uid, gid))

    def addNode(self, name, hidId="0003:0000046D:0000C54F", driver="hid-generic", descriptor=C54F_HIDPP, present=True, number=None, devFile=True, nodeNumber=None, character=True):
        number = number or "243:" + name[len("hidraw"):]
        device = os.path.join(self.sysRoot, name, "device")
        os.makedirs(device)
        if (devFile):
            with open(os.path.join(self.sysRoot, name, "dev"), "w") as f:
                f.write(number + "\n")
        self.describe(name, hidId, driver, descriptor)
        path = os.path.join(self.devRoot, name)
        if (present and character):
            self.makeDevice(path, nodeNumber or number)
        elif (present):
            with open(path, "w") as f:
                f.write("")
            os.chmod(path, 0o666)
        return path

    def describe(self, name, hidId, driver, descriptor):
        device = os.path.join(self.sysRoot, name, "device")
        with open(os.path.join(device, "uevent"), "w") as f:
            f.write("DRIVER=" + driver + "\nHID_ID=" + hidId + "\n")
        with open(os.path.join(device, "report_descriptor"), "wb") as f:
            f.write(descriptor)

    def addUdevEntry(self, number, lines):
        with open(os.path.join(self.udevRoot, "c" + number), "w") as f:
            f.write("\n".join(lines) + "\n")

    def runClear(self, settled=True):
        output = io.StringIO()
        with redirect_stdout(output):
            code = clearAccess.main(self.sysRoot, self.devRoot, self.udevRoot, settled=settled)
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
        self.assertEqual(self.owners, [(first, 0, GROUP_ID), (second, 0, GROUP_ID)])

    def testAMissingGroupLeavesEveryNodeAsItWas(self):
        paths = [self.addNode("hidraw4"), self.addNode("hidraw5")]
        with mock.patch.object(clearAccess.grp, "getgrnam", side_effect=KeyError("getgrnam(): name not found: 'logitech-mouse-battery'")):
            code, output = self.runClear()
        self.assertEqual((code, output), (1, "Couldn't find the logitech-mouse-battery group, so no node was changed\n"))
        self.assertEqual((self.removed, self.owners, [self.mode(path) for path in paths]), ([], [], [0o666, 0o666]))

    def testTheGroupAndModeGoOnBeforeTheAclComesOff(self):
        path = self.addNode("hidraw4")
        steps = []
        realChmod = os.chmod
        with mock.patch.object(clearAccess.os, "chown", lambda *args: steps.append("chown")), mock.patch.object(clearAccess.os, "chmod", lambda *args: steps.append("chmod") or realChmod(*args)), mock.patch.object(clearAccess.os, "removexattr", lambda *args: steps.append("removexattr")):
            self.assertEqual(self.runClear(), (0, "Closed access to " + path + "\n"))
        self.assertEqual(steps, ["chown", "chmod", "removexattr"])

    def testANodeAnotherRuleGivesTheSeatKeepsItsAclButGetsItsOwnerAndMode(self):
        path = self.addNode("hidraw4", number="243:4")
        self.addUdevEntry("243:4", ["E:DRIVER=hid-generic", "G:seat", "G:systemd", "G:uaccess", "Q:seat", "Q:systemd", "Q:uaccess", "V:1"])
        self.assertEqual(self.runClear(), (0, "Kept the seat's access to " + path + ", since another package's udev rule gives it\n"))
        self.assertEqual((self.removed, self.owners, self.mode(path)), ([], [(path, 0, GROUP_ID)], 0o660))

    def testAUaccessTagIsntTrustedWhenUdevDidntSettle(self):
        path = self.addNode("hidraw4", number="243:4")
        self.addUdevEntry("243:4", ["G:uaccess", "Q:uaccess", "V:1"])
        self.assertEqual(self.runClear(settled=False), (0, "Closed access to " + path + "\n"))
        self.assertEqual((self.removed, self.owners), ([(path, "system.posix_acl_access")], [(path, 0, GROUP_ID)]))

    def testATagTheNodeOnlyOnceHadDoesntKeepItsAcl(self):
        path = self.addNode("hidraw4", number="243:4")
        self.addUdevEntry("243:4", ["G:seat", "G:systemd", "G:uaccess", "Q:seat", "Q:systemd", "V:1"])
        self.assertEqual(self.runClear(), (0, "Closed access to " + path + "\n"))
        self.assertEqual((self.removed, self.owners), ([(path, "system.posix_acl_access")], [(path, 0, GROUP_ID)]))

    def testANodeWithNoUdevEntryIsClosed(self):
        path = self.addNode("hidraw4", number="243:4")
        self.assertEqual(self.runClear(), (0, "Closed access to " + path + "\n"))
        self.assertEqual(self.removed, [(path, "system.posix_acl_access")])

    def testANodeWithNoDevFileIsLeftAlone(self):
        path = self.addNode("hidraw4", devFile=False)
        self.assertEqual(self.runClear(), (0, ""))
        self.assertEqual((self.removed, self.owners, self.mode(path)), ([], [], 0o666))

    def testANodeThatIsntACharacterDeviceIsLeftAlone(self):
        path = self.addNode("hidraw4", character=False)
        self.assertEqual(self.runClear(), (0, ""))
        self.assertEqual((self.removed, self.owners, self.mode(path)), ([], [], 0o666))

    def testANodeWhoseNumberIsntTheCheckedDevicesIsLeftAlone(self):
        path = self.addNode("hidraw4", nodeNumber="243:5")
        self.assertEqual(self.runClear(), (0, ""))
        self.assertEqual((self.removed, self.owners, self.mode(path)), ([], [], 0o666))

    def testALinkInPlaceOfTheNodeIsntFollowed(self):
        path = self.addNode("hidraw4", present=False)
        target = os.path.join(self.root, "target")
        self.makeDevice(target, "243:4")
        os.symlink(target, path)
        self.assertEqual(self.runClear(), (0, ""))
        self.assertEqual((self.removed, self.owners, self.mode(target)), ([], [], 0o666))

    def swapAfterCheck(self, checks):
        path = self.addNode("hidraw4")
        old = os.open(path, os.O_RDONLY)
        self.addCleanup(os.close, old)
        realIsHidppNode = clearAccess.hidpp.isHidppNode
        count = []

        def checkThenSwap(uevent, descriptor):
            result = realIsHidppNode(uevent, descriptor)
            count.append(1)
            if (len(count) == checks):
                # the receiver goes away and a security key takes its name and number
                os.remove(path)
                self.makeDevice(path, "243:4")
                self.describe("hidraw4", "0003:00001050:00000407", "hid-generic", GENERIC_DESKTOP)
            return result

        with mock.patch.object(clearAccess.hidpp, "isHidppNode", checkThenSwap):
            result = self.runClear()
        return (path, old, result)

    def testADeviceThatTakesTheNameRightAfterTheCheckIsLeftAlone(self):
        path, old, result = self.swapAfterCheck(1)
        self.assertEqual(result, (0, ""))
        self.assertEqual((self.removed, self.owners, self.mode(path)), ([], [], 0o666))

    def testADeviceThatTakesTheNameLaterIsLeftAloneWhileTheCheckedNodeIsClosed(self):
        path, old, result = self.swapAfterCheck(2)
        self.assertEqual(result, (0, "Closed access to " + path + "\n"))
        self.assertEqual((len(self.owners), self.mode(path), stat.S_IMODE(self.realFstat(old).st_mode)), (1, 0o666, 0o660))

    def runMainBlock(self, *args):
        script = os.path.join(SRC, "clearAccess.py")
        seen = []

        # the script's main block calls a main of its own, so the profile hook reads the flag off that call
        def watch(frame, event, arg):
            if (event == "call" and frame.f_code.co_name == "main" and frame.f_code.co_filename == script):
                seen.append(frame.f_locals["settled"])

        # no node is found, so the run never reaches the host's real devices
        with mock.patch.object(sys, "argv", [script, *args]), mock.patch.object(clearAccess.hidpp, "findHidppNodes", return_value=[]):
            sys.setprofile(watch)
            try:
                with self.assertRaises(SystemExit) as caught:
                    runpy.run_path(script, run_name="__main__")
            finally:
                sys.setprofile(None)
        return (caught.exception.code, seen)

    def testTheSettledArgumentReachesMain(self):
        self.assertEqual(self.runMainBlock("--settled"), (0, [True]))
        self.assertEqual(self.runMainBlock(), (0, [False]))

    def testItImportsInIsolatedModeWithoutRunning(self):
        # -I leaves the script's folder off sys.path, so the module has to put it back for its hidpp import
        script = "import importlib.util; spec = importlib.util.spec_from_file_location('clearAccess', " + repr(os.path.join(SRC, "clearAccess.py")) + "); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); print(module.hidpp.__name__)"
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (0, "hidpp\n"), result.stderr)


if (__name__ == "__main__"):
    unittest.main()
