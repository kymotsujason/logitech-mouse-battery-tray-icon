import os
import re
import unittest
import xml.etree.ElementTree as ElementTree

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtGui import QColor, QIcon, QImageReader
from PyQt6.QtWidgets import QApplication

import autostart
import icon
import serviceState
import tray
import version

app = QApplication.instance() or QApplication([])

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGING = os.path.join(REPO, "packaging")
SRC = os.path.join(REPO, "src")


def entryKeys(path):
    with open(path) as f:
        lines = f.read().splitlines()
    return (lines[0], dict(line.split("=", 1) for line in lines[1:] if "=" in line))


class DesktopEntryTests(unittest.TestCase):
    def testTheMenuEntryIsNamedForThePortalAppId(self):
        # the xdg portal finds the app by this file name, so a renamed id without a renamed file brings its warning back
        firstLine, keys = entryKeys(os.path.join(PACKAGING, tray.APP_ID + ".desktop"))
        self.assertEqual(firstLine, "[Desktop Entry]")
        self.assertEqual((keys["Name"], keys["Exec"], keys["Icon"]), ("Mouse Battery", "logitech-mouse-battery", "logitech-mouse-battery"))
        self.assertNotIn("NoDisplay", keys)
        self.assertNotIn("Hidden", keys)

    def testTheLoginEntryIsNamedForTheOverride(self):
        # a user override only replaces the system entry when both carry the same file name
        firstLine, keys = entryKeys(os.path.join(PACKAGING, "autostart", autostart.FILE_NAME))
        self.assertEqual(firstLine, "[Desktop Entry]")
        self.assertEqual((keys["Exec"], keys["TryExec"]), ("logitech-mouse-battery", "logitech-mouse-battery"))
        self.assertNotIn("Hidden", keys)


RULE_FILE = "70-logitech-mouse-battery.rules"
RULE = 'ACTION!="remove", SUBSYSTEM=="hidraw", KERNELS=="0003:046D:*", DRIVERS=="hid-generic", PROGRAM="/usr/bin/python3 -I -B /usr/share/logitech-mouse-battery/isHidpp.py %S%p", GROUP="logitech-mouse-battery", MODE="0660", TAG+="systemd", ENV{SYSTEMD_WANTS}+="logitech-mouse-battery.service"'


class UdevRuleTests(unittest.TestCase):
    def readRule(self):
        with open(os.path.join(PACKAGING, RULE_FILE)) as f:
            return [line for line in f.read().splitlines() if line and not line.startswith("#")]

    def testTheRuleIsTheSpecsLine(self):
        self.assertEqual(self.readRule(), [RULE])

    def testTheRuleRunsTheCheckFromTheLaunchersFolder(self):
        with open(os.path.join(PACKAGING, "logitech-mouse-battery")) as f:
            appDir = next(line.split("=", 1)[1] for line in f.read().splitlines() if line.startswith("APP_DIR="))
        self.assertIn(" " + appDir + "/isHidpp.py ", self.readRule()[0])
        self.assertTrue(os.path.exists(os.path.join(SRC, "isHidpp.py")))


USER = "logitech-mouse-battery"
UNIT_NAME = "logitech-mouse-battery.service"
SYSUSERS_LINE = 'u logitech-mouse-battery - "Mouse Battery service" - -'
SANDBOX = {
    "NoNewPrivileges": "yes",
    "CapabilityBoundingSet": "",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "PrivateNetwork": "yes",
    "RestrictAddressFamilies": "AF_UNIX",
    "DevicePolicy": "closed",
    "DeviceAllow": "char-hidraw rw",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectKernelLogs": "yes",
    "ProtectControlGroups": "yes",
    "ProtectClock": "yes",
    "ProtectHostname": "yes",
    "RestrictNamespaces": "yes",
    "RestrictRealtime": "yes",
    "LockPersonality": "yes",
    "SystemCallFilter": "@system-service",
}
SERVICE_FILES = [
    ("packaging/systemd/logitech-mouse-battery.service", "/usr/lib/systemd/system/logitech-mouse-battery.service"),
    ("packaging/dbus/io.github.kymotsujason.LogitechMouseBattery.conf", "/usr/share/dbus-1/system.d/io.github.kymotsujason.LogitechMouseBattery.conf"),
    ("packaging/dbus/io.github.kymotsujason.LogitechMouseBattery.service", "/usr/share/dbus-1/system-services/io.github.kymotsujason.LogitechMouseBattery.service"),
    ("packaging/sysusers/logitech-mouse-battery.conf", "/usr/lib/sysusers.d/logitech-mouse-battery.conf"),
]
ALLOWED_CALLS = {(serviceState.INTERFACE, "GetState"), (serviceState.INTERFACE, "ReadAll"), ("org.freedesktop.DBus.Introspectable", "Introspect"), ("org.freedesktop.DBus.Peer", "Ping"), ("org.freedesktop.DBus.Peer", "GetMachineId")}


