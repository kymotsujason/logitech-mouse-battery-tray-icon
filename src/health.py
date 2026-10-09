import math
import statistics
import time
from dataclasses import dataclass, field

import hidpp
import mice

DRAIN_LINE = 0.85
CHARGE_LINE = 1.15
READY_BUCKETS = 3
ERROR_DAYS = 7
SLOW_READINGS = 5
TIME_LEFT_DROPS = 5
TIME_TO_FULL_RISES = 3
DISCHARGE = 0
CHARGE = 1

ALERT = "alert"
DRAIN = "drain"
CHARGE_LINE_KIND = "charge"
GOOD = "good"
LEARNING = "learning"
SYMBOL_CHECK = "check"
SYMBOL_WARNING = "warning"
SYMBOL_CLOCK = "clock"

CHARGING_ERROR_TEXT = "The mouse reported a charging error recently."
CHARGING_SLOW_TEXT = "The mouse reported charging slowly during its last charge. Try another cable or charger."
GOOD_TEXT = "The battery is doing as well as when tracking began."
ROUGH_TEXT = "This mouse reports only rough levels, so drain and charge speed can't be measured."
VOLTAGE_TEXT = "This mouse reports only its voltage, so drain and charge speed can't be measured."
NO_WAKES_TEXT = "This mouse doesn't say when it wakes, so drain can't be measured. Charging speed still can."
LARGE_STEPS_TEXT = "This mouse reports its level in large steps, so drain can't be measured."
WAKES_UNSEEN_TEXT = "This mouse's wakes aren't visible yet, so drain isn't being measured."


@dataclass(frozen=True)
class Line:
    kind: str
    text: str


@dataclass
class Health:
    lines: list = field(default_factory=list)
    symbol: str | None = None
    readyRanges: int = 0
    timeLeft: float | None = None
    timeToFull: float | None = None
    cycles: int = 0
    lastCharged: str | None = None
    chargingSpeed: str | None = None
    hoursPerCharge: list = field(default_factory=list)
    chargingByPower: dict = field(default_factory=dict)


def powerLabel(power):
    # only 0 and 2 have been measured, so other values are named by their number
    if (power == 2):
        return "wireless charger"
    if (power):
        return "charger " + str(power)
    return "charger"


def bucketRatios(entry):
    if (not entry.get("first") or not entry.get("last")):
        return None
    return [last / entry["first"] for last in entry["last"]]


def comparison(buckets):
    # a figure only ever compares a stretch of the battery with the same stretch, and three ready stretches make a comparison
    ready = [ratios for ratios in (bucketRatios(entry) for entry in buckets) if ratios is not None]
    if (len(ready) < READY_BUCKETS):
        return None
    return [statistics.median(ratios[index] for ratios in ready) for index in range(len(ready[0]))]


def readyCount(buckets):
    return sum(1 for entry in buckets if bucketRatios(entry) is not None)


def alertLine(summary, now):
    lastError = summary.get("lastError")
    if (lastError is not None and now - lastError <= ERROR_DAYS * 24 * 60 * 60):
        return Line(ALERT, CHARGING_ERROR_TEXT)
    lastCharge = summary.get("lastCharge")
    if (lastCharge is not None and lastCharge["readingCount"] >= SLOW_READINGS and lastCharge["slowCount"] > lastCharge["readingCount"] / 2):
        return Line(ALERT, CHARGING_SLOW_TEXT)
    return None


def drainLine(figures):
    # every figure has to cross the line, so a verdict holds for three sessions in a row
    if (figures is None or not all(figure <= DRAIN_LINE for figure in figures)):
        return None
    n = round(100 * (1 - figures[0]))
    return Line(DRAIN, "A charge lasts about " + str(n) + "% less than when tracking began. If the mouse's report rate, lighting, and how you use it haven't changed, the battery may be wearing.")


def chargeLine(comparisons):
    ready = {power: figures for power, figures in comparisons.items() if figures is not None}
    slower = [power for power, figures in ready.items() if all(figure >= CHARGE_LINE for figure in figures)]
    if (not slower):
        return None
    n = min(round(100 * (ready[power][0] - 1)) for power in slower)
    if (len(ready) == 1):
        return Line(CHARGE_LINE_KIND, "Charging takes about " + str(n) + "% longer than it used to. Try another cable or charger, and if that doesn't help, the battery may be wearing.")
    if (len(slower) < len(ready)):
        names = " and the ".join(powerLabel(power) for power in slower)
        return Line(CHARGE_LINE_KIND, "Charging on the " + names + " takes about " + str(n) + "% longer than it used to. Try another cable or charger.")
    return Line(CHARGE_LINE_KIND, "Charging takes about " + str(n) + "% longer than it used to, so the battery may be wearing.")


