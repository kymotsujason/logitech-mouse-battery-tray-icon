import os
import shutil
import tempfile
import unittest

import settings


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


if (__name__ == "__main__"):
    unittest.main()
