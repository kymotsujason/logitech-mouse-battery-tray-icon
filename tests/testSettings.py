import contextlib
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from PyQt6.QtCore import QByteArray, QPoint, QSettings
from PyQt6.QtGui import QColor

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



MISSING = object()
# every value goes in under both keys this app reads, written by the real QSettings
QT_VALUES = ["month", "60", " month ", "month ", "mo\"nth", "mo\\nth", "mo\nnth", "\t\r\x07\x08\x0b\x0c", "a,b", "a, b", "a;b", "a=b", "@month", "@@x", "@Invalid()", "", "caf\u00e9", "first\u2028second", "\x01\x7f", "x\u00a0y", ";x", "[x]", "#x", " ", "\\x41", "\U0001F600", 60, ["a", "b"], ["month"], QByteArray(b"60"), QPoint(1, 2), None, QColor("red")]
# forms a hand or another writer leaves, each read by the real QSettings too
HAND_FORMS = [
    "[General]\nlevelRange=\"month\"\nsleepCheckSeconds=\"60\"\n",
    "[General]\nlevelRange=\"mo\"nth\nsleepCheckSeconds=6\"0\"\n",
    "[General]\nlevelRange=\"mon\" \"th\"\n",
    "[General]\nlevelRange=mo\\x6eth\nsleepCheckSeconds=\\x36\\60\n",
    "[General]\nlevelRange=\\x1006d\\155onth\n",
    "[General]\nlevelRange=\\month\nsleepCheckSeconds=\\q60\n",
    "[General]\nlevelRange=\\u006donth\n",
    "[General]\nlevelRange=month ;a comment\nsleepCheckSeconds=60;\n",
    "[General]\n;levelRange=day\nlevelRange=month\n",
    "[General]\n#levelRange=month\n",
    "[General]\nlevel%52ange=month\nsleep%U0043heckSeconds=60\n",
    "[General]\nlevel%5Range=month\n",
    "[General]\nlevel\\Range=month\n",
    "[General]\nlevelRange\\=month\n",
    "[General]\n \t levelRange \t = \t month \t \n",
    "[General]\nlevelRange=mo\\\nnth\nsleepCheckSeconds=6\\\r\n0\n",
    "[General]\nlevelRange=\"month\nsleepCheckSeconds=60\"\n",
    "[General]\nlevelRange=day\nlevelRange=month\n",
    "levelRange=month\n[Other]\nsleepCheckSeconds=60\n",
    "[general]\nlevelRange=month\n",
    "[ GENERAL ] trailing\nlevelRange=month\n",
    "[%General]\nlevelRange=month\n",
    "[Gen%65ral]\nlevelRange=month\n",
    "[Other]\n[General]\nlevelRange=month\n",
    "[General\nlevelRange=month\n",
    "[General]\r\nlevelRange=month\r\nsleepCheckSeconds=60\r\n",
    "[General]\rlevelRange=month\rsleepCheckSeconds=60\r",
    "[General]\nlevelRange=@@month\n",
    "[General]\nlevelRange=@String(mo\\x6eth)\n",
    "[General]\nlevelRange=@ByteArray(month)\nsleepCheckSeconds=@Invalid()\n",
    "[General]\nlevelRange=month, day\nsleepCheckSeconds=\"60\",\n",
    "[General]\nlevelRange=\"month, day\"\n",
    "[General]\nlevelRange=\\x\nsleepCheckSeconds=60\\\n",
    "[General]\nlevelRange\nsleepCheckSeconds\n",
    "[General]\nlevelRange=a  \\\nsleepCheckSeconds=\\x6d6f\\777777\\x1F600\n",
    "[General]\nlevelRange=\"a\"  b  \nsleepCheckSeconds=\"6\" ;0\n",
    "[General]\nlevelRange=\\\"mo\nsleepCheckSeconds=mon th  \n",
    "[General]\nlevelRange=a\\t \nsleepCheckSeconds=\\08\n",
    "\ufeff[Other]\nlevelRange=month\n",
    "\ufefflevelRange=month\n[General]\n\ufeffsleepCheckSeconds=60\n",
    "[General]\nlevelRange=\ufeffmonth\nsleepCheckSeconds=\\x36\ufeff0\n",
    "[General]\nlevelRange=mo\ufeffnth\nsleepCheckSeconds=\ufeff\ufeff60\n",
    "[General]\n\ufeff\ufefflevelRange=month\n[\ufeffGeneral]\nsleepCheckSeconds=60\n",
]


