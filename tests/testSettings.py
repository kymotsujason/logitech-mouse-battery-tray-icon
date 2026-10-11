import contextlib
import fcntl
import io
import os
import shutil
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock

from PyQt6.QtCore import QByteArray, QPoint, QSettings
from PyQt6.QtGui import QColor

import settings

# the suite runs as root in the containers, so the tests act as a non root account and root's exemptions can't pass a test on their own
ACCOUNT = os.getuid() or 1000
CANARY_CALLS = []


def recordCanary(tag):
    CANARY_CALLS.append(tag)
    return tag


class Canary:
    # QSettings stores this as a PyQt_PyObject pickle, and reading that pickle back calls recordCanary
    def __reduce__(self):
        return (recordCanary, ("ran",))


def writeCanaries(path):
    writer = QSettings(path, QSettings.Format.IniFormat)
    for key in (settings.LEVEL_RANGE_KEY, settings.SLEEP_CHECK_KEY, "Other/note"):
        writer.setValue(key, Canary())
    writer.sync()
    del writer


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

    def plantOtherFile(self):
        os.makedirs(self.folder)
        with open(settings.settingsPath(self.configHome), "w") as f:
            f.write("[General]\nlevelRange=month\n")
        self.owners[settings.settingsPath(self.configHome)] = ACCOUNT + 1

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
        for plant in (self.plantFileLink, self.plantFolderLink, self.plantOtherFolder, self.plantOtherFile):
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

    def testNothingUnpicklesAFileAnotherAccountOwns(self):
        uses = (("saveLevelRange", lambda home: settings.saveLevelRange(home, "day"), False), ("saveSleepCheck", lambda home: settings.saveSleepCheck(home, 30), False), ("loadLevelRange", settings.loadLevelRange, "week"), ("loadSleepCheck", settings.loadSleepCheck, 0))
        for owned in ("folder", "file"):
            for name, use, expected in uses:
                with self.subTest(owned=owned, use=name):
                    home = os.path.join(self.temp.name, owned + name)
                    path = settings.settingsPath(home)
                    os.makedirs(os.path.dirname(path))
                    writeCanaries(path)
                    self.owners[os.path.dirname(path) if owned == "folder" else path] = ACCOUNT + 1
                    with open(path, "rb") as f:
                        before = f.read()
                    del CANARY_CALLS[:]
                    with contextlib.redirect_stderr(io.StringIO()):
                        result = use(home)
                    self.assertEqual(CANARY_CALLS, [])
                    self.assertEqual(result, expected)
                    with open(path, "rb") as f:
                        self.assertEqual(f.read(), before)
                    self.assertEqual(os.listdir(os.path.dirname(path)), ["settings.ini"])

    def testNothingUnpicklesThisAccountsOwnFileWhoeverCanWriteIt(self):
        # a group the account shares, or any account at all, can put a pickle in a file this account owns, so the pickle is read as text and never run
        uses = (("saveLevelRange", lambda home: settings.saveLevelRange(home, "day"), True, "[General]\nlevelRange=day\n"), ("saveSleepCheck", lambda home: settings.saveSleepCheck(home, 30), True, "[General]\nsleepCheckSeconds=30\n"), ("loadLevelRange", settings.loadLevelRange, "week", None), ("loadSleepCheck", settings.loadSleepCheck, 0, None))
        for mode in (0o600, 0o664, 0o666):
            for name, use, expected, written in uses:
                with self.subTest(mode=oct(mode), use=name):
                    home = os.path.join(self.temp.name, oct(mode) + name)
                    path = settings.settingsPath(home)
                    os.makedirs(os.path.dirname(path))
                    writeCanaries(path)
                    os.chmod(path, mode)
                    with open(path, "rb") as f:
                        before = f.read()
                    self.assertIn(b"recordCanary", before)
                    del CANARY_CALLS[:]
                    said = io.StringIO()
                    with contextlib.redirect_stderr(said):
                        result = use(home)
                    self.assertEqual(CANARY_CALLS, [])
                    self.assertEqual((result, said.getvalue()), (expected, ""))
                    with open(path, "rb") as f:
                        self.assertEqual(f.read(), before if written is None else written.encode())
                    self.assertEqual(os.listdir(os.path.dirname(path)), ["settings.ini"])

    def testAFileAndFolderOtherAccountsCanWriteAreStillRead(self):
        # a umask of 002 leaves group write on the user's own files, and a vfat or CIFS home reports 0777 for everything, so neither turns the user's settings away
        for folderMode, fileMode in ((0o775, 0o664), (0o777, 0o666)):
            with self.subTest(folder=oct(folderMode), file=oct(fileMode)):
                if (os.path.isdir(self.folder)):
                    shutil.rmtree(self.folder)
                os.makedirs(self.folder)
                path = settings.settingsPath(self.configHome)
                with open(path, "w") as f:
                    f.write("[General]\nlevelRange=month\nsleepCheckSeconds=60\n")
                os.chmod(self.folder, folderMode)
                os.chmod(path, fileMode)
                self.assertEqual(self.loadSaying(settings.loadLevelRange), ("month", ""))
                self.assertEqual(self.loadSaying(settings.loadSleepCheck), (60, ""))
                self.assertTrue(settings.saveSleepCheck(self.configHome, 30))
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), fileMode)
                self.assertEqual(self.loadSaying(settings.loadSleepCheck), (30, ""))

    def testTheCanaryRunsWhenQSettingsReadsTheFile(self):
        # the checks above only mean something if the planted pickle really runs once QSettings reads the file
        os.makedirs(self.folder)
        writeCanaries(settings.settingsPath(self.configHome))
        # QSettings keeps the files it has read in a cache by their path, so a copy is what gets read
        copy = os.path.join(self.temp.name, "copy.ini")
        shutil.copyfile(settings.settingsPath(self.configHome), copy)
        del CANARY_CALLS[:]
        reader = QSettings(copy, QSettings.Format.IniFormat)
        reader.value(settings.LEVEL_RANGE_KEY)
        del reader
        self.assertIn("ran", CANARY_CALLS)

    def testALinkThisAccountMadeIsFollowed(self):
        os.makedirs(self.folder)
        os.symlink(self.target, settings.settingsPath(self.configHome))
        self.assertTrue(settings.saveSleepCheck(self.configHome, 30))
        self.assertTrue(os.path.islink(settings.settingsPath(self.configHome)))
        self.assertEqual(self.targetText(), "[General]\nlevelRange=month\nsleepCheckSeconds=30\n")
        self.assertEqual(self.loadSaying(settings.loadLevelRange), ("month", ""))

    def testASaveWritesOnlyTheAppsTwoKeys(self):
        os.makedirs(self.folder)
        with open(settings.settingsPath(self.configHome), "w") as f:
            f.write("; a comment\n[General]\nlevelRange=month\nnote=1\nsleepCheckSeconds=30\n\n[Other]\nkept=1\n")
        self.assertTrue(settings.saveSleepCheck(self.configHome, 60))
        with open(settings.settingsPath(self.configHome)) as f:
            self.assertEqual(f.read(), "[General]\nlevelRange=month\nsleepCheckSeconds=60\n")
        self.assertEqual((settings.loadSleepCheck(self.configHome), settings.loadLevelRange(self.configHome)), (60, "month"))