def iniSections(path):
    sections = {}
    current = None
    with open(path) as f:
        for line in f.read().splitlines():
            if (line.startswith("[") and line.endswith("]")):
                current = sections.setdefault(line[1:-1], {})
            elif ("=" in line and not line.startswith("#")):
                key, value = line.split("=", 1)
                current[key.strip()] = value.strip()
    return sections


def readText(*parts):
    with open(os.path.join(REPO, *parts)) as f:
        return f.read()


class ServiceFileTests(unittest.TestCase):
    def testTheSysusersLineMakesTheUserAndItsGroup(self):
        self.assertEqual(readText("packaging", "sysusers", "logitech-mouse-battery.conf"), SYSUSERS_LINE + "\n")

    def testThePreinstallMakesTheSameUser(self):
        self.assertIn("echo '" + SYSUSERS_LINE + "' | systemd-sysusers --replace=/usr/lib/sysusers.d/logitech-mouse-battery.conf -\n", readText("packaging", "preinstall.sh"))

    def testTheUnitRunsTheServiceAsItsOwnUser(self):
        sections = iniSections(os.path.join(PACKAGING, "systemd", UNIT_NAME))
        service = sections["Service"]
        self.assertEqual({key: service[key] for key in ("Type", "BusName", "ExecStart", "User", "Group", "Restart", "RestartSec")}, {"Type": "dbus", "BusName": serviceState.SERVICE_NAME, "ExecStart": "/usr/bin/python3 -B /usr/share/logitech-mouse-battery/service.py", "User": USER, "Group": USER, "Restart": "on-failure", "RestartSec": "2"})
        self.assertEqual((sections["Unit"]["StartLimitIntervalSec"], sections["Unit"]["StartLimitBurst"]), ("60", "20"))
        # it starts when udev or a caller wants it, never at boot by itself
        self.assertNotIn("Install", sections)

    def testTheUnitLoadsHidBeforeItStarts(self):
        unit = iniSections(os.path.join(PACKAGING, "systemd", UNIT_NAME))["Unit"]
        self.assertEqual((unit.get("Wants"), unit.get("After")), ("modprobe@hid.service", "modprobe@hid.service"))

    def testTheUnitIsSandboxed(self):
        service = iniSections(os.path.join(PACKAGING, "systemd", UNIT_NAME))["Service"]
        self.assertEqual({key: service.get(key) for key in SANDBOX}, SANDBOX)
        self.assertNotIn("PrivateDevices", service)
        self.assertNotIn("MemoryDenyWriteExecute", service)

    def testTheBusPolicyLetsOnlyTheServiceUserOwnTheNameAndCallsOnlyItsMethods(self):
        root = ElementTree.parse(os.path.join(PACKAGING, "dbus", serviceState.SERVICE_NAME + ".conf")).getroot()
        owners = [(policy.get("user"), rule.get("own")) for policy in root.iter("policy") for rule in policy if rule.get("own") is not None]
        self.assertEqual(owners, [(USER, serviceState.SERVICE_NAME)])
        calls = [rule for policy in root.iter("policy") if policy.get("context") == "default" for rule in policy]
        self.assertTrue(all(rule.tag == "allow" and rule.get("send_destination") == serviceState.SERVICE_NAME and rule.get("send_member") for rule in calls))
        self.assertEqual({(rule.get("send_interface"), rule.get("send_member")) for rule in calls}, ALLOWED_CALLS)

    def testTheActivationFileStartsTheUnit(self):
        sections = iniSections(os.path.join(PACKAGING, "dbus", serviceState.SERVICE_NAME + ".service"))
        self.assertEqual(sections, {"D-BUS Service": {"Name": serviceState.SERVICE_NAME, "Exec": "/bin/false", "User": USER, "SystemdService": UNIT_NAME}})

    def testThePacmanHookRunsAfterTheBusReload(self):
        name = "logitech-mouse-battery.hook"
        # pacman runs PostTransaction hooks in file name order, and the bus has to hold the new policy first
        self.assertLess("dbus-reload.hook", name)
        self.assertLess("35-systemd-udev-reload.hook", name)
        lines = readText("packaging", "alpm", name).splitlines()
        for line in ("Type = Path", "Operation = Install", "Operation = Upgrade", "Target = usr/share/logitech-mouse-battery/*", "When = PostTransaction", "Exec = /usr/share/logitech-mouse-battery/afterInstall.sh"):
            self.assertIn(line, lines)

    def testTheArchInstallFileStopsTheServiceOnRemoval(self):
        text = readText("packaging", "aur", "logitech-mouse-battery.install")
        self.assertIn("pre_remove() {\n    systemctl stop logitech-mouse-battery.service 2>/dev/null || true\n}\n", text)

    def testThePostinstallRunsAfterInstall(self):
        self.assertEqual(readText("packaging", "postinstall.sh"), "#!/bin/sh\n/usr/share/logitech-mouse-battery/afterInstall.sh\nexit 0\n")


