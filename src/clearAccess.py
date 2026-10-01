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


def closeNode(path):
    try:
        os.removexattr(path, ACL_ATTRIBUTE)
    except OSError as error:
        if (error.errno not in NO_ACL_ERRORS):
            raise
    os.chown(path, 0, grp.getgrnam(GROUP).gr_gid)
    os.chmod(path, 0o660)


def main(sysRoot="/sys/class/hidraw", devRoot="/dev"):
    failed = False
    for path in hidpp.findHidppNodes(sysRoot, devRoot):
        try:
            closeNode(path)
        except FileNotFoundError:
            # the node went away, or a container's /sys shows a host node its /dev doesn't have
            continue
        except (OSError, KeyError) as error:
            print("Couldn't close access to " + path + ": " + str(error))
            failed = True
            continue
        print("Closed access to " + path)
    return 1 if failed else 0


if (__name__ == "__main__"):
    sys.exit(main())
