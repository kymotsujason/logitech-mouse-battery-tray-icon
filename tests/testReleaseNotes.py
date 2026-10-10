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

    def write(self, contents, newline=None):
        with open(self.path, "w", encoding="utf-8", newline=newline) as f:
            f.write(contents)

    def testTheCheckFailsASectionHeadedUnreleased(self):
        self.write(SAMPLE.replace("## 1.2.0, 2026-10-10", "## 1.2.0, unreleased"))
        result = self.notes("--check", "1.2.0", self.path)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("1.2.0", result.stderr)
        self.assertIn("unreleased", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def testTheCheckFailsAHeadingWithoutAProperDate(self):
        for heading in ("## 1.2.0, 2026-10", "## 1.2.0, 2026-13-40", "## 1.2.0, 10/10/2026", "## 1.2.0,"):
            self.write(SAMPLE.replace("## 1.2.0, 2026-10-10", heading))
            result = self.notes("--check", "1.2.0", self.path)
            self.assertEqual(result.returncode, 1, heading)
            self.assertIn("1.2.0", result.stderr, heading)

    def testTheCheckPassesADatedSection(self):
        result = self.notes("--check", "1.2.0", self.path)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def testTheCheckFailsAMissingSection(self):
        result = self.notes("--check", "9.9.9", self.path)
        self.assertEqual(result.returncode, 1)
        self.assertIn("9.9.9", result.stderr)

    def testASectionWithNoEntriesFailsTheCheckAndGivesNoNotes(self):
        empties = {"empty": "## 1.2.0, 2026-10-10\n## 1.1.0, 2026-10-02", "whitespace": "## 1.2.0, 2026-10-10\n   \n\t\n\n## 1.1.0, 2026-10-02", "heading only": "## 1.2.0, 2026-10-10\n\n### Added\n\n## 1.1.0, 2026-10-02"}
        populated = "## 1.2.0, 2026-10-10\n\n### Added\n\n- The newest thing.\n\n## 1.1.0, 2026-10-02"
        self.assertIn(populated, SAMPLE)
        for name, text in empties.items():
            self.write(SAMPLE.replace(populated, text))
            for args in (("--check", "1.2.0"), ("1.2.0",)):
                with self.subTest(section=name, args=args):
                    result = self.notes(*args, self.path)
                    self.assertEqual((result.returncode, result.stdout), (1, ""))
                    self.assertIn("1.2.0", result.stderr)
                    self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def testAPopulatedSectionPassesTheEntryCheck(self):
        for args in (("--check", "1.2.0"), ("1.2.0",)):
            self.assertEqual(self.notes(*args, self.path).returncode, 0, args)

    def assertRefused(self, contents):
        self.write(contents)
        for args in (("--check", "1.2.0"), ("1.2.0",)):
            with self.subTest(contents=contents, args=args):
                result = self.notes(*args, self.path)
                self.assertEqual((result.returncode, result.stdout), (1, ""))
                self.assertIn("1.2.0", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def testASectionWithOnlyCommentedEntriesFails(self):
        populated = "### Added\n\n- The newest thing.\n"
        for comment in ("<!--\n- TODO write notes\n-->\n", "<!-- - TODO write notes -->\n", "<!--\n- TODO write notes\n"):
            self.assertRefused(SAMPLE.replace(populated, "### Added\n\n" + comment))

    def testASectionWithACommentBesideAnEntryFails(self):
        for comment in ("<!--\n- TODO write notes\n-->\n- The newest thing.\n", "- The newest thing. <!-- a note to self -->\n", "- The newest thing.\n<!-- one more -->\n"):
            self.assertRefused(SAMPLE.replace("- The newest thing.\n", comment))

    def testACommentOpenerInCodeFailsInsteadOfCuttingTheNotes(self):
        span = "- Render `<!--` as text in the label.\n- The newest thing.\n"
        fence = "- The newest thing.\n\n```html\n<!-- kept -->\n```\n\n- One more thing.\n"
        for code in (span, fence):
            self.assertRefused(SAMPLE.replace("- The newest thing.\n", code))

    def testACommentInAnotherSectionLeavesThisOneAlone(self):
        self.write(SAMPLE.replace("- A middle thing.\n", "- A middle thing. <!-- a note to self -->\n"))
        self.assertEqual(self.notes("--check", "1.2.0", self.path).returncode, 0)
        result = self.notes("1.2.0", self.path)
        self.assertEqual((result.returncode, result.stdout), (0, "### Added\n\n- The newest thing." + FOOTER))
        self.assertEqual(self.notes("1.1.0", self.path).returncode, 1)

    def testACrlfFileGivesTheSameNotes(self):
        self.write(SAMPLE, newline="\r\n")
        with open(self.path, "rb") as f:
            self.assertIn(b"\r\n", f.read())
        result = self.notes("1.1.0", self.path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "### Changed\n\n- A middle thing.\n\n### Fixed\n\n- Another middle thing." + FOOTER)
        self.assertEqual(self.notes("--check", "1.1.0", self.path).returncode, 0)

    def testExtraSpacesAfterTheHashesStillMatch(self):
        self.write(SAMPLE.replace("## 1.1.0, 2026-10-02", "##   1.1.0,  2026-10-02 "))
        result = self.notes("1.1.0", self.path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "### Changed\n\n- A middle thing.\n\n### Fixed\n\n- Another middle thing." + FOOTER)
        self.assertEqual(self.notes("--check", "1.1.0", self.path).returncode, 0)

    def testASuffixedVersionDoesntMatchItsRelease(self):
        self.write(SAMPLE.replace("## 1.2.0, 2026-10-10", "## 1.2.0rc1, 2026-10-10"))
        self.assertEqual(self.notes("1.2.0", self.path).returncode, 1)
        self.assertEqual(self.notes("1.2.0rc1", self.path).stdout, "### Added\n\n- The newest thing." + FOOTER)

    def testANonAsciiChangelogReadsAsUtf8(self):
        self.write(SAMPLE.replace("The newest thing.", "The newest thing, caf\u00e9."))
        result = subprocess.run([sys.executable, "-B", SCRIPT, "1.2.0", self.path], capture_output=True, env={**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8"), "### Added\n\n- The newest thing, caf\u00e9." + FOOTER)

    def testTheRealChangelogHasASectionForEveryTagAndTheCurrentVersion(self):
        for name in ("1.0.0", "1.0.1", "1.1.0", "1.1.1", version.VERSION):
            result = self.notes(name)
            self.assertEqual(result.returncode, 0, name + ": " + result.stderr)
            self.assertTrue(result.stdout.startswith("### "), name)


if (__name__ == "__main__"):
    unittest.main()
