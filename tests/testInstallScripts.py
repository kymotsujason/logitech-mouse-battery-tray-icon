import os
import shutil
import stat
import subprocess
import tempfile
import unittest

PACKAGING = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging")
TOOLS = ("systemd-detect-virt", "systemd-notify", "systemctl", "udevadm", "python3", "systemd-sysusers", "timeout")
STUB = """#!/bin/sh
name=${0##*/}
echo "$name $*" >> "$STUB_LOG"
if [ "$name" = "timeout" ]; then
    shift
    exec "$@"
fi
if [ "$name" = "systemd-sysusers" ]; then
    while read -r line; do
        echo "stdin $line" >> "$STUB_LOG"
    done
fi
if [ "$name" = "systemd-detect-virt" ]; then
    [ "$STUB_CHROOT" = "1" ] && exit 0
    exit 1
fi
case " $STUB_FAIL " in
    *" $name "*|*" $name:$1 "*) exit 1 ;;
esac
exit 0
"""
STEPS = [
    "systemctl daemon-reload",
    "systemctl reload dbus.service",
    "udevadm control --reload",
    "timeout 30 udevadm trigger --settle --subsystem-match=hidraw --action=change",
    "udevadm trigger --settle --subsystem-match=hidraw --action=change",
    "python3 -I -B /usr/share/logitech-mouse-battery/clearAccess.py --settled",
    "systemctl reset-failed logitech-mouse-battery.service",
    "systemctl try-restart --no-block logitech-mouse-battery.service",
]
UNSETTLED_STEPS = [step.replace(" --settled", "") for step in STEPS]
CHECKS = ["systemd-detect-virt --quiet --chroot", "systemd-notify --booted"]


class InstallScriptTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.log = os.path.join(self.folder, "log")
        for name in TOOLS:
            path = os.path.join(self.folder, name)
            with open(path, "w") as f:
                f.write(STUB)
            os.chmod(path, stat.S_IRWXU)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def runScript(self, name, *args, fail="", chroot="0", notBooted=False):
        env = {"PATH": self.folder, "STUB_LOG": self.log, "STUB_FAIL": fail + (" systemd-notify" if notBooted else ""), "STUB_CHROOT": chroot}
        script = name if name == "-c" else os.path.join(PACKAGING, name)
        result = subprocess.run(["/bin/sh", script, *args], env=env, capture_output=True, text=True)
        calls = []
        if (os.path.exists(self.log)):
            with open(self.log) as f:
                calls = f.read().splitlines()
        return (result.returncode, result.stdout, calls)

    def testAfterInstallRunsEveryStepInOrder(self):
        code, output, calls = self.runScript("afterInstall.sh")
        self.assertEqual((code, calls), (0, CHECKS + STEPS))
        self.assertEqual([line for line in output.splitlines() if line.startswith("Mouse Battery: ")], ["Mouse Battery: reloading systemd", "Mouse Battery: reloading the system bus", "Mouse Battery: reloading the udev rules", "Mouse Battery: applying the rule to plugged in receivers and cables", "Mouse Battery: closing the access 1.0.x gave", "Mouse Battery: clearing the service's failed starts", "Mouse Battery: restarting the service if it runs"])

    def testAFailingStepIsReportedAndTheRestStillRun(self):
        code, output, calls = self.runScript("afterInstall.sh", fail="udevadm python3")
        self.assertEqual((code, calls), (0, CHECKS + UNSETTLED_STEPS))
        self.assertEqual(output.count("that step failed. Unplug the receiver or cable and plug it back in, or reboot."), 3)

    def testClearAccessIsToldUdevSettledOnlyWhenSettleSucceeds(self):
        self.assertEqual(self.runScript("afterInstall.sh", fail="udevadm")[::2], (0, CHECKS + UNSETTLED_STEPS))
        os.remove(self.log)
        self.assertEqual(self.runScript("afterInstall.sh", fail="systemctl")[::2], (0, CHECKS + STEPS))

    def testClearAccessIsntToldUdevSettledWhenTheReloadOrTheTriggerFailed(self):
        for fail in ("udevadm:control", "udevadm:trigger"):
            if (os.path.exists(self.log)):
                os.remove(self.log)
            self.assertEqual(self.runScript("afterInstall.sh", fail=fail)[::2], (0, CHECKS + UNSETTLED_STEPS), fail)

    def testAStuckEventElsewhereDoesntStopClearAccessTrustingTheTag(self):
        code, output, calls = self.runScript("afterInstall.sh", fail="udevadm:settle")
        self.assertEqual((code, calls), (0, CHECKS + STEPS))
        self.assertNotIn("that step failed.", output)

    def testAResetFailedThatFailsIsntReported(self):
        code, output, calls = self.runScript("afterInstall.sh", fail="systemctl")
        self.assertEqual((code, calls), (0, CHECKS + STEPS))
        self.assertEqual(output.count("that step failed."), 3)

    def testAfterInstallSkipsAChroot(self):
        code, output, calls = self.runScript("afterInstall.sh", chroot="1")
        self.assertEqual((code, calls), (0, CHECKS[:1]))
        self.assertIn("skipped", output)

    def testAfterInstallSkipsARootThatIsntBooted(self):
        code, output, calls = self.runScript("afterInstall.sh", notBooted=True)
        self.assertEqual((code, calls), (0, CHECKS))
        self.assertIn("skipped", output)

    def testPreremoveStopsTheServiceOnlyOnARealRemoval(self):
        for args, expected in ((("remove",), ["systemctl stop logitech-mouse-battery.service"]), (("0",), ["systemctl stop logitech-mouse-battery.service"]), (("upgrade", "1.1.1"), []), (("1",), [])):
            if (os.path.exists(self.log)):
                os.remove(self.log)
            self.assertEqual(self.runScript("preremove.sh", *args)[::2], (0, expected), args)

    def testPostremoveReloadsAndStopsTheServiceOnlyOnARealRemoval(self):
        reloads = ["systemctl daemon-reload", "systemctl reload dbus.service", "systemctl stop logitech-mouse-battery.service", "udevadm control --reload", "udevadm trigger --subsystem-match=hidraw --action=change"]
        for args, expected in ((("remove",), CHECKS + reloads), (("purge",), CHECKS + reloads), (("0",), CHECKS + reloads), (("upgrade", "1.1.1"), []), (("1",), [])):
            if (os.path.exists(self.log)):
                os.remove(self.log)
            self.assertEqual(self.runScript("postremove.sh", *args)[::2], (0, expected), args)

    def runArchFunctions(self, *names):
        script = ". " + os.path.join(PACKAGING, "aur", "logitech-mouse-battery.install") + "\n"
        for name in names:
            script += "if command -v " + name + " >/dev/null; then " + name + " 1.1.1-1 1.1.0-1; fi\n"
        return self.runScript("-c", script)

    def testTheArchInstallFileStopsTheServiceBeforeAndAfterARemoval(self):
        self.assertEqual(self.runArchFunctions("pre_remove", "post_remove")[::2], (0, ["systemctl stop logitech-mouse-battery.service"] * 2))

    def testTheArchInstallFileLeavesTheServiceRunningOnAnUpgrade(self):
        self.assertEqual(self.runArchFunctions("pre_upgrade", "post_upgrade", "pre_install", "post_install")[::2], (0, []))

    def testPostremoveNeverFails(self):
        self.assertEqual(self.runScript("postremove.sh", "remove", fail="systemctl udevadm")[0], 0)

    def testPreinstallMakesTheUserFromItsLine(self):
        code, output, calls = self.runScript("preinstall.sh")
        self.assertEqual((code, calls), (0, ["systemd-sysusers --replace=/usr/lib/sysusers.d/logitech-mouse-battery.conf -", 'stdin u logitech-mouse-battery - "Mouse Battery service" - -']))

    def testPreinstallFailsWhenItCantMakeTheUser(self):
        self.assertNotEqual(self.runScript("preinstall.sh", fail="systemd-sysusers")[0], 0)


if (__name__ == "__main__"):
    unittest.main()