# every value goes in under both keys this app reads, written by the real QSettings
QT_VALUES = ["month", "60", " month ", "month ", "mo\"nth", "mo\\nth", "mo\nnth", "\t\r\x07\x08\x0b\x0c", "a,b", "a, b", "a;b", "a=b", "@month", "@@x", "@Invalid()", "", "caf\u00e9", "first\u2028second", "\x01\x7f", "x\u00a0y", ";x", "[x]", "#x", " ", "\\x41", "\U0001F600", 60, ["a", "b"], ["month"], QByteArray(b"60"), QPoint(1, 2), None, QColor("red")]
# forms a hand or another writer leaves
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
    "[General]\nsleepCheckSeconds=@Variant(\\0\\0\\0\\x2\\0\\0\\0<)\nlevelRange=@Variant(\\0\\0\\0\\x2\\0\\0\\0<)\n",
]
# the forms that hold a plain or quoted choice in the General group, while every other form reads as no choice
CHOICE_FORMS = {
    "[General]\nlevelRange=\"month\"\nsleepCheckSeconds=\"60\"\n": ("month", 60),
    "[General]\n;levelRange=day\nlevelRange=month\n": ("month", 0),
    "[General]\n \t levelRange \t = \t month \t \n": ("month", 0),
    "[General]\nlevelRange=day\nlevelRange=month\n": ("month", 0),
    "[Other]\n[General]\nlevelRange=month\n": ("month", 0),
    "[General]\r\nlevelRange=month\r\nsleepCheckSeconds=60\r\n": ("month", 60),
    "[General]\rlevelRange=month\rsleepCheckSeconds=60\r": ("month", 60),
}


