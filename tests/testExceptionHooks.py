import io
import sys
import threading
import unittest
from unittest import mock

import exceptionHooks


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


if (__name__ == "__main__"):
    unittest.main()
