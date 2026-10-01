import hidpp

LOW_PERCENT = 10
CRITICAL_PERCENT = 5
REARM_PERCENT = 20


class LowBatteryWarner:
    def __init__(self):
        self.sent = {}

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
            return reading.percent
        if (reading.percent <= LOW_PERCENT and LOW_PERCENT not in sent):
            return reading.percent
        return None

    def markSent(self, key, percent):
        sent = self.sent.setdefault(key, set())
        sent.add(LOW_PERCENT)
        if (percent <= CRITICAL_PERCENT):
            sent.add(CRITICAL_PERCENT)

    def forget(self, key):
        self.sent.pop(key, None)