class HandReaderFormTests(unittest.TestCase):
    # the file is read by hand instead of through QSettings, so an odd form reads as no choice and runs nothing
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

    def qtChoices(self, found):
        level = found.get(settings.LEVEL_RANGE_KEY, settings.LEVEL_RANGE_DEFAULT)
        level = level if isinstance(level, str) and level in settings.LEVEL_RANGE_CHOICES else settings.LEVEL_RANGE_DEFAULT
        seconds = found.get(settings.SLEEP_CHECK_KEY, 0)
        try:
            seconds = int(seconds) if isinstance(seconds, (str, int)) else 0
        except ValueError:
            seconds = 0
        return (level, seconds if seconds in settings.SLEEP_CHECK_CHOICES else 0)

    def loaded(self, home):
        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            choices = (settings.loadLevelRange(home), settings.loadSleepCheck(home))
        self.assertEqual(said.getvalue(), "")
        return choices

    def assertNoChoiceQSettingsWouldntFind(self, home):
        # the hand reader turns away forms QSettings reads, but it never finds a choice QSettings wouldn't
        theirs = self.qtChoices(self.qtRead(home))
        for ours, qt, default in zip(self.loaded(home), theirs, (settings.LEVEL_RANGE_DEFAULT, 0)):
            self.assertIn(ours, (default, qt))

    def testNoValueQSettingsWritesReadsAsAnotherChoice(self):
        for value in QT_VALUES:
            with self.subTest(value=repr(value)):
                self.assertNoChoiceQSettingsWouldntFind(self.qtWrite({settings.LEVEL_RANGE_KEY: value, settings.SLEEP_CHECK_KEY: value}))

    def testChoicesQSettingsWroteAsTextLoad(self):
        self.assertEqual(self.loaded(self.qtWrite({settings.LEVEL_RANGE_KEY: "month", settings.SLEEP_CHECK_KEY: "60"})), ("month", 60))

    def testHandWrittenFormsReadOnlyAsPlainOrQuotedChoices(self):
        self.assertLessEqual(set(CHOICE_FORMS), set(HAND_FORMS))
        for text in HAND_FORMS:
            with self.subTest(text=text):
                home = self.handWrite(text)
                level, seconds = CHOICE_FORMS.get(text, (settings.LEVEL_RANGE_DEFAULT, 0))
                self.assertEqual(self.loaded(home), (level, seconds))
                self.assertNoChoiceQSettingsWouldntFind(home)
                self.assertTrue(settings.saveSleepCheck(home, 30))
                self.assertEqual(self.fileText(home), "[General]\n" + ("" if text not in CHOICE_FORMS else "levelRange=" + level + "\n") + "sleepCheckSeconds=30\n")

    def testQuotedChoicesLoad(self):
        home = self.handWrite("[General]\nlevelRange=\"month\"\nsleepCheckSeconds=\"60\"\n")
        self.assertEqual(self.loaded(home), ("month", 60))

    def testAPackedValueIsNoChoice(self):
        # no release wrote anything but plain text, so a packed value, even one QSettings reads as a choice, is one to turn away
        home = self.handWrite("[General]\nsleepCheckSeconds=@Variant(\\0\\0\\0\\x2\\0\\0\\0<)\nlevelRange=@Variant(\\0\\0\\0\\n\\0\\0\\0\\n\\0m\\0o\\0n\\0t\\0h)\n")
        self.assertEqual(self.qtChoices(self.qtRead(home)), ("month", 60))
        self.assertEqual(self.loaded(home), ("week", 0))
        self.assertTrue(settings.saveLevelRange(home, "day"))
        self.assertEqual(self.fileText(home), "[General]\nlevelRange=day\n")

    def testAMalformedPythonValueIsNoChoice(self):
        # a pickle naming a function that doesn't exist made QSettings raise SystemError as it read the file
        home = self.qtWrite({settings.LEVEL_RANGE_KEY: Canary(), settings.SLEEP_CHECK_KEY: "60"})
        text = self.fileText(home)
        self.assertIn("recordCanary", text)
        with open(settings.settingsPath(home), "w", encoding="utf-8", newline="") as f:
            f.write(text.replace("recordCanary", "missingCanar"))
        self.assertEqual(self.loaded(home), ("week", 60))
        self.assertTrue(settings.saveLevelRange(home, "day"))
        self.assertEqual(self.fileText(home), "[General]\nlevelRange=day\nsleepCheckSeconds=60\n")

    def testALineSeparatorInAnotherGroupDoesntSplitTheChoices(self):
        home = self.qtWrite({"Other/note": "first\u2028levelRange=day", settings.LEVEL_RANGE_KEY: "month", settings.SLEEP_CHECK_KEY: "30"})
        self.assertTrue(settings.saveSleepCheck(home, 60))
        self.assertEqual(self.fileText(home), "[General]\nlevelRange=month\nsleepCheckSeconds=60\n")

    def testASaveReplacesValuesSpanningLines(self):
        home = self.handWrite("[General]\nnote=\"a\nlevelRange=x\"\nlevelRange=mo\\\nnth\nsleep%43heckSeconds=30 ;kept\n")
        self.assertTrue(settings.saveLevelRange(home, "day"))
        self.assertTrue(settings.saveSleepCheck(home, 60))
        self.assertEqual(self.qtRead(home), {settings.LEVEL_RANGE_KEY: "day", settings.SLEEP_CHECK_KEY: "60"})

    def testASaveKeepsTheChoicesOfACrlfFile(self):
        home = self.handWrite("[General]\r\nlevelRange=month\r\n[Other]\r\nx=1\r\n")
        self.assertTrue(settings.saveSleepCheck(home, 30))
        self.assertEqual(self.qtRead(home), {settings.LEVEL_RANGE_KEY: "month", settings.SLEEP_CHECK_KEY: "30"})

    def testASaveWritesTheGeneralGroupWhenOnlyOthersExist(self):
        home = self.qtWrite({"Other/x": 1})
        self.assertTrue(settings.saveLevelRange(home, "day"))
        self.assertEqual(self.fileText(home), "[General]\nlevelRange=day\n")

    def testLongCharacterCodesDontStallALoadOrASave(self):
        for escape in ("\\x" + "1" * 200000, "\\" + "1" * 200000):
            with self.subTest(escape=escape[:4]):
                home = self.handWrite("[General]\nnote=" + escape + "\nsleepCheckSeconds=60\nlevelRange=month\n")
                started = time.monotonic()
                loaded = (settings.loadSleepCheck(home), settings.loadLevelRange(home))
                saved = settings.saveLevelRange(home, "day")
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual((loaded, saved, settings.loadLevelRange(home)), ((60, "month"), True, "day"))

    def testASettingAddedAfterAnUnfinishedValueStaysItsOwn(self):
        for ending in ("tail\\", "\"tail"):
            for key, value, save, load in ((settings.SLEEP_CHECK_KEY, "60", lambda home: settings.saveSleepCheck(home, 60), settings.loadSleepCheck), (settings.LEVEL_RANGE_KEY, "day", lambda home: settings.saveLevelRange(home, "day"), settings.loadLevelRange)):
                with self.subTest(ending=ending, key=key):
                    home = self.handWrite("[General]\nnote=" + ending)
                    self.assertTrue(save(home))
                    self.assertEqual(str(load(home)), value)
                    self.assertEqual(self.qtRead(home), {key: value})


