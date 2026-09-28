import os
import shutil
import subprocess
import tempfile
import unittest

import version

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHER = os.path.join(REPO, "packaging", "logitech-mouse-battery")
APP_DIR_LINE = "APP_DIR=/usr/share/logitech-mouse-battery"


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.appDir = os.path.join(temp.name, "app")
        os.makedirs(self.appDir)
        for name in ("check", "tray"):
            self.writeScript(name, "import sys\nprint('" + name + "', sys.flags.dont_write_bytecode, sys.argv[1:])\n")
        shutil.copy(os.path.join(REPO, "version.py"), self.appDir)
        with open(LAUNCHER) as f:
            text = f.read()
        # the copy runs against a stand in app folder, thus the real folder line has to be there to replace
        self.assertIn(APP_DIR_LINE + "\n", text)
        self.launcher = os.path.join(temp.name, "launcher")
        with open(self.launcher, "w") as f:
            f.write(text.replace(APP_DIR_LINE, "APP_DIR=" + self.appDir))

    def writeScript(self, name, text):
        with open(os.path.join(self.appDir, name + ".py"), "w") as f:
            f.write(text)

    def launch(self, *args):
        result = subprocess.run(["sh", self.launcher, *args], capture_output=True, text=True)
        return (result.returncode, result.stdout)

    def testCheckRunsTheTerminalCheckWithoutBytecode(self):
        self.assertEqual(self.launch("check"), (0, "check 1 []\n"))

    def testNoArgumentRunsTheTray(self):
        self.assertEqual(self.launch(), (0, "tray 1 []\n"))

    def testVersionPrintsTheVersionFile(self):
        self.assertEqual(self.launch("--version"), (0, version.VERSION + "\n"))

    def testTheCheckKeepsItsExitStatus(self):
        self.writeScript("check", "import sys\nsys.exit(1)\n")
        self.assertEqual(self.launch("check"), (1, ""))

    def testTheLauncherIsExecutable(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))


if (__name__ == "__main__"):
    unittest.main()
