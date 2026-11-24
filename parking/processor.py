from .hours import facility_status, closed_message
from .models import SystemSettings


def facility(request):
    data = {"cfg": SystemSettings.load()}

    if not request.user.is_authenticated:
        is_open, next_open, cfg = facility_status()
        data["closed_notice"] = None if is_open else closed_message(next_open, cfg)

    return data