import zoneinfo
from datetime import timedelta

from django.utils import timezone

from .models import SystemSettings


def is_admin_account(user):
    """Administrators are never blocked, so they can always fix the hours."""
    profile = getattr(user, "profile", None)
    return user.is_superuser or (profile is not None and profile.role == "admin")


def facility_status():
    """Returns (is_open, next_open_datetime_or_None, cfg)."""
    cfg = SystemSettings.load()

    if cfg.open_24_7 or cfg.open_time == cfg.close_time:
        return True, None, cfg

    tz = zoneinfo.ZoneInfo(cfg.timezone or "Asia/Manila")
    now = timezone.now().astimezone(tz)
    t, o, c = now.time(), cfg.open_time, cfg.close_time

    is_open = (o <= t < c) if o < c else (t >= o or t < c)
    if is_open:
        return True, None, cfg

    nxt = now.replace(hour=o.hour, minute=o.minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return False, nxt, cfg


def closed_message(nxt, cfg):
    tz = zoneinfo.ZoneInfo(cfg.timezone or "Asia/Manila")
    today = timezone.now().astimezone(tz).date()
    day = "today" if nxt.date() == today else "tomorrow"
    time_str = nxt.strftime("%I:%M %p").lstrip("0")
    return (
        f"{cfg.facility_name} is currently closed. "
        f"You can come back {day} at {time_str}."
    )