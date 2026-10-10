import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

import settings

# the suite runs as root in the containers, so the tests act as a non root account and root's exemptions can't pass a test on their own
ACCOUNT = os.getuid() or 1000


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.folder)

    def testNothingSavedIsOff(self):
        self.assertEqual(settings.loadSleepCheck(self.folder), 0)

    def testAChoiceSavedAsTextReadsBackAsANumber(self):
        self.assertTrue(settings.saveSleepCheck(self.folder, 30))
        self.assertEqual(settings.loadSleepCheck(self.folder), 30)
        with open(settings.settingsPath(self.folder)) as f:
            self.assertIn("sleepCheckSeconds=30", f.read())

    def testAValueThatIsntAChoiceIsOff(self):
        os.makedirs(os.path.dirname(settings.settingsPath(self.folder)))
        for text in ("45", "soon", ""):
            with open(settings.settingsPath(self.folder), "w") as f:
                f.write("[General]\nsleepCheckSeconds=" + text + "\n")
            self.assertEqual(settings.loadSleepCheck(self.folder), 0, text)


    def testNothingSavedIsTheWeek(self):
        self.assertEqual(settings.loadLevelRange(self.folder), "week")

    def testAPickedRangeIsKeptBesideTheSleepCheck(self):
        self.assertTrue(settings.saveSleepCheck(self.folder, 60))
        self.assertTrue(settings.saveLevelRange(self.folder, "halfYear"))
        self.assertEqual((settings.loadLevelRange(self.folder), settings.loadSleepCheck(self.folder)), ("halfYear", 60))

    def testARangeThatIsntAChoiceIsTheWeek(self):
        os.makedirs(os.path.dirname(settings.settingsPath(self.folder)))
        with open(settings.settingsPath(self.folder), "w") as f:
            f.write("[General]\nlevelRange=decade\n")
        self.assertEqual(settings.loadLevelRange(self.folder), "week")

    def testTheChoicesAreTheServicesRanges(self):
        import history
        self.assertEqual(settings.LEVEL_RANGE_CHOICES, tuple(history.LEVEL_RANGES))


