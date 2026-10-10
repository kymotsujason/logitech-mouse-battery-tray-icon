import json
import os
import re
import shutil
import subprocess
import tempfile
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
ALLOWED_CALLS = {(serviceState.INTERFACE, "GetState"), (serviceState.INTERFACE, "ReadAll"), (serviceState.INTERFACE, "ReadSilent"), (serviceState.INTERFACE, "GetHistory"), (serviceState.INTERFACE, "GetLevels"), ("org.freedesktop.DBus.Introspectable", "Introspect"), ("org.freedesktop.DBus.Peer", "Ping"), ("org.freedesktop.DBus.Peer", "GetMachineId")}


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
        self.assertEqual({key: service[key] for key in ("Type", "BusName", "ExecStart", "User", "Group", "Restart", "SyslogIdentifier")}, {"Type": "dbus", "BusName": serviceState.SERVICE_NAME, "ExecStart": "/usr/bin/python3 -B /usr/share/logitech-mouse-battery/service.py", "User": USER, "Group": USER, "Restart": "on-failure", "SyslogIdentifier": USER})

    def testAServiceThatKeepsFailingSlowlyStillHitsItsStartLimit(self):
        sections = iniSections(os.path.join(PACKAGING, "systemd", UNIT_NAME))
        restartSeconds = int(sections["Service"]["RestartSec"])
        interval, burst = int(sections["Unit"]["StartLimitIntervalSec"]), int(sections["Unit"]["StartLimitBurst"])
        # a start that takes up to 5 s to fail, such as python3 importing PyQt6 on a cold cache, has to fill the burst inside the interval, or the restarts never stop
        self.assertLess((restartSeconds + 5) * burst, interval)
        self.assertGreaterEqual(restartSeconds * (burst - 1), 10)
        # it starts when udev or a caller wants it, never at boot by itself
        self.assertNotIn("Install", sections)

    def testTheUnitGivesTheHistoryItsOwnFolder(self):
        service = iniSections(os.path.join(PACKAGING, "systemd", UNIT_NAME))["Service"]
        self.assertEqual((service.get("StateDirectory"), service.get("StateDirectoryMode")), ("logitech-mouse-battery", "0750"))

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
        policies = list(root)
        self.assertEqual([(policy.tag, policy.attrib) for policy in policies], [("policy", {"user": USER}), ("policy", {"context": "default"})])
        self.assertEqual([(rule.tag, rule.attrib) for rule in policies[0]], [("allow", {"own": serviceState.SERVICE_NAME})])
        calls = [("allow", {"send_destination": serviceState.SERVICE_NAME, "send_interface": interface, "send_member": member}) for interface, member in ALLOWED_CALLS]
        self.assertCountEqual([(rule.tag, rule.attrib) for rule in policies[1]], calls)

    def testTheActivationFileStartsTheUnit(self):
        sections = iniSections(os.path.join(PACKAGING, "dbus", serviceState.SERVICE_NAME + ".service"))
        self.assertEqual(sections, {"D-BUS Service": {"Name": serviceState.SERVICE_NAME, "Exec": "/bin/false", "User": USER, "SystemdService": UNIT_NAME}})

    def testThePacmanHookRunsAfterTheBusReload(self):
        name = re.search(r"usr/share/libalpm/hooks/([^\"\s]+)", readText("packaging", "aur", "PKGBUILD")).group(1)
        # pacman runs PostTransaction hooks in file name order, and the group, the bus policy, and the udev rules have to be in place first
        for earlier in ("30-systemd-sysusers.hook", "35-systemd-udev-reload.hook", "dbus-reload.hook"):
            self.assertLess(earlier, name)
        lines = readText("packaging", "alpm", name).splitlines()
        for line in ("Type = Path", "Operation = Install", "Operation = Upgrade", "Target = usr/share/logitech-mouse-battery/*", "When = PostTransaction", "Exec = /usr/share/logitech-mouse-battery/afterInstall.sh"):
            self.assertIn(line, lines)

    def testTheArchInstallFileStopsTheServiceOnRemoval(self):
        text = readText("packaging", "aur", "logitech-mouse-battery.install")
        self.assertIn("pre_remove() {\n    systemctl stop logitech-mouse-battery.service 2>/dev/null || true\n}\n", text)
        self.assertIn("post_remove() {\n    if systemd-detect-virt --quiet --chroot || ! systemd-notify --booted; then\n        return 0\n    fi\n", text)
        self.assertIn("    systemctl stop logitech-mouse-battery.service 2>/dev/null || true\n    systemctl reset-failed logitech-mouse-battery.service 2>/dev/null || true\n", text)

    def testThePostinstallRunsAfterInstall(self):
        self.assertEqual(readText("packaging", "postinstall.sh"), "#!/bin/sh\n/usr/share/logitech-mouse-battery/afterInstall.sh\nexit 0\n")


