import sys
import threading
import traceback


def printException(kind, value, tb):
    traceback.print_exception(kind, value, tb)


def printThreadException(args):
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)


def installExceptionHooks():
    # PyQt6 aborts the whole process on an exception in a slot unless sys.excepthook is replaced, and nothing restarts an app started from autostart
    sys.excepthook = printException
    threading.excepthook = printThreadException