SVG = "{http://www.w3.org/2000/svg}"
METAINFO = "io.github.kymotsujason.LogitechMouseBattery.metainfo.xml"
RAW_MAIN = "https://raw.githubusercontent.com/kymotsujason/logitech-mouse-battery-tray-icon/main/"


class AppIconTests(unittest.TestCase):
    def setUp(self):
        self.root = ElementTree.parse(os.path.join(PACKAGING, "logitech-mouse-battery.svg")).getroot()

    def testTheIconUsesDesignGsTwentyTwoUnitGrid(self):
        self.assertEqual(self.root.get("viewBox"), "0 0 22 22")
        bodies = [rect for rect in self.root.iter(SVG + "rect") if (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height"), rect.get("rx")) == ("4", "1", "14", "20", "7")]
        self.assertEqual(len(bodies), 1)

    def testTheFillIsTheTrayGreen(self):
        fills = [element for element in self.root.iter() if element.get("fill") == icon.GREEN]
        self.assertEqual(len(fills), 1)

    def testTheIconUsesNoClipOrMask(self):
        # Qt's SVG renderer ignores clipPath and mask
        tags = [element.tag for element in self.root.iter()]
        self.assertNotIn(SVG + "clipPath", tags)
        self.assertNotIn(SVG + "mask", tags)

    def testQtDrawsTheFillAtThreeQuarters(self):
        formats = [bytes(f) for f in QImageReader.supportedImageFormats()]
        if (b"svg" not in formats):
            self.skipTest("this Qt has no SVG plugin")
        image = QIcon(os.path.join(PACKAGING, "logitech-mouse-battery.svg")).pixmap(220, 220).toImage()
        self.assertNotEqual(image.pixelColor(70, 40), QColor(icon.GREEN))
        self.assertEqual(image.pixelColor(110, 150), QColor(icon.GREEN))


class MetainfoTests(unittest.TestCase):
    def setUp(self):
        self.root = ElementTree.parse(os.path.join(PACKAGING, METAINFO)).getroot()

    def testItNamesTheAppAndItsMenuEntry(self):
        self.assertEqual(self.root.findtext("id"), "io.github.kymotsujason.LogitechMouseBattery")
        self.assertEqual(self.root.findtext("name"), "Mouse Battery")
        self.assertEqual(self.root.findtext("launchable"), tray.APP_ID + ".desktop")
        self.assertEqual(self.root.findtext("project_license"), "GPL-3.0-or-later")

    def testTheNewestReleaseIsTheAppsVersion(self):
        self.assertEqual(self.root.find("releases/release").get("version"), version.VERSION)

    def testTheScreenshotIsInTheRepo(self):
        url = self.root.findtext("screenshots/screenshot/image")
        self.assertTrue(url.startswith(RAW_MAIN))
        self.assertTrue(os.path.exists(os.path.join(REPO, url[len(RAW_MAIN):])))


def appFiles():
    # every Python module in src/ is part of the app, so a new one has to be packaged as well
    modules = sorted(name for name in os.listdir(SRC) if name.endswith(".py"))
    return modules + ["NotoSans-Medium.ttf", "OFL.txt"]


class NfpmTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PACKAGING, "nfpm.yaml")) as f:
            self.text = f.read()

    def testEveryAppFileGoesToTheAppFolderAs0644(self):
        # tray.py and check.py are executable in git, and the PKGBUILD installs every app file 0644, so the deb and rpm do too
        for name in appFiles():
            self.assertIn("  - src: src/" + name + "\n    dst: /usr/share/logitech-mouse-battery/" + name + "\n    file_info:\n      mode: 0644\n", self.text)

    def testTheVersionComesFromTheBuild(self):
        self.assertIn("\nversion: ${VERSION}\n", self.text)

    def testDebianAsksForPythonThreeElevenOrLater(self):
        self.assertIn("      - python3 (>= 3.11)\n", self.text)

    def testRpmAsksForPythonThreeElevenOrLater(self):
        self.assertIn("  rpm:\n    depends:\n      - python3 >= 3.11\n", self.text)

    def testTheFourScriptsSetUpAndRemoveTheService(self):
        self.assertIn("scripts:\n  preinstall: packaging/preinstall.sh\n  postinstall: packaging/postinstall.sh\n  preremove: packaging/preremove.sh\n  postremove: packaging/postremove.sh\n", self.text)

    def testEachServiceFileGoesToItsPlace(self):
        for source, destination in SERVICE_FILES:
            self.assertIn("  - src: " + source + "\n    dst: " + destination + "\n", self.text)
        self.assertIn("  - src: packaging/afterInstall.sh\n    dst: /usr/share/logitech-mouse-battery/afterInstall.sh\n    file_info:\n      mode: 0755\n", self.text)

    def testBothPackagesNeedSystemdAndTheDebNeedsItFirst(self):
        self.assertEqual(self.text.count("      - python3-pyqt6\n      - systemd\n"), 2)
        self.assertIn("\ndeb:\n  predepends:\n    - systemd\n", self.text)

    def testTheLicenseGoesToEachDistrosPlace(self):
        self.assertIn("  - src: LICENSE\n    dst: /usr/share/doc/logitech-mouse-battery/copyright\n    packager: deb\n", self.text)
        self.assertIn("  - src: LICENSE\n    dst: /usr/share/licenses/logitech-mouse-battery/LICENSE\n    packager: rpm\n", self.text)


class PkgbuildTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(PACKAGING, "aur", "PKGBUILD")) as f:
            self.text = f.read()

    def testTheVersionIsTheAppsVersion(self):
        self.assertIn("\npkgver=" + version.VERSION + "\n", self.text)

    def testEveryAppFileGoesToTheAppFolder(self):
        line = next(line for line in self.text.splitlines() if '-t "$pkgdir/usr/share/$pkgname"' in line)
        self.assertEqual(line.split('-t "$pkgdir/usr/share/$pkgname"', 1)[1].split(), ["src/" + name for name in appFiles()])

    def testTheSrcinfoMatchesTheVersion(self):
        with open(os.path.join(PACKAGING, "aur", ".SRCINFO")) as f:
            self.assertIn("\tpkgver = " + version.VERSION + "\n", f.read())

    def testTheServiceFilesAreInstalled(self):
        for source, destination in SERVICE_FILES + [("packaging/alpm/logitech-mouse-battery.hook", "/usr/share/libalpm/hooks/logitech-mouse-battery.hook")]:
            self.assertIn('    install -Dm644 ' + source + ' "$pkgdir' + destination + '"\n', self.text)
        self.assertIn('    install -Dm755 packaging/afterInstall.sh "$pkgdir/usr/share/$pkgname/afterInstall.sh"\n', self.text)

    def testItNeedsSystemdAndRunsItsInstallFile(self):
        self.assertIn("\ndepends=('python' 'python-pyqt6' 'systemd')\n", self.text)
        self.assertIn("\ninstall=logitech-mouse-battery.install\n", self.text)


