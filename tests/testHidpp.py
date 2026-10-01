import os
import tempfile
import time
import unittest
from unittest import mock

import hidpp
from tests.fakes.descriptors import C54F_DESCRIPTOR, GENERIC_DESKTOP_DESCRIPTOR
from tests.fakes.fakeDevice import NAME, FakeDevice, deviceError, mouseHandler, receiverError, reply



class FindNodeTests(unittest.TestCase):
    def makeNode(self, root, name, hidId, descriptor, driver="hid-generic"):
        base = os.path.join(root, name, "device")
        os.makedirs(base)
        with open(os.path.join(base, "uevent"), "w") as f:
            f.write("DRIVER=" + driver + "\nHID_ID=" + hidId + "\nHID_NAME=Test\n")
        with open(os.path.join(base, "report_descriptor"), "wb") as f:
            f.write(bytes(descriptor))

    def testReadUevent(self):
        fields = hidpp.readUevent("DRIVER=hid-generic\nHID_ID=0003:0000046D:0000C54F\nnoise\n")
        self.assertEqual(fields, {"DRIVER": "hid-generic", "HID_ID": "0003:0000046D:0000C54F"})

    def testFindsOnlyUsbLogitechHidppNodesOnHidGeneric(self):
        with tempfile.TemporaryDirectory() as root:
            self.makeNode(root, "hidraw2", "0003:0000046D:0000C54F", GENERIC_DESKTOP_DESCRIPTOR)
            self.makeNode(root, "hidraw4", "0003:0000046D:0000C54F", C54F_DESCRIPTOR)
            self.makeNode(root, "hidraw6", "0003:00001CA3:00000701", C54F_DESCRIPTOR)
            self.makeNode(root, "hidraw7", "0003:0000046D:0000C52B", C54F_DESCRIPTOR, driver="logitech-djreceiver")
            self.makeNode(root, "hidraw9", "0005:0000046D:0000B034", C54F_DESCRIPTOR)
            self.assertEqual(hidpp.findHidppNodes(root, "/dev"), ["/dev/hidraw4"])

    def testMissingDirectoryGivesNoNodes(self):
        self.assertEqual(hidpp.findHidppNodes("/nonexistent/hidraw", "/dev"), [])

    def testUeventWithABadByteStillMatches(self):
        with tempfile.TemporaryDirectory() as root:
            base = os.path.join(root, "hidraw4", "device")
            os.makedirs(base)
            with open(os.path.join(base, "uevent"), "wb") as f:
                f.write(b"DRIVER=hid-generic\nHID_ID=0003:0000046D:0000C54F\nHID_NAME=Test\xe9\n")
            with open(os.path.join(base, "report_descriptor"), "wb") as f:
                f.write(bytes(C54F_DESCRIPTOR))
            self.assertEqual(hidpp.findHidppNodes(root, "/dev"), ["/dev/hidraw4"])


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.device = None

    def tearDown(self):
        if (self.device is not None):
            self.device.close()

    def testRequestSendsLongReportAndReturnsParams(self):
        self.device = FakeDevice(mouseHandler())
        params = self.device.node().request(1, 0x00, 1, bytes([0, 0, 0xAA]))
        self.assertEqual(params[2], 0xAA)
        sent = self.device.requests[0]
        self.assertEqual(len(sent), 20)
        self.assertEqual(sent[:3], bytes([0x11, 1, 0x00]))
        self.assertEqual(sent[3] >> 4, 1)
        self.assertIn(sent[3] & 0x0F, range(0x08, 0x10))
        self.assertEqual(sent[4:], bytes([0, 0, 0xAA]).ljust(16, b"\x00"))

    def testLateReplyDoesNotAnswerTheNextRequest(self):
        # the first request times out, and its reply shows up during the second one
        def handle(request):
            if (len(self.device.requests) == 1):
                return []
            first = self.device.requests[0]
            return [reply(first, [99]), reply(request, [7])]

        self.device = FakeDevice(handle)
        node = self.device.node()
        self.assertIsNone(node.request(1, 0x00, 0, bytes([0x10, 0x04]), timeout=0.05))
        self.assertEqual(node.request(1, 0x00, 0, bytes([0x10, 0x04]))[0], 7)
        self.assertNotEqual(self.device.requests[0][3], self.device.requests[1][3])

    def testReceiverErrorReturnsNoneQuickly(self):
        self.device = FakeDevice(lambda request: [receiverError(request)])
        start = time.monotonic()
        self.assertIsNone(self.device.node().request(1, 0x00, 1))
        self.assertLess(time.monotonic() - start, 0.5)

    def testDeviceErrorReturnsNone(self):
        self.device = FakeDevice(lambda request: [deviceError(request)])
        self.assertIsNone(self.device.node().request(1, 0x05, 1))

    def testTimeoutReturnsNone(self):
        self.device = FakeDevice(lambda request: [])
        start = time.monotonic()
        self.assertIsNone(self.device.node().request(1, 0x00, 1, timeout=0.1))
        self.assertGreaterEqual(time.monotonic() - start, 0.09)

    def testReportDuringRequestGoesToEventHandler(self):
        event = bytes([0x11, 1, 5, 0x00, 42, 0x04, 0, 0]).ljust(20, b"\x00")
        self.device = FakeDevice(lambda request: [event, reply(request, [7])])
        events = []
        params = self.device.node(events.append).request(1, 0x03, 2)
        self.assertEqual(params[0], 7)
        self.assertEqual(events, [event])

    def testReadReportWithoutDataReturnsNone(self):
        self.device = FakeDevice(lambda request: [])
        os.set_blocking(self.device.appSide.fileno(), False)
        self.assertIsNone(self.device.node().readReport())


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.device = None

    def tearDown(self):
        if (self.device is not None):
            self.device.close()

    def testFeatureLookup(self):
        self.device = FakeDevice(mouseHandler())
        node = self.device.node()
        self.assertEqual(hidpp.getFeatureIndex(node, 1, hidpp.FEATURE_UNIFIED_BATTERY), 5)
        self.assertIsNone(hidpp.getFeatureIndex(node, 1, hidpp.FEATURE_BATTERY_VOLTAGE))

    def testNameIsReadInTwoChunks(self):
        self.device = FakeDevice(mouseHandler())
        self.assertEqual(hidpp.readName(self.device.node(), 1), "PRO X3 SUPERSTRIKE")
        chunkStarts = [r[4] for r in self.device.requests if r[2] == 2 and (r[3] >> 4) == 1]
        self.assertEqual(chunkStarts, [0, 16])

    def testFindsUnifiedBatteryOnReceiverSlot(self):
        self.device = FakeDevice(mouseHandler())
        self.assertEqual(hidpp.findBattery(self.device.node(), 1), (hidpp.ANSWER, hidpp.BatteryFeature(1, hidpp.FEATURE_UNIFIED_BATTERY, 5, True)))

    def testFindsMouseOnCable(self):
        self.device = FakeDevice(mouseHandler(slot=0xFF))
        self.assertEqual(hidpp.searchNode(self.device.node(), pingTimeout=0.02).mice[0].slot, 0xFF)

    def testNothingAnsweringGivesNone(self):
        self.device = FakeDevice(lambda request: [])
        self.assertEqual(hidpp.searchNode(self.device.node(), pingTimeout=0.02).mice, ())

    def testUnifiedBatteryReading(self):
        self.device = FakeDevice(mouseHandler())
        feature = hidpp.BatteryFeature(1, hidpp.FEATURE_UNIFIED_BATTERY, 5, True)
        self.assertEqual(hidpp.readBattery(self.device.node(), feature), hidpp.BatteryReading(81, 0))

    def testUnifiedBatteryWithoutPercent(self):
        answers = {(5, 0): [0x0F, 0x00], (5, 1): [0, 0x04, 1, 1]}
        self.device = FakeDevice(mouseHandler(answers=answers))
        node = self.device.node()
        kind, feature = hidpp.findBattery(node, 1)
        self.assertFalse(feature.hasPercent)
        self.assertEqual(hidpp.readBattery(node, feature), hidpp.BatteryReading(None, 1))

    def testBatteryStatusFeature(self):
        self.device = FakeDevice(mouseHandler(features={0x0005: 2, 0x1000: 6}, answers={(6, 0): [55, 50, 1]}))
        node = self.device.node()
        kind, feature = hidpp.findBattery(node, 1)
        self.assertEqual(feature, hidpp.BatteryFeature(1, hidpp.FEATURE_BATTERY_STATUS, 6, True))
        self.assertEqual(hidpp.readBattery(node, feature), hidpp.BatteryReading(55, 1))

    def testBatteryStatusLevelZeroIsOnlyAReadingWhileDischarging(self):
        answers = {}
        self.device = FakeDevice(mouseHandler(features={0x0005: 2, 0x1000: 6}, answers=answers))
        node = self.device.node()
        feature = hidpp.BatteryFeature(1, hidpp.FEATURE_BATTERY_STATUS, 6, True)
        for status, expected in ((1, hidpp.BatteryReading(None, 1)), (3, hidpp.BatteryReading(100, 3)), (0, hidpp.BatteryReading(0, 0))):
            with self.subTest(status=status):
                answers[(6, 0)] = [0, 0, status]
                self.assertEqual(hidpp.readBattery(node, feature), expected)

    def testFailedReadGivesNone(self):
        self.device = FakeDevice(lambda request: [receiverError(request)])
        feature = hidpp.BatteryFeature(1, hidpp.FEATURE_UNIFIED_BATTERY, 5, True)
        self.assertIsNone(hidpp.readBattery(self.device.node(), feature))


