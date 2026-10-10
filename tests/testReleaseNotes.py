import os
import subprocess
import sys
import tempfile
import unittest

import version

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "packaging", "releaseNotes.py")
FOOTER = "\n\nInstall steps are in the README.\n"
SAMPLE = """# Changelog

Intro text.

## 1.2.0.1, 2026-10-12

### Fixed

- The prefix trap.

## 1.2.0, 2026-10-10

### Added

- The newest thing.

## 1.1.0, 2026-10-02

### Changed

- A middle thing.

### Fixed

- Another middle thing.

## 1.0.0, 2026-09-28

### Added

- The first thing.
"""


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = os.path.join(temp.name, "CHANGELOG.md")
        with open(self.path, "w") as f:
            f.write(SAMPLE)

    def notes(self, *args):
        return subprocess.run([sys.executable, "-B", SCRIPT, *args], capture_output=True, text=True)

    def testAMiddleSectionStopsAtTheNextHeading(self):
        result = self.notes("1.1.0", self.path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "### Changed\n\n- A middle thing.\n\n### Fixed\n\n- Another middle thing." + FOOTER)

    def testTheNewestSectionComesAfterAnExtraOneAndIntro(self):
        result = self.notes("1.2.0", self.path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "### Added\n\n- The newest thing." + FOOTER)

    def testTheLastSectionRunsToTheEndOfTheFile(self):
        result = self.notes("1.0.0", self.path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "### Added\n\n- The first thing." + FOOTER)

    def testAMissingVersionExitsOneWithNothingOnStdout(self):
        result = self.notes("9.9.9", self.path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("9.9.9", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def testAVersionThatPrefixesAnotherHeadingDoesntMatchIt(self):
        for name in ("1.2", "1.2.0.1", "11.2.0", "1.0"):
            self.assertEqual(self.notes(name, self.path).returncode, 1 if name != "1.2.0.1" else 0, name)
        self.assertEqual(self.notes("1.2.0.1", self.path).stdout, "### Fixed\n\n- The prefix trap." + FOOTER)

    def testAnUnreadableFileExitsOne(self):
        result = self.notes("1.0.0", self.path + ".missing")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def testNoVersionArgumentExitsOne(self):
        result = self.notes()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def testTheRealChangelogHasASectionForEveryTagAndTheCurrentVersion(self):
        for name in ("1.0.0", "1.0.1", "1.1.0", "1.1.1", version.VERSION):
            result = self.notes(name)
            self.assertEqual(result.returncode, 0, name + ": " + result.stderr)
            self.assertTrue(result.stdout.startswith("### "), name)


if (__name__ == "__main__"):
    unittest.main()