class FileOnlyTests(unittest.TestCase):
    # PyQt unpickles what QSettings reads, so a load or a save never hands the file to QSettings at all
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = os.path.join(self.temp.name, "config")

    def testLoadsAndSavesNeverMakeAQSettings(self):
        made = []

        class Recording(QSettings):
            def __init__(self, *args):
                made.append(args)
                super().__init__(*args)

        with mock.patch("PyQt6.QtCore.QSettings", Recording), mock.patch.object(settings, "QSettings", Recording, create=True):
            self.assertTrue(settings.saveSleepCheck(self.home, 60))
            self.assertTrue(settings.saveLevelRange(self.home, "day"))
            self.assertEqual((settings.loadSleepCheck(self.home), settings.loadLevelRange(self.home)), (60, "day"))
        self.assertEqual(made, [])

    def testAFailedSaveChangesNothing(self):
        self.assertTrue(settings.saveSleepCheck(self.home, 60))
        path = settings.settingsPath(self.home)
        with mock.patch.object(settings.autostart, "writeText", side_effect=OSError("No space left on device")):
            self.assertFalse(settings.saveLevelRange(self.home, "day"))
        self.assertEqual((settings.loadLevelRange(self.home), settings.loadSleepCheck(self.home)), ("week", 60))
        self.assertEqual(os.listdir(os.path.dirname(path)), ["settings.ini"])

    def testASaveOfAValueThatIsntAChoiceChangesNothing(self):
        self.assertTrue(settings.saveSleepCheck(self.home, 60))
        path = settings.settingsPath(self.home)
        with open(path) as f:
            before = f.read()
        self.assertEqual((settings.saveLevelRange(self.home, "day\n[Other]"), settings.saveLevelRange(self.home, None), settings.saveSleepCheck(self.home, 45), settings.saveSleepCheck(self.home, "60\nx=1")), (False, False, False, False))
        with open(path) as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["settings.ini"])


class OverlappingSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = self.temp.name

    def testOverlappingSavesToDifferentKeysKeepBoth(self):
        # each save waits after its read for the other to read too, which only happens when nothing keeps the two apart
        self.assertTrue(settings.saveLevelRange(self.home, "month"))
        realReadFile = settings.autostart.readFile
        bothRead = threading.Barrier(2, timeout=0.5)

        def readThenWait(*args, **kwargs):
            found = realReadFile(*args, **kwargs)
            with contextlib.suppress(threading.BrokenBarrierError):
                bothRead.wait()
            return found

        results = {}

        def save(name, call):
            results[name] = call()

        with mock.patch.object(settings.autostart, "readFile", readThenWait):
            threads = [threading.Thread(target=save, args=("range", lambda: settings.saveLevelRange(self.home, "day"))), threading.Thread(target=save, args=("sleep", lambda: settings.saveSleepCheck(self.home, 30)))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(results, {"range": True, "sleep": True})
        self.assertEqual((settings.loadLevelRange(self.home), settings.loadSleepCheck(self.home)), ("day", 30))
        self.assertEqual(os.listdir(os.path.dirname(settings.settingsPath(self.home))), ["settings.ini"])

    def testASaveGivesUpWhileAnotherHoldsTheLock(self):
        self.assertTrue(settings.saveLevelRange(self.home, "month"))
        folder = os.path.dirname(settings.settingsPath(self.home))
        holder = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX)
        started = time.monotonic()
        with mock.patch.object(settings, "LOCK_WAIT_SECONDS", 0.2):
            self.assertFalse(settings.saveLevelRange(self.home, "day"))
        self.assertLess(time.monotonic() - started, 1.0)
        fcntl.flock(holder, fcntl.LOCK_UN)
        self.assertEqual(settings.loadLevelRange(self.home), "month")
        self.assertTrue(settings.saveLevelRange(self.home, "day"))
        self.assertEqual(os.listdir(folder), ["settings.ini"])

if (__name__ == "__main__"):
    unittest.main()
