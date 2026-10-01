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
        mouse = FakeMouse("/dev/hidraw7#2", "/dev/hidraw7", hidpp.BatteryReading(None, 3, millivolts=3900, level="good"), pathKey=True, asleep=True, lastRead=1790000100.5, wokeAt=1790000000.25)
        state = serviceState.decodeState(serviceState.encodeState(FakeList([mouse], ["/dev/hidraw7"], ["/dev/hidraw9"], busy=True, searching=True)))
        self.assertEqual(state, ServiceState(True, True, ("/dev/hidraw7",), ("/dev/hidraw9",), (ServiceEntry("/dev/hidraw7#2", True, None, "/dev/hidraw7", hidpp.BatteryReading(None, 3, 3900, "good"), True, 1790000100.5, 1790000000.25),)))

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
        self.assertIsNone(serviceState.decodeState(EXAMPLE.replace("1790000000.0", "NaN")))
        self.assertIsNone(serviceState.decodeState(exampleWith(lambda fields: fields.update(nodes="/dev/hidraw4"))))

    def testAnExtraKeyIsIgnored(self):
        def addExtras(fields):
            fields["later"] = 1
            fields["mice"][0]["later"] = 2
        self.assertEqual(serviceState.decodeState(exampleWith(addExtras)), serviceState.decodeState(EXAMPLE))


if (__name__ == "__main__"):
    unittest.main()
