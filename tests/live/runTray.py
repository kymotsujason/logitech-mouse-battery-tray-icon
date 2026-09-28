import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import hidpp
import tray
from tests.fakeDevice import FakeDevice, mouseHandler

percentFile = os.environ.get("LIVE_PERCENT_FILE")


def currentPercent():
    try:
        with open(percentFile) as f:
            return int(f.read().strip())
    except (OSError, TypeError, ValueError):
        return 81


def handler(request):
    return mouseHandler(answers={(5, 0): [0x0F, 0x02], (5, 1): [currentPercent(), 0x04, 0, 0]})(request)


# a fake mouse keeps the real receiver out of the check
device = FakeDevice(handler)
hidpp.findHidppNodes = lambda *args, **kwargs: ["/dev/hidrawFake"]
hidpp.HidppNode.open = staticmethod(lambda path, onEvent=None: device.node(onEvent))
if (os.environ.get("LIVE_NOTICE_MS")):
    tray.NO_TRAY_NOTICE_MS = int(os.environ["LIVE_NOTICE_MS"])
if (os.environ.get("LIVE_EARLY_TRAY")):
    # the control for checkLateTray.sh builds the tray icon before any host, the way Qt 6.4's cached check breaks it
    tray.hostRegistered = lambda bus: True
sys.exit(tray.main())
