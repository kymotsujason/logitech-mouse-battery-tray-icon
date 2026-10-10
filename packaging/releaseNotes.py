import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOOTER = "Install steps are in the README."


def sectionLines(lines, version):
    heading = "## " + version + ","
    found = None
    for line in lines:
        if (line.startswith("## ")):
            if (found is not None):
                break
            if (line.startswith(heading)):
                found = []
        elif (found is not None):
            found.append(line)
    return found


def main(argv):
    if (len(argv) not in (2, 3)):
        print("usage: python3 packaging/releaseNotes.py <VERSION> [CHANGELOG path]", file=sys.stderr)
        return 1
    version = argv[1]
    path = argv[2] if len(argv) == 3 else os.path.join(REPO, "CHANGELOG.md")
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as error:
        print("Couldn't read " + path + ": " + str(error), file=sys.stderr)
        return 1
    found = sectionLines(lines, version)
    if (found is None):
        print(path + " has no section for " + version + ", so add one before releasing", file=sys.stderr)
        return 1
    print("\n".join(found).strip("\n") + "\n\n" + FOOTER)
    return 0


if (__name__ == "__main__"):
    sys.exit(main(sys.argv))
