from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Asia/Shanghai")


def next_work_time(now=None):
    local = (now or datetime.now(ZONE)).astimezone(ZONE)
    if local.weekday() < 5 and 9 <= local.hour < 18:
        return None
    if local.hour >= 18 or local.weekday() >= 5:
        local += timedelta(days=1)
    while local.weekday() >= 5:
        local += timedelta(days=1)
    return local.replace(hour=9, minute=0, second=0, microsecond=0)
