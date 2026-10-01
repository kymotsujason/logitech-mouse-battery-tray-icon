import os
import sys

repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(repo, "src"))

import tray
import trayHost

if (os.environ.get("LIVE_NOTICE_MS")):
    trayHost.NO_TRAY_NOTICE_MS = int(os.environ["LIVE_NOTICE_MS"])
if (os.environ.get("LIVE_EARLY_TRAY")):
    # the control for checkLateTray.sh builds the tray icon before any host, the way Qt 6.4's cached check breaks it
    trayHost.hostRegistered = lambda bus: True
sys.exit(tray.main())