class QSettingsFormTests(unittest.TestCase):
    # QSettings wrote these files before 1.2.0 stopped using it, so they're read as QSettings reads them
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.count = 0

    def newHome(self):
        self.count += 1
        home = os.path.join(self.temp.name, "home" + str(self.count))
        os.makedirs(os.path.dirname(settings.settingsPath(home)))
        return home

    def qtWrite(self, values):
        home = self.newHome()
        writer = QSettings(settings.settingsPath(home), QSettings.Format.IniFormat)
        for key, value in values.items():
            writer.setValue(key, value)
        writer.sync()
        self.assertEqual(writer.status(), QSettings.Status.NoError)
        del writer
        return home

    def handWrite(self, text):
        home = self.newHome()
        with open(settings.settingsPath(home), "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return home

    def fileText(self, home):
        with open(settings.settingsPath(home), encoding="utf-8", newline="") as f:
            return f.read()

    def qtRead(self, home):
        # read from a fresh copy, since QSettings keeps the files it has read in a cache by their path
        self.count += 1
        copy = os.path.join(self.temp.name, "read" + str(self.count) + ".ini")
        shutil.copyfile(settings.settingsPath(home), copy)
        reader = QSettings(copy, QSettings.Format.IniFormat)
        found = {key: reader.value(key) for key in reader.allKeys()}
        del reader
        return found

    def appChoices(self, found):
        # what the app made of QSettings' answer while it read through QSettings
        level = found.get(settings.LEVEL_RANGE_KEY, settings.LEVEL_RANGE_DEFAULT)
        level = level if isinstance(level, str) and level in settings.LEVEL_RANGE_CHOICES else settings.LEVEL_RANGE_DEFAULT
        seconds = found.get(settings.SLEEP_CHECK_KEY, 0)
        try:
            seconds = int(seconds) if isinstance(seconds, (str, int)) else 0
        except ValueError:
            seconds = 0
        return (level, seconds if seconds in settings.SLEEP_CHECK_CHOICES else 0)

    def assertReadsAsQSettings(self, home, exact=True):
        found = self.qtRead(home)
        values = settings.readValues(home)
        for key in (settings.LEVEL_RANGE_KEY, settings.SLEEP_CHECK_KEY):
            theirs = found.get(key, MISSING)
            ours = values.get(key, MISSING)
            if (theirs is MISSING):
                self.assertIs(ours, MISSING, key)
            elif (isinstance(theirs, str) and exact):
                self.assertEqual(ours, theirs, key)
            elif (not isinstance(theirs, str)):
                # a list, a byte array, an invalid value, or another type is no choice, so it reads as nothing
                self.assertIsNone(ours, key)
        self.assertEqual((settings.loadLevelRange(home), settings.loadSleepCheck(home)), self.appChoices(found))

    def testEveryValueQSettingsWritesReadsTheSame(self):
        for value in QT_VALUES:
            with self.subTest(value=repr(value)):
                home = self.qtWrite({settings.LEVEL_RANGE_KEY: value, settings.SLEEP_CHECK_KEY: value})
                self.assertReadsAsQSettings(home)

    def testHandWrittenFormsReadAsQSettingsReadsThem(self):
        for text in HAND_FORMS:
            with self.subTest(text=text):
                self.assertReadsAsQSettings(self.handWrite(text))

    def testQuotedChoicesLoad(self):
        home = self.handWrite("[General]\nlevelRange=\"month\"\nsleepCheckSeconds=\"60\"\n")
        self.assertEqual((settings.loadLevelRange(home), settings.loadSleepCheck(home)), ("month", 60))

    def testAVariantValueReadsAsNothingAndIsKept(self):
        text = "[General]\nlevelRange=@Variant(\\0\\0\\0\\n\\0\\0\\0\\n\\0m\\0o\\0n\\0t\\0h)\n"
        home = self.handWrite(text)
        # QSettings unpacks this to month, and this app gives such a value up as unreadable instead
        self.assertEqual(self.qtRead(home)[settings.LEVEL_RANGE_KEY], "month")
        self.assertEqual(settings.loadLevelRange(home), settings.LEVEL_RANGE_DEFAULT)
        self.assertTrue(settings.saveSleepCheck(home, 60))
        self.assertEqual(self.fileText(home), text + "sleepCheckSeconds=60\n")

    def testASaveKeepsALineSeparatorInAnotherGroup(self):
        home = self.qtWrite({"Other/note": "first\u2028second", settings.SLEEP_CHECK_KEY: 30})
        before = self.fileText(home)
        self.assertTrue(settings.saveSleepCheck(home, 60))
        self.assertEqual(self.fileText(home), before.replace("sleepCheckSeconds=30", "sleepCheckSeconds=60"))
        self.assertEqual(self.qtRead(home), {"Other/note": "first\u2028second", settings.SLEEP_CHECK_KEY: "60"})

    def testASaveReplacesValuesSpanningLines(self):
        home = self.handWrite("[General]\nnote=\"a\nlevelRange=x\"\nlevelRange=mo\\\nnth\nsleep%43heckSeconds=30 ;kept\n")
        self.assertTrue(settings.saveLevelRange(home, "day"))
        self.assertTrue(settings.saveSleepCheck(home, 60))
        self.assertEqual(self.qtRead(home), {"note": "a\nlevelRange=x", settings.LEVEL_RANGE_KEY: "day", settings.SLEEP_CHECK_KEY: "60"})
        self.assertIn(";kept", self.fileText(home))

    def testASaveKeepsACrlfFile(self):
        home = self.handWrite("[General]\r\nlevelRange=month\r\n[Other]\r\nx=1\r\n")
        self.assertTrue(settings.saveSleepCheck(home, 30))
        self.assertEqual(self.qtRead(home), {settings.LEVEL_RANGE_KEY: "month", settings.SLEEP_CHECK_KEY: "30", "Other/x": "1"})
        self.assertTrue(self.fileText(home).startswith("[General]\r\nlevelRange=month\r\n"))

    def testASaveAddsTheGeneralGroupWhenOnlyOthersExist(self):
        home = self.qtWrite({"Other/x": 1})
        self.assertTrue(settings.saveLevelRange(home, "day"))
        self.assertEqual(self.qtRead(home), {"Other/x": "1", settings.LEVEL_RANGE_KEY: "day"})


class OverlappingSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name

    def testOverlappingSavesNeverPublishAnUnfinishedFile(self):
        path = settings.settingsPath(self.home)
        self.assertTrue(settings.saveLevelRange(self.home, "month"))
        realReplace = os.replace
        realFdopen = os.fdopen
        published = []
        second = []

        def publishLate(*args, **kwargs):
            if (second):
                return realReplace(*args, **kwargs)
            second.append(None)

            # the first save publishes once the second has made its own temporary file and before it writes anything there
            def publishFirst(fd, *more, **options):
                if (more[:1] == ("w",) and not published):
                    realReplace(*args, **kwargs)
                    with open(path) as f:
                        published.append(f.read())
                return realFdopen(fd, *more, **options)

            with mock.patch.object(os, "fdopen", publishFirst):
                second[0] = settings.saveLevelRange(self.home, "day")
            if (not published):
                realReplace(*args, **kwargs)
                with open(path) as f:
                    published.append(f.read())

        with mock.patch.object(os, "replace", publishLate):
            first = settings.saveSleepCheck(self.home, 30)
        self.assertEqual(published, ["[General]\nlevelRange=month\nsleepCheckSeconds=30\n"])
        self.assertEqual((first, second), (True, [True]))
        # the last save wins, and neither leaves a temporary file behind
        with open(path) as f:
            self.assertEqual(f.read(), "[General]\nlevelRange=day\n")
        self.assertEqual(os.listdir(os.path.dirname(path)), ["settings.ini"])


if (__name__ == "__main__"):
    unittest.main()
