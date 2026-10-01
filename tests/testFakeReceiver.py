import os
import struct
import sys
import unittest

import hidDescriptor
import hidpp

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "booted"))

import fakeReceiver


class FakeReceiverTests(unittest.TestCase):
    def testTheCreateEventFollowsTheKernelLayout(self):
        event = fakeReceiver.createEvent(b"Logitech USB Receiver", fakeReceiver.DESCRIPTOR)
        self.assertEqual(len(event), fakeReceiver.EVENT_SIZE)
        self.assertEqual(struct.unpack_from("<I", event, 0)[0], 11)
        self.assertEqual(event[4:4 + 21], b"Logitech USB Receiver")
        self.assertEqual(struct.unpack_from("<HHIIII", event, 4 + 256), (len(fakeReceiver.DESCRIPTOR), 0x03, 0x046D, 0xC54F, 0, 0))
        self.assertEqual(event[4 + 276:4 + 276 + len(fakeReceiver.DESCRIPTOR)], fakeReceiver.DESCRIPTOR)

    def testAnInputEventCarriesItsSizeAndData(self):
        event = fakeReceiver.inputEvent(b"\x10\x01\x00\x10\x04\x02\x5a")
        self.assertEqual(struct.unpack_from("<IH", event, 0), (12, 7))
        self.assertEqual(event[6:13], b"\x10\x01\x00\x10\x04\x02\x5a")

    def testAnOutputEventGivesBackTheReportTheHostWrote(self):
        report = bytes([0x10, 0x01, 0x00, 0x10, 0x00, 0x00, 0x5a])
        raw = struct.pack("<I", 6) + report.ljust(4096, b"\x00") + struct.pack("<HB", len(report), 1)
        self.assertEqual(fakeReceiver.parseEvent(raw), (6, report))
        self.assertEqual(fakeReceiver.parseEvent(struct.pack("<I", 2).ljust(fakeReceiver.EVENT_SIZE, b"\x00")), (2, b""))

    def testTheDescriptorPassesTheRulesCheck(self):
        self.assertTrue(hidDescriptor.isHidppDescriptor(fakeReceiver.DESCRIPTOR))
        self.assertTrue(hidpp.isHidppNode("DRIVER=hid-generic\nHID_ID=0003:0000046D:0000C54F\n", fakeReceiver.DESCRIPTOR))

    def testAPingGetsTheFakeMousesAnswer(self):
        ping = bytes([0x10, 0x01, 0x00, 0x1a, 0x00, 0x00, 0x5a])
        kind, request = fakeReceiver.parseEvent(struct.pack("<I", 6) + ping.ljust(4096, b"\x00") + struct.pack("<HB", len(ping), 1))
        replies = fakeReceiver.mouseHandler()(request)
        self.assertEqual((kind, len(replies), replies[0][:3]), (6, 1, b"\x10\x01\x00"))


if (__name__ == "__main__"):
    unittest.main()
