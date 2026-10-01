#!/usr/bin/env python3
import sys
import time

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtDBus import QDBusConnection, QDBusMessage

import dbusCalls
import hidpp
import mice
import serviceState
import upower

# enough for the bus to start the service through activation
READ_ALL_TIMEOUT_MS = 25 * 1000
POLL_MS = 250
WAIT_SECONDS = 10.0
# the tray's waiting text has no "run this again", so the terminal keeps its own line
QUIET_TEXT = "No mouse answered. Move your mouse to wake it, then run this again."
OLD_TEXT = "Some mice were still being read, so their readings may be old."


def printMouse(name, source, reading, asleep=False, lastRead=None):
    print((name or mice.DEFAULT_NAME) + " (" + source + ")")
    if (reading is None):
        print("  no battery reply")
        return
    line = "  " + hidpp.describeReading(reading)
    if (asleep and lastRead is not None):
        line += ", asleep, read " + mice.readingTime(lastRead, time.time())
    print(line)


def callService(bus, member, timeoutMs):
    message = QDBusMessage.createMethodCall(serviceState.SERVICE_NAME, serviceState.OBJECT_PATH, serviceState.INTERFACE, member)
    reply = dbusCalls.call(bus, message, timeoutMs)
    if (dbusCalls.failure(reply) is not None):
        return None
    return reply


def readService(bus):
    # a terminal may block, so this waits for the reads it asked for, and gives back None when the service can't be reached
    if (callService(bus, "ReadAll", READ_ALL_TIMEOUT_MS) is None):
        return (None, False)
    deadline = time.monotonic() + WAIT_SECONDS
    while (True):
        reply = callService(bus, "GetState", dbusCalls.DEFAULT_TIMEOUT_MS)
        arguments = reply.arguments() if reply is not None else []
        state = serviceState.decodeState(arguments[0]) if (arguments and isinstance(arguments[0], str)) else None
        if (state is None):
            return (None, False)
        if (not state.busy):
            return (state, False)
        if (time.monotonic() >= deadline):
            return (state, True)
        time.sleep(POLL_MS / 1000)


def printService(state, stillBusy):
    for path in state.deniedPaths:
        print("No access to " + path + ". Unplug the receiver or cable and plug it back in.")
    for entry in state.mice:
        printMouse(entry.name, entry.source, entry.reading, entry.asleep, entry.lastRead)
    if (stillBusy):
        print(OLD_TEXT)
    return len(state.mice)


def checkUPower(bus):
    found, errors = upower.listMice(bus)
    for mouse in found:
        printMouse(mouse.name, "UPower", mouse.reading)
    for error in sorted(set(errors)):
        # UPower missing from the bus isn't a fault worth printing, since mice it would read just aren't there
        if (error not in upower.ABSENT_ERRORS):
            print("UPower answered " + error)
    return len(found)


def main(bus=None):
    # QtDBus wants an application object before it talks to the system bus
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    if (bus is None):
        bus = QDBusConnection.systemBus()
    state, stillBusy = readService(bus)
    found = 0
    if (state is not None):
        found += printService(state, stillBusy)
    found += checkUPower(bus)
    if (state is None):
        print(mice.STATUS_TEXT[mice.STATUS_NO_SERVICE])
    elif (found == 0 and not state.nodes and not state.deniedPaths):
        print(mice.STATUS_TEXT[mice.STATUS_NONE])
    elif (found == 0 and state.nodes):
        print(QUIET_TEXT)
    if (found == 0):
        sys.exit(1)


if (__name__ == "__main__"):
    main()
