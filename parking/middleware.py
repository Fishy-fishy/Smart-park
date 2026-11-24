from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect

from .hours import facility_status, closed_message, is_admin_account


class OperatingHoursMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user

        if user.is_authenticated and not is_admin_account(user):
            is_open, next_open, cfg = facility_status()

            if not is_open:
                logout(request)
                messages.warning(request, closed_message(next_open, cfg))
                return redirect("login")

        return self.get_response(request)