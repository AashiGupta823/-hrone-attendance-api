"""Standalone unit tests for the date/time/rounding helpers (no MongoDB needed)."""
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP

IST_OFFSET = timedelta(hours=5, minutes=30)


def parse_hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start, shift_end):
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    return (eh, em) <= (sh, sm)


def utc_to_ist_naive(dt_utc):
    return dt_utc + IST_OFFSET


def ist_naive_to_utc(dt_ist):
    return dt_ist - IST_OFFSET


def truncate_seconds(dt):
    return dt.replace(microsecond=0)


def compute_record_date(punch_in_utc, shift_start, shift_end):
    """R1: returns 'YYYY-MM-DD' attendance date for a punch-in instant."""
    punch_in_utc = truncate_seconds(punch_in_utc)
    ist = utc_to_ist_naive(punch_in_utc)
    overnight = is_overnight(shift_start, shift_end)
    eh, em = parse_hhmm(shift_end)
    if overnight and (ist.hour, ist.minute, ist.second) < (eh, em, 0):
        record_date = ist.date() - timedelta(days=1)
    else:
        record_date = ist.date()
    return record_date.isoformat()


def shift_instant_utc(record_date_str, hhmm, is_end, overnight):
    """UTC instant for a shift boundary (start or end) of a given attendance record date."""
    d = date.fromisoformat(record_date_str)
    if is_end and overnight:
        d = d + timedelta(days=1)
    h, m = parse_hhmm(hhmm)
    ist_naive = datetime(d.year, d.month, d.day, h, m, 0)
    return ist_naive_to_utc(ist_naive)


def compute_late_minutes(punch_in_utc, record_date_str, shift_start, shift_end):
    """R2."""
    punch_in_utc = truncate_seconds(punch_in_utc)
    overnight = is_overnight(shift_start, shift_end)
    start_instant = shift_instant_utc(record_date_str, shift_start, False, overnight)
    diff_seconds = (punch_in_utc - start_instant).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_overtime_minutes(punch_out_utc, record_date_str, shift_start, shift_end):
    """R3."""
    punch_out_utc = truncate_seconds(punch_out_utc)
    overnight = is_overnight(shift_start, shift_end)
    end_instant = shift_instant_utc(record_date_str, shift_end, True, overnight)
    diff_seconds = (punch_out_utc - end_instant).total_seconds()
    diff_minutes = int(diff_seconds // 60) if diff_seconds >= 0 else int(-(-diff_seconds // 60) * -1)
    if diff_seconds >= 0 and diff_minutes >= 30:
        return diff_minutes
    return 0


def round_half_up(value, decimals):
    q = Decimal(10) ** -decimals
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in_utc, punch_out_utc):
    """R4."""
    punch_in_utc = truncate_seconds(punch_in_utc)
    punch_out_utc = truncate_seconds(punch_out_utc)
    total_seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    hours = Decimal(total_seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours):
    """R5."""
    return work_hours < 4.50


# --------------------------------------------------------------------------- #
# Tests from the spec examples
# --------------------------------------------------------------------------- #
def ist(y, m, d, h, mi, s=0):
    """Build a naive-UTC datetime from an IST wall-clock time (for test convenience)."""
    return ist_naive_to_utc(datetime(y, m, d, h, mi, s))


# R2 examples: shift 09:30
assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0), "2026-07-06", "09:30", "18:30") == 0, "09:40:00 should be 0"
assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), "2026-07-06", "09:30", "18:30") == 10, "09:40:01 should be 10"
assert compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), "2026-07-06", "09:30", "18:30") == 45, "10:15:59 should be 45"
print("R2 OK")

# R1/overnight: shift 22:00-06:00, punch-in 21:55 IST same day -> date = that day
d = compute_record_date(ist(2026, 7, 6, 21, 55, 0), "22:00", "06:00")
assert d == "2026-07-06", d
# punch-in 00:30 IST (after midnight) -> belongs to previous day's shift
d2 = compute_record_date(ist(2026, 7, 7, 0, 30, 0), "22:00", "06:00")
assert d2 == "2026-07-06", d2
print("R1 overnight OK")

# DATA_MODEL sample #1: in 09:28, out 18:35, shift 09:30-18:30 -> work_hours 9.12, late 0, OT 0 (5 min < 30)
pin = ist(2026, 7, 6, 9, 28, 0)
pout = ist(2026, 7, 6, 18, 35, 0)
wh = compute_work_hours(pin, pout)
assert wh == 9.12, wh
late = compute_late_minutes(pin, "2026-07-06", "09:30", "18:30")
assert late == 0, late
ot = compute_overtime_minutes(pout, "2026-07-06", "09:30", "18:30")
assert ot == 0, ot
print("Sample #1 OK:", wh, late, ot)

# DATA_MODEL sample #2: in 10:05, out 19:10 -> work_hours 9.08, late 35, OT 40
pin = ist(2026, 7, 6, 10, 5, 0)
pout = ist(2026, 7, 6, 19, 10, 0)
wh = compute_work_hours(pin, pout)
late = compute_late_minutes(pin, "2026-07-06", "09:30", "18:30")
ot = compute_overtime_minutes(pout, "2026-07-06", "09:30", "18:30")
assert wh == 9.08, wh
assert late == 35, late
assert ot == 40, ot
print("Sample #2 OK:", wh, late, ot)

# DATA_MODEL sample #4: WFH 3.5 hours -> half_day True
pin = ist(2026, 7, 7, 9, 30, 0)
pout = ist(2026, 7, 7, 13, 0, 0)
wh = compute_work_hours(pin, pout)
assert wh == 3.5, wh
assert compute_half_day(wh) is True
print("Sample #4 OK:", wh)

# DATA_MODEL sample #7: overnight 22:00-06:00, in 21:55, out next day 06:40 -> work_hours 8.75, OT 40
pin = ist(2026, 7, 6, 21, 55, 0)
pout = ist(2026, 7, 7, 6, 40, 0)
wh = compute_work_hours(pin, pout)
ot = compute_overtime_minutes(pout, "2026-07-06", "22:00", "06:00")
assert wh == 8.75, wh
assert ot == 40, ot
print("Sample #7 (overnight) OK:", wh, ot)

# R4 half-up rounding check: a value that would differ between banker's and half-up rounding
assert round_half_up(2.345, 2) == 2.35
assert round_half_up(2.005, 2) == 2.01  # python's round() would give 2.0 here due to banker's/float issues
print("R8 half-up rounding OK")

print("ALL HELPER TESTS PASSED")
