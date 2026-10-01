import datetime
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKGBUILD = "packaging/aur/PKGBUILD"
SRCINFO = "packaging/aur/.SRCINFO"
METAINFO = "packaging/io.github.kymotsujason.LogitechMouseBattery.metainfo.xml"
VERSION_FILE = "src/version.py"
VERSION_FORMAT =re.compile(r"^\d+\.\d+\.\d+$")


def readFile(name):
    with open(os.path.join(REPO, name)) as f:
        return f.read()


def readVersion():
    match = re.search(r'VERSION = "([^"]*)"', readFile(VERSION_FILE))
    return match.group(1) if match else None


def requireSub(pattern, replacement, text, name):
    result, count = re.subn(pattern, replacement, text, flags=re.M)
    if (count == 0):
        raise ValueError(name + " doesn't hold the current version in the expected form")
    return result


def requireReplace(old, new, text, name):
    if (old not in text):
        raise ValueError(name + " doesn't hold " + old)
    return text.replace(old, new)


def bumpedFiles(old, new, today):
    # every new text is worked out before anything is written, thus a file that doesn't match leaves all of them as they were
    files = {}
    files[VERSION_FILE] = requireSub(r'^VERSION = "' + re.escape(old) + '"$', 'VERSION = "' + new + '"', readFile(VERSION_FILE), VERSION_FILE)
    pkgbuild = requireSub(r"^pkgver=" + re.escape(old) + "$", "pkgver=" + new, readFile(PKGBUILD), PKGBUILD)
    files[PKGBUILD] = re.sub(r"^pkgrel=\d+$", "pkgrel=1", pkgbuild, flags=re.M)
    srcinfo = requireReplace(old, new, readFile(SRCINFO), SRCINFO)
    files[SRCINFO] = re.sub(r"^\tpkgrel = \d+$", "\tpkgrel = 1", srcinfo, flags=re.M)
    files[METAINFO] = requireReplace("  <releases>\n", '  <releases>\n    <release version="' + new + '" date="' + today + '"/>\n', readFile(METAINFO), METAINFO)
    readme = readFile("README.md")
    for pattern in ("logitech-mouse-battery_{}_all.deb", "logitech-mouse-battery-{}-1.noarch.rpm", "logitech-mouse-battery-{}-1-any.pkg.tar.zst"):
        readme = requireReplace(pattern.format(old), pattern.format(new), readme, "README.md")
    files["README.md"] = readme
    return files


def main(argv):
    if (len(argv) != 2 or not VERSION_FORMAT.match(argv[1])):
        print("usage: python3 packaging/bumpVersion.py <MAJOR.MINOR.PATCH>", file=sys.stderr)
        return 1
    new = argv[1]
    old = readVersion()
    if (old is None or new == old):
        print("version.py holds " + str(old) + ", thus there's nothing to bump to " + new, file=sys.stderr)
        return 1
    try:
        files = bumpedFiles(old, new, datetime.date.today().isoformat())
    except ValueError as error:
        print(str(error) + ", thus nothing was changed", file=sys.stderr)
        return 1
    for name, text in files.items():
        with open(os.path.join(REPO, name), "w") as f:
            f.write(text)
    print(old + " to " + new + ", commit and push to main to publish v" + new)
    return 0


if (__name__ == "__main__"):
    sys.exit(main(sys.argv))
