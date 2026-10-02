import time

import hidpp

LOW_PERCENT = 10
CRITICAL_PERCENT = 5
REARM_PERCENT = 20
# a device picks its own unit id and the level it reports, so without a limit it could fire a notice per identify or per report
NOTICE_WINDOW_SECONDS = 60 * 60
NOTICE_LIMIT = 4


class LowBatteryWarner:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.sent = {}
        # each notice of the last window as (time, key, threshold), which outlives the mouse leaving the list
        self.notices = []

    def update(self, key, reading, asleep):
        if (asleep or reading is None):
            return None
        if (reading.charging in hidpp.EXTERNAL_POWER):
            self.sent.pop(key, None)
            return None
        if (reading.charging != hidpp.CHARGING_DISCHARGING or reading.percent is None):
            return None
        # a charge the app never saw (suspend, mouse off, wall charger, new batteries) only shows as a high reading, and the margin over LOW_PERCENT keeps a wobbling level from warning twice
        if (reading.percent > REARM_PERCENT):
            self.sent.pop(key, None)
            return None
        sent = self.sent.get(key, set())
        if (reading.percent <= CRITICAL_PERCENT and CRITICAL_PERCENT not in sent):
            return reading.percent if self.allowed(key, CRITICAL_PERCENT) else None
        if (reading.percent <= LOW_PERCENT and LOW_PERCENT not in sent):
            return reading.percent if self.allowed(key, LOW_PERCENT) else None
        return None

    def allowed(self, key, threshold):
        # a held back warning stays due, so it goes out with a later reading once the window allows it
        now = self.clock()
        self.notices = [notice for notice in self.notices if now - notice[0] < NOTICE_WINDOW_SECONDS]
        if (len(self.notices) >= NOTICE_LIMIT):
            return False
        return not any(notice[1] == key and notice[2] == threshold for notice in self.notices)

    def markSent(self, key, percent):
        sent = self.sent.setdefault(key, set())
        sent.add(LOW_PERCENT)
        threshold = LOW_PERCENT
        if (percent <= CRITICAL_PERCENT):
            sent.add(CRITICAL_PERCENT)
            threshold = CRITICAL_PERCENT
        self.notices.append((self.clock(), key, threshold))

    def forget(self, key, pathKey=False):
        self.sent.pop(key, None)
        # a key built from a path can pass to a different mouse, which gets warnings of its own
        if (pathKey):
            self.notices = [notice for notice in self.notices if notice[1] != key]
