import os
import sys

repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, repo)
sys.path.insert(0, os.path.join(repo, "src"))

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtDBus import QDBusConnection

import hidpp
import service
import serviceState
from tests.fakes.fakeDevice import FakeDevice, mouseHandler

percentFile = os.environ.get("LIVE_PERCENT_FILE")


def currentPercent():
    try:
        with open(percentFile) as f:
            return int(f.read().strip())
    except (OSError, TypeError, ValueError):
        return 81


def handler(request):
    return mouseHandler(answers={(5, 0): [0x0F, 0x02], (5, 1): [currentPercent(), 0x04, 0, 0]})(request)


# a fake receiver keeps every real one out of the tests and the live checks
device = FakeDevice(handler)
hidpp.findHidppNodes = lambda *args, **kwargs: ["/dev/hidrawFake"]
hidpp.HidppNode.open = staticmethod(lambda path, onEvent=None: device.node(onEvent))
if (len(sys.argv) > 2 and sys.argv[2] == "failOnce"):
    realEncode = serviceState.encodeState
    calls = []

    def failingOnce(receiverList):
        calls.append(1)
        if (len(calls) == 1):
            raise RuntimeError("the first state fails")
        return realEncode(receiverList)

    serviceState.encodeState = failingOnce
app = QCoreApplication(sys.argv[:1])
sys.exit(service.main(QDBusConnection.connectToBus(sys.argv[1], "service")))
