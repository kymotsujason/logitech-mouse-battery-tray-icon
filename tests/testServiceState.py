import json
import unittest

import hidpp
import serviceState
from serviceState import ServiceEntry, ServiceState

EXAMPLE = '{"version": 1, "busy": false, "searching": false, "nodes": ["/dev/hidraw4"], "deniedPaths": [], "mice": [{"key": "02bc524c", "pathKey": false, "name": "PRO X3 SUPERSTRIKE", "source": "/dev/hidraw4", "percent": 53, "charging": 0, "millivolts": null, "level": null, "asleep": false, "lastRead": 1790000000.0, "wokeAt": 1789999000.0}]}'
EXAMPLE_ENTRY = ServiceEntry("02bc524c", False, "PRO X3 SUPERSTRIKE", "/dev/hidraw4", hidpp.BatteryReading(53, 0), False, 1790000000.0, 1789999000.0)


class FakeMouse:
    def __init__(self, key, nodePath, reading=None, pathKey=False, name=None, asleep=False, lastRead=None, wokeAt=None):
        self.key = key
        self.pathKey = pathKey
        self.name = name
        self.nodePath = nodePath
        self.reading = reading
        self.asleep = asleep
        self.lastRead = lastRead
        self.wokeAt = wokeAt
        self.reportsItself = False
        self.wakeIndex = None
        self.historyRevision = 0


class FakeList:
    def __init__(self, mice, nodes, denied, busy=False, searching=False):
        self.mice = mice
        self.nodes = {path: None for path in nodes}
        self.denied = set(denied)
        self.busy = busy
        self.searching = searching

    def isBusy(self):
        return self.busy

    def isSearching(self):
        return self.searching


def exampleWith(change):
    fields = json.loads(EXAMPLE)
    change(fields)
    return json.dumps(fields)


class StateTests(unittest.TestCase):
    def testTheSpecsExampleDecodes(self):
        self.assertEqual(serviceState.decodeState(EXAMPLE), ServiceState(False, False, ("/dev/hidraw4",), (), (EXAMPLE_ENTRY,)))

    def testAStateRoundTripsWithEveryField(self):
        mouse = FakeMouse("/dev/hidraw7#2", "/dev/hidraw7", hidpp.BatteryReading(None, 3, millivolts=3900, level="good"), pathKey=True, asleep=True, lastRead=1790000100.0, wokeAt=1790000000.0)
        state = serviceState.decodeState(serviceState.encodeState(FakeList([mouse], ["/dev/hidraw7"], ["/dev/hidraw9"], busy=True, searching=True)))
        self.assertEqual(state, ServiceState(True, True, ("/dev/hidraw7",), ("/dev/hidraw9",), (ServiceEntry("/dev/hidraw7#2", True, None, "/dev/hidraw7", hidpp.BatteryReading(None, 3, 3900, "good"), True, 1790000100.0, 1790000000.0),)))

    def testStampsGoOutInWholeSecondsSoARepeatedReadingEncodesTheSame(self):
        reading = hidpp.BatteryReading(53, 0)
        first = FakeList([FakeMouse("a", "/dev/hidraw1", reading, lastRead=1790000100.2, wokeAt=1790000000.9)], ["/dev/hidraw1"], [])
        second = FakeList([FakeMouse("a", "/dev/hidraw1", reading, lastRead=1790000100.7, wokeAt=1790000000.1)], ["/dev/hidraw1"], [])
        self.assertEqual(serviceState.encodeState(first), serviceState.encodeState(second))
        entry = serviceState.decodeState(serviceState.encodeState(first)).mice[0]
        self.assertEqual((entry.lastRead, entry.wokeAt), (1790000100, 1790000000))

    def testAMouseWithNoReadingYetHasNullReadingFields(self):
        fields = json.loads(serviceState.encodeState(FakeList([FakeMouse("02bc524c", "/dev/hidraw4")], ["/dev/hidraw4"], [])))
        entry = fields["mice"][0]
        self.assertEqual((entry["percent"], entry["charging"], entry["millivolts"], entry["level"], entry["lastRead"]), (None, None, None, None, None))
        self.assertIsNone(serviceState.decodeState(json.dumps(fields)).mice[0].reading)

    def testEqualStatesEncodeTheSameWhateverTheOrder(self):
        first = FakeList([FakeMouse("b", "/dev/hidraw2"), FakeMouse("a", "/dev/hidraw1")], ["/dev/hidraw2", "/dev/hidraw1"], ["/dev/hidraw9", "/dev/hidraw8"])
        second = FakeList([FakeMouse("a", "/dev/hidraw1"), FakeMouse("b", "/dev/hidraw2")], ["/dev/hidraw1", "/dev/hidraw2"], ["/dev/hidraw8", "/dev/hidraw9"])
        self.assertEqual(serviceState.encodeState(first), serviceState.encodeState(second))

    def testTheKeysAreSorted(self):
        text = serviceState.encodeState(FakeList([FakeMouse("a", "/dev/hidraw1")], ["/dev/hidraw1"], []))
        self.assertEqual(text, json.dumps(json.loads(text), sort_keys=True))

    def testAnUnknownVersionDecodesToNone(self):
        for version in (2, 0, True, "1", None):
            self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields.update(version=version))), version)

    def testAMissingKeyDecodesToNone(self):
        for key in serviceState.STATE_KEYS:
            self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields.pop(key))), key)
        for key in serviceState.ENTRY_KEYS:
            self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields["mice"][0].pop(key))), key)

    def testMalformedJsonDecodesToNone(self):
        for text in ("", "{", "null", "[]", "1", None):
            self.assertIsNone(serviceState.decodeState(text), text)

    def testABadStampOrListDecodesToNone(self):
        self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields["mice"][0].update(lastRead="soon"))))
        self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields["mice"][0].update(wokeAt="soon"))))
        self.assertIsNone(serviceState.decodeState(EXAMPLE.replace("1790000000.0", "NaN")))
        self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields.update(nodes="/dev/hidraw4"))))

    def testAReadingFieldOfTheWrongTypeDecodesToNone(self):
        cases = [("percent", "53"), ("percent", True), ("percent", 53.5), ("charging", "0"), ("charging", False), ("millivolts", "3900"), ("millivolts", [1]), ("level", 5)]
        for key, value in cases:
            self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields["mice"][0].update({key: value}))), (key, value))

    def testAFlagOrPathOfTheWrongTypeDecodesToNone(self):
        stateCases = [("busy", "false"), ("busy", 0), ("searching", None), ("searching", 1), ("nodes", [4]), ("nodes", [None]), ("deniedPaths", [["/dev/hidraw9"]]), ("mice", {}), ("mice", [5])]
        for key, value in stateCases:
            self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields.update({key: value}))), (key, value))
        entryCases = [("pathKey", "false"), ("pathKey", 0), ("pathKey", None), ("asleep", "false"), ("asleep", 1), ("name", 5), ("name", ["PRO X3"]), ("key", 5), ("source", None)]
        for key, value in entryCases:
            self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields["mice"][0].update({key: value}))), (key, value))

    def testTheFlagsTheServiceSendsStillDecode(self):
        def setFlags(fields):
            fields.update(busy=True, searching=True, deniedPaths=["/dev/hidraw9"])
            fields["mice"][0].update(pathKey=True, asleep=True)
        state = serviceState.decodeState(exampleWith(setFlags))
        self.assertEqual((state.busy, state.searching, state.deniedPaths, state.mice[0].pathKey, state.mice[0].asleep), (True, True, ("/dev/hidraw9",), True, True))

    def testAReadingWithOnlyAVoltageOrALevelStillDecodes(self):
        voltage = exampleWith(lambda fields: fields["mice"][0].update(percent=None, millivolts=3900))
        level = exampleWith(lambda fields: fields["mice"][0].update(percent=None, level="good"))
        self.assertEqual(serviceState.decodeState(voltage).mice[0].reading, hidpp.BatteryReading(None, 0, 3900))
        self.assertEqual(serviceState.decodeState(level).mice[0].reading, hidpp.BatteryReading(None, 0, level="good"))

    def testAnExtraKeyIsIgnored(self):
        def addExtras(fields):
            fields["later"] = 1
            fields["mice"][0]["later"] = 2
        self.assertEqual(serviceState.decodeState(exampleWith(addExtras)), serviceState.decodeState(EXAMPLE))