class PlantedSettingsTests(unittest.TestCase):
    # a run as root with another account's XDG_CONFIG_HOME finds their files, so each is told apart by the owner it reports
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.configHome = os.path.join(self.temp.name, "config")
        os.makedirs(self.configHome)
        self.folder = os.path.dirname(settings.settingsPath(self.configHome))
        self.elsewhere = os.path.join(self.temp.name, "elsewhere")
        os.makedirs(self.elsewhere)
        self.target = os.path.join(self.elsewhere, "target.ini")
        with open(self.target, "w") as f:
            f.write("[General]\nlevelRange=month\n")
        self.owners = {}
        realUid = os.getuid()
        realFstat = os.fstat

        def ownerFor(fd, *args, **kwargs):
            info = realFstat(fd, *args, **kwargs)
            fields = list(info[:10])
            fields[4] = self.owners.get(os.readlink("/proc/self/fd/" + str(fd)), ACCOUNT if info.st_uid == realUid else info.st_uid)
            return os.stat_result(fields)

        for patch in (mock.patch.object(os, "getuid", return_value=ACCOUNT), mock.patch.object(os, "fstat", ownerFor)):
            patch.start()
            self.addCleanup(patch.stop)

    def targetText(self):
        with open(self.target) as f:
            return f.read()

    def plantFileLink(self):
        os.makedirs(self.folder)
        os.symlink(self.target, settings.settingsPath(self.configHome))
        self.owners[settings.settingsPath(self.configHome)] = ACCOUNT + 1

    def plantFolderLink(self):
        with open(os.path.join(self.elsewhere, "settings.ini"), "w") as f:
            f.write("[General]\nlevelRange=month\n")
        os.symlink(self.elsewhere, self.folder)
        self.owners[self.folder] = ACCOUNT + 1

    def plantOtherFolder(self):
        os.makedirs(self.folder)
        with open(settings.settingsPath(self.configHome), "w") as f:
            f.write("[General]\nlevelRange=month\n")
        self.owners[self.folder] = ACCOUNT + 1

    def loadSaying(self, load):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            value = load(self.configHome)
        return (value, errors.getvalue())

    def testASaveDoesntFollowAFileLinkAnotherAccountMade(self):
        self.plantFileLink()
        saved = (settings.saveLevelRange(self.configHome, "day"), settings.saveSleepCheck(self.configHome, 30))
        self.assertEqual(self.targetText(), "[General]\nlevelRange=month\n")
        self.assertEqual(saved, (False, False))

    def testASaveDoesntFollowAFolderLinkAnotherAccountMade(self):
        self.plantFolderLink()
        saved = settings.saveLevelRange(self.configHome, "day")
        self.assertEqual(sorted(os.listdir(self.elsewhere)), ["settings.ini", "target.ini"])
        with open(os.path.join(self.elsewhere, "settings.ini")) as f:
            self.assertEqual(f.read(), "[General]\nlevelRange=month\n")
        self.assertFalse(saved)

    def testASaveLeavesAFolderAnotherAccountOwnsAlone(self):
        self.plantOtherFolder()
        saved = settings.saveLevelRange(self.configHome, "day")
        self.assertEqual(sorted(os.listdir(self.folder)), ["settings.ini"])
        with open(settings.settingsPath(self.configHome)) as f:
            self.assertEqual(f.read(), "[General]\nlevelRange=month\n")
        self.assertFalse(saved)

    def testASaveLeavesAFileAnotherAccountOwnsAlone(self):
        os.makedirs(self.folder)
        path = settings.settingsPath(self.configHome)
        with open(path, "w") as f:
            f.write("[General]\nlevelRange=month\n")
        self.owners[path] = ACCOUNT + 1
        saved = settings.saveLevelRange(self.configHome, "day")
        with open(path) as f:
            self.assertEqual(f.read(), "[General]\nlevelRange=month\n")
        self.assertFalse(saved)

    def testALoadReadsNothingThroughAPlantedLinkOrFolder(self):
        for plant in (self.plantFileLink, self.plantFolderLink, self.plantOtherFolder):
            with self.subTest(plant=plant.__name__):
                self.owners.clear()
                if (os.path.islink(self.folder)):
                    os.unlink(self.folder)
                elif (os.path.isdir(self.folder)):
                    shutil.rmtree(self.folder)
                plant()
                value, said = self.loadSaying(settings.loadLevelRange)
                self.assertEqual(value, "week")
                self.assertEqual(len(said.splitlines()), 1, said)
                self.assertIn("Couldn't read the settings in " + settings.settingsPath(self.configHome), said)

    def testALinkThisAccountMadeIsFollowed(self):
        os.makedirs(self.folder)
        os.symlink(self.target, settings.settingsPath(self.configHome))
        self.assertTrue(settings.saveSleepCheck(self.configHome, 30))
        self.assertTrue(os.path.islink(settings.settingsPath(self.configHome)))
        self.assertEqual(self.targetText(), "[General]\nlevelRange=month\nsleepCheckSeconds=30\n")
        self.assertEqual(self.loadSaying(settings.loadLevelRange), ("month", ""))

    def testAQSettingsFileKeepsItsOtherKeys(self):
        os.makedirs(self.folder)
        with open(settings.settingsPath(self.configHome), "w") as f:
            f.write("[General]\nlevelRange=month\nsleepCheckSeconds=30\n\n[Other]\nkept=1\n")
        self.assertTrue(settings.saveSleepCheck(self.configHome, 60))
        with open(settings.settingsPath(self.configHome)) as f:
            self.assertEqual(f.read(), "[General]\nlevelRange=month\nsleepCheckSeconds=60\n\n[Other]\nkept=1\n")
        self.assertEqual((settings.loadSleepCheck(self.configHome), settings.loadLevelRange(self.configHome)), (60, "month"))


if (__name__ == "__main__"):
    unittest.main()