SVG = "{http://www.w3.org/2000/svg}"
METAINFO = "io.github.kymotsujason.LogitechMouseBattery.metainfo.xml"
APP_ICON_FILL = "#F5F5F7"
APP_ICON_OUTLINE = "#636366"
# each state's fill color and where its fill starts at 220 px, with no fill for a mouse that has no reading
STATE_ICONS = {"charging": (icon.GREEN, 60), "low": (icon.RED, 150), "asleep": (icon.GRAY, 60), "empty": (None, None)}
RAW_MAIN = "https://raw.githubusercontent.com/kymotsujason/logitech-mouse-battery-tray-icon/main/"


class AppIconTests(unittest.TestCase):
    def setUp(self):
        self.root = ElementTree.parse(os.path.join(PACKAGING, "logitech-mouse-battery.svg")).getroot()

    def testTheIconUsesDesignGsTwentyTwoUnitGrid(self):
        self.assertEqual(self.root.get("viewBox"), "0 0 22 22")
        bodies = [rect for rect in self.root.iter(SVG + "rect") if (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height"), rect.get("rx")) == ("4", "1", "14", "20", "7")]
        self.assertEqual(len(bodies), 1)

    def testTheIconCarriesNoBatteryStateColor(self):
        colors = [element.get(name) for element in self.root.iter() for name in ("fill", "stroke")]
        for stateColor in (icon.GREEN, icon.RED):
            self.assertNotIn(stateColor, colors)

    def testTheFillIsLightWithADarkOutline(self):
        fills = [element for element in self.root.iter() if element.get("fill") == APP_ICON_FILL]
        outlines = [rect for rect in self.root.iter(SVG + "rect") if rect.get("stroke") == APP_ICON_OUTLINE]
        self.assertEqual((len(fills), len(outlines)), (1, 1))

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
        self.assertNotEqual(image.pixelColor(70, 40), QColor(APP_ICON_FILL))
        self.assertEqual(image.pixelColor(110, 150), QColor(APP_ICON_FILL))
        # the outline's stroke runs from 4.0 to 4.7 units, so 43 px at this size sits inside it
        self.assertEqual(image.pixelColor(43, 110), QColor(APP_ICON_OUTLINE))


class StateIconTests(unittest.TestCase):
    def path(self, suffix):
        return os.path.join(PACKAGING, icon.APP_ICON + "-" + suffix + ".svg")

    def testEachStateIconIsInstalledByEveryPackage(self):
        nfpm = readText("packaging", "nfpm.yaml")
        pkgbuild = readText("packaging", "aur", "PKGBUILD")
        checkPackage = readText("tests", "containers", "checkPackage.sh")
        for suffix in STATE_ICONS:
            name = icon.APP_ICON + "-" + suffix + ".svg"
            self.assertIn("  - src: packaging/" + name + "\n    dst: /usr/share/icons/hicolor/scalable/apps/" + name + "\n", nfpm)
            self.assertIn('    install -Dm644 packaging/' + name + ' "$pkgdir/usr/share/icons/hicolor/scalable/apps/' + name + '"\n', pkgbuild)
            self.assertIn(" /usr/share/icons/hicolor/scalable/apps/$name-" + suffix + ".svg ", checkPackage)

    def testEachStateIconSharesTheAppIconsShape(self):
        for suffix in STATE_ICONS:
            root = ElementTree.parse(self.path(suffix)).getroot()
            self.assertEqual(root.get("viewBox"), "0 0 22 22", suffix)
            outlines = [rect for rect in root.iter(SVG + "rect") if rect.get("stroke") == APP_ICON_OUTLINE]
            self.assertEqual(len(outlines), 1, suffix)
            tags = [element.tag for element in root.iter()]
            self.assertNotIn(SVG + "clipPath", tags)
            self.assertNotIn(SVG + "mask", tags)

    def testQtDrawsEachStateInItsColor(self):
        formats = [bytes(f) for f in QImageReader.supportedImageFormats()]
        if (b"svg" not in formats):
            self.skipTest("this Qt has no SVG plugin")
        for suffix, (color, fillTop) in STATE_ICONS.items():
            image = QIcon(self.path(suffix)).pixmap(220, 220).toImage()
            if (color is None):
                self.assertLess(image.pixelColor(110, 180).alpha(), 255, suffix)
                continue
            self.assertEqual(image.pixelColor(110, 200), QColor(color), suffix)
            # just above and below where the fill starts, left of the hole the fill leaves around the wheel
            self.assertNotEqual(image.pixelColor(60, fillTop - 4), QColor(color), suffix)
            self.assertEqual(image.pixelColor(60, fillTop + 4), QColor(color), suffix)


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
    return sorted(name for name in os.listdir(SRC) if name.endswith(".py"))


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
CHECKOUT = "uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n        with:\n          persist-credentials: false\n"


class WorkflowTests(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(WORKFLOWS, name)) as f:
            return f.read()

    def releaseJob(self):
        return self.read("release.yml").split("\n  release:\n", 1)[1]

    def testOnlyTheReleaseJobCanWrite(self):
        text = self.read("release.yml")
        self.assertIn("\npermissions:\n  contents: read\n", text)
        for permission in ("contents: write", "id-token: write", "attestations: write"):
            self.assertEqual(text.count(permission), 1, permission)
        self.assertIn("\n    permissions:\n      contents: write\n      id-token: write\n      attestations: write\n", self.releaseJob())
        self.assertNotIn("write", self.read("ci.yml"))

    def testEveryActionIsPinnedToACommit(self):
        for name in ("release.yml", "ci.yml"):
            uses = [line.strip() for line in self.read(name).splitlines() if line.strip().startswith(("uses:", "- uses:"))]
            self.assertGreater(len(uses), 0, name)
            for line in uses:
                self.assertRegex(line, r"^(- )?uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$", name)

    def testTheReleaseAttestsEveryChecksummedAssetBeforePublishing(self):
        release = self.releaseJob()
        self.assertIn("uses: actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6 # v4.2.2\n        with:\n          subject-checksums: dist/SHA256SUMS\n", release)
        self.assertLess(release.index("> SHA256SUMS"), release.index("subject-checksums"))
        self.assertLess(release.index("subject-checksums"), release.index("gh release create"))

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

    def testThePublishStepPassesTheChangelogNotesFile(self):
        release = self.releaseJob()
        publish = release.split("gh release create", 1)[1].split("\n", 1)[0]
        self.assertIn(' --notes-file /tmp/notes.md ', publish)
        self.assertNotIn("--notes ", publish)
        self.assertIn('python3 packaging/releaseNotes.py "$VERSION" > /tmp/notes.md\n', release)
        # a failure to write the notes stops the step before it deletes a draft
        self.assertLess(release.index("packaging/releaseNotes.py"), release.index("gh api --paginate"))
        self.assertLess(release.index("gh api --paginate"), release.index("gh api -X DELETE"))
        self.assertLess(release.index("gh api -X DELETE"), release.index("gh release create"))

    def testTheCheckJobFailsWhenTheVersionHasNoChangelogSection(self):
        check = self.read("release.yml").split("\n  release:\n", 1)[0]
        self.assertIn('if ! python3 packaging/releaseNotes.py --check "$version"; then\n', check)
        self.assertEqual(check.count("releaseNotes.py"), 1)
        self.assertIn("::error::CHANGELOG.md has no dated section with entries for $version", check)
        self.assertLess(check.index("releaseNotes.py"), check.index("released=false"))

    def checkStep(self):
        check = self.read("release.yml").split("\n  release:\n", 1)[0]
        start = check.index("      - name: Find the version and whether it has a release\n")
        lines = check[start:].split("\n        run: |\n", 1)[1].split("\n")
        body = []
        for line in lines:
            if (line.strip() != "" and not line.startswith("          ")):
                break
            body.append(line[10:])
        return "\n".join(body)

    def runCheck(self, changelog, view="release not found", viewCode=1):
        # the step runs in a copy of the repo's parts it reads, with a gh that answers the release lookup as told
        with tempfile.TemporaryDirectory() as folder:
            os.makedirs(os.path.join(folder, "bin"))
            os.makedirs(os.path.join(folder, "src"))
            os.makedirs(os.path.join(folder, "packaging"))
            shutil.copy(os.path.join(PACKAGING, "releaseNotes.py"), os.path.join(folder, "packaging"))
            with open(os.path.join(folder, "src", "version.py"), "w") as f:
                f.write('VERSION = "1.1.1"\n')
            with open(os.path.join(folder, "CHANGELOG.md"), "w") as f:
                f.write(changelog)
            gh = os.path.join(folder, "bin", "gh")
            with open(gh, "w") as f:
                f.write('#!/bin/sh\nprintf "%s\\n" "$FAKE_VIEW"\nexit "$FAKE_CODE"\n')
            os.chmod(gh, 0o755)
            output = os.path.join(folder, "output")
            open(output, "w").close()
            env = {"PATH": os.path.join(folder, "bin") + ":" + os.environ["PATH"], "FAKE_VIEW": view, "FAKE_CODE": str(viewCode), "GITHUB_OUTPUT": output, "GITHUB_REPOSITORY": "owner/repo"}
            result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", self.checkStep()], cwd=folder, env=env, capture_output=True, text=True)
            with open(output) as f:
                outputs = f.read().splitlines()
        return (result.returncode, result.stdout + result.stderr, outputs)

    def testTheCheckJobStopsWithoutADatedSectionWithEntries(self):
        for name, changelog in (("undated", "# Changelog\n\n## 1.1.1, unreleased\n\n### Fixed\n\n- A thing.\n"), ("missing", "# Changelog\n\n## 1.1.0, 2026-10-02\n\n### Fixed\n\n- A thing.\n"), ("empty", "# Changelog\n\n## 1.1.1, 2026-10-08\n\n### Fixed\n\n")):
            with self.subTest(changelog=name):
                code, output, outputs = self.runCheck(changelog)
                self.assertNotEqual(code, 0, output)
                self.assertNotIn("released=false", outputs)
                self.assertNotIn("has no release yet", output)
                self.assertIn("::error::CHANGELOG.md has no dated section with entries for 1.1.1", output)

    def testTheCheckJobPublishesADatedSection(self):
        code, output, outputs = self.runCheck("# Changelog\n\n## 1.1.1, 2026-10-08\n\n### Fixed\n\n- A thing.\n")
        self.assertEqual(code, 0, output)
        self.assertEqual(outputs, ["version=1.1.1", "released=false"])

    def testTheCheckJobSkipsAVersionWithARelease(self):
        code, output, outputs = self.runCheck("# Changelog\n", view="title: v1.1.1", viewCode=0)
        self.assertEqual(code, 0, output)
        self.assertEqual(outputs, ["version=1.1.1", "released=true"])

    def testTheCheckJobStopsWhenTheReleaseLookupFails(self):
        code, output, outputs = self.runCheck("# Changelog\n\n## 1.1.1, 2026-10-08\n\n### Fixed\n\n- A thing.\n", view="HTTP 502: Bad Gateway")
        self.assertNotEqual(code, 0, output)
        self.assertEqual(outputs, ["version=1.1.1"])
        self.assertIn("HTTP 502", output)

    def testTheReleaseChecksTheUpgradeBeforePublishing(self):
        release = self.releaseJob()
        self.assertIn("run: tests/containers/testUpgrade.sh\n", release)
        self.assertLess(release.index("testUpgrade.sh"), release.index("gh release create"))

    def testTheReleaseChecksTheBootedServiceBeforePublishing(self):
        release = self.releaseJob()
        self.assertIn("run: tests/booted/checkService.sh\n", release)
        self.assertLess(release.index("tests/booted/checkService.sh"), release.index("gh release create"))

    def testTheReleaseChecksTheServiceUpgradeBeforePublishing(self):
        release = self.releaseJob()
        self.assertIn("run: tests/booted/checkServiceUpgrade.sh\n", release)
        self.assertLess(release.index("tests/booted/checkService.sh"), release.index("tests/booted/checkServiceUpgrade.sh"))
        self.assertLess(release.index("tests/booted/checkServiceUpgrade.sh"), release.index("gh release create"))

    def testCiRunsTheLateTrayCheckAndEveryBuild(self):
        ci = self.read("ci.yml")
        for script in ("tests/live/checkLateTray.sh", "packaging/tarball.sh", "packaging/build.sh", "packaging/buildArch.sh", "tests/containers/testPackages.sh", "tests/containers/testUpgrade.sh", "tests/booted/checkService.sh", "tests/booted/checkServiceUpgrade.sh"):
            self.assertIn("run: " + script + "\n", ci)
        self.assertLess(ci.index("tests/booted/checkService.sh"), ci.index("tests/booted/checkServiceUpgrade.sh"))

    def testTheBuildImagesArePinnedByDigest(self):
        for script in ("build.sh", "buildArch.sh"):
            with open(os.path.join(PACKAGING, script)) as f:
                lines = f.read().splitlines()
            runs = [line for line in lines if line.strip().startswith("docker run")]
            images = [line for line in lines if line.startswith("image=")]
            self.assertEqual((len(runs), len(images)), (1, 1), script)
            self.assertRegex(images[0], r"^image=[\w./-]+(:[\w.-]+)?@sha256:[0-9a-f]{64}$", script)
            self.assertIn(' "$image" ', runs[0], script)

    def testTheAurBundleCarriesTheInstallFile(self):
        self.assertIn('run: tar -czf "dist/logitech-mouse-battery-$VERSION-aur.tar.gz" -C dist/aur PKGBUILD .SRCINFO logitech-mouse-battery.install\n', self.releaseJob())

    def tagCheck(self):
        release = self.releaseJob()
        start = release.index("      - name: Check that the version's tag, if one exists, points at this commit\n")
        self.assertLess(release.index("subject-checksums"), start)
        self.assertLess(start, release.index("gh release create"))
        lines = release[start:].split("\n        run: |\n", 1)[1].split("\n")
        body = []
        for line in lines:
            if (line.strip() != "" and not line.startswith("          ")):
                break
            body.append(line[10:])
        return "\n".join(body)

    def runTagCheck(self, output, code):
        with tempfile.TemporaryDirectory() as folder:
            git = os.path.join(folder, "git")
            with open(git, "w") as f:
                f.write('#!/bin/sh\necho "$@" > "$FAKE_FOLDER/args"\nprintf "%s" "$FAKE_OUTPUT"\nexit "$FAKE_CODE"\n')
            os.chmod(git, 0o755)
            env = {"PATH": folder + ":" + os.environ["PATH"], "FAKE_FOLDER": folder, "FAKE_OUTPUT": output, "FAKE_CODE": str(code), "VERSION": "1.1.1", "GITHUB_SHA": "a" * 40, "GITHUB_REPOSITORY": "owner/repo"}
            result = subprocess.run(["bash", "-e", "-c", self.tagCheck()], env=env, capture_output=True, text=True)
            with open(os.path.join(folder, "args")) as f:
                args = f.read().split()
        return (result.returncode, result.stdout + result.stderr, args)

    def testTheReleaseAsksForTheTagAndItsPeeledCommit(self):
        self.assertEqual(self.runTagCheck("", 2)[2], ["ls-remote", "--exit-code", "--tags", "https://github.com/owner/repo.git", "refs/tags/v1.1.1", "refs/tags/v1.1.1^{}"])

    def testTheReleasePublishesWhenNoTagExists(self):
        self.assertEqual(self.runTagCheck("", 2)[0], 0)

    def testTheReleasePublishesUnderATagAtThisCommit(self):
        self.assertEqual(self.runTagCheck("a" * 40 + "\trefs/tags/v1.1.1\n", 0)[0], 0)
        self.assertEqual(self.runTagCheck("b" * 40 + "\trefs/tags/v1.1.1\n" + "a" * 40 + "\trefs/tags/v1.1.1^{}\n", 0)[0], 0)

    def testTheReleaseStopsOnATagAtAnotherCommit(self):
        code, output, args = self.runTagCheck("c" * 40 + "\trefs/tags/v1.1.1\n", 0)
        self.assertEqual(code, 1)
        self.assertIn("::error::v1.1.1 already points at " + "c" * 40, output)
        # an annotated tag's own line names the tag object, so a tag object that happens to match proves nothing
        code, output, args = self.runTagCheck("a" * 40 + "\trefs/tags/v1.1.1\n" + "c" * 40 + "\trefs/tags/v1.1.1^{}\n", 0)
        self.assertEqual(code, 1)
        self.assertIn("::error::v1.1.1 already points at " + "c" * 40, output)

    def testTheReleaseStopsWhenTheTagLookupFails(self):
        code, output, args = self.runTagCheck("", 128)
        self.assertEqual(code, 1)
        self.assertIn("::error::couldn't look up the v1.1.1 tag", output)

    def publishStep(self):
        release = self.releaseJob()
        start = release.index("      - name: Publish the release\n")
        lines = release[start:].split("\n        run: |\n", 1)[1].split("\n")
        body = []
        for line in lines:
            if (line.strip() != "" and not line.startswith("          ")):
                break
            body.append(line[10:])
        return "\n".join(body)

    def runPublish(self, releases, version="1.1.1"):
        # the fake hands the step's own --jq filter to jq, so the filter is what picks the drafts
        if (shutil.which("jq") is None):
            self.skipTest("jq isn't installed")
        with tempfile.TemporaryDirectory() as folder:
            gh = os.path.join(folder, "gh")
            with open(gh, "w") as f:
                f.write('#!/bin/sh\necho "$@" >> "$FAKE_FOLDER/calls"\nif [ "$1" = api ] && [ "$2" = --paginate ] && [ "$4" = --jq ]; then printf "%s" "$FAKE_LISTING" | jq -r "$5"; fi\n')
            os.chmod(gh, 0o755)
            env = {"PATH": folder + ":" + os.environ["PATH"], "FAKE_FOLDER": folder, "FAKE_LISTING": json.dumps(releases), "VERSION": version, "GITHUB_SHA": "a" * 40, "GITHUB_REPOSITORY": "owner/repo"}
            result = subprocess.run(["bash", "-e", "-c", self.publishStep()], cwd=REPO, env=env, capture_output=True, text=True)
            calls = []
            if (os.path.exists(os.path.join(folder, "calls"))):
                with open(os.path.join(folder, "calls")) as f:
                    calls = f.read().splitlines()
        return (result.returncode, result.stdout + result.stderr, calls)

    def release(self, releaseId, tag="v1.1.1", draft=True, author="github-actions[bot]"):
        return {"id": releaseId, "tag_name": tag, "draft": draft, "author": None if author is None else {"login": author}}

    def testAStrandedDraftIsDeletedBeforeThePublish(self):
        code, output, calls = self.runPublish([self.release(77), self.release(78, tag="v1.1.0"), self.release(79, draft=False)])
        self.assertEqual(code, 0, output)
        self.assertIn("found a draft release for v1.1.1", output)
        deletes = [call for call in calls if call.startswith("api -X DELETE")]
        self.assertEqual(deletes, ["api -X DELETE repos/owner/repo/releases/77"])
        self.assertTrue(calls[-1].startswith("release create v1.1.1 "), calls)
        self.assertLess(calls.index(deletes[0]), len(calls) - 1)

    def testAHandMadeDraftStopsThePublishBeforeAnyChange(self):
        # the owner's draft for the version may hold the notes meant for it, and a deleted draft can't be brought back
        for other in ("kymotsujason", None):
            with self.subTest(author=other):
                code, output, calls = self.runPublish([self.release(77), self.release(80, author=other), self.release(81, author=other)])
                self.assertEqual(code, 1)
                self.assertIn("::error::v1.1.1 has draft releases this workflow didn't make (80 81), so publish or delete them by hand and run the release again", output)
                self.assertEqual(len(calls), 1, calls)
                self.assertTrue(calls[0].startswith("api --paginate "), calls)

    def testThePublishDeletesNothingWithoutADraft(self):
        code, output, calls = self.runPublish([self.release(79, draft=False), self.release(78, tag="v1.1.0")])
        self.assertEqual(code, 0, output)
        self.assertEqual([call for call in calls if "DELETE" in call], [])
        self.assertNotIn("draft", output)
        self.assertEqual(len(calls), 2)

    def testAVersionWithoutNotesStopsThePublishBeforeAnyCall(self):
        code, output, calls = self.runPublish([self.release(77)], version="9.9.9")
        self.assertNotEqual(code, 0)
        self.assertIn("no section for 9.9.9", output)
        self.assertEqual(calls, [])

    def testTheDraftListingOnlyKeepsDraftsOfThisTag(self):
        self.assertIn(".draft and .tag_name == \\\"v$VERSION\\\"", self.publishStep())

if (__name__ == "__main__"):
    unittest.main()
