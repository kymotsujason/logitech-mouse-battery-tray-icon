#!/usr/bin/env python3
import errno
import grp
import os
import sys

# the install scripts run this with -I, which leaves the script's own folder off sys.path, so the sibling import needs it put back
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hidpp

GROUP = "logitech-mouse-battery"
ACL_ATTRIBUTE = "system.posix_acl_access"
# a node with no ACL, or on a filesystem with no ACLs at all, has nothing to remove
NO_ACL_ERRORS = (errno.ENODATA, errno.EOPNOTSUPP)
# udev's database lists the tags the node's latest event set as Q lines, while G lines keep every tag it ever had
SEAT_TAG_LINE = b"Q:uaccess"


def seatKeepsAccess(path, sysRoot, udevRoot):
    try:
        with open(os.path.join(sysRoot, os.path.basename(path), "dev")) as f:
            number = f.read().strip()
    except (OSError, ValueError):
        return False
    major, colon, minor = number.partition(":")
    if (not colon or not major.isdigit() or not minor.isdigit()):
        return False
    try:
        with open(os.path.join(udevRoot, "c" + major + ":" + minor), "rb") as f:
            return SEAT_TAG_LINE in f.read().splitlines()
    except OSError:
        return False


def closeNode(path, keepAcl=False):
    if (not keepAcl):
        try:
            os.removexattr(path, ACL_ATTRIBUTE)
        except OSError as error:
            if (error.errno not in NO_ACL_ERRORS):
                raise
    os.chown(path, 0, grp.getgrnam(GROUP).gr_gid)
    os.chmod(path, 0o660)


def main(sysRoot="/sys/class/hidraw", devRoot="/dev", udevRoot="/run/udev/data"):
    failed = False
    for path in hidpp.findHidppNodes(sysRoot, devRoot):
        kept = seatKeepsAccess(path, sysRoot, udevRoot)
        try:
            closeNode(path, keepAcl=kept)
        except FileNotFoundError:
            # the node went away, or a container's /sys shows a host node its /dev doesn't have
            continue
        except (OSError, KeyError) as error:
            print("Couldn't close access to " + path + ": " + str(error))
            failed = True
            continue
        if (kept):
            print("Kept the seat's access to " + path + ", since another package's udev rule gives it")
        else:
            print("Closed access to " + path)
    return 1 if failed else 0


if (__name__ == "__main__"):
    sys.exit(main())
