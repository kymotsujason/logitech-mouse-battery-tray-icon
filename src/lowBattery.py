import time

import hidpp

LOW_PERCENT = 10
CRITICAL_PERCENT = 5
REARM_PERCENT = 20
# a device picks its own unit id and the level it reports, so without a limit it could fire a notice per identify or per report
NOTICE_WINDOW_SECONDS = 60 * 60
NOTICE_LIMIT = 4


def thresholdFor(percent):
    return CRITICAL_PERCENT if percent <= CRITICAL_PERCENT else LOW_PERCENT


class LowBatteryWarner:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.sent = {}
        # each notice of the last window as (time, key, threshold), which outlives the mouse leaving the list
        self.notices = []
        # each warning handed out and not yet answered, by (key, threshold), with what it added to sent so a failed one can give that back
        self.claims = {}

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
            return self.claim(key, CRITICAL_PERCENT, reading.percent)
        # the critical warning covers the low one, which keeps a low claim to readings above CRITICAL_PERCENT
        if (reading.percent <= LOW_PERCENT and LOW_PERCENT not in sent and CRITICAL_PERCENT not in sent):
            return self.claim(key, LOW_PERCENT, reading.percent)
        return None

    def claim(self, key, threshold, percent):
        if (not self.allowed(key, threshold)):
            return None
        # a warning counts from the moment it's handed out, so fresh mice in one state can't all pass the limit before the first notice is answered
        sent = self.sent.setdefault(key, set())
        added = {LOW_PERCENT, threshold} - sent
        sent |= added
        notice = (self.clock(), key, threshold)
        self.notices.append(notice)
        self.claims[(key, threshold)] = (notice, added)
        return percent

    def confirm(self, key, percent):
        self.claims.pop((key, thresholdFor(percent)), None)

    def giveBack(self, key, percent):
        # a notice that didn't go out leaves its warning due, so it goes out with a later reading
        claim = self.claims.pop((key, thresholdFor(percent)), None)
        if (claim is None):
            return
        notice, added = claim
        if (notice in self.notices):
            self.notices.remove(notice)
        if (key in self.sent):
            self.sent[key] -= added

    def allowed(self, key, threshold):
        # a held back warning stays due, so it goes out with a later reading once the window allows it
        now = self.clock()
        self.notices = [notice for notice in self.notices if now - notice[0] < NOTICE_WINDOW_SECONDS]
        if (len(self.notices) >= NOTICE_LIMIT):
            return False
        return not any(notice[1] == key and notice[2] == threshold for notice in self.notices)

    def forget(self, key, pathKey=False):
        self.sent.pop(key, None)
        # an answer that comes after the mouse left finds no claim, so it can't arm anything for the key again
        self.claims = {claimKey: claim for claimKey, claim in self.claims.items() if claimKey[0] != key}
        # a key built from a path can pass to a different mouse, which gets warnings of its own
        if (pathKey):
            self.notices = [notice for notice in self.notices if notice[1] != key]
