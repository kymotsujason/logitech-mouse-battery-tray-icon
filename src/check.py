#!/usr/bin/env python3
import sys

from PyQt6.QtCore import QCoreApplication

import hidpp
import mice
import upower


def printMouse(name, source, reading):
    print((name or mice.DEFAULT_NAME) + " (" + source + ")")
    if (reading is None):
        print("  no battery reply")
    else:
        print("  " + hidpp.describeReading(reading))


def checkHidpp():
    paths = hidpp.findHidppNodes()
    denied = 0
    searched = 0
    found = 0
    for path in paths:
        try:
            node = hidpp.HidppNode.open(path)
        except PermissionError:
            print("No access to " + path + ". Unplug the receiver or cable and plug it back in.")
            denied += 1
            continue
        except OSError as error:
            print("Couldn't open " + path + ": " + str(error))
            continue
        try:
            result = hidpp.searchNode(node)
        except OSError as error:
            print("Lost " + path + " while reading it: " + str(error))
            continue
        except Exception as error:
            # a device that answers in a way the protocol code doesn't expect shouldn't end the check for every other one
            print("Couldn't read " + path + ": " + str(error))
            continue
        finally:
            node.close()
        searched += 1
        for mouse in result.mice:
            printMouse(mouse.name, path, mouse.reading)
            found += 1
    return (len(paths), denied, searched, found)


def checkUPower():
    found, errors = upower.listMice()
    for mouse in found:
        printMouse(mouse.name, "UPower", mouse.reading)
    for error in sorted(set(errors)):
        # UPower missing from the bus isn't a fault worth printing, since mice it would read just aren't there
        if (error not in upower.ABSENT_ERRORS):
            print("UPower answered " + error)
    return len(found)


def main():
    # QtDBus wants an application object before it talks to the system bus
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    nodes, denied, searched, found = checkHidpp()
    found += checkUPower()
    if (found):
        return
    if (nodes == 0):
        print(mice.STATUS_TEXT[mice.STATUS_NONE])
    elif (searched > 0):
        # the tray's waiting text has no "run this again", so the terminal keeps its own line
        print("No mouse answered. Move your mouse to wake it, then run this again.")
    elif (denied < nodes):
        # every node that wasn't denied failed to open or dropped out, so waking the mouse can't help
        print(mice.STATUS_TEXT[mice.STATUS_DENIED])
    sys.exit(1)


if (__name__ == "__main__"):
    main()