def pingOnly(slot=1):
    def handle(request):
        if (request[1] == slot and request[2] == 0 and (request[3] >> 4) == 1):
            return [reply(request, [4, 2, request[6]])]
        return []

    return handle


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.device = None
        self.patch = mock.patch.object(hidpp, "REQUEST_TIMEOUT", 0.05)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        if (self.device is not None):
            self.device.close()

    def testRequestOutcomeKinds(self):
        self.device = FakeDevice(mouseHandler())
        node = self.device.node()
        self.assertEqual(node.requestOutcome(1, 0x00, 0, bytes([0x10, 0x04]))[0], hidpp.ANSWER)
        self.assertEqual(node.requestOutcome(1, 0x09, 1), (hidpp.ERROR, None))
        self.assertEqual(node.requestOutcome(2, 0x00, 0), (hidpp.TIMEOUT, None))

    def testLookupFeatureOutcomes(self):
        self.device = FakeDevice(mouseHandler())
        node = self.device.node()
        self.assertEqual(hidpp.lookupFeature(node, 1, hidpp.FEATURE_UNIFIED_BATTERY), (hidpp.ANSWER, 5))
        self.assertEqual(hidpp.lookupFeature(node, 1, hidpp.FEATURE_BATTERY_VOLTAGE), (hidpp.ANSWER, 0))
        self.assertEqual(hidpp.lookupFeature(node, 3, hidpp.FEATURE_NAME), (hidpp.TIMEOUT, None))

    def testPingSlotsCollectsAnswers(self):
        self.device = FakeDevice(mouseHandler())
        self.assertEqual(hidpp.pingSlots(self.device.node(), timeout=0.05), [1])

    def testPingSlotsSendsEveryPingBeforeWaiting(self):
        # answers arrive only once the seventh ping is in, which a ping at a time loop never sees
        mouse = mouseHandler()

        def handle(request):
            if (len(self.device.requests) < 7):
                return []
            return [answer for sent in self.device.requests for answer in mouse(sent)]

        self.device = FakeDevice(handle)
        self.assertEqual(hidpp.pingSlots(self.device.node(), timeout=0.3), [1])

    def testErrorRepliesMeanNoDevice(self):
        self.device = FakeDevice(lambda request: [receiverError(request)])
        start = time.monotonic()
        self.assertEqual(hidpp.pingSlots(self.device.node(), timeout=1.0), [])
        self.assertLess(time.monotonic() - start, 0.5)

    def testSearchFindsTheMouse(self):
        self.device = FakeDevice(mouseHandler())
        result = hidpp.searchNode(self.device.node(), pingTimeout=0.05)
        feature = hidpp.BatteryFeature(1, hidpp.FEATURE_UNIFIED_BATTERY, 5, True)
        self.assertEqual(result.mice, (hidpp.FoundMouse(1, "02bc524c", NAME.decode(), feature, hidpp.BatteryReading(81, 0)),))
        self.assertEqual((result.skipped, result.unknown), (frozenset(), frozenset()))

    def testSearchSkipsAKeyboard(self):
        self.device = FakeDevice(mouseHandler(kind=0))
        result = hidpp.searchNode(self.device.node(), pingTimeout=0.05)
        self.assertEqual((result.mice, result.skipped), ((), frozenset({1})))

    def testSearchSkipsADeviceWithoutBattery(self):
        self.device = FakeDevice(mouseHandler(features={0x0003: 3, 0x0005: 2}))
        self.assertEqual(hidpp.searchNode(self.device.node(), pingTimeout=0.05).skipped, frozenset({1}))

    def testSkippedSlotsAreNotPinged(self):
        self.device = FakeDevice(mouseHandler())
        result = hidpp.searchNode(self.device.node(), skipped=frozenset({1}), pingTimeout=0.05)
        self.assertEqual(result.mice, ())
        self.assertFalse(any(sent[1] == 1 for sent in self.device.requests))

    def testTimeoutLeavesTheSlotUnknown(self):
        self.device = FakeDevice(pingOnly())
        result = hidpp.searchNode(self.device.node(), pingTimeout=0.05)
        self.assertEqual((result.mice, result.skipped, result.unknown), ((), frozenset(), frozenset({1})))

    def testErrorReplyLeavesTheSlotUnknown(self):
        answers = {}

        def handle(request):
            if (request[2] == 0 and (request[3] >> 4) == 0):
                return [deviceError(request)]
            return mouseHandler(answers=answers)(request)

        self.device = FakeDevice(handle)
        self.assertEqual(hidpp.searchNode(self.device.node(), pingTimeout=0.05).unknown, frozenset({1}))

    def testMissingDeviceInformationGivesNoUnitId(self):
        self.device = FakeDevice(mouseHandler(features={0x0005: 2, 0x1004: 5}))
        self.assertIsNone(hidpp.searchNode(self.device.node(), pingTimeout=0.05).mice[0].unitId)

    def testZeroUnitIdIsNone(self):
        self.device = FakeDevice(mouseHandler(unitId=bytes(4)))
        self.assertIsNone(hidpp.searchNode(self.device.node(), pingTimeout=0.05).mice[0].unitId)

    def testReportDuringASearchGoesToTheEventHandler(self):
        event = bytes([0x11, 2, 9, 0x00, 1, 2, 3]).ljust(20, b"\x00")
        mouse = mouseHandler()
        self.device = FakeDevice(lambda request: [event] + mouse(request) if request[1] == 1 and request[2] == 0 else mouse(request))
        events = []
        hidpp.searchNode(self.device.node(events.append), pingTimeout=0.05)
        self.assertIn(event, events)