class VersionMentionTests(unittest.TestCase):
    def testTheReadmesPackageNamesCarryTheVersion(self):
        with open(os.path.join(REPO, "README.md")) as f:
            found = re.findall(r"logitech-mouse-battery[_-](\d+\.\d+\.\d+)[_-]", f.read())
        self.assertGreater(len(found), 0)
        self.assertEqual(set(found), {version.VERSION})

    def testTheSrcinfoSourceCarriesTheVersion(self):
        with open(os.path.join(PACKAGING, "aur", ".SRCINFO")) as f:
            source = next(line for line in f.read().splitlines() if line.strip().startswith("source = "))
        self.assertEqual(set(re.findall(r"\d+\.\d+\.\d+", source)), {version.VERSION})


WORKFLOWS = os.path.join(REPO, ".github", "workflows")
CHECKOUT = "uses: actions/checkout@v7\n        with:\n          persist-credentials: false\n"


class WorkflowTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(WORKFLOWS, name)) as f:
            return f.read()

    def releaseJob(self):
        return self.read("release.yml").split("\n  release:\n", 1)[1]

    def testOnlyTheReleaseJobCanWrite(self):
        text = self.read("release.yml")
        self.assertIn("\npermissions:\n  contents: read\n", text)
        self.assertEqual(text.count("contents: write"), 1)
        self.assertIn("\n    permissions:\n      contents: write\n", self.releaseJob())

    def testOnlyMainPublishes(self):
        self.assertIn("    if: needs.check.outputs.released == 'false' && github.ref == 'refs/heads/main'\n", self.releaseJob())

    def testEveryCheckoutDropsItsCredentials(self):
        for name in ("release.yml", "ci.yml"):
            text = self.read(name)
            self.assertGreater(text.count("uses: actions/checkout@"), 0)
            self.assertEqual(text.count("uses: actions/checkout@"), text.count(CHECKOUT), name)

    def testEveryJobHasATimeout(self):
        self.assertIn("  check:\n    runs-on: ubuntu-24.04\n    timeout-minutes: 10\n", self.read("release.yml"))
        self.assertIn("\n    timeout-minutes: 60\n", self.releaseJob())
        self.assertIn("  test:\n    runs-on: ubuntu-24.04\n    timeout-minutes: 60\n", self.read("ci.yml"))

    def testTheReleaseChecksTheLateTrayAndPublishesChecksums(self):
        release = self.releaseJob()
        self.assertIn("run: tests/live/checkLateTray.sh\n", release)
        self.assertIn("run: cd dist && sha256sum *.deb *.rpm *.pkg.tar.zst *.tar.gz > SHA256SUMS\n", release)
        publish = release.split("gh release create", 1)[1].split("\n", 1)[0]
        self.assertTrue(publish.endswith(" dist/SHA256SUMS"), publish)
        self.assertLess(release.index("checkLateTray.sh"), release.index("gh release create"))
        self.assertLess(release.index("-aur.tar.gz"), release.index("> SHA256SUMS"))
        self.assertLess(release.index("> SHA256SUMS"), release.index("gh release create"))

    def testTheReleaseChecksTheUpgradeBeforePublishing(self):
        release = self.releaseJob()
        self.assertIn("run: tests/containers/testUpgrade.sh\n", release)
        self.assertLess(release.index("testUpgrade.sh"), release.index("gh release create"))

    def testTheReleaseChecksTheBootedServiceBeforePublishing(self):
        release = self.releaseJob()
        self.assertIn("run: tests/booted/checkService.sh\n", release)
        self.assertLess(release.index("tests/booted/checkService.sh"), release.index("gh release create"))

    def testCiRunsTheLateTrayCheckAndEveryBuild(self):
        ci = self.read("ci.yml")
        for script in ("tests/live/checkLateTray.sh", "packaging/tarball.sh", "packaging/build.sh", "packaging/buildArch.sh", "tests/containers/testPackages.sh", "tests/containers/testUpgrade.sh", "tests/booted/checkService.sh"):
            self.assertIn("run: " + script + "\n", ci)

    def testTheAurBundleCarriesTheInstallFile(self):
        self.assertIn('run: tar -czf "dist/logitech-mouse-battery-$VERSION-aur.tar.gz" -C dist/aur PKGBUILD .SRCINFO logitech-mouse-battery.install\n', self.releaseJob())


if (__name__ == "__main__"):
    unittest.main()
