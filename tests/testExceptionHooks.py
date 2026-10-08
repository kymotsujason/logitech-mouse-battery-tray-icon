import errno
import io
import sys
import threading
import unittest
from unittest import mock

import exceptionHooks


class BrokenStream(io.StringIO):
    # stderr once the journal's end of it has gone away
    def write(self, text):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


class ExceptionHookTests(unittest.TestCase):
    def testInstallingReplacesBothHooks(self):
        with mock.patch.object(sys, "excepthook", sys.excepthook), mock.patch.object(threading, "excepthook", threading.excepthook):
            exceptionHooks.installExceptionHooks()
            self.assertEqual((sys.excepthook, threading.excepthook), (exceptionHooks.printException, exceptionHooks.printThreadException))

    def testAnExceptionPrintsItsTraceback(self):
        errors = io.StringIO()
        try:
            raise RuntimeError("slot failed")
        except RuntimeError:
            info = sys.exc_info()
        with mock.patch.object(sys, "stderr", errors):
            exceptionHooks.printException(*info)
        self.assertIn("RuntimeError: slot failed", errors.getvalue())

    def testAThreadExceptionPrintsItsTraceback(self):
        errors = io.StringIO()
        with mock.patch.object(sys, "stderr", errors), mock.patch.object(threading, "excepthook", exceptionHooks.printThreadException):
            worker = threading.Thread(target=lambda: 1 / 0)
            worker.start()
            worker.join()
        self.assertIn("ZeroDivisionError", errors.getvalue())

    def testAThreadExceptionNamesItsThread(self):
        errors = io.StringIO()
        with mock.patch.object(sys, "stderr", errors), mock.patch.object(threading, "excepthook", exceptionHooks.printThreadException):
            worker = threading.Thread(target=lambda: 1 / 0, name="hidpp /dev/hidraw4")
            worker.start()
            worker.join()
        self.assertEqual(errors.getvalue().splitlines()[0], "Exception in thread hidpp /dev/hidraw4:")

    def testPrintLineWritesOneLineToStderr(self):
        errors = io.StringIO()
        with mock.patch.object(sys, "stderr", errors):
            exceptionHooks.printLine("Reached the battery service again")
        self.assertEqual(errors.getvalue(), "Reached the battery service again\n")

    def testPrintLineOnABrokenStderrDoesntRaise(self):
        # a closed stream raises ValueError instead of OSError
        closed = io.StringIO()
        closed.close()
        for stream in (BrokenStream(), closed):
            with self.subTest(stream=stream), mock.patch.object(sys, "stderr", stream):
                exceptionHooks.printLine("UPower answered org.freedesktop.DBus.Error.NoReply")


if (__name__ == "__main__"):
    unittest.main()
