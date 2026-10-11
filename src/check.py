#!/usr/bin/env python3
import signal
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
LIMITED_TEXT = "Reads are limited right now, so these readings may be old."
UNKNOWN_CALLER_TEXT = "The battery service couldn't tell which account asked, so these readings may be old."
UNREADABLE_TEXT = "The battery service answered with a state this version can't read."
SEE_WHY_TEXT = "  Run \"systemctl status logitech-mouse-battery\" to see why."


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
        return (None, reply.errorName() + ": " + reply.errorMessage())
    return (reply, None)


def unreachable(error):
    return [mice.STATUS_TEXT[mice.STATUS_NO_SERVICE], "  " + error, SEE_WHY_TEXT]


def readService(bus):
    # a terminal may block, so this waits for the reads it asked for, and gives back the state, the lines to print under it, and the lines to print when there's no state
    notes = []
    reply, error = callService(bus, "ReadAll", READ_ALL_TIMEOUT_MS)
    # the service turned the read down but answered, so its state is still worth printing
    refusals = {serviceState.READ_ALL_LIMITED: LIMITED_TEXT, serviceState.CALLER_UNKNOWN: UNKNOWN_CALLER_TEXT}
    if (error is not None):
        name = error.partition(":")[0]
        if (name not in refusals):
            return (None, [], unreachable(error))
        notes.append(refusals[name])
    deadline = time.monotonic() + WAIT_SECONDS
    last = None
    while (True):
        reply, error = callService(bus, "GetState", dbusCalls.DEFAULT_TIMEOUT_MS)
        if (error is not None):
            # a state the service gave a moment ago is still worth printing, marked as possibly old
            if (last is not None):
                return (last, notes + [OLD_TEXT, "  " + error], None)
            return (None, [], unreachable(error))
        arguments = reply.arguments()
        state = serviceState.decodeState(arguments[0]) if (arguments and isinstance(arguments[0], str)) else None
        if (state is None):
            if (last is not None):
                return (last, notes + [OLD_TEXT, "  " + UNREADABLE_TEXT], None)
            return (None, [], [UNREADABLE_TEXT, SEE_WHY_TEXT])
        last = state
        if (not state.busy):
            return (state, notes, None)
        if (time.monotonic() >= deadline):
            return (state, notes + [OLD_TEXT], None)
        time.sleep(POLL_MS / 1000)


def printService(state, notes):
    for path in state.deniedPaths:
        print("No access to " + path + ". Unplug the receiver or cable and plug it back in.")
    for entry in state.mice:
        printMouse(entry.name, entry.source, entry.reading, entry.asleep, entry.lastRead)
    for note in notes:
        print(note)
    return len(state.mice)


def checkUPower(bus):
    found, errors = upower.listMice(bus)
    for mouse in found:
        printMouse(mouse.name, "UPower", mouse.reading, asleep=mouse.asleep, lastRead=mouse.updatedAt)
    for error in sorted(set(errors)):
        # UPower missing from the bus isn't a fault worth printing, since mice it would read just aren't there
        if (error not in upower.ABSENT_ERRORS):
            print("UPower answered " + error)
    return len(found)


def main(bus=None):
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    # a terminal whose locale isn't UTF-8 may not take every letter a mouse's name can hold, so one it can't show prints as a question mark
    if (hasattr(sys.stdout, "reconfigure")):
        sys.stdout.reconfigure(errors="replace")
    # QtDBus wants an application object before it talks to the system bus
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])
    if (bus is None):
        bus = QDBusConnection.systemBus()
    state, notes, problem = readService(bus)
    found = 0
    if (state is not None):
        found += printService(state, notes)
    found += checkUPower(bus)
    if (state is None):
        for line in problem:
            print(line)
    elif (found == 0 and not state.nodes and state.searching):
        print(mice.STATUS_TEXT[mice.STATUS_SEARCHING])
    elif (found == 0 and not state.nodes and not state.deniedPaths):
        print(mice.STATUS_TEXT[mice.STATUS_NONE])
    elif (found == 0 and state.nodes):
        print(QUIET_TEXT)
    if (found == 0):
        sys.exit(1)


if (__name__ == "__main__"):
    main()
