import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOOTER = "Install steps are in the README."
USAGE = "usage: python3 packaging/releaseNotes.py [--check] <VERSION> [CHANGELOG path]"


def heading(line):
    # a section heading is "## <version>, <date>", and Markdown allows more than one space after the hashes
    match = re.match(r"##[ \t]+(.*)$", line)
    if (match is None):
        return None
    version, comma, date = match.group(1).partition(",")
    return (version.strip(), date.strip())


def section(lines, version):
    found = None
    for line in lines:
        parts = heading(line)
        if (parts is not None):
            if (found is not None):
                break
            if (parts[0] == version):
                found = (parts[1], [])
        elif (found is not None):
            found[1].append(line)
    return found


def isDate(text):
    if (re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) is None):
        return False
    try:
        time.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def main(argv):
    arguments = argv[1:]
    check = "--check" in arguments
    if (check):
        arguments.remove("--check")
    if (len(arguments) not in (1, 2)):
        print(USAGE, file=sys.stderr)
        return 1
    version = arguments[0]
    path = arguments[1] if len(arguments) == 2 else os.path.join(REPO, "CHANGELOG.md")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except (OSError, UnicodeDecodeError) as error:
        print("Couldn't read " + path + ": " + str(error), file=sys.stderr)
        return 1
    found = section(lines, version)
    if (found is None):
        print(path + " has no section for " + version + ", so add one before releasing", file=sys.stderr)
        return 1
    date, body = found
    # GitHub hides an HTML comment, and telling one apart from a literal "<!--" in code takes a Markdown parser, so a section holding one is refused instead of stripped
    if (any("<!--" in line for line in body)):
        print(path + " has \"<!--\" under " + version + ", which GitHub may hide as an HTML comment, so remove it or write it another way before releasing", file=sys.stderr)
        return 1
    # a heading with nothing under it would publish a release whose notes are only the footer
    if (not any(line.strip().startswith("- ") for line in body)):
        print(path + " has no entries under " + version + ", so add at least one before releasing", file=sys.stderr)
        return 1
    if (check):
        if (not isDate(date)):
            print(path + " heads " + version + " with \"" + date + "\" instead of a YYYY-MM-DD date, so date it before releasing", file=sys.stderr)
            return 1
        return 0
    print("\n".join(body).strip("\n") + "\n\n" + FOOTER)
    return 0


if (__name__ == "__main__"):
    sys.exit(main(sys.argv))