def drainMessage(summary, reading, readyRanges):
    if (reading is not None and reading.percent is None and reading.level is not None):
        return Line(LEARNING, ROUGH_TEXT)
    if (reading is not None and reading.percent is None and reading.millivolts is not None):
        return Line(LEARNING, VOLTAGE_TEXT)
    if (not summary.get("wakeSupport")):
        return Line(LEARNING, NO_WAKES_TEXT)
    if (summary["drain"].get("largeSteps")):
        return Line(LEARNING, LARGE_STEPS_TEXT)
    # a summary from a service older than the field says nothing either way
    if (not summary.get("wakesVisible", True)):
        return Line(LEARNING, WAKES_UNSEEN_TEXT)
    return Line(LEARNING, "Still learning, " + str(readyRanges) + " of " + str(READY_BUCKETS) + " battery ranges measured.")


def healthLines(summary, reading, now):
    drainFigures = comparison(summary["drain"]["buckets"])
    charges = {entry["power"]: comparison(entry["buckets"]) for entry in summary["charge"]}
    readyRanges = readyCount(summary["drain"]["buckets"])
    lines = []
    for line in (alertLine(summary, now), drainLine(drainFigures), chargeLine(charges)):
        if (line is not None):
            lines.append(line)
    anyReady = drainFigures is not None or any(figures is not None for figures in charges.values())
    if (not lines and anyReady):
        lines.append(Line(GOOD, GOOD_TEXT))
    # a learning or capability message is the headline only when nothing else shows
    if (drainFigures is None):
        lines.append(drainMessage(summary, reading, readyRanges))
    return lines[:3], readyRanges


def symbolFor(line):
    if (line is None):
        return None
    if (line.kind == GOOD):
        return SYMBOL_CHECK
    if (line.kind == LEARNING):
        return SYMBOL_CLOCK
    return SYMBOL_WARNING


def monthlySeries(summary):
    hours = []
    byPower = {}
    for month in summary["monthly"]:
        if (month["drain"] is not None):
            hours.append((month["month"], month["drain"] * 100 / 60))
        for power, minutes in month["charge"]:
            byPower.setdefault(powerLabel(power), []).append((month["month"], minutes * 10))
    return hours, byPower


def chargingSpeedText(summary):
    lastCharge = summary.get("lastCharge")
    if (lastCharge is None):
        return None
    for month in reversed(summary["monthly"]):
        for power, minutes in month["charge"]:
            if (power == lastCharge["power"]):
                return str(round(minutes * 10)) + " min per 10%"
    return None


def lastChargedText(summary):
    lastCharge = summary.get("lastCharge")
    if (lastCharge is None):
        return None
    moment = time.localtime(lastCharge["endAt"] if lastCharge["endAt"] is not None else lastCharge["startAt"])
    return mice.MONTH_NAMES[moment.tm_mon - 1] + " " + str(moment.tm_mday) + ", " + powerLabel(lastCharge["power"])


def openFigures(summary, reading):
    opened = summary.get("open")
    if (opened is None or opened["median"] is None or reading is None or reading.percent is None):
        return (None, None)
    if (opened["kind"] == DISCHARGE and reading.charging not in hidpp.EXTERNAL_POWER and opened["count"] >= TIME_LEFT_DROPS):
        return (opened["median"] * reading.percent, None)
    if (opened["kind"] == CHARGE and reading.charging in (hidpp.CHARGING_CHARGING, hidpp.CHARGING_SLOW) and opened["count"] >= TIME_TO_FULL_RISES):
        return (None, opened["median"] * (100 - reading.percent))
    return (None, None)


def assess(summary, reading=None, now=None):
    now = time.time() if now is None else now
    lines, readyRanges = healthLines(summary, reading, now)
    timeLeft, timeToFull = openFigures(summary, reading)
    hours, byPower = monthlySeries(summary)
    drained = sum(month["drained"] for month in summary["monthly"])
    return Health(lines, symbolFor(lines[0] if lines else None), readyRanges, timeLeft, timeToFull, math.floor(drained / 100), lastChargedText(summary), chargingSpeedText(summary), hours, byPower)


def duration(minutes):
    if (minutes < 60):
        return str(max(1, round(minutes))) + " min"
    hours = round(minutes / 60)
    return str(hours) + (" hour" if hours == 1 else " hours")


def statusSuffix(health):
    if (health.timeLeft is not None):
        return ", about " + duration(health.timeLeft) + " left"
    if (health.timeToFull is not None):
        return ", full in about " + duration(health.timeToFull)
    return ""
