import errno
import os
import stat
import tempfile
import threading
import unittest
from unittest import mock

import autostart

# the suite runs as root in the containers, so the tests act as a non root account and root's exemptions can't pass a test on their own
ACCOUNT = os.getuid() or 1000


class AutostartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.configHome = self.temp.name
        self.path = autostart.overridePath(self.configHome)
        self.owners = {}
        realUid = os.getuid()
        realFstat = os.fstat

        def ownerFor(fd, *args, **kwargs):
            info = realFstat(fd, *args, **kwargs)
            fields = list(info[:10])
            fields[4] = self.owners.get(os.readlink("/proc/self/fd/" + str(fd)), ACCOUNT if info.st_uid == realUid else info.st_uid)
            return os.stat_result(fields)

        for patch in (mock.patch.object(autostart.os, "getuid", return_value=ACCOUNT), mock.patch.object(autostart.os, "fstat", ownerFor)):
            patch.start()
            self.addCleanup(patch.stop)

    def read(self):
        with open(self.path) as f:
            return f.read()

    def testEnabledWithoutAnOverride(self):
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOffWritesTheOverrideAndMakesTheFolder(self):
        autostart.setEnabled(self.configHome, False)
        self.assertEqual(self.read(), "[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\n")
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testTurningItBackOnDeletesTheAppsOwnOverride(self):
        autostart.setEnabled(self.configHome, False)
        autostart.setEnabled(self.configHome, True)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOnKeepsKeysAnotherToolWrote(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\nX-GNOME-Autostart-enabled=false\nExec=logitech-mouse-battery\n")
        autostart.setEnabled(self.configHome, True)
        self.assertIn("Hidden=false", self.read())
        self.assertIn("X-GNOME-Autostart-enabled=true", self.read())
        self.assertNotIn("X-GNOME-Autostart-enabled=false", self.read())
        self.assertNotIn("Hidden=true", self.read())
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOffKeepsKeysAnotherToolWrote(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nX-Custom=1\n")
        autostart.setEnabled(self.configHome, False)
        self.assertIn("X-Custom=1", self.read())
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testTheGnomeKeyAloneCanTurnItOff(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nX-GNOME-Autostart-enabled=false\nExec=logitech-mouse-battery\n")
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testTurningItOffSetsTheGnomeKeyWhereItIs(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nX-GNOME-Autostart-enabled=true\nExec=logitech-mouse-battery\n")
        autostart.setEnabled(self.configHome, False)
        self.assertIn("X-GNOME-Autostart-enabled=false", self.read())
        self.assertIn("Hidden=true", self.read())
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testAnOverrideAnotherUserOwnsIsLeftUnchanged(self):
        os.makedirs(os.path.dirname(self.path))
        original = "[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n"
        with open(self.path, "w") as f:
            f.write(original)
        with mock.patch.object(autostart.os, "getuid", return_value=ACCOUNT + 1):
            message = self.refusal(False)
        self.assertEqual(message, "[Errno 13] Permission denied, the folder belongs to uid " + str(ACCOUNT) + ", not this account: '" + os.path.realpath(os.path.dirname(self.path)) + "'")
        self.assertEqual(self.read(), original)

    def testTurningItOffKeepsALineSeparatorInsideAValue(self):
        # a desktop entry's lines end only at a newline, so a Unicode line separator is part of its value
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nComment=first\u2028Hidden=false\nExec=logitech-mouse-battery\n")
        autostart.setEnabled(self.configHome, False)
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("\nComment=first\u2028Hidden=false\n", text)
        self.assertIn("\nHidden=true\n", text)
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testALineSeparatorInsideAValueStartsNoKey(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\u2028Hidden=true\nExec=logitech-mouse-battery\n")
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOnDeletesAnOverrideWithoutExec(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\nX-GNOME-Autostart-enabled=false\n")
        autostart.setEnabled(self.configHome, True)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testTurningItOnDeletesAnEmptyOverride(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("")
        autostart.setEnabled(self.configHome, True)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testFolderAtOverridePathReadsAsEnabled(self):
        os.makedirs(self.path)
        self.assertTrue(autostart.isEnabled(self.configHome))

    def testAFolderAtTheOverrideLeavesNoDescriptorOpen(self):
        os.makedirs(self.path)
        before = len(os.listdir("/proc/self/fd"))
        # the menu reads the override at every open, so a descriptor left each time would add up
        results = [(autostart.isEnabled(self.configHome), self.outcome(False)) for i in range(5)]
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertEqual(results, [(True, "[Errno 22] Not a regular file: '" + os.path.realpath(self.path) + "'")] * 5)

    def testAFailedFdopenInAWriteLeavesNoDescriptorOpen(self):
        os.makedirs(os.path.dirname(self.path))
        before = len(os.listdir("/proc/self/fd"))
        with mock.patch.object(autostart.os, "fdopen", side_effect=OSError(errno.ENOMEM, "Cannot allocate memory")):
            self.assertEqual(self.outcome(False), "[Errno 12] Cannot allocate memory")
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [])

    def testInvalidUtf8SurvivesTurningItOff(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "wb") as f:
            f.write(b"[Desktop Entry]\nType=Application\nName=Mouse Battery\nX-Custom=\xff\xfe\n")
        autostart.setEnabled(self.configHome, False)
        with open(self.path, "rb") as f:
            content = f.read()
        self.assertIn(b"\xff\xfe", content)
        self.assertFalse(autostart.isEnabled(self.configHome))

    def testSetEnabledRaisesWhenAutostartFolderIsAFile(self):
        with open(os.path.dirname(self.path), "w") as f:
            f.write("not a folder")
        with self.assertRaises(OSError):
            autostart.setEnabled(self.configHome, False)

    def testTurningItOnWithNoOverrideCreatesNothing(self):
        autostart.setEnabled(self.configHome, True)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(os.path.dirname(self.path)))

    def testAFailedWriteLeavesTheOldOverrideWhole(self):
        os.makedirs(os.path.dirname(self.path))
        original = "[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n"
        with open(self.path, "w") as f:
            f.write(original)
        with mock.patch.object(autostart.os, "replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                autostart.setEnabled(self.configHome, False)
        self.assertEqual(self.read(), original)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [autostart.FILE_NAME])

    def testAWriteLeavesNoTemporaryFile(self):
        autostart.setEnabled(self.configHome, False)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [autostart.FILE_NAME])

    def testAFailedCreateLeavesNoOverrideOrTemporaryFile(self):
        with mock.patch.object(autostart.os, "replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
            with self.assertRaises(OSError):
                autostart.setEnabled(self.configHome, False)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [])

    def testASymlinkedOverrideStaysALinkAfterWritingThroughIt(self):
        os.makedirs(os.path.dirname(self.path))
        with tempfile.TemporaryDirectory() as other:
            target = os.path.join(other, "real.desktop")
            with open(target, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n")
            os.symlink(target, self.path)
            autostart.setEnabled(self.configHome, False)
            self.assertTrue(os.path.islink(self.path))
            self.assertEqual(os.path.realpath(self.path), target)
            with open(target) as f:
                self.assertIn("Hidden=true", f.read())

    def refusal(self, enabled, configHome=None):
        with self.assertRaises(PermissionError) as caught:
            autostart.setEnabled(self.configHome if configHome is None else configHome, enabled)
        return str(caught.exception)

    def testALinkIntoAnotherAccountsFolderIsRefusedAndLeftUnchanged(self):
        os.makedirs(os.path.dirname(self.path))
        original = "[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n"
        with tempfile.TemporaryDirectory() as other:
            target = os.path.join(other, "real.desktop")
            with open(target, "w") as f:
                f.write(original)
            os.symlink(target, self.path)
            # the folder the link leads into belongs to another account, as /etc does to root
            self.owners[os.path.realpath(other)] = ACCOUNT + 1
            message = self.refusal(False)
            self.assertEqual(message, "[Errno 13] Permission denied, the folder belongs to uid " + str(ACCOUNT + 1) + ", not this account: '" + os.path.realpath(other) + "'")
            with open(target) as f:
                self.assertEqual(f.read(), original)

    def testAnAutostartFolderLinkFromAnotherAccountIsRefused(self):
        folder = os.path.dirname(self.path)
        with tempfile.TemporaryDirectory() as linked:
            os.symlink(linked, folder)
            # the autostart folder is a link another account made, as a link to /etc/xdg/autostart would be
            self.owners[folder] = ACCOUNT + 1
            message = self.refusal(False)
            self.assertEqual(message, "[Errno 13] Permission denied, a link uid " + str(ACCOUNT + 1) + " made: '" + os.path.realpath(self.configHome) + "/autostart'")
            self.assertEqual(os.listdir(linked), [])

    def testAnAutostartFolderLinkRootMadeIsFollowed(self):
        folder = os.path.dirname(self.path)
        with tempfile.TemporaryDirectory() as linked:
            os.symlink(linked, folder)
            # root made the autostart folder link into a folder this account owns, as it may make the config folder link
            self.owners[folder] = 0
            autostart.setEnabled(self.configHome, False)
            with open(os.path.join(linked, autostart.FILE_NAME)) as f:
                self.assertIn("Hidden=true", f.read())

    def testAConfigFolderLinkFromAnotherAccountIsRefused(self):
        with tempfile.TemporaryDirectory() as outer, tempfile.TemporaryDirectory() as linked:
            configHome = os.path.join(outer, "config")
            os.symlink(linked, configHome)
            # the config folder itself is a link another account made, as a link from ~/.config to /etc/xdg would be
            self.owners[configHome] = ACCOUNT + 1
            message = self.refusal(False, configHome)
            self.assertEqual(message, "[Errno 13] Permission denied, a link uid " + str(ACCOUNT + 1) + " made: '" + os.path.join(os.path.realpath(outer), "config") + "'")
            self.assertEqual(os.listdir(linked), [])

    def testAConfigFolderLinkFromAnotherAccountKeepsItsOverride(self):
        with tempfile.TemporaryDirectory() as outer, tempfile.TemporaryDirectory() as linked:
            configHome = os.path.join(outer, "config")
            os.symlink(linked, configHome)
            target = os.path.join(linked, "autostart", autostart.FILE_NAME)
            os.makedirs(os.path.dirname(target))
            with open(target, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\n")
            # the config folder is a link another account made, and the override at its end is that account's
            self.owners[configHome] = ACCOUNT + 1
            self.refusal(True, configHome)
            self.assertTrue(os.path.exists(target))

    def testAConfigFolderLinkRootMadeIsFollowed(self):
        with tempfile.TemporaryDirectory() as outer, tempfile.TemporaryDirectory() as linked:
            configHome = os.path.join(outer, "config")
            os.symlink(linked, configHome)
            # root made the config folder link, as a system setup script would
            self.owners[configHome] = 0
            autostart.setEnabled(configHome, False)
            with open(os.path.join(linked, "autostart", autostart.FILE_NAME)) as f:
                self.assertIn("Hidden=true", f.read())

    def testAConfigFolderLinkThisAccountMadeIsFollowed(self):
        with tempfile.TemporaryDirectory() as outer, tempfile.TemporaryDirectory() as linked:
            configHome = os.path.join(outer, "config")
            # a dotfile manager links ~/.config into a folder the same account owns
            os.symlink(linked, configHome)
            autostart.setEnabled(configHome, False)
            target = os.path.join(linked, "autostart", autostart.FILE_NAME)
            with open(target) as f:
                self.assertIn("Hidden=true", f.read())
            autostart.setEnabled(configHome, True)
            self.assertFalse(os.path.exists(target))

    def testAConfigFolderAnotherAccountOwnsGetsNoAutostartFolder(self):
        # the config folder is another account's, as a user's ~/.config is for a run as root
        self.owners[os.path.realpath(self.configHome)] = ACCOUNT + 1
        message = self.refusal(False)
        self.assertEqual(message, "[Errno 13] Permission denied, the folder belongs to uid " + str(ACCOUNT + 1) + ", not this account: '" + os.path.realpath(self.configHome) + "'")
        self.assertEqual(os.listdir(self.configHome), [])

    def testAnOverrideFileAnotherAccountOwnsIsLeftUnchanged(self):
        os.makedirs(os.path.dirname(self.path))
        original = "[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n"
        with open(self.path, "w") as f:
            f.write(original)
        # only the override file is another account's, while its folder is this one's
        self.owners[os.path.realpath(self.path)] = ACCOUNT + 1
        message = self.refusal(False)
        self.assertEqual(message, "[Errno 13] Permission denied, the file belongs to uid " + str(ACCOUNT + 1) + ", not this account: '" + os.path.realpath(self.path) + "'")
        self.assertEqual(self.read(), original)

    def testAnOverrideFileAnotherAccountOwnsIsntDeleted(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\n")
        # only the override file is another account's, while its folder is this one's
        self.owners[os.path.realpath(self.path)] = ACCOUNT + 1
        message = self.refusal(True)
        self.assertEqual(message, "[Errno 13] Permission denied, the file belongs to uid " + str(ACCOUNT + 1) + ", not this account: '" + os.path.realpath(self.path) + "'")
        self.assertTrue(os.path.exists(self.path))

    def swapFolder(self, victim):
        # another account renames the autostart folder and puts a link in its place once the checks are done, as a link to /etc/xdg/autostart would be
        folder = os.path.dirname(self.path)
        moved = os.path.join(self.configHome, "moved")
        if (not os.path.islink(folder)):
            os.rename(folder, moved)
            os.symlink(victim, folder)
        return moved

    def testAFolderSwappedAfterTheChecksDoesntTakeTheWrite(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n")
        realOpen = os.open
        with tempfile.TemporaryDirectory() as victim:

            def swapThenOpen(path, *args, **kwargs):
                if (str(path).endswith(".new")):
                    self.swapFolder(victim)
                return realOpen(path, *args, **kwargs)

            with mock.patch.object(autostart.os, "open", swapThenOpen):
                autostart.setEnabled(self.configHome, False)
            self.assertEqual(os.listdir(victim), [])
        with open(os.path.join(self.configHome, "moved", autostart.FILE_NAME)) as f:
            self.assertIn("Hidden=true", f.read())

    def testAFolderSwappedAfterTheChecksDoesntTakeTheRemoval(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\n")
        with tempfile.TemporaryDirectory() as victim:
            # the system's own entry sits in the folder the link leads to
            kept = os.path.join(victim, autostart.FILE_NAME)
            with open(kept, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n")
            realRemove = os.remove
            realUnlink = os.unlink

            def swapThenRemove(path, *args, **kwargs):
                self.swapFolder(victim)
                return realRemove(path, *args, **kwargs)

            def swapThenUnlink(path, *args, **kwargs):
                self.swapFolder(victim)
                return realUnlink(path, *args, **kwargs)

            with mock.patch.object(autostart.os, "remove", swapThenRemove), mock.patch.object(autostart.os, "unlink", swapThenUnlink):
                autostart.setEnabled(self.configHome, True)
            self.assertTrue(os.path.exists(kept))
        self.assertEqual(os.listdir(os.path.join(self.configHome, "moved")), [])

    def stuckAfter(self, call):
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(2)
        stuck = thread.is_alive()
        if (stuck):
            # a writer coming and going lets a read stuck on the FIFO finish, so the test fails instead of hanging the suite
            os.close(os.open(self.path, os.O_WRONLY | os.O_NONBLOCK))
            thread.join(2)
        return stuck

    def testAFifoAtTheOverrideDoesntHangTheMenu(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        result = []
        self.assertFalse(self.stuckAfter(lambda: result.append(autostart.isEnabled(self.configHome))))
        self.assertEqual(result, [True])

    def testAFifoAtTheOverrideIsRefusedAndLeftInPlace(self):
        os.makedirs(os.path.dirname(self.path))
        os.mkfifo(self.path)
        errors = []

        def turnOff():
            try:
                autostart.setEnabled(self.configHome, False)
            except OSError as error:
                errors.append(str(error))

        self.assertFalse(self.stuckAfter(turnOff))
        self.assertEqual(errors, ["[Errno 22] Not a regular file: '" + os.path.realpath(self.path) + "'"])
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.path).st_mode))

    def testALinkAtTheTemporaryNameIsntWrittenThrough(self):
        os.makedirs(os.path.dirname(self.path))
        with tempfile.TemporaryDirectory() as other:
            victim = os.path.join(other, "victim")
            with open(victim, "w") as f:
                f.write("keep")
            os.symlink(victim, self.path + ".new")
            autostart.setEnabled(self.configHome, False)
            with open(victim) as f:
                self.assertEqual(f.read(), "keep")
        self.assertFalse(os.path.islink(self.path))
        self.assertIn("Hidden=true", self.read())

    def testA0400OverrideRaisesPermissionErrorAndIsLeftUnchanged(self):
        os.makedirs(os.path.dirname(self.path))
        original = "[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n"
        with open(self.path, "w") as f:
            f.write(original)
        os.chmod(self.path, 0o400)
        message = self.refusal(False)
        self.assertEqual(message, "[Errno 13] Permission denied, the file is read only: '" + os.path.realpath(self.path) + "'")
        self.assertEqual(self.read(), original)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [autostart.FILE_NAME])

    def outcome(self, enabled, configHome=None):
        # only an OSError may come out, since the tray catches nothing else and would end
        try:
            autostart.setEnabled(self.configHome if configHome is None else configHome, enabled)
        except OSError as error:
            return str(error)
        return None

    def testAnOverrideLinkToTheTopFolderReadsAsEnabledAndIsRefused(self):
        os.makedirs(os.path.dirname(self.path))
        for target in ("/", "//"):
            with self.subTest(target=target):
                os.symlink(target, self.path)
                try:
                    self.assertIs(autostart.isEnabled(self.configHome), True)
                    self.assertEqual(self.outcome(False), "[Errno 21] Is a directory: '/'")
                    self.assertEqual(self.outcome(True), "[Errno 21] Is a directory: '/'")
                    self.assertTrue(os.path.islink(self.path))
                finally:
                    os.unlink(self.path)

    def testAnAutostartFolderLinkToTheTopFolderIsRefused(self):
        os.symlink("/", os.path.dirname(self.path))
        # the top folder is root's, and the test's owner faking would hand it to this account when the suite runs as root
        self.owners["/"] = 0
        self.assertIs(autostart.isEnabled(self.configHome), True)
        self.assertEqual(self.outcome(False), "[Errno 13] Permission denied, the folder belongs to uid 0, not this account: '/'")
        self.assertIsNone(self.outcome(True))
        self.assertFalse(os.path.exists(os.path.join("/", autostart.FILE_NAME)))

    def testARelativeAutostartFolderLinkIsFollowed(self):
        configHome = os.path.join(self.configHome, "config")
        stowed = os.path.join(self.configHome, "dotfiles", "autostart")
        os.makedirs(configHome)
        os.makedirs(stowed)
        # GNU stow links each folder with a relative target
        os.symlink("../dotfiles/autostart", os.path.join(configHome, "autostart"))
        self.assertIsNone(self.outcome(False, configHome))
        with open(os.path.join(stowed, autostart.FILE_NAME)) as f:
            self.assertEqual(f.read(), "[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\n")
        self.assertIs(autostart.isEnabled(configHome), False)
        self.assertIsNone(self.outcome(True, configHome))
        self.assertEqual(os.listdir(stowed), [])
        self.assertTrue(os.path.islink(os.path.join(configHome, "autostart")))

    def testADotDotInALinkClimbsFromWhereTheLinkLeads(self):
        with tempfile.TemporaryDirectory() as other:
            deep = os.path.join(other, "deep")
            os.makedirs(deep)
            os.symlink(deep, os.path.dirname(self.path))
            real = os.path.join(other, "real.desktop")
            with open(real, "w") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n")
            # ".." climbs out of the folder the autostart link leads to, as the kernel does, and not back up the path as written
            os.symlink("../real.desktop", os.path.join(deep, autostart.FILE_NAME))
            self.assertIsNone(self.outcome(False))
            with open(real) as f:
                self.assertIn("Hidden=true", f.read())
            self.assertIs(autostart.isEnabled(self.configHome), False)
            self.assertEqual(sorted(os.listdir(self.configHome)), ["autostart"])

    def testALinkLoopIsRefused(self):
        os.makedirs(os.path.dirname(self.path))
        other = os.path.join(os.path.dirname(self.path), "other.desktop")
        os.symlink("other.desktop", self.path)
        os.symlink(autostart.FILE_NAME, other)
        # the walk follows 40 links, the kernel's limit, and refuses at the 41st, which is the override's own name
        message = "[Errno 40] Too many levels of symbolic links: '" + os.path.realpath(self.path) + "'"
        self.assertIs(autostart.isEnabled(self.configHome), True)
        self.assertEqual(self.outcome(False), message)
        self.assertEqual(self.outcome(True), message)
        self.assertTrue(os.path.islink(self.path))
        self.assertTrue(os.path.islink(other))

    def testAConfigFolderThatDoesntExistYetIsMade(self):
        configHome = os.path.join(self.configHome, "missing", "config")
        self.assertIs(autostart.isEnabled(configHome), True)
        self.assertIsNone(self.outcome(True, configHome))
        self.assertEqual(os.listdir(self.configHome), [])
        self.assertIsNone(self.outcome(False, configHome))
        with open(autostart.overridePath(configHome)) as f:
            self.assertEqual(f.read(), "[Desktop Entry]\nType=Application\nName=Mouse Battery\nHidden=true\n")
        self.assertIs(autostart.isEnabled(configHome), False)

    def testA0600OverrideKeepsItsMode(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Mouse Battery\nExec=logitech-mouse-battery\n")
        os.chmod(self.path, 0o600)
        autostart.setEnabled(self.configHome, False)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


if (__name__ == "__main__"):
    unittest.main()