class EventTests(unittest.TestCase):
    feature = hidpp.BatteryFeature(1, hidpp.FEATURE_UNIFIED_BATTERY, 5, True)

    def testBatteryEvent(self):
        report = bytes([0x11, 1, 5, 0x00, 64, 0x04, 1, 1]).ljust(20, b"\x00")
        self.assertEqual(hidpp.parseEvent(report, self.feature), (hidpp.EVENT_BATTERY, hidpp.BatteryReading(64, 1)))

    def testLinkDownAndUp(self):
        down = bytes([0x10, 1, 0x41, 0x10, 0x40, 0x00, 0x00])
        up = bytes([0x10, 1, 0x41, 0x10, 0x00, 0x00, 0x00])
        self.assertEqual(hidpp.parseEvent(down, self.feature), (hidpp.EVENT_LINK_DOWN, None))
        self.assertEqual(hidpp.parseEvent(up, self.feature), (hidpp.EVENT_LINK_UP, None))

    def testOtherSlotIsIgnored(self):
        report = bytes([0x11, 2, 5, 0x00, 64, 0x04, 1, 1]).ljust(20, b"\x00")
        self.assertEqual(hidpp.parseEvent(report, self.feature), (hidpp.EVENT_NONE, None))

    def testRepliesAndErrorsAreIgnored(self):
        lateReply = bytes([0x11, 1, 5, 0x1B, 81, 0x04, 0, 0]).ljust(20, b"\x00")
        otherProgramReply = bytes([0x11, 1, 5, 0x1C, 81, 0x04, 0, 0]).ljust(20, b"\x00")
        lateError = bytes([0x10, 1, 0x8F, 5, 0x1B, 0x09, 0x00])
        self.assertEqual(hidpp.parseEvent(lateReply, self.feature), (hidpp.EVENT_NONE, None))
        self.assertEqual(hidpp.parseEvent(otherProgramReply, self.feature), (hidpp.EVENT_NONE, None))
        self.assertEqual(hidpp.parseEvent(lateError, self.feature), (hidpp.EVENT_NONE, None))

    def testIsReply(self):
        self.assertTrue(hidpp.isReply(bytes([0x11, 1, 5, 0x1B, 81, 0x04, 0, 0])))
        self.assertTrue(hidpp.isReply(bytes([0x10, 0xFF, 0x8F, 0, 0x1A, 0x09, 0x00])))
        self.assertFalse(hidpp.isReply(bytes([0x11, 1, 5, 0x00, 64, 0x04, 1, 1])))
        self.assertFalse(hidpp.isReply(bytes([0x10, 1, 0x41, 0x1C, 0x40, 0x00, 0x00])))
        self.assertFalse(hidpp.isReply(bytes([0x10, 1])))

    def testBatteryStatusEvent(self):
        feature = hidpp.BatteryFeature(1, hidpp.FEATURE_BATTERY_STATUS, 6, True)
        report = bytes([0x11, 1, 6, 0x00, 55, 50, 1]).ljust(20, b"\x00")
        self.assertEqual(hidpp.parseEvent(report, feature), (hidpp.EVENT_BATTERY, hidpp.BatteryReading(55, 1)))

    def testBatteryStatusEventWithLevelZero(self):
        feature = hidpp.BatteryFeature(1, hidpp.FEATURE_BATTERY_STATUS, 6, True)
        for status, expected in ((1, hidpp.BatteryReading(None, 1)), (3, hidpp.BatteryReading(100, 3)), (0, hidpp.BatteryReading(0, 0))):
            with self.subTest(status=status):
                report = bytes([0x11, 1, 6, 0x00, 0, 0, status]).ljust(20, b"\x00")
                self.assertEqual(hidpp.parseEvent(report, feature), (hidpp.EVENT_BATTERY, expected))

    def testUnknownReportFromMouseIsOther(self):
        report = bytes([0x11, 1, 9, 0x10, 1, 2, 3]).ljust(20, b"\x00")
        self.assertEqual(hidpp.parseEvent(report, self.feature), (hidpp.EVENT_OTHER, None))


class TextTests(unittest.TestCase):
    def testDescribeReading(self):
        self.assertEqual(hidpp.describeReading(hidpp.BatteryReading(81, 0)), "81%, discharging")
        self.assertEqual(hidpp.describeReading(hidpp.BatteryReading(None, 1, 3900)), "3900 mV, charging")
        self.assertEqual(hidpp.levelText(hidpp.BatteryReading(None, 0)), "unknown level")
        self.assertEqual(hidpp.chargingName(9), "status 9")


if (__name__ == "__main__"):
    unittest.main()
