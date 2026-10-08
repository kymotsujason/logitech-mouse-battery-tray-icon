#!/usr/bin/env python3
import errno
import grp
import os
import stat
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


def deviceNumber(rdev):
    return str(os.major(rdev)) + ":" + str(os.minor(rdev))


def checkHeldNode(fd, sysRoot, name):
    info = os.fstat(fd)
    if (not stat.S_ISCHR(info.st_mode)):
        return None
    folder = os.path.join(sysRoot, name)
    try:
        with open(os.path.join(folder, "dev")) as f:
            number = f.read().strip()
        with open(os.path.join(folder, "device", "uevent"), encoding="utf-8", errors="replace") as f:
            uevent = f.read()
        with open(os.path.join(folder, "device", "report_descriptor"), "rb") as f:
            descriptor = f.read()
    except (OSError, ValueError):
        return None
    if (number != deviceNumber(info.st_rdev) or not hidpp.isHidppNode(uevent, descriptor)):
        return None
    return info.st_rdev


def seatKeepsAccess(rdev, udevRoot):
    try:
        with open(os.path.join(udevRoot, "c" + deviceNumber(rdev)), "rb") as f:
            return SEAT_TAG_LINE in f.read().splitlines()
    except OSError:
        return False


def closeNode(fd, gid, keepAcl=False):
    # fchown and fchmod refuse an O_PATH descriptor, so the changes go through its /proc link, which names the same inode
    link = "/proc/self/fd/" + str(fd)
    # the group goes on before the ACL comes off, so a step that fails leaves the node open to the seat instead of to no one
    os.chown(link, 0, gid)
    os.chmod(link, 0o660)
    if (keepAcl):
        return
    try:
        os.removexattr(link, ACL_ATTRIBUTE)
    except OSError as error:
        if (error.errno not in NO_ACL_ERRORS):
            raise


def main(sysRoot="/sys/class/hidraw", devRoot="/dev", udevRoot="/run/udev/data", settled=False):
    try:
        gid = grp.getgrnam(GROUP).gr_gid
    except KeyError:
        print("Couldn't find the " + GROUP + " group, so no node was changed")
        return 1
    failed = False
    for path in hidpp.findHidppNodes(sysRoot, devRoot):
        try:
            # O_PATH holds the inode without opening the device, and a device that later takes this name gets a new inode
            fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            # the node went away, or a container's /sys shows a host node its /dev doesn't have
            continue
        except OSError as error:
            print("Couldn't close access to " + path + ": " + str(error))
            failed = True
            continue
        try:
            # checked again after the open, so the check sees the held device or a newer one, and a newer one has left the held inode unlinked
            rdev = checkHeldNode(fd, sysRoot, os.path.basename(path))
            if (rdev is None):
                continue
            # a change event udev hasn't run yet leaves 1.0.x's tag in its database, while running it puts back any ACL another rule gives
            kept = settled and seatKeepsAccess(rdev, udevRoot)
            closeNode(fd, gid, keepAcl=kept)
        except OSError as error:
            print("Couldn't close access to " + path + ": " + str(error))
            failed = True
            continue
        finally:
            os.close(fd)
        if (kept):
            print("Kept the seat's access to " + path + ", since another package's udev rule gives it")
        else:
            print("Closed access to " + path)
    return 1 if failed else 0


if (__name__ == "__main__"):
    sys.exit(main(settled="--settled" in sys.argv[1:]))
