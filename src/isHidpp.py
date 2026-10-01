import os
import sys

# udev runs this with -I, which leaves the script's own folder off sys.path, so the sibling import needs it put back
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hidDescriptor


def main(argv):
    if (len(argv) != 2):
        return 1
    try:
        with open(os.path.join(argv[1], "device", "report_descriptor"), "rb") as f:
            descriptor = f.read()
    except OSError:
        return 1
    return 0 if hidDescriptor.isHidppDescriptor(descriptor) else 1


if (__name__ == "__main__"):
    sys.exit(main(sys.argv))