if (__name__ == "__main__"):
    unittest.main()


class NewFieldTests(unittest.TestCase):
    def testTheNewFieldsRoundTrip(self):
        mouse = FakeMouse("02bc524c", "/dev/hidraw4", hidpp.BatteryReading(22, 1, power=2), lastRead=1790000000.0)
        mouse.reportsItself = True
        mouse.wakeIndex = 23
        mouse.historyRevision = 7
        entry = serviceState.decodeState(serviceState.encodeState(FakeList([mouse], ["/dev/hidraw4"], []))).mice[0]
        self.assertEqual((entry.reading, entry.reportsItself, entry.wakeSupport, entry.historyRevision), (hidpp.BatteryReading(22, 1, power=2), True, True, 7))

    def testAStateFromA111ServiceGetsTheDefaults(self):
        entry = serviceState.decodeState(EXAMPLE).mice[0]
        self.assertEqual((entry.reading.power, entry.reportsItself, entry.wakeSupport, entry.historyRevision), (None, False, False, 0))

    def testANewFieldOfTheWrongTypeReadsAsItsDefault(self):
        def change(fields):
            fields["mice"][0].update({"power": "2", "reportsItself": 1, "wakeSupport": "yes", "historyRevision": True})
        entry = serviceState.decodeState(exampleWith(change)).mice[0]
        self.assertEqual((entry.reading.power, entry.reportsItself, entry.wakeSupport, entry.historyRevision), (None, False, False, 0))

    def testTheNewFieldsStayOutOfTheRequiredKeys(self):
        # a 1.1.1 tray checks every required key, so a 1.2.0 field there would make it turn every state away
        self.assertEqual(serviceState.ENTRY_KEYS, ("key", "pathKey", "name", "source", "percent", "charging", "millivolts", "level", "asleep", "lastRead", "wokeAt"))
