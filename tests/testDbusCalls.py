import unittest

from PyQt6.QtDBus import QDBusMessage, QDBusObjectPath, QDBusVariant

import dbusCalls


class DbusCallsTests(unittest.TestCase):
    def testPlainUnwrapsNestedVariants(self):
        self.assertEqual(dbusCalls.plain(QDBusVariant(QDBusVariant(5))), 5)

    def testPathTextTakesEitherForm(self):
        self.assertEqual(dbusCalls.pathText(QDBusObjectPath("/a/b")), "/a/b")
        self.assertEqual(dbusCalls.pathText("/a/b"), "/a/b")
        self.assertEqual(dbusCalls.pathText(QDBusVariant(QDBusObjectPath("/a/b"))), "/a/b")

    def testFailureGivesTheErrorNameOrNone(self):
        call = QDBusMessage.createMethodCall("a.b", "/x", "a.b", "M")
        self.assertIsNone(dbusCalls.failure(call.createReply([1])))
        self.assertEqual(dbusCalls.failure(call.createErrorReply("org.freedesktop.DBus.Error.NoReply", "late")), "org.freedesktop.DBus.Error.NoReply")

    def testCallPassesTheTimeout(self):
        calls = []

        class Bus:
            def call(self, message, mode, timeout):
                calls.append(timeout)
                return message.createReply([])

        dbusCalls.call(Bus(), QDBusMessage.createMethodCall("a.b", "/x", "a.b", "M"))
        dbusCalls.call(Bus(), QDBusMessage.createMethodCall("a.b", "/x", "a.b", "M"), 5000)
        self.assertEqual(calls, [2000, 5000])


if (__name__ == "__main__"):
    unittest.main()
