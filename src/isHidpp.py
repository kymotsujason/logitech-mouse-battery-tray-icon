import errno
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
    except OSError as error:
        # a node unplugged during the rule is no match, while any other failure leaves the node without the group, so udev's debug log gets the reason
        if (error.errno in (errno.ENOENT, errno.ENODEV)):
            return 1
        print("Couldn't read the report descriptor under " + argv[1] + ": " + str(error), file=sys.stderr)
        return 2
    return 0 if hidDescriptor.isHidppDescriptor(descriptor) else 1


if (__name__ == "__main__"):
    sys.exit(main(sys.argv))
