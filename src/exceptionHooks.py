import sys
import threading
import traceback


def printLine(text):
    # the journal's end of stderr can go away under a running process, and a print that raises would end the slot before the state change after it
    try:
        print(text, file=sys.stderr)
    except (OSError, ValueError):
        pass


def printException(kind, value, tb):
    traceback.print_exception(kind, value, tb)


def printThreadException(args):
    # Python's own hook names the thread, which says which worker died when the traceback alone doesn't
    name = args.thread.name if args.thread is not None else str(threading.get_ident())
    print("Exception in thread " + name + ":", file=sys.stderr)
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)


def installExceptionHooks():
    # PyQt6 aborts the whole process on an exception in a slot unless sys.excepthook is replaced, and nothing restarts an app started from autostart
    sys.excepthook = printException
    threading.excepthook = printThreadException
