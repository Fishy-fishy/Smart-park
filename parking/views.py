import math
import re
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .hours import facility_status, closed_message, is_admin_account
from django.contrib import messages
from django.contrib.auth import (
    get_user_model,
    login,
    logout,
    update_session_auth_hash,
)
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
from django.contrib.auth.views import LoginView
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import (
    Avg,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Prefetch,
    Q,
    Sum,
)
from django.db.models.deletion import ProtectedError
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timesince import timesince, timeuntil
from django.views.decorators.http import require_POST

from .forms import DriverSignUpForm
from .models import (
    Allocation,
    DriverPreference,
    ParkingSlot,
    Payment,
    Profile,
    Reservation,
    SystemSettings,
    Vehicle,
    Zone,
)

User = get_user_model()


def slot_sort_key(slot_number):
    """A-04 -> 4, B-5 -> 5, E0-7 -> 7, E-010 -> 10 (uses the last number)."""
    nums = re.findall(r"\d+", slot_number or "")
    return (int(nums[-1]) if nums else 0, slot_number or "")


# ==========================================================
# ROLE HELPERS
# ==========================================================

def get_role(user):

    if not user.is_authenticated:
        return None

    profile = getattr(user, "profile", None)

    if profile is not None:
        return profile.role

    return "admin" if user.is_superuser else "driver"


def is_approved_admin(user):

    if not user.is_authenticated:
        return False

    profile = getattr(user, "profile", None)

    return (
        profile is not None
        and profile.role == Profile.Role.ADMIN
        and profile.approval_status == Profile.ApprovalStatus.APPROVED
    )


def is_main_admin(user):

    if not user.is_authenticated:
        return False

    profile = getattr(user, "profile", None)

    return (
        profile is not None
        and profile.role == Profile.Role.ADMIN
        and profile.approval_status == Profile.ApprovalStatus.APPROVED
        and profile.is_main_admin
    )


def home_for(user):

    if get_role(user) == Profile.Role.ADMIN:

        if is_approved_admin(user):
            return reverse("admin_request")

        return reverse("login")

    return reverse("driver_home")


# ==========================================================
# ROLE ACCESS DECORATOR
# ==========================================================

def role_required(role):

    def decorator(view):

        @wraps(view)
        def wrapper(request, *args, **kwargs):

            if not request.user.is_authenticated:
                return redirect("login")

            user_role = get_role(request.user)

            if (
                user_role == Profile.Role.ADMIN
                and not is_approved_admin(request.user)
            ):

                messages.error(
                    request,
                    "Your administrator account has not been approved."
                )

                return redirect("login")

            if user_role != role:

                messages.error(
                    request,
                    "You are not authorized to access this page."
                )

                return redirect(home_for(request.user))

            return view(request, *args, **kwargs)

        return wrapper

    return decorator


# ==========================================================
# MAIN ADMIN ACCESS DECORATOR
# ==========================================================

def main_admin_required(view):

    @wraps(view)
    @login_required(login_url="login")
    def wrapper(request, *args, **kwargs):

        if not is_main_admin(request.user):

            messages.error(
                request,
                "Only the Main Administrator can manage user accounts."
            )

            return redirect(home_for(request.user))

        return view(request, *args, **kwargs)

    return wrapper


# ==========================================================
# AUTO-COMPLETE EXPIRED RESERVATIONS
#
# - CONFIRMED reservations whose end time has passed become COMPLETED
#   and their reserved slot is freed (unless a vehicle is still parked
#   there or another confirmed reservation is still upcoming/active).
# - PENDING reservations whose end time has passed can no longer be
#   approved, so they are marked CANCELLED.
#
# This runs whenever the reservation page, parking slot page, live slot
# map or driver dashboard is loaded. It can also be run on a schedule
# (see the optional management command) so slots are freed even when
# nobody is browsing the site.
# ==========================================================

def auto_complete_expired_reservations():

    now = timezone.now()

    with transaction.atomic():

        expired = list(
            Reservation.objects
            .select_for_update()
            .filter(
                status=Reservation.Status.CONFIRMED,
                end_time__lte=now,
            )
        )

        for reservation in expired:

            reservation.status = Reservation.Status.COMPLETED
            reservation.save(update_fields=["status"])

            slot = ParkingSlot.objects.select_for_update().get(
                pk=reservation.slot_id
            )

            # Only a slot that is still marked reserved can be freed.
            if slot.status != ParkingSlot.Status.RESERVED:
                continue

            active_allocation = Allocation.objects.filter(
                slot=slot,
                exit_time__isnull=True
            ).exists()

            another_confirmed = Reservation.objects.filter(
                slot=slot,
                status=Reservation.Status.CONFIRMED,
                end_time__gt=now,
            ).exclude(
                pk=reservation.pk
            ).exists()

            if not active_allocation and not another_confirmed:
                slot.status = ParkingSlot.Status.AVAILABLE
                slot.save(update_fields=["status"])

        # Pending requests that nobody approved before they ended.
        Reservation.objects.filter(
            status=Reservation.Status.PENDING,
            end_time__lte=now,
        ).update(status=Reservation.Status.CANCELLED)


# ==========================================================
# LOGIN
# ==========================================================

class SmartLoginView(LoginView):

    template_name = "login.html"
    redirect_authenticated_user = False

    def get_success_url(self):

        if is_approved_admin(self.request.user):
            return reverse("admin_request")

        return reverse("driver_home")

    def form_valid(self, form):

        user = form.get_user()
        chosen = self.request.POST.get("role", "admin")

        profile = getattr(user, "profile", None)

        # Check administrator approval.
        if profile and profile.role == Profile.Role.ADMIN:

            if profile.approval_status == Profile.ApprovalStatus.PENDING:

                form.add_error(
                    None,
                    "Your administrator account is still waiting for approval."
                )

                return self.form_invalid(form)

            if profile.approval_status == Profile.ApprovalStatus.REJECTED:

                form.add_error(
                    None,
                    "Your administrator registration was rejected."
                )

                return self.form_invalid(form)

        # ===== NEW: check operating hours (drivers only) =====
        if not is_admin_account(user):

            is_open, next_open, cfg = facility_status()

            if not is_open:

                form.add_error(None, closed_message(next_open, cfg))

                return self.form_invalid(form)
        # =====================================================

        # Check selected login role.
        if get_role(user) != chosen:

            label = (
                "an Administrator"
                if chosen == "admin"
                else "a Driver"
            )

            form.add_error(
                None,
                f"This account is not {label} account. "
                "Please select the correct login type."
            )

            return self.form_invalid(form)

        response = super().form_valid(form)

        # Remember me.
        if self.request.POST.get("remember_me"):
            self.request.session.set_expiry(60 * 60 * 24 * 14)
        else:
            self.request.session.set_expiry(0)

        return response


# ==========================================================
# DRIVER REGISTRATION
# ==========================================================

def register(request):

    if request.user.is_authenticated:
        return redirect(home_for(request.user))

    if request.method == "POST":

        form = DriverSignUpForm(request.POST)

        if form.is_valid():

            with transaction.atomic():

                user = form.save()

                profile = user.profile
                profile.role = Profile.Role.DRIVER
                profile.approval_status = Profile.ApprovalStatus.APPROVED
                profile.phone = form.cleaned_data.get("phone", "")
                profile.save()

            # Do not log the new driver in automatically.
            # Send them back to the login page to sign in themselves.
            messages.success(
                request,
                "Your driver account has been created successfully. "
                "Please log in."
            )

            return redirect("login")

    else:
        form = DriverSignUpForm()

    return render(
        request,
        "login.html",
        {
            "reg_form": form,
            "show_register": True,
            "registration_role": "driver",
        }
    )


# ==========================================================
# ADMINISTRATOR REGISTRATION
# ==========================================================

def admin_register(request):

    if request.user.is_authenticated:
        return redirect(home_for(request.user))

    if request.method == "POST":

        form = DriverSignUpForm(request.POST)

        if form.is_valid():

            with transaction.atomic():

                user = form.save()

                profile = user.profile
                profile.role = Profile.Role.ADMIN
                profile.approval_status = Profile.ApprovalStatus.PENDING
                profile.phone = form.cleaned_data.get("phone", "")
                profile.is_main_admin = False
                profile.save()

            messages.success(
                request,
                "Administrator registration submitted successfully. "
                "Please wait for approval from an existing administrator."
            )

            return redirect("login")

    else:
        form = DriverSignUpForm()

    return render(
        request,
        "login.html",
        {
            "reg_form": form,
            "show_register": True,
            "registration_role": "admin",
        }
    )


# ==========================================================
# ADMINISTRATOR REQUESTS + DRIVER ACCOUNTS (MERGED PAGE)
# ==========================================================

@role_required("admin")
def admin_request(request):

    pending_admins = Profile.objects.filter(
        role=Profile.Role.ADMIN,
        approval_status=Profile.ApprovalStatus.PENDING
    ).select_related("user").order_by("-created_at")

    approved_admins = Profile.objects.filter(
        role=Profile.Role.ADMIN,
        approval_status=Profile.ApprovalStatus.APPROVED
    ).select_related("user").order_by("-created_at")

    rejected_admins = Profile.objects.filter(
        role=Profile.Role.ADMIN,
        approval_status=Profile.ApprovalStatus.REJECTED
    ).select_related("user").order_by("-created_at")

    # Driver accounts (shown only to the Main Administrator in the template).
    driver_profiles = Profile.objects.filter(
        role=Profile.Role.DRIVER
    ).select_related("user").order_by("-created_at")

    context = {
        "pending_admins": pending_admins,
        "pending_requests": pending_admins,
        "approved_admins": approved_admins,
        "rejected_admins": rejected_admins,
        "driver_profiles": driver_profiles,

        "pending_count": pending_admins.count(),

        "total_admins": (
            pending_admins.count()
            + approved_admins.count()
            + rejected_admins.count()
        ),

        "total_drivers": driver_profiles.count(),

        "is_main_admin": is_main_admin(request.user),
    }

    return render(
        request,
        "admin_request.html",
        context
    )


# ==========================================================
# APPROVE ADMINISTRATOR
# ==========================================================

@role_required("admin")
@require_POST
def approve_admin(request, pk):

    with transaction.atomic():

        profile = get_object_or_404(
            Profile.objects.select_for_update().select_related("user"),
            pk=pk,
            role=Profile.Role.ADMIN
        )

        if profile.is_main_admin:

            messages.error(
                request,
                "The Main Administrator account cannot be changed here."
            )

            return redirect("admin_request")

        if profile.approval_status != Profile.ApprovalStatus.PENDING:

            messages.warning(
                request,
                "This administrator request is no longer pending."
            )

            return redirect("admin_request")

        profile.approval_status = Profile.ApprovalStatus.APPROVED
        profile.save(update_fields=["approval_status"])

    messages.success(
        request,
        f"Administrator account '{profile.user.username}' "
        "has been approved successfully."
    )

    return redirect("admin_request")


# ==========================================================
# REJECT ADMINISTRATOR
# ==========================================================

@role_required("admin")
@require_POST
def reject_admin(request, pk):

    with transaction.atomic():

        profile = get_object_or_404(
            Profile.objects.select_for_update().select_related("user"),
            pk=pk,
            role=Profile.Role.ADMIN
        )

        if profile.is_main_admin:

            messages.error(
                request,
                "The Main Administrator account cannot be changed here."
            )

            return redirect("admin_request")

        if profile.approval_status != Profile.ApprovalStatus.PENDING:

            messages.warning(
                request,
                "This administrator request is no longer pending."
            )

            return redirect("admin_request")

        profile.approval_status = Profile.ApprovalStatus.REJECTED
        profile.save(update_fields=["approval_status"])

    messages.warning(
        request,
        f"Administrator request from '{profile.user.username}' "
        "has been rejected."
    )

    return redirect("admin_request")


# ==========================================================
# ADMINISTRATOR DASHBOARD
# ==========================================================

@role_required("admin")
def dashboard(request):

    # Administrators should use the Admin Requests page.
    return redirect("admin_request")


# ==========================================================
# USER MANAGEMENT (MERGED INTO admin_request)
# Kept only so old links / urls.py entries don't break.
# You can delete this view and its URL once you're ready.
# ==========================================================

@main_admin_required
def user_management(request):

    return redirect("admin_request")


# ==========================================================
# EDIT USER ACCOUNT
# MAIN ADMINISTRATOR ONLY
# (submitted from the modal on admin_request.html)
# ==========================================================

@main_admin_required
@require_POST
def edit_user(request, pk):

    profile = get_object_or_404(
        Profile.objects.select_related("user"),
        pk=pk
    )

    if profile.is_main_admin or profile.user.is_superuser:

        messages.error(
            request,
            "The Main Administrator account cannot be edited."
        )

        return redirect("admin_request")

    user = profile.user

    username = request.POST.get("username", "").strip()
    first_name = request.POST.get("first_name", "").strip()
    last_name = request.POST.get("last_name", "").strip()
    email = request.POST.get("email", "").strip()
    phone = request.POST.get("phone", "").strip()

    errors = []

    if not username:

        errors.append("Username is required.")

    elif User.objects.filter(
        username__iexact=username
    ).exclude(pk=user.pk).exists():

        errors.append("That username is already taken.")

    if not email:

        errors.append("Email address is required.")

    elif User.objects.filter(
        email__iexact=email
    ).exclude(pk=user.pk).exists():

        errors.append("That email address is already being used.")

    if errors:

        for error in errors:
            messages.error(request, error)

        return redirect("admin_request")

    with transaction.atomic():

        user.username = username
        user.first_name = first_name
        user.last_name = last_name
        user.email = email

        user.save(
            update_fields=[
                "username",
                "first_name",
                "last_name",
                "email",
            ]
        )

        profile.phone = phone
        profile.save(update_fields=["phone"])

    messages.success(
        request,
        f"Account '{username}' has been updated successfully."
    )

    return redirect("admin_request")


# ==========================================================
# CHANGE USER PASSWORD
# MAIN ADMINISTRATOR ONLY
# (submitted from the modal on admin_request.html)
# ==========================================================

@main_admin_required
@require_POST
def change_user_password(request, pk):

    profile = get_object_or_404(
        Profile.objects.select_related("user"),
        pk=pk
    )

    if profile.is_main_admin or profile.user.is_superuser:

        messages.error(
            request,
            "The Main Administrator password cannot be changed here."
        )

        return redirect("admin_request")

    form = SetPasswordForm(
        profile.user,
        request.POST
    )

    if form.is_valid():

        form.save()

        messages.success(
            request,
            f"The password for '{profile.user.username}' "
            "has been changed successfully."
        )

    else:

        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)

    return redirect("admin_request")


# ==========================================================
# DELETE USER ACCOUNT
# MAIN ADMINISTRATOR ONLY
# ==========================================================

@main_admin_required
@require_POST
def delete_user(request, pk):

    profile = get_object_or_404(
        Profile.objects.select_related("user"),
        pk=pk
    )

    user = profile.user

    if profile.is_main_admin or user.is_superuser:

        messages.error(
            request,
            "The Main Administrator account cannot be deleted."
        )

        return redirect("admin_request")

    if user.pk == request.user.pk:

        messages.error(
            request,
            "You cannot delete your own account while logged in."
        )

        return redirect("admin_request")

    username = user.username

    delete_account_data(user)

    messages.success(
        request,
        f"Account '{username}' and all of its vehicles and "
        "allocation history have been deleted."
    )

    return redirect("admin_request")


def delete_account_data(user):
    """
    Deletes a user together with their vehicles, allocations and
    reservations, and frees any slot they were holding (a slot reserved by
    a deleted reservation would otherwise stay "reserved").

    Used by delete_user() (main admin) and by the driver's own
    "Delete account" button on the Settings page.
    """

    with transaction.atomic():

        vehicles = Vehicle.objects.filter(user=user)

        # Slots this driver is holding (parked or reserved) must be freed.
        slot_ids = set(
            Allocation.objects.filter(
                vehicle__in=vehicles,
                exit_time__isnull=True,
            ).values_list("slot_id", flat=True)
        ) | set(
            Reservation.objects.filter(
                vehicle__in=vehicles,
                status=Reservation.Status.CONFIRMED,
            ).values_list("slot_id", flat=True)
        )

        # Their payments are removed automatically (CASCADE).
        Allocation.objects.filter(vehicle__in=vehicles).delete()

        # Reservations of these vehicles, then the vehicles themselves.
        Reservation.objects.filter(vehicle__in=vehicles).delete()
        vehicles.delete()

        # Free the slots, unless something else still uses them.
        now = timezone.now()

        for slot in ParkingSlot.objects.select_for_update().filter(
            pk__in=slot_ids,
            status__in=[
                ParkingSlot.Status.OCCUPIED,
                ParkingSlot.Status.RESERVED,
            ],
        ):
            still_parked = Allocation.objects.filter(
                slot=slot,
                exit_time__isnull=True,
            ).exists()

            still_reserved = Reservation.objects.filter(
                slot=slot,
                status=Reservation.Status.CONFIRMED,
                end_time__gt=now,
            ).exists()

            if not still_parked and not still_reserved:
                slot.status = ParkingSlot.Status.AVAILABLE
                slot.save(update_fields=["status"])

        user.delete()


# ==========================================================
# PARKING SLOT MANAGEMENT
# ==========================================================

@login_required
def parking_slot(request):

    # Free slots whose reservations have ended.
    auto_complete_expired_reservations()

    is_admin = is_approved_admin(request.user)

    # Block unapproved administrator accounts.
    if (
        get_role(request.user) == Profile.Role.ADMIN
        and not is_admin
    ):

        messages.error(
            request,
            "Your administrator account is not approved."
        )

        return redirect("login")

    # ADD NEW PARKING SLOT
    if request.method == "POST":

        if not is_admin:

            messages.error(
                request,
                "Only administrators can add parking slots."
            )

            return redirect("parking_slot")

        zone = Zone.objects.filter(
            pk=request.POST.get("zone")
        ).first()

        slot_number = request.POST.get(
            "slot_number", ""
        ).strip().upper()

        slot_type = request.POST.get(
            "slot_type",
            ParkingSlot.SlotType.CAR
        )

        try:

            distance_rank = int(
                request.POST.get("distance_rank") or 0
            )

        except (ValueError, TypeError):

            distance_rank = 0

        if zone is None or not slot_number:

            messages.error(
                request,
                "Please choose a zone and enter a slot number."
            )

        elif slot_type not in ParkingSlot.SlotType.values:

            messages.error(
                request,
                "Invalid parking slot type."
            )

        elif ParkingSlot.objects.filter(
            zone=zone,
            slot_number=slot_number
        ).exists():

            messages.error(
                request,
                f"Slot {slot_number} already exists in {zone.name}."
            )

        else:

            ParkingSlot.objects.create(
                zone=zone,
                slot_number=slot_number,
                slot_type=slot_type,
                distance_rank=max(distance_rank, 0)
            )

            messages.success(
                request,
                f"Slot {slot_number} added to {zone.name}."
            )

        return redirect("parking_slot")

    # SEARCH AND FILTER
    q = request.GET.get("q", "").strip()
    zone_id = request.GET.get("zone")
    status = request.GET.get("status")
    slot_type = request.GET.get("slot_type")

    slots = (
        ParkingSlot.objects
        .select_related("zone")
        .prefetch_related(
            Prefetch(
                "allocations",
                queryset=Allocation.objects.filter(
                    exit_time__isnull=True
                ).select_related("vehicle"),
                to_attr="active_allocs"
            )
        )
    )

    if q:

        slots = slots.filter(
            Q(slot_number__icontains=q)
            |
            Q(
                allocations__vehicle__plate_number__icontains=q,
                allocations__exit_time__isnull=True
            )
        ).distinct()

    if zone_id:
        slots = slots.filter(zone_id=zone_id)

    if status:
        slots = slots.filter(status=status)

    if slot_type:
        slots = slots.filter(slot_type=slot_type)

    # PARKING SLOT STATISTICS
    counts = ParkingSlot.objects

    context = {

        "is_admin": is_admin,

        "slots": slots,

        "zones_admin": Zone.objects.annotate(
            slot_total=Count("slots", distinct=True),
            slot_free=Count(
                "slots",
                filter=Q(slots__status=ParkingSlot.Status.AVAILABLE),
                distinct=True,
            ),
        ),

        "all_zones": Zone.objects.filter(
            is_active=True
        ),

        "status_choices": ParkingSlot.Status.choices,

        "type_choices": ParkingSlot.SlotType.choices,

        "total_slots": counts.count(),

        "available_count": counts.filter(
            status=ParkingSlot.Status.AVAILABLE
        ).count(),

        "occupied_count": counts.filter(
            status=ParkingSlot.Status.OCCUPIED
        ).count(),

        "reserved_count": counts.filter(
            status=ParkingSlot.Status.RESERVED
        ).count(),

        "maintenance_count": counts.filter(
            status=ParkingSlot.Status.MAINTENANCE
        ).count(),
    }

    if is_admin:
        sessions, sessions_due = build_active_sessions()
        context["active_sessions"] = sessions
        context["sessions_total_due"] = sessions_due

    return render(
        request,
        "parking_slot.html",
        context
    )


# ==========================================================
# CHANGE PARKING SLOT STATUS
# ==========================================================

@role_required("admin")
@require_POST
def slot_status(request, pk):

    slot = get_object_or_404(
        ParkingSlot,
        pk=pk
    )

    new_status = request.POST.get("status")

    if new_status not in (
        ParkingSlot.Status.AVAILABLE,
        ParkingSlot.Status.MAINTENANCE
    ):

        messages.error(
            request,
            "Invalid parking slot status."
        )

    elif slot.status in (
        ParkingSlot.Status.OCCUPIED,
        ParkingSlot.Status.RESERVED
    ):

        messages.error(
            request,
            f"Slot {slot.slot_number} is currently in use "
            "and cannot be changed."
        )

    else:

        slot.status = new_status

        slot.save(
            update_fields=["status"]
        )

        messages.success(
            request,
            f"Slot {slot.slot_number} is now "
            f"{slot.get_status_display().lower()}."
        )

    return redirect("parking_slot")


# ==========================================================
# ADD ZONE
# ==========================================================

@role_required("admin")
@require_POST
def add_zone(request):

    name = request.POST.get("name", "").strip()[:100]
    description = request.POST.get("description", "").strip()[:255]

    try:

        distance_rank = max(
            int(request.POST.get("distance_rank") or 0), 0
        )

    except (ValueError, TypeError):

        distance_rank = 0

    if not name:

        messages.error(request, "Please enter a zone name.")

    elif Zone.objects.filter(name__iexact=name).exists():

        messages.error(
            request,
            f"A zone named '{name}' already exists."
        )

    else:

        Zone.objects.create(
            name=name,
            description=description,
            distance_rank=distance_rank,
            is_active=True,
        )

        messages.success(
            request,
            f"Zone '{name}' has been added. You can now add slots to it."
        )

    return redirect("parking_slot")


# ==========================================================
# ACTIVATE / DEACTIVATE ZONE
# ==========================================================

@role_required("admin")
@require_POST
def toggle_zone(request, pk):

    zone = get_object_or_404(Zone, pk=pk)

    zone.is_active = not zone.is_active
    zone.save(update_fields=["is_active"])

    messages.success(
        request,
        f"Zone '{zone.name}' is now "
        f"{'active' if zone.is_active else 'inactive'}."
    )

    return redirect("parking_slot")


# ==========================================================
# DELETE PARKING SLOT (only unused slots with no history)
# ==========================================================

@role_required("admin")
@require_POST
def delete_slot(request, pk):

    slot = get_object_or_404(ParkingSlot, pk=pk)

    if slot.status not in (
        ParkingSlot.Status.AVAILABLE,
        ParkingSlot.Status.MAINTENANCE,
    ):

        messages.error(
            request,
            f"Slot {slot.slot_number} is in use and cannot be deleted."
        )

        return redirect("parking_slot")

    number = slot.slot_number

    try:

        slot.delete()

    except ProtectedError:

        messages.error(
            request,
            f"Slot {number} has parking history and cannot be deleted. "
            "Set it to Maintenance instead."
        )

        return redirect("parking_slot")

    messages.success(request, f"Slot {number} has been deleted.")

    return redirect("parking_slot")


# ==========================================================
# LIVE SLOT MAP (shared by the JSON API and the dashboard)
# ==========================================================
def build_slot_map(user):
    """
    Builds the data used by the Live Slot Map.
    Slots are sorted 1-10 inside each zone, and zones A, B, C...
    """

    auto_complete_expired_reservations()

    is_admin = is_approved_admin(user)

    slots = (
        ParkingSlot.objects
        .select_related("zone")
        .prefetch_related(
            Prefetch(
                "allocations",
                queryset=Allocation.objects.filter(
                    exit_time__isnull=True
                ).select_related("vehicle"),
                to_attr="active_allocs"
            )
        )
        .filter(zone__is_active=True)
    )

    counts = {
        ParkingSlot.Status.AVAILABLE: 0,
        ParkingSlot.Status.OCCUPIED: 0,
        ParkingSlot.Status.RESERVED: 0,
        ParkingSlot.Status.MAINTENANCE: 0,
    }

    zones = {
        zone.id: {
            "id": zone.id,
            "name": zone.name,
            "available": 0,
            "slots": [],
        }
        for zone in Zone.objects.filter(is_active=True)
    }

    for slot in slots:

        counts[slot.status] = counts.get(slot.status, 0) + 1

        zone_data = zones.get(slot.zone_id)

        if zone_data is None:
            continue

        if slot.status == ParkingSlot.Status.AVAILABLE:
            zone_data["available"] += 1

        item = {
            "id": slot.id,
            "number": slot.slot_number,
            "status": slot.status,
            "status_label": slot.get_status_display(),
            "slot_type": slot.slot_type,
        }

        if slot.status == ParkingSlot.Status.OCCUPIED and slot.active_allocs:

            alloc = slot.active_allocs[0]
            vehicle = alloc.vehicle

            if is_admin or vehicle.user_id == user.id:

                type_display = getattr(
                    vehicle, "get_vehicle_type_display", None
                )

                item["plate"] = vehicle.plate_number
                item["vehicle_type"] = (
                    type_display()
                    if type_display
                    else getattr(vehicle, "vehicle_type", "")
                )
                item["since"] = timezone.localtime(
                    alloc.entry_time
                ).strftime("%b %d, %I:%M %p")

        zone_data["slots"].append(item)

    # Sort slots 1-10 inside every zone.
    for z in zones.values():
        z["slots"].sort(key=lambda s: slot_sort_key(s["number"]))

    total = sum(counts.values())
    occupied = counts[ParkingSlot.Status.OCCUPIED]

    return {
        "total": total,
        "available": counts[ParkingSlot.Status.AVAILABLE],
        "occupied": occupied,
        "reserved": counts[ParkingSlot.Status.RESERVED],
        "maintenance": counts[ParkingSlot.Status.MAINTENANCE],
        "occupancy_rate": round(occupied * 100 / total) if total else 0,
        "slot_types": list(ParkingSlot.SlotType.values),
        # Zones in A, B, C... order
        "zones": sorted(zones.values(), key=lambda z: z["name"]),
    }


@login_required
def slot_map_data(request):

    # Unapproved administrators get no data.
    if (
        get_role(request.user) == Profile.Role.ADMIN
        and not is_approved_admin(request.user)
    ):

        return JsonResponse(
            {"error": "Not approved."},
            status=403
        )

    return JsonResponse(build_slot_map(request.user))


# ==========================================================
# NOTIFICATIONS (bell icon in the top bar)
#
# One endpoint, two audiences:
#   - approved administrators get system-wide alerts and requests
#   - drivers get notifications about their own vehicles and reservations
#
# Each item has a stable "id". The browser remembers which ids the user
# has already seen, so an item becomes "new" again whenever its id changes
# (for example when a reservation goes from pending to confirmed).
# ==========================================================

def _note(nid, level, icon, title, text, when=None, url=""):
    """Build one notification dict. `when=None` means 'right now'."""

    now = timezone.now()
    when = when or now

    if when > now:
        ago = "in " + timeuntil(when, now, depth=1)
    elif (now - when).total_seconds() < 60:
        ago = "Just now"
    else:
        ago = timesince(when, now, depth=1) + " ago"

    return {
        "id": nid,
        "level": level,          # success | info | warning | danger
        "icon": icon,            # a bootstrap-icons class
        "title": title,
        "text": text,
        "ts": when.timestamp(),
        "ago": ago,
        "url": url,
    }


def _local(dt):
    return timezone.localtime(dt).strftime("%b %d, %I:%M %p")


def _driver_notifications(user):

    now = timezone.now()
    items = []

    # ---- parking activity for this driver's vehicles ----
    allocations = (
        Allocation.objects
        .filter(vehicle__user=user)
        .select_related("vehicle", "slot", "slot__zone")
        .order_by("-entry_time")[:6]
    )

    for a in allocations:

        place = f"slot {a.slot.slot_number} ({a.slot.zone.name})"

        if a.exit_time:
            items.append(_note(
                f"out-{a.pk}", "info", "bi-box-arrow-right",
                "Slot released",
                f"{a.vehicle.plate_number} left {place}.",
                a.exit_time,
                reverse("driver_home"),
            ))
        else:
            items.append(_note(
                f"in-{a.pk}", "success", "bi-car-front-fill",
                "You're parked",
                f"{a.vehicle.plate_number} is in {place}.",
                a.entry_time,
                reverse("driver_home"),
            ))

    # ---- this driver's reservations ----
    reservations = (
        Reservation.objects
        .filter(created_by=user)
        .select_related("slot", "slot__zone")
        .order_by("-created_at")[:8]
    )

    for r in reservations:

        place = f"{r.slot.slot_number} ({r.slot.zone.name})"
        when = getattr(r, "updated_at", None) or r.created_at
        url = reverse("reservation")
        nid = f"res-{r.pk}-{r.status}"

        if r.status == Reservation.Status.PENDING:
            items.append(_note(
                nid, "warning", "bi-hourglass-split",
                "Reservation pending",
                f"Slot {place} is waiting for administrator approval.",
                when, url,
            ))

        elif r.status == Reservation.Status.CONFIRMED:
            items.append(_note(
                nid, "success", "bi-calendar2-check-fill",
                "Reservation approved",
                f"Slot {place} is reserved for {_local(r.start_time)}.",
                when, url,
            ))

            # Friendly reminders for approved reservations.
            if now < r.start_time <= now + timedelta(hours=1):
                items.append(_note(
                    f"res-soon-{r.pk}", "warning", "bi-alarm-fill",
                    "Reservation starts soon",
                    f"Slot {place} starts {_local(r.start_time)}.",
                    None, url,
                ))
            elif (
                r.start_time <= now < r.end_time
                and r.end_time - now <= timedelta(minutes=30)
            ):
                items.append(_note(
                    f"res-ending-{r.pk}", "warning", "bi-alarm-fill",
                    "Reservation ending soon",
                    f"Slot {place} ends {_local(r.end_time)}.",
                    None, url,
                ))

        elif r.status == Reservation.Status.REJECTED:
            items.append(_note(
                nid, "danger", "bi-x-circle-fill",
                "Reservation rejected",
                f"Your request for slot {place} was not approved.",
                when, url,
            ))

        elif r.status == Reservation.Status.CANCELLED:
            items.append(_note(
                nid, "info", "bi-slash-circle",
                "Reservation cancelled",
                f"Your reservation for slot {place} was cancelled.",
                when, url,
            ))

        elif r.status == Reservation.Status.COMPLETED:
            items.append(_note(
                nid, "info", "bi-check2-circle",
                "Reservation completed",
                f"Your reservation for slot {place} has ended.",
                when, url,
            ))

    return items


def _admin_notifications():

    items = []

    # ---- reservation requests waiting for approval ----
    pending_reservations = (
        Reservation.objects
        .filter(
            status=Reservation.Status.PENDING,
            end_time__gt=timezone.now(),
        )
        .select_related("vehicle", "slot", "slot__zone")
        .order_by("-created_at")[:5]
    )

    for r in pending_reservations:
        items.append(_note(
            f"res-pending-{r.pk}", "warning", "bi-bookmark-star-fill",
            "Reservation request",
            f"{r.vehicle.plate_number} requested slot {r.slot.slot_number} "
            f"({r.slot.zone.name}) for {_local(r.start_time)}.",
            r.created_at,
            reverse("reservation"),
        ))

    # ---- administrator accounts waiting for approval ----
    pending_admins = Profile.objects.filter(
        role=Profile.Role.ADMIN,
        approval_status=Profile.ApprovalStatus.PENDING,
    ).count()

    if pending_admins:
        items.append(_note(
            f"admin-req-{pending_admins}", "warning", "bi-person-gear",
            "Administrator approval needed",
            f"{pending_admins} administrator "
            f"request{'s' if pending_admins != 1 else ''} waiting for review.",
            None,
            reverse("admin_request"),
        ))

    # ---- lot status alerts ----
    total = ParkingSlot.objects.count()
    occupied = ParkingSlot.objects.filter(
        status=ParkingSlot.Status.OCCUPIED
    ).count()
    maintenance = ParkingSlot.objects.filter(
        status=ParkingSlot.Status.MAINTENANCE
    ).count()

    # The alert level is set by the administrator in Settings > Alerts.
    cfg = SystemSettings.load()

    if (
        cfg.al_occupancy
        and total
        and occupied * 100 >= total * cfg.al_occupancy_pct
    ):
        items.append(_note(
            f"occ-high-{timezone.localdate()}", "danger",
            "bi-exclamation-triangle-fill",
            "Lot almost full",
            f"{occupied} of {total} slots are occupied.",
            None,
            reverse("parking_slot"),
        ))

    if maintenance:
        items.append(_note(
            f"maint-{maintenance}", "info", "bi-cone-striped",
            "Slots under maintenance",
            f"{maintenance} slot{'s are' if maintenance != 1 else ' is'} "
            "currently unavailable.",
            None,
            reverse("parking_slot"),
        ))

    # ---- recent parking activity (all vehicles) ----
    allocations = (
        Allocation.objects
        .select_related("vehicle", "slot", "slot__zone")
        .order_by("-entry_time")[:5]
    )

    for a in allocations:

        place = f"slot {a.slot.slot_number} ({a.slot.zone.name})"

        if a.exit_time:
            items.append(_note(
                f"adm-out-{a.pk}", "info", "bi-box-arrow-right",
                "Vehicle left",
                f"{a.vehicle.plate_number} left {place}.",
                a.exit_time,
                reverse("parking_slot"),
            ))
        else:
            items.append(_note(
                f"adm-in-{a.pk}", "success", "bi-car-front-fill",
                "Vehicle parked",
                f"{a.vehicle.plate_number} parked in {place}.",
                a.entry_time,
                reverse("parking_slot"),
            ))

    return items


@login_required
def notifications_data(request):

    # Unapproved administrators get nothing.
    if (
        get_role(request.user) == Profile.Role.ADMIN
        and not is_approved_admin(request.user)
    ):

        return JsonResponse(
            {"error": "Not approved."},
            status=403
        )

    # Make sure reservation statuses are current before reporting them.
    auto_complete_expired_reservations()

    if is_approved_admin(request.user):
        role = "admin"
        items = _admin_notifications()
    else:
        role = "driver"
        items = _driver_notifications(request.user)

    items.sort(key=lambda i: i["ts"], reverse=True)

    return JsonResponse({
        "role": role,
        "items": items[:12],
    })


# ==========================================================
# QUICK ALLOCATE
# ==========================================================

def required_slot_type(vehicle_type):
    """
    The slot type a vehicle must be parked in.

    If your SlotType has a value matching the vehicle type (car, motorcycle,
    van, pwd ...), that type is used. Any vehicle type without its own slot
    type falls back to regular CAR slots.
    """

    if vehicle_type in ParkingSlot.SlotType.values:
        return vehicle_type

    return ParkingSlot.SlotType.CAR


@role_required("driver")
@require_POST
def allocate_slot(request):

    plate = " ".join(
        request.POST.get("plate_number", "").upper().split()
    )

    vehicle_type = request.POST.get("vehicle_type", "car")
    zone_id = request.POST.get("zone") or None
    slot_id = request.POST.get("slot_id") or None

    # Basic validation
    if not plate:
        messages.error(
            request,
            "Please enter a plate number."
        )
        return redirect("driver_home")

    allowed_types = [
        value
        for value, _label in Vehicle._meta.get_field(
            "vehicle_type"
        ).flatchoices
    ]

    if allowed_types and vehicle_type not in allowed_types:
        messages.error(
            request,
            "Invalid vehicle type."
        )
        return redirect("driver_home")

    try:
        zone_id = int(zone_id) if zone_id else None
        slot_id = int(slot_id) if slot_id else None

    except (ValueError, TypeError):
        messages.error(
            request,
            "Invalid zone or slot selection."
        )
        return redirect("driver_home")

    with transaction.atomic():

        # -------------------------------------------------
        # SYSTEM-WIDE LOCK
        # Prevent simultaneous allocation requests.
        # -------------------------------------------------
        list(
            Zone.objects.select_for_update().order_by("pk")
        )

        # -------------------------------------------------
        # ONLY ONE ACTIVE PARKING SESSION ALLOWED
        # -------------------------------------------------
        active_allocation = (
            Allocation.objects
            .filter(exit_time__isnull=True)
            .select_related(
                "vehicle",
                "slot",
                "slot__zone"
            )
            .first()
        )

        if active_allocation:

            messages.error(
                request,
                f"Parking is currently occupied by "
                f"{active_allocation.vehicle.plate_number} "
                f"at Slot {active_allocation.slot.slot_number} "
                f"({active_allocation.slot.zone.name}). "
                "Only one vehicle can park at a time. "
                "Please wait until the current vehicle exits."
            )

            return redirect("driver_home")

        # -------------------------------------------------
        # CHECK VEHICLE OWNERSHIP
        # -------------------------------------------------
        vehicle = (
            Vehicle.objects
            .select_for_update()
            .filter(plate_number__iexact=plate)
            .first()
        )

        if vehicle is not None:

            if vehicle.user_id != request.user.id:

                messages.error(
                    request,
                    f"Plate {plate} is registered to another account."
                )

                return redirect("driver_home")

            # Prevent duplicate active allocation for this vehicle.
            if Allocation.objects.filter(
                vehicle=vehicle,
                exit_time__isnull=True
            ).exists():

                messages.warning(
                    request,
                    f"Vehicle {plate} is already parked. "
                    "Release its slot before allocating a new one."
                )

                return redirect("driver_home")
            
            if vehicle_type != vehicle.vehicle_type:
                messages.warning(
                    request,
                    f"{plate} is registered as "
                    f"{vehicle.get_vehicle_type_display()}, so a "
                    f"{vehicle.get_vehicle_type_display()} slot was used."
                )

            vehicle_type = vehicle.vehicle_type

        # -------------------------------------------------
        # FIND AVAILABLE PARKING SLOT
        # -------------------------------------------------
        candidates = (
            ParkingSlot.objects
            .select_for_update()
            .filter(
                status=ParkingSlot.Status.AVAILABLE,
                slot_type=required_slot_type(vehicle_type),
                zone__in=Zone.objects.filter(is_active=True),
            )
        )

        if slot_id:

            # Manually selected slot from Live Slot Map.
            slot = candidates.filter(pk=slot_id).first()

            if slot is None:

                messages.error(
                    request,
                    "That slot is no longer available, or it does "
                    "not fit this vehicle type. Please pick another one."
                )

                return redirect("driver_home")

        else:

            # Automatic allocation: nearest available slot.
            if zone_id:
                candidates = candidates.filter(zone_id=zone_id)

            slot = candidates.order_by(
                "zone__distance_rank",
                "distance_rank",
                "slot_number"
            ).first()

            if slot is None:

                messages.error(
                    request,
                    "No available slot for this vehicle type"
                    + (" in the selected zone." if zone_id else ".")
                )

                return redirect("driver_home")

        # -------------------------------------------------
        # REGISTER VEHICLE IF IT DOES NOT EXIST
        # -------------------------------------------------
        if vehicle is None:

            profile = getattr(request.user, "profile", None)

            vehicle = Vehicle.objects.create(
                user=request.user,
                plate_number=plate,
                vehicle_type=vehicle_type,
                owner_name=(
                    request.user.get_full_name()
                    or request.user.username
                ),
                contact_number=getattr(
                    profile, "phone", ""
                ) or "",
            )

        # -------------------------------------------------
        # CREATE PARKING ALLOCATION
        # -------------------------------------------------
        Allocation.objects.create(
            vehicle=vehicle,
            slot=slot,
            allocated_by=request.user,
            entry_time=timezone.now(),
        )

        # Mark slot as occupied.
        slot.status = ParkingSlot.Status.OCCUPIED
        slot.save(update_fields=["status"])

    # -------------------------------------------------
    # SUCCESS MESSAGE
    # -------------------------------------------------
    messages.success(
        request,
        f"Slot {slot.slot_number} ({slot.zone.name}) "
        f"has been allocated to {plate}."
    )

    return redirect("driver_home")


# ==========================================================
# PAYMENTS
#
# FEE RULES  (set by the administrator in Settings > Pricing)
#   - the first "free minutes" (default 60) are free
#   - after that, every started hour costs the hourly rate
#   - a driver can pay more than once; only what is still unpaid is due
#
# The first time Settings is used, it starts from these settings.py values:
#   PARKING_HOURLY_RATE = 20
#   PARKING_FREE_MINUTES = 60
# ==========================================================

def get_free_minutes():
    return int(SystemSettings.load().free_minutes)


def get_hourly_rate():
    return Decimal(str(SystemSettings.load().hourly_rate))


def calculate_fee(allocation, until=None):
    """Total fee for a parking session so far (or until `until`)."""

    end = until or allocation.exit_time or timezone.now()

    elapsed_seconds = max(
        (end - allocation.entry_time).total_seconds(), 0
    )

    billable_minutes = max(
        math.ceil(elapsed_seconds / 60) - get_free_minutes(), 0
    )

    hours = math.ceil(billable_minutes / 60)

    return {
        "billable_minutes": billable_minutes,
        "hours": hours,
        "amount": get_hourly_rate() * hours,
    }


def paid_total(allocation):
    """Sum of CONFIRMED payments for this session."""

    total = allocation.payments.filter(
        is_paid=True
    ).aggregate(t=Sum("amount"))["t"]

    return total or Decimal("0.00")


def amount_due(allocation, until=None):
    """What the driver still owes (never negative)."""

    due = calculate_fee(allocation, until)["amount"] - paid_total(allocation)

    return max(due, Decimal("0.00"))


# ----------------------------------------------------------
# Admin payment statistics (revenue cards, 7-day chart, method split,
# and the full list of payments waiting for confirmation).
# Revenue only counts CONFIRMED payments (is_paid=True) and is dated by
# the moment the admin confirmed it (paid_at).
# ----------------------------------------------------------

def day_bounds(day):
    """Start (inclusive) and end (exclusive) of a local day, timezone-aware."""
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1)


def admin_payment_stats():

    today = timezone.localdate()
    today_start, today_end = day_bounds(today)
    month_start, _ = day_bounds(today.replace(day=1))

    confirmed = Payment.objects.filter(is_paid=True)
    pending = Payment.objects.filter(is_paid=False)

    def total(qs):
        return qs.aggregate(t=Sum("amount"))["t"] or Decimal("0.00")

    # ---- last 7 days (oldest -> today) ----
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]

    per_day = defaultdict(Decimal)

    rows = confirmed.filter(
        paid_at__gte=day_bounds(days[0])[0]
    ).values_list("paid_at", "amount")

    for paid_at, amount in rows:
        per_day[timezone.localtime(paid_at).date()] += amount

    amounts = [per_day.get(d) or Decimal("0.00") for d in days]
    highest = max(amounts)

    revenue_chart = [
        {
            "label": d.strftime("%a"),
            "amount": amt,
            "pct": round(max(float(amt / highest * 100), 3), 1) if amt else 0,
        }
        for d, amt in zip(days, amounts)
    ]

    # ---- payment method split (this month) ----
    month_paid = confirmed.filter(paid_at__gte=month_start)

    gcash_total = total(month_paid.filter(method=Payment.Method.GCASH))
    cash_total = total(month_paid.filter(method=Payment.Method.CASH))
    split = gcash_total + cash_total

    return {
        "rev_today": total(
            confirmed.filter(paid_at__gte=today_start, paid_at__lt=today_end)
        ),
        "rev_month": total(month_paid),
        "pending_amount": total(pending),
        "pending_count": pending.count(),
        "payments_today": Payment.objects.filter(
            created_at__gte=today_start,
            created_at__lt=today_end,
        ).count(),

        "revenue_chart": revenue_chart if highest else [],
        "gcash_total": gcash_total,
        "cash_total": cash_total,
        "gcash_pct": round(float(gcash_total / split * 100), 1) if split else 0,
        "cash_pct": round(float(cash_total / split * 100), 1) if split else 0,

        # Every unconfirmed payment, not just the latest 200 rows.
        "pending_payments": (
            pending
            .select_related(
                "allocation__vehicle__user",
                "allocation__slot__zone",
            )
            .order_by("created_at")
        ),
    }


# ----------------------------------------------------------
# Payments page
# Driver: current session + own history.  Admin: all payments.
# ----------------------------------------------------------

@login_required
def payments(request):

    is_admin = is_approved_admin(request.user)

    if get_role(request.user) == Profile.Role.ADMIN and not is_admin:

        messages.error(
            request,
            "Your administrator account is not approved."
        )

        return redirect("login")

    history = Payment.objects.select_related(
        "allocation__vehicle__user",
        "allocation__slot__zone",
    )

    my_current = None
    already_paid = Decimal("0.00")
    pending_payment = None

    if not is_admin:

        history = history.filter(allocation__vehicle__user=request.user)

        my_current = (
            Allocation.objects
            .filter(vehicle__user=request.user, exit_time__isnull=True)
            .select_related("vehicle", "slot__zone")
            .order_by("-entry_time")
            .first()
        )

        if my_current:
            already_paid = paid_total(my_current)
            pending_payment = my_current.payments.filter(
                is_paid=False
            ).first()

    history = history.order_by("-created_at")

    context = {
        "is_admin": is_admin,
        "my_current": my_current,
        "already_paid": already_paid,
        "pending_payment": pending_payment,
        "hourly_rate": get_hourly_rate(),
        "free_minutes": get_free_minutes(),
        # Admins get more rows because the table has search, filters
        # and pagination in the browser.
        "payments": history[:200] if is_admin else history[:50],
    }

    if is_admin:
        context.update(admin_payment_stats())

    return render(request, "payment.html", context)


# ----------------------------------------------------------
# Make a payment (driver, own vehicle, open session only)
# The amount is ALWAYS recalculated here. The "amount" posted by
# the page is ignored, so it cannot be tampered with.
# ----------------------------------------------------------
@login_required
@require_POST
def make_payment(request, pk):

    if get_role(request.user) != Profile.Role.DRIVER:
        messages.error(
            request,
            "Only drivers can make payments."
        )
        return redirect(home_for(request.user))

    with transaction.atomic():

        allocation = get_object_or_404(
            Allocation.objects.select_for_update().select_related(
                "vehicle",
                "slot"
            ),
            pk=pk
        )

        # Verify vehicle ownership.
        if allocation.vehicle.user_id != request.user.id:
            messages.error(
                request,
                "You are not allowed to pay for this parking session."
            )
            return redirect("payments")

        # Session must still be active.
        if allocation.exit_time is not None:
            messages.warning(
                request,
                "This parking session has already ended."
            )
            return redirect("payments")

        # Accept only GCash and Cash Counter.
        method = request.POST.get("method", "").strip()

        allowed_methods = (
            Payment.Method.GCASH,
            Payment.Method.CASH,
        )

        if method not in allowed_methods:
            messages.error(
                request,
                "Please select GCash or Cash Counter."
            )
            return redirect("payments")

        # Check payment method settings.
        cfg = SystemSettings.load()

        if (
            (method == Payment.Method.GCASH and not cfg.pay_gcash)
            or (method == Payment.Method.CASH and not cfg.pay_cash)
        ):
            messages.error(
                request,
                "That payment method is currently disabled."
            )
            return redirect("payments")

        # Prevent duplicate pending payments.
        if allocation.payments.filter(is_paid=False).exists():
            messages.warning(
                request,
                "You already have a payment waiting for confirmation."
            )
            return redirect("payments")

        # Calculate payment on the server.
        due = amount_due(allocation)

        if due <= 0:
            messages.info(
                request,
                "You have nothing to pay right now."
            )
            return redirect("payments")

        # Submit payment as pending.
        # GCash QR payment does not require a reference number.
        Payment.objects.create(
            allocation=allocation,
            amount=due,
            method=method,
            is_paid=False,
            reference="",
            paid_by=request.user,
            paid_at=None,
        )

    messages.success(
        request,
        f"Your payment of ₱{due:.2f} has been submitted. "
        "Please wait for administrator confirmation."
    )

    return redirect("payments")

# ----------------------------------------------------------
# Confirm / reject a pending payment (approved admins only)
# e.g. after the driver hands cash to the attendant.
# Rejecting removes the pending record so the driver can try again.
# ----------------------------------------------------------

@role_required("admin")
@require_POST
def payment_confirm(request, pk):

    released = False

    with transaction.atomic():

        payment = get_object_or_404(
            Payment.objects.select_for_update().select_related(
                "allocation__vehicle",
                "allocation__slot",
            ),
            pk=pk
        )

        if payment.is_paid:
            messages.warning(
                request,
                "This payment has already been confirmed."
            )
            return redirect("payments")

        payment.is_paid = True
        payment.confirmed_by = request.user
        payment.paid_at = timezone.now()
        payment.save(
            update_fields=["is_paid", "confirmed_by", "paid_at"]
        )

        # ---- Auto-release the vehicle once everything is paid ----
        allocation = Allocation.objects.select_for_update().get(
            pk=payment.allocation_id
        )

        if allocation.exit_time is None:

            # Judge the fee at the moment the driver submitted the payment,
            # so waiting for the attendant never adds extra charges.
            still_due = amount_due(allocation, payment.created_at)

            if still_due <= 0:

                allocation.exit_time = timezone.now()
                allocation.save(update_fields=["exit_time"])

                slot = ParkingSlot.objects.select_for_update().get(
                    pk=allocation.slot_id
                )

                if slot.status == ParkingSlot.Status.OCCUPIED:
                    slot.status = ParkingSlot.Status.AVAILABLE
                    slot.save(update_fields=["status"])

                released = True

    if released:
        messages.success(
            request,
            f"Payment #{payment.pk} of ₱{payment.amount:.2f} confirmed. "
            f"{allocation.vehicle.plate_number} has been released and "
            f"slot {allocation.slot.slot_number} is available again."
        )
    else:
        messages.success(
            request,
            f"Payment #{payment.pk} of ₱{payment.amount:.2f} confirmed."
        )

    return redirect("payments")


@role_required("admin")
@require_POST
def payment_reject(request, pk):

    with transaction.atomic():

        payment = get_object_or_404(
            Payment.objects.select_for_update(),
            pk=pk
        )

        if payment.is_paid:

            messages.warning(
                request,
                "A confirmed payment cannot be rejected."
            )

            return redirect("payments")

        payment_number = payment.pk

        payment.delete()

    messages.warning(
        request,
        f"Payment #{payment_number} has been rejected. "
        "The driver can submit another payment."
    )

    return redirect("payments")


# ==========================================================
# RELEASE A SLOT (vehicle leaves)
# The owner of the vehicle or an approved administrator.
#
# Payment rules:
#   - a driver cannot leave while a payment is still due
#   - an administrator can still release it (with a warning), e.g. when
#     the driver paid cash at the counter
#   - a session that was free all the way gets a "free" record in history
# ==========================================================

def safe_next(request, default):
    """Return the 'next' URL from the form if it is a safe local URL."""

    target = request.POST.get("next", "")

    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target

    return default


@login_required
@require_POST
def release_slot(request, pk):

    if (
        get_role(request.user) == Profile.Role.ADMIN
        and not is_approved_admin(request.user)
    ):

        messages.error(
            request,
            "Your administrator account is not approved."
        )

        return redirect("login")

    with transaction.atomic():

        allocation = get_object_or_404(
            Allocation.objects.select_for_update(),
            pk=pk
        )

        vehicle = allocation.vehicle

        if not (
            is_approved_admin(request.user)
            or vehicle.user_id == request.user.id
        ):

            messages.error(
                request,
                "You are not allowed to release this slot."
            )

            return redirect(safe_next(request, home_for(request.user)))

        if allocation.exit_time is not None:

            messages.warning(
                request,
                "This vehicle has already left."
            )

            return redirect(safe_next(request, home_for(request.user)))

        now = timezone.now()
        due = amount_due(allocation, now)
        unpaid_warning = None

        if due > 0:

            if is_approved_admin(request.user):

                unpaid_warning = (
                    f"\u20b1{due:.2f} was still unpaid for {vehicle.plate_number}."
                )

            else:

                if allocation.payments.filter(is_paid=False).exists():

                    messages.warning(
                        request,
                        "Your payment is still waiting for confirmation. "
                        "You can leave once it is confirmed."
                    )

                else:

                    messages.error(
                        request,
                        f"Please pay \u20b1{due:.2f} before leaving. "
                        "Your free parking time is over, so the extra "
                        "time must be paid."
                    )

                return redirect("payments")

        allocation.exit_time = now
        allocation.save(update_fields=["exit_time"])

        # Nothing was ever charged: keep a "free" record for the history.
        if (
            not allocation.payments.exists()
            and calculate_fee(allocation)["amount"] == 0
        ):

            Payment.objects.create(
                allocation=allocation,
                amount=Decimal("0.00"),
                method=Payment.Method.FREE,
                is_paid=True,
                paid_by=allocation.vehicle.user,
                paid_at=now,
            )

        slot = allocation.slot

        # Only free the slot if it is still marked occupied.
        if slot.status == ParkingSlot.Status.OCCUPIED:

            slot.status = ParkingSlot.Status.AVAILABLE
            slot.save(update_fields=["status"])

    messages.success(
        request,
        f"{vehicle.plate_number} has left. "
        f"Slot {slot.slot_number} is available again."
    )

    if unpaid_warning:
        messages.warning(request, unpaid_warning)

    return redirect(safe_next(request, home_for(request.user)))


# ==========================================================
# DRIVER DASHBOARD
# ==========================================================

@role_required("driver")
def driver_home(request):

    mine = Allocation.objects.filter(
        vehicle__user=request.user
    ).select_related(
        "vehicle",
        "slot__zone"
    )

    # build_slot_map() also auto-completes expired reservations.
    slot_map = build_slot_map(request.user)

    # The driver's saved (non-archived) vehicles.
    vehicles_qs = request.user.vehicles.filter(is_archived=False)

    # The parking session that is still open (the "My Current Parking" card).
    my_current = (
        mine.filter(exit_time__isnull=True)
        .order_by("-entry_time")
        .first()
    )

    # Choices for the Vehicle Type dropdown: [(value, label), ...]
    vehicle_types = list(
        Vehicle._meta.get_field("vehicle_type").flatchoices
    )

    # PWD slots/type are only offered to drivers who have a PWD vehicle
    # (or a profile flag named is_pwd, if you have one).
    profile = getattr(request.user, "profile", None)
    can_use_pwd = (
        bool(getattr(profile, "is_pwd", False))
        or vehicles_qs.filter(vehicle_type="pwd").exists()
    )

    # Zones and their slots, for the first paint.
    # (Zone already has its own available_count property, so no annotation.)
    zones = list(
        Zone.objects
        .filter(is_active=True)
        .prefetch_related("slots")
        .order_by("name")
    )

    for zone in zones:
        zone.sorted_slots = sorted(
            zone.slots.all(),
            key=lambda s: slot_sort_key(s.slot_number),
        )

    context = {

        "is_admin": False,

        "vehicles": vehicles_qs,
        "vehicle_types": vehicle_types,
        "can_use_pwd": can_use_pwd,

        # My Current Parking card
        "my_current": my_current,
        "hourly_rate": get_hourly_rate(),
        "free_minutes": get_free_minutes(),

        # Driver stat cards
        "my_vehicles_count": vehicles_qs.count(),
        "my_reservations_count": Reservation.objects.filter(
            created_by=request.user,
            status__in=[
                Reservation.Status.PENDING,
                Reservation.Status.CONFIRMED,
            ],
        ).count(),
        "my_sessions_count": mine.count(),

        "recent_allocations": mine.order_by("-entry_time")[:5],

        # Live Slot Map / stat cards (first paint; JS keeps them fresh)
        "zones": zones,
        "available_count": slot_map["available"],
        "occupied_count": slot_map["occupied"],
        "reserved_count": slot_map["reserved"],
        "total_slots": slot_map["total"],
        "occupancy_rate": slot_map["occupancy_rate"],
    }

    return render(
        request,
        "dashboard.html",
        context
    )


# ==========================================================
# ALLOCATION HISTORY (ADMINISTRATORS) / MY HISTORY (DRIVERS)
#
# Both pages share the same template (allocation_history.html) and the
# same filtering / pagination code (_history_response):
#
#   allocation_history -> approved administrators only, every allocation
#   my_history         -> drivers only, only their own vehicles
#
# Supports search, status / zone / date filters and pagination.
# ==========================================================

HISTORY_PAGE_SIZE = 10


def _format_duration(delta):
    """timedelta -> '2d 3h', '1h 20m' or '45m'. None -> '-'."""

    if not delta:
        return "-"

    total_minutes = int(delta.total_seconds() // 60)

    days, rest = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(rest, 60)

    if days:
        return f"{days}d {hours}h"

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


def _history_response(request, my_only):
    """
    Builds and renders the history page.

    my_only=False -> every allocation (administrator view)
    my_only=True  -> only allocations of the logged-in driver's vehicles
    """

    is_admin = not my_only

    base = (
        Allocation.objects
        .select_related("vehicle", "vehicle__user", "slot", "slot__zone")
        .order_by("-entry_time")
    )

    # Allocations whose driver account was deleted (vehicle has no owner)
    # are never shown, so no "None None" driver appears.
    base = base.filter(vehicle__user__isnull=False)

    # Drivers only see allocations for their own vehicles.
    if my_only:
        base = base.filter(vehicle__user=request.user)

    # ---------------- FILTERS ----------------
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    zone_id = request.GET.get("zone", "").strip()
    date_from = parse_date(request.GET.get("date_from", "").strip() or "")
    date_to = parse_date(request.GET.get("date_to", "").strip() or "")

    # Administrators can also narrow the list to one driver account.
    user_filter = request.GET.get("user", "").strip()

    allocations = base

    if q:

        search = (
            Q(vehicle__plate_number__icontains=q)
            | Q(slot__slot_number__icontains=q)
        )

        # Administrators can also search by driver name or username.
        if is_admin:
            search |= (
                Q(vehicle__user__username__icontains=q)
                | Q(vehicle__user__first_name__icontains=q)
                | Q(vehicle__user__last_name__icontains=q)
            )

        allocations = allocations.filter(search)

    if is_admin and user_filter.isdigit():
        allocations = allocations.filter(
            vehicle__user_id=int(user_filter)
        )

    if status == "parked":
        allocations = allocations.filter(exit_time__isnull=True)

    elif status == "completed":
        allocations = allocations.filter(exit_time__isnull=False)

    if zone_id.isdigit():
        allocations = allocations.filter(slot__zone_id=int(zone_id))

    if date_from:
        allocations = allocations.filter(entry_time__gte=day_bounds(date_from)[0])

    if date_to: 
        allocations = allocations.filter(entry_time__lt=day_bounds(date_to)[1])

    # ---------------- STAT CARDS ----------------
    # Based on everything the user is allowed to see (not the filters),
    # so the cards always show overall totals.
    totals = base.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(exit_time__isnull=True)),
        completed=Count("pk", filter=Q(exit_time__isnull=False)),
        avg=Avg(
            ExpressionWrapper(
                F("exit_time") - F("entry_time"),
                output_field=DurationField(),
            ),
            filter=Q(exit_time__isnull=False),
        ),
    )

    # ---------------- DRIVER ACCOUNTS (administrators only) ----------------
    # Total allocations per user account, always across all history
    # (not affected by the filters), like the stat cards.
    driver_summary = []
    selected_driver = None

    if is_admin:

        rows = (
            base.order_by()
            .values(
                "vehicle__user_id",
                "vehicle__user__username",
                "vehicle__user__first_name",
                "vehicle__user__last_name",
            )
            .annotate(
                total=Count("pk"),
                active=Count("pk", filter=Q(exit_time__isnull=True)),
                completed=Count("pk", filter=Q(exit_time__isnull=False)),
            )
            .order_by("-total", "vehicle__user__username")
        )

        for row in rows:

            full_name = (
                f"{row['vehicle__user__first_name']} "
                f"{row['vehicle__user__last_name']}"
            ).strip()

            driver_summary.append({
                "id": row["vehicle__user_id"],
                "username": row["vehicle__user__username"],
                "name": full_name or row["vehicle__user__username"],
                "total": row["total"],
                "active": row["active"],
                "completed": row["completed"],
            })

        if user_filter.isdigit():
            selected_driver = User.objects.filter(
                pk=int(user_filter)
            ).first()

    # ---------------- PAGINATION ----------------
    paginator = Paginator(allocations, HISTORY_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "is_admin": is_admin,
        "is_my_history": my_only,
        "page_title": "My History" if my_only else "Allocation History",

        "page_obj": page_obj,

        # All zones (even inactive ones can appear in old history).
        "zones": Zone.objects.order_by("distance_rank", "name"),

        "total_count": totals["total"],
        "active_count": totals["active"],
        "completed_count": totals["completed"],
        "avg_duration": _format_duration(totals["avg"]),

        # Administrators only
        "driver_summary": driver_summary,
        "selected_driver": selected_driver,
    }

    return render(
        request,
        "allocation_history.html",
        context
    )


@role_required("admin")
def allocation_history(request):
    """Administrators only: every allocation in the system."""

    return _history_response(request, my_only=False)


@role_required("driver")
def my_history(request):
    """Drivers only: allocations for the driver's own vehicles."""

    return _history_response(request, my_only=True)


# ==========================================================
# VEHICLES
# Admins see every vehicle; drivers only see their own.
# ==========================================================

def _clean_vehicle_form(request, exclude_pk=None):
    """Validate plate + type from the modal. Returns (plate, type, error)."""

    plate = " ".join(
        request.POST.get("plate_number", "").upper().split()
    )
    vehicle_type = request.POST.get("vehicle_type", "car")

    allowed_types = [
        value
        for value, _label in Vehicle._meta.get_field(
            "vehicle_type"
        ).flatchoices
    ]

    if not plate:
        return plate, vehicle_type, "Please enter a plate number."

    if allowed_types and vehicle_type not in allowed_types:
        return plate, vehicle_type, "Invalid vehicle type."

    duplicate = Vehicle.objects.filter(plate_number__iexact=plate)

    if exclude_pk:
        duplicate = duplicate.exclude(pk=exclude_pk)

    if duplicate.exists():
        return plate, vehicle_type, f"Plate {plate} is already registered."

    return plate, vehicle_type, None


def _can_manage_vehicle(user, vehicle):
    return is_approved_admin(user) or vehicle.user_id == user.id


@login_required
def vehicles(request):

    if (
        get_role(request.user) == Profile.Role.ADMIN
        and not is_approved_admin(request.user)
    ):
        messages.error(
            request,
            "Your administrator account is not approved."
        )
        return redirect("login")

    is_admin = is_approved_admin(request.user)

    qs = Vehicle.objects.select_related("user").filter(
        is_archived=False
    ).order_by("-pk")

    if not is_admin:
        qs = qs.filter(user=request.user)

    total = qs.count()
    cars = qs.filter(vehicle_type="car").count()
    motorcycles = qs.filter(vehicle_type="motorcycle").count()

    context = {
        "is_admin": is_admin,
        "home_url": home_for(request.user),
        "vehicles": qs,
        "total_vehicles": total,
        "car_count": cars,
        "motorcycle_count": motorcycles,
        "other_count": total - cars - motorcycles,
    }

    return render(request, "vehicles.html", context)


@login_required
@require_POST
def vehicle_add(request):

    if (
        get_role(request.user) == Profile.Role.ADMIN
        and not is_approved_admin(request.user)
    ):
        return redirect("login")

    plate, vehicle_type, error = _clean_vehicle_form(request)

    if error:
        messages.error(request, error)
        return redirect("vehicles")

    profile = getattr(request.user, "profile", None)

    Vehicle.objects.create(
        user=request.user,
        plate_number=plate,
        vehicle_type=vehicle_type,
        owner_name=(
            request.user.get_full_name() or request.user.username
        ),
        contact_number=getattr(profile, "phone", "") or "",
    )

    messages.success(request, f"Vehicle {plate} has been added.")

    return redirect("vehicles")


@login_required
@require_POST
def vehicle_edit(request, pk):

    vehicle = get_object_or_404(
        Vehicle,
        pk=pk,
        is_archived=False
    )

    if not _can_manage_vehicle(request.user, vehicle):
        messages.error(
            request,
            "You are not allowed to edit this vehicle."
        )
        return redirect("vehicles")

    plate, vehicle_type, error = _clean_vehicle_form(
        request, exclude_pk=vehicle.pk
    )

    if error:
        messages.error(request, error)
        return redirect("vehicles")

    vehicle.plate_number = plate
    vehicle.vehicle_type = vehicle_type
    vehicle.save(update_fields=["plate_number", "vehicle_type"])

    messages.success(
        request,
        f"Vehicle {plate} has been updated."
    )

    return redirect("vehicles")


@login_required
@require_POST
def vehicle_delete(request, pk):

    vehicle = get_object_or_404(
        Vehicle,
        pk=pk,
        is_archived=False
    )

    if not _can_manage_vehicle(request.user, vehicle):
        messages.error(
            request,
            "You are not allowed to archive this vehicle."
        )
        return redirect("vehicles")

    plate = vehicle.plate_number

    # Prevent archiving a vehicle that is currently parked.
    if Allocation.objects.filter(
        vehicle=vehicle,
        exit_time__isnull=True
    ).exists():

        messages.error(
            request,
            f"Vehicle {plate} is currently parked. "
            "Release its slot before archiving it."
        )

        return redirect("vehicles")

    # Archive instead of permanently deleting.
    vehicle.is_archived = True
    vehicle.save(update_fields=["is_archived"])

    messages.success(
        request,
        f"Vehicle {plate} has been archived successfully. "
        "Its parking history has been preserved."
    )

    return redirect("vehicles")


# ==========================================================
# RESERVATION MANAGEMENT
# Shared page for Drivers and Administrators
# ==========================================================

@login_required
def reservation_page(request):

    auto_complete_expired_reservations()

    user_role = get_role(request.user)
    is_admin = is_approved_admin(request.user)

    if user_role == Profile.Role.ADMIN and not is_admin:
        messages.error(
            request,
            "Your administrator account is not approved."
        )
        return redirect("login")

    if user_role not in (
        Profile.Role.ADMIN,
        Profile.Role.DRIVER
    ):
        return redirect(home_for(request.user))

    if is_admin:
        reservations = Reservation.objects.select_related(
            "vehicle",
            "vehicle__user",
            "slot",
            "slot__zone",
            "created_by",
        ).order_by("-created_at")

    else:
        reservations = Reservation.objects.select_related(
            "vehicle",
            "slot",
            "slot__zone",
        ).filter(
            created_by=request.user
        ).order_by("-created_at")

    total_reservations = reservations.count()

    pending_reservations = reservations.filter(
        status=Reservation.Status.PENDING
    ).count()

    approved_reservations = reservations.filter(
        status=Reservation.Status.CONFIRMED
    ).count()

    rejected_reservations = reservations.filter(
        status=Reservation.Status.REJECTED
    ).count()

    completed_reservations = reservations.filter(
        status=Reservation.Status.COMPLETED
    ).count()

    context = {
        "is_admin": is_admin,
        "now": timezone.now(),
        "reservations": reservations,

        "vehicles": Vehicle.objects.filter(
            user=request.user,
            is_archived=False
        ).order_by("plate_number"),

        "slots": ParkingSlot.objects.filter(
            status=ParkingSlot.Status.AVAILABLE,
            zone__is_active=True
        ).select_related("zone").order_by(
            "zone__distance_rank",
            "distance_rank",
            "slot_number"
        ),

        "zones": Zone.objects.filter(
            is_active=True
        ),

        "total_reservations": total_reservations,
        "pending_reservations": pending_reservations,
        "approved_reservations": approved_reservations,
        "rejected_reservations": rejected_reservations,
        "completed_reservations": completed_reservations,
    }

    return render(
        request,
        "reservation.html",
        context
    )


# ==========================================================
# CREATE RESERVATION
# DRIVER ONLY
# ==========================================================

@role_required("driver")
@require_POST
def reservation_create(request):

    vehicle_id = request.POST.get("vehicle")
    slot_id = request.POST.get("slot")

    start_time_input = request.POST.get("start_time", "").strip()
    end_time_input = request.POST.get("end_time", "").strip()

    # Validate vehicle ownership.
    vehicle = Vehicle.objects.filter(
        pk=vehicle_id,
        user=request.user,
        is_archived=False
    ).first()

    if vehicle is None:
        messages.error(
            request,
            "Please select a valid vehicle registered to your account."
        )
        return redirect("reservation")

    # Validate selected parking slot.
    slot = ParkingSlot.objects.select_related(
        "zone"
    ).filter(
        pk=slot_id,
        zone__is_active=True
    ).first()

    if slot is None:
        messages.error(
            request,
            "The selected parking slot does not exist."
        )
        return redirect("reservation")

    if slot.status != ParkingSlot.Status.AVAILABLE:
        messages.error(
            request,
            "The selected parking slot is no longer available."
        )
        return redirect("reservation")

    # Make sure the slot matches the vehicle type.
    if slot.slot_type != vehicle.vehicle_type:
        messages.error(
            request,
            "The selected parking slot does not match your vehicle type."
        )
        return redirect("reservation")

    # Parse date and time.
    start_time = parse_datetime(start_time_input)
    end_time = parse_datetime(end_time_input)

    if start_time is None or end_time is None:
        messages.error(
            request,
            "Please provide valid reservation dates and times."
        )
        return redirect("reservation")

    # Convert datetime-local values to the current timezone.
    if timezone.is_naive(start_time):
        start_time = timezone.make_aware(start_time)

    if timezone.is_naive(end_time):
        end_time = timezone.make_aware(end_time)

    now = timezone.now()

    if start_time <= now:
        messages.error(
            request,
            "Your reservation must start at a future date and time."
        )
        return redirect("reservation")

    if end_time <= start_time:
        messages.error(
            request,
            "The end time must be later than the start time."
        )
        return redirect("reservation")

    # Prevent overlapping reservations for the same slot.
    overlapping = Reservation.objects.filter(
        slot=slot,
        status__in=[
            Reservation.Status.PENDING,
            Reservation.Status.CONFIRMED,
        ],
        start_time__lt=end_time,
        end_time__gt=start_time,
    ).exists()

    if overlapping:
        messages.error(
            request,
            "This parking slot already has a reservation "
            "during your selected time."
        )
        return redirect("reservation")

    # Prevent reserving a vehicle that is currently parked.
    if Allocation.objects.filter(
        vehicle=vehicle,
        exit_time__isnull=True
    ).exists():
        messages.error(
            request,
            "This vehicle is currently parked. "
            "Release its parking slot before making a reservation."
        )
        return redirect("reservation")

    # Save reservation.
    with transaction.atomic():

        Reservation.objects.create(
            vehicle=vehicle,
            slot=slot,
            start_time=start_time,
            end_time=end_time,
            created_by=request.user,
            status=Reservation.Status.PENDING,
        )

    messages.success(
        request,
        f"Your reservation request for {slot.slot_number} "
        f"has been submitted successfully. "
        "Please wait for administrator approval."
    )

    return redirect("reservation")


# ==========================================================
# APPROVE RESERVATION
# APPROVED ADMINISTRATORS ONLY
# ==========================================================

@role_required("admin")
@require_POST
def reservation_approve(request, pk):

    with transaction.atomic():

        reservation = get_object_or_404(
            Reservation.objects.select_for_update().select_related(
                "vehicle",
                "slot",
                "slot__zone",
            ),
            pk=pk
        )

        slot = ParkingSlot.objects.select_for_update().get(
            pk=reservation.slot_id
        )

        if reservation.status != Reservation.Status.PENDING:
            messages.warning(
                request,
                "This reservation is no longer pending."
            )
            return redirect("reservation")

        if reservation.end_time <= timezone.now():
            messages.error(
                request,
                "This reservation has already expired."
            )
            return redirect("reservation")

        # Check for another overlapping active reservation.
        overlapping = Reservation.objects.filter(
            slot=slot,
            status=Reservation.Status.CONFIRMED,
            start_time__lt=reservation.end_time,
            end_time__gt=reservation.start_time,
        ).exclude(
            pk=reservation.pk
        ).exists()

        if overlapping:
            messages.error(
                request,
                "This slot already has another confirmed reservation "
                "during the selected period."
            )
            return redirect("reservation")

        # The slot must still be available.
        if slot.status != ParkingSlot.Status.AVAILABLE:
            messages.error(
                request,
                f"Slot {slot.slot_number} is no longer available."
            )
            return redirect("reservation")

        # Check whether the vehicle is already parked.
        if Allocation.objects.filter(
            vehicle=reservation.vehicle,
            exit_time__isnull=True
        ).exists():
            messages.error(
                request,
                "This vehicle is currently parked and cannot be approved "
                "for another reservation."
            )
            return redirect("reservation")

        reservation.status = Reservation.Status.CONFIRMED
        reservation.save(update_fields=["status"])

        # Mark slot as reserved.
        slot.status = ParkingSlot.Status.RESERVED
        slot.save(update_fields=["status"])

    messages.success(
        request,
        f"Reservation #{reservation.pk} has been approved successfully."
    )

    return redirect("reservation")


# ==========================================================
# REJECT RESERVATION
# APPROVED ADMINISTRATORS ONLY
# ==========================================================

@role_required("admin")
@require_POST
def reservation_reject(request, pk):

    with transaction.atomic():

        reservation = get_object_or_404(
            Reservation.objects.select_for_update(),
            pk=pk
        )

        if reservation.status != Reservation.Status.PENDING:
            messages.warning(
                request,
                "Only pending reservations can be rejected."
            )
            return redirect("reservation")

        reservation.status = Reservation.Status.REJECTED

        reservation.save(
            update_fields=["status"]
        )

    messages.warning(
        request,
        f"Reservation #{reservation.pk} has been rejected."
    )

    return redirect("reservation")


# ==========================================================
# CANCEL RESERVATION
# DRIVER: OWN RESERVATIONS
# ADMIN: ANY RESERVATION
# ==========================================================

@login_required
@require_POST
def reservation_cancel(request, pk):

    user_role = get_role(request.user)
    is_admin = is_approved_admin(request.user)

    if user_role == Profile.Role.ADMIN and not is_admin:
        messages.error(
            request,
            "Your administrator account is not approved."
        )
        return redirect("login")

    with transaction.atomic():

        reservation = get_object_or_404(
            Reservation.objects.select_for_update().select_related(
                "vehicle",
                "slot",
            ),
            pk=pk
        )

        # Check permission.
        if not (
            is_admin
            or reservation.created_by_id == request.user.id
        ):
            messages.error(
                request,
                "You are not allowed to cancel this reservation."
            )
            return redirect("reservation")

        if reservation.status not in (
            Reservation.Status.PENDING,
            Reservation.Status.CONFIRMED,
        ):
            messages.error(
                request,
                "This reservation cannot be cancelled."
            )
            return redirect("reservation")

        reservation.status = Reservation.Status.CANCELLED
        reservation.save(update_fields=["status"])

        # Release the reserved slot only when it is not occupied.
        slot = reservation.slot

        if slot.status == ParkingSlot.Status.RESERVED:

            active_allocation = Allocation.objects.filter(
                slot=slot,
                exit_time__isnull=True
            ).exists()

            another_confirmed = Reservation.objects.filter(
                slot=slot,
                status=Reservation.Status.CONFIRMED,
            ).exclude(
                pk=reservation.pk
            ).exists()

            if not active_allocation and not another_confirmed:
                slot.status = ParkingSlot.Status.AVAILABLE
                slot.save(update_fields=["status"])

    messages.success(
        request,
        f"Reservation #{reservation.pk} has been cancelled."
    )

    return redirect("reservation")


# ==========================================================
# COMPLETE RESERVATION
# APPROVED ADMINISTRATORS ONLY
# (Admins can still complete a reservation early by hand;
#  otherwise it completes automatically once its end time passes.)
# ==========================================================

@role_required("admin")
@require_POST
def reservation_complete(request, pk):

    with transaction.atomic():

        reservation = get_object_or_404(
            Reservation.objects.select_for_update().select_related(
                "slot"
            ),
            pk=pk
        )

        if reservation.status != Reservation.Status.CONFIRMED:
            messages.error(
                request,
                "Only confirmed reservations can be completed."
            )
            return redirect("reservation")

        reservation.status = Reservation.Status.COMPLETED

        reservation.save(
            update_fields=["status"]
        )

        slot = ParkingSlot.objects.select_for_update().get(
            pk=reservation.slot_id
        )

        # Do not free a slot if a vehicle is still parked there.
        active_allocation = Allocation.objects.filter(
            slot=slot,
            exit_time__isnull=True
        ).exists()

        another_confirmed = Reservation.objects.filter(
            slot=slot,
            status=Reservation.Status.CONFIRMED,
        ).exclude(
            pk=reservation.pk
        ).exists()

        if (
            slot.status == ParkingSlot.Status.RESERVED
            and not active_allocation
            and not another_confirmed
        ):
            slot.status = ParkingSlot.Status.AVAILABLE
            slot.save(update_fields=["status"])

    messages.success(
        request,
        f"Reservation #{reservation.pk} has been completed."
    )

    return redirect("reservation")


def build_active_sessions():
    """Every vehicle that is parked right now, with what it owes."""

    now = timezone.now()

    allocations = (
        Allocation.objects
        .filter(exit_time__isnull=True)
        .select_related("vehicle", "vehicle__user", "slot", "slot__zone")
        .prefetch_related("payments")
        .order_by("entry_time")
    )

    sessions = []
    total_due = Decimal("0.00")

    for a in allocations:

        fee = calculate_fee(a, now)
        payments = list(a.payments.all())

        paid = sum(
            (p.amount for p in payments if p.is_paid),
            Decimal("0.00")
        )
        pending = next((p for p in payments if not p.is_paid), None)
        due = max(fee["amount"] - paid, Decimal("0.00"))

        if due <= 0 and fee["amount"] == 0:
            state = "free"
        elif due <= 0:
            state = "paid"
        elif pending:
            state = "pending"
        else:
            state = "owes"

        user = a.vehicle.user

        if user:
            driver = user.get_full_name() or user.username
        else:
            driver = a.vehicle.owner_name or "Unknown Driver"

        total_due += due

        sessions.append({
            "allocation_pk": a.pk,
            "plate": a.vehicle.plate_number,
            "vehicle_type": a.vehicle.get_vehicle_type_display(),
            "driver": driver,
            "slot": a.slot.slot_number,
            "zone": a.slot.zone.name,
            "entry_time": a.entry_time,
            "duration": _format_duration(now - a.entry_time),
            "fee": fee["amount"],
            "paid": paid,
            "due": due,
            "state": state,
        })

    return sessions, total_due

@login_required
@require_POST
def reservation_checkin(request, pk):

    is_admin = is_approved_admin(request.user)

    if get_role(request.user) == Profile.Role.ADMIN and not is_admin:
        messages.error(request, "Your administrator account is not approved.")
        return redirect("login")

    with transaction.atomic():

        reservation = get_object_or_404(
            Reservation.objects.select_for_update().select_related("vehicle", "slot"),
            pk=pk
        )

        if not (is_admin or reservation.created_by_id == request.user.id):
            messages.error(request, "You are not allowed to check in this reservation.")
            return redirect("reservation")

        if reservation.status != Reservation.Status.CONFIRMED:
            messages.error(request, "Only approved reservations can be checked in.")
            return redirect("reservation")

        now = timezone.now()

        if now < reservation.start_time:
            messages.error(request, "It is not yet time for this reservation.")
            return redirect("reservation")

        if now >= reservation.end_time:
            messages.error(request, "This reservation has already ended.")
            return redirect("reservation")

        # Same rule as allocate_slot: only one vehicle parked at a time.
        if Allocation.objects.filter(exit_time__isnull=True).exists():
            messages.error(
                request,
                "Parking is currently occupied. Please wait until "
                "the current vehicle exits."
            )
            return redirect("reservation")

        slot = ParkingSlot.objects.select_for_update().get(pk=reservation.slot_id)

        if slot.status != ParkingSlot.Status.RESERVED:
            messages.error(request, f"Slot {slot.slot_number} is not available for check-in.")
            return redirect("reservation")

        Allocation.objects.create(
            vehicle=reservation.vehicle,
            slot=slot,
            allocated_by=request.user,
            entry_time=now,
        )

        slot.status = ParkingSlot.Status.OCCUPIED
        slot.save(update_fields=["status"])

        reservation.status = Reservation.Status.COMPLETED
        reservation.save(update_fields=["status"])

    messages.success(
        request,
        f"Checked in. {reservation.vehicle.plate_number} is now parked "
        f"in slot {slot.slot_number}."
    )

    return redirect("driver_home")


# ==========================================================
# SETTINGS PAGE  (admin settings / driver settings in one template)
#
#   Administrators -> system-wide settings (SystemSettings, one row)
#   Drivers        -> personal preferences (DriverPreference, one row each)
#
# Every form on settings.html posts a hidden "section" value; each
# section has its own handler below and is validated on the server.
# ==========================================================

COMMON_TIMEZONES = [
    "Asia/Manila", "Asia/Singapore", "Asia/Tokyo", "Asia/Hong_Kong",
    "Asia/Bangkok", "Asia/Dubai", "Europe/London", "America/New_York",
    "America/Los_Angeles", "UTC",
]

AUTO_RELEASE_OPTIONS = [
    (0, "Never"), (12, "12 hours"), (24, "24 hours"), (48, "48 hours"),
]

ALLOC_MODES = {"nearest_entrance", "nearest_exit", "balanced", "first_free"}
NO_SHOW_ACTIONS = {"warn", "fee", "block"}
PAYMENT_CHOICES = {"gcash", "cash"}
PRIORITY_KEYS = {"reservation", "pwd", "subscriber", "walkin"}

# section name -> panel id in settings.html (reopens the same tab after saving)
ADMIN_PANELS = {
    "general": "a-general",
    "allocation": "a-allocation",
    "pricing": "a-pricing",
    "reservations": "a-reservations",
    "alerts": "a-alerts",
    "security": "a-security",
    "reset": "a-security",
}

DRIVER_PANELS = {
    "profile": "d-profile",
    "parking": "d-parking",
    "notifications": "d-notifications",
    "password": "d-security",
    "privacy": "d-security",
    "delete_account": "d-security",
}


# ---------- small validators ----------

def _bool(post, name):
    """Unticked HTML checkboxes are not posted at all."""
    return name in post


def _int(post, name, lo, hi, label=None):
    label = label or name.replace("_", " ")
    try:
        val = int(str(post.get(name, "")).strip())
    except ValueError:
        raise ValidationError(f"{label.capitalize()} must be a whole number.")
    if not lo <= val <= hi:
        raise ValidationError(
            f"{label.capitalize()} must be between {lo} and {hi}."
        )
    return val


def _dec(post, name, lo, hi, label=None):
    label = label or name.replace("_", " ")
    try:
        val = Decimal(str(post.get(name, "")).strip())
    except InvalidOperation:
        raise ValidationError(f"{label.capitalize()} must be a number.")
    if not Decimal(lo) <= val <= Decimal(hi):
        raise ValidationError(
            f"{label.capitalize()} must be between {lo} and {hi}."
        )
    return val


def _str(post, name, maxlen, required=False, label=None):
    label = label or name.replace("_", " ")
    val = post.get(name, "").strip()
    if required and not val:
        raise ValidationError(f"{label.capitalize()} is required.")
    if len(val) > maxlen:
        raise ValidationError(
            f"{label.capitalize()} is too long (max {maxlen} characters)."
        )
    return val


def _choice(post, name, allowed, label=None):
    label = label or name.replace("_", " ")
    val = post.get(name, "")
    if val not in allowed:
        raise ValidationError(f"Invalid value for {label}.")
    return val


def _int_choice(post, name, allowed, label=None):
    label = label or name.replace("_", " ")
    try:
        val = int(post.get(name, ""))
    except ValueError:
        raise ValidationError(f"Invalid value for {label}.")
    if val not in allowed:
        raise ValidationError(f"Invalid value for {label}.")
    return val


def _time_value(post, name, label=None):
    label = label or name.replace("_", " ")
    try:
        return datetime.strptime(post.get(name, ""), "%H:%M").time()
    except ValueError:
        raise ValidationError(f"Enter a valid {label}.")


def _email_value(value, label="email"):
    if value:
        try:
            validate_email(value)
        except ValidationError:
            raise ValidationError(f"Enter a valid {label}.")
    return value


# ---------- ADMIN section handlers ----------

def _save_general(request, cfg):
    p = request.POST
    cfg.facility_name = _str(p, "facility_name", 120, required=True, label="facility name")
    cfg.facility_code = _str(p, "facility_code", 20, label="facility code")
    cfg.facility_address = _str(p, "facility_address", 255, label="address")
    cfg.contact_email = _email_value(
        _str(p, "contact_email", 254, label="contact email"), "contact email"
    )

    tz = p.get("timezone", "")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError("Choose a valid timezone.")
    cfg.timezone = tz

    cfg.open_24_7 = _bool(p, "open_24_7")
    if not cfg.open_24_7:       # time inputs are disabled (not posted) when open 24/7
        opening = _time_value(p, "open_time", "opening time")
        closing = _time_value(p, "close_time", "closing time")
        if opening >= closing:
            raise ValidationError("Closing time must be after opening time.")
        cfg.open_time, cfg.close_time = opening, closing
    cfg.save()


def _save_allocation(request, cfg):
    p = request.POST
    cfg.alloc_mode = _choice(p, "alloc_mode", ALLOC_MODES, "allocation mode")
    cfg.match_vehicle_type = _bool(p, "match_vehicle_type")
    cfg.reserve_pwd = _bool(p, "reserve_pwd")
    cfg.driver_can_choose = _bool(p, "driver_can_choose")
    cfg.emergency_buffer = _int(p, "emergency_buffer", 0, 20, "emergency buffer")
    cfg.auto_release_hours = _int_choice(
        p, "auto_release_hours", {h for h, _ in AUTO_RELEASE_OPTIONS}, "auto-release"
    )

    order = [k for k in p.get("priority_order", "").split(",") if k]
    if len(order) != len(PRIORITY_KEYS) or set(order) != PRIORITY_KEYS:
        raise ValidationError("Invalid priority order.")
    cfg.priority_order = ",".join(order)
    cfg.save()


def _save_pricing(request, cfg):
    p = request.POST
    cfg.free_minutes = _int(p, "free_minutes", 0, 1440, "free minutes")
    cfg.hourly_rate = _dec(p, "hourly_rate", 0, 100000, "hourly rate")
    cfg.pay_gcash = _bool(p, "pay_gcash")
    cfg.pay_cash = _bool(p, "pay_cash")
    if not (cfg.pay_gcash or cfg.pay_cash):
        raise ValidationError("Enable at least one payment method.")
    cfg.save()


def _save_reservations(request, cfg):
    p = request.POST
    cfg.res_enabled = _bool(p, "res_enabled")
    cfg.res_advance_days = _int(p, "res_advance_days", 1, 365, "advance booking days")
    cfg.res_max_hours = _int(p, "res_max_hours", 1, 72, "max booking length")
    cfg.res_hold_minutes = _int(p, "res_hold_minutes", 0, 240, "hold time")
    cfg.res_cancel_minutes = _int(p, "res_cancel_minutes", 0, 1440, "cancellation window")
    cfg.res_per_driver = _int(p, "res_per_driver", 1, 20, "reservations per driver")
    cfg.no_show_action = _choice(p, "no_show_action", NO_SHOW_ACTIONS, "no-show action")
    cfg.save()


def _save_alerts(request, cfg):
    p = request.POST
    cfg.al_occupancy = _bool(p, "al_occupancy")
    cfg.al_occupancy_pct = _int(p, "al_occupancy_pct", 50, 100, "occupancy alert level")
    cfg.al_overstay = _bool(p, "al_overstay")
    cfg.al_new_driver = _bool(p, "al_new_driver")
    cfg.al_payment = _bool(p, "al_payment")

    recipients = [
        e.strip() for e in p.get("al_recipients", "").split(",") if e.strip()
    ]
    for e in recipients:
        _email_value(e, f"recipient email ({e})")
    cfg.al_recipients = ", ".join(recipients)[:500]

    cfg.dr_assigned = _bool(p, "dr_assigned")
    cfg.dr_reminder = _bool(p, "dr_reminder")
    cfg.dr_reminder_min = _int_choice(p, "dr_reminder_min", {15, 30, 60}, "reminder lead time")
    cfg.save()


def _save_security(request, cfg):
    p = request.POST
    cfg.require_2fa = _bool(p, "require_2fa")
    cfg.session_timeout = _int(p, "session_timeout", 5, 1440, "session timeout")
    cfg.min_password = _int(p, "min_password", 6, 32, "minimum password length")
    cfg.lock_attempts = _int(p, "lock_attempts", 3, 20, "failed login limit")
    cfg.self_register = _bool(p, "self_register")
    cfg.retain_history = _int_choice(p, "retain_history", {90, 365, 1095}, "history retention")
    cfg.mask_plates = _bool(p, "mask_plates")
    cfg.audit_log = _bool(p, "audit_log")
    cfg.save()


def _reset_settings(request, cfg):
    cfg.delete()
    SystemSettings.load()       # recreates the row (starting from settings.py values)


ADMIN_HANDLERS = {
    "general": _save_general,
    "allocation": _save_allocation,
    "pricing": _save_pricing,
    "reservations": _save_reservations,
    "alerts": _save_alerts,
    "security": _save_security,
    "reset": _reset_settings,
}


# ---------- DRIVER section handlers ----------

def _save_profile(request, prefs):
    p, user = request.POST, request.user

    user.first_name = _str(p, "first_name", 150, label="first name")
    user.last_name = _str(p, "last_name", 150, label="last name")
    email = _email_value(_str(p, "email", 254), "email")

    if not email:
        raise ValidationError("Email address is required.")
    if User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
        raise ValidationError("That email address is already being used.")

    user.email = email
    user.save(update_fields=["first_name", "last_name", "email"])

    phone = _str(p, "phone", 30, label="mobile number")
    if phone and not all(ch.isdigit() or ch in "+-() " for ch in phone):
        raise ValidationError("Mobile number contains invalid characters.")

    profile = getattr(user, "profile", None)     # phone lives on Profile
    if profile is not None:
        profile.phone = phone
        profile.save(update_fields=["phone"])


def _save_parking(request, prefs):
    p = request.POST

    vehicle_id = p.get("default_vehicle", "")
    if vehicle_id:
        vehicle = Vehicle.objects.filter(
            pk=vehicle_id, user=request.user, is_archived=False
        ).first()                                # must be the driver's own vehicle
        if vehicle is None:
            raise ValidationError("Choose one of your own vehicles.")
        prefs.default_vehicle = vehicle
    else:
        prefs.default_vehicle = None

    zone_id = p.get("preferred_zone", "")
    if zone_id:
        zone = Zone.objects.filter(pk=zone_id, is_active=True).first()
        if zone is None:
            raise ValidationError("That zone is not available.")
        prefs.preferred_zone = zone
    else:
        prefs.preferred_zone = None

    prefs.prefer_covered = _bool(p, "prefer_covered")
    prefs.prefer_exit = _bool(p, "prefer_exit")
    prefs.needs_pwd = _bool(p, "needs_pwd")
    prefs.needs_ev = _bool(p, "needs_ev")
    prefs.auto_release = _int_choice(p, "auto_release", {0, 4, 8, 12}, "auto-release")
    prefs.default_payment = _choice(p, "default_payment", PAYMENT_CHOICES, "payment method")
    prefs.save()


def _save_notifications(request, prefs):
    p = request.POST
    prefs.ch_app = _bool(p, "ch_app")
    prefs.ch_email = _bool(p, "ch_email")
    prefs.ch_sms = _bool(p, "ch_sms")
    prefs.n_assigned = _bool(p, "n_assigned")
    prefs.n_reminder = _bool(p, "n_reminder")
    prefs.n_expiry = _int_choice(p, "n_expiry", {0, 10, 15, 30}, "expiry warning")
    prefs.n_receipt = _bool(p, "n_receipt")
    prefs.n_promo = _bool(p, "n_promo")
    prefs.save()


def _save_privacy(request, prefs):
    prefs.mask_plate = _bool(request.POST, "mask_plate")
    prefs.signin_alerts = _bool(request.POST, "signin_alerts")
    prefs.save(update_fields=["mask_plate", "signin_alerts"])


DRIVER_HANDLERS = {
    "profile": _save_profile,
    "parking": _save_parking,
    "notifications": _save_notifications,
    "privacy": _save_privacy,
}


# ---------- the view ----------

def _settings_back(panel):
    return redirect(f"{reverse('settings')}#{panel}")


@login_required
def settings_page(request):

    role = get_role(request.user)
    is_admin = is_approved_admin(request.user)

    # Same guards as the rest of the pages
    if role == Profile.Role.ADMIN and not is_admin:
        messages.error(request, "Your administrator account is not approved.")
        return redirect("login")

    if role not in (Profile.Role.ADMIN, Profile.Role.DRIVER):
        return redirect(home_for(request.user))

    # ---------------- POST: save one section ----------------
    if request.method == "POST":

        section = request.POST.get("section", "")
        panels = ADMIN_PANELS if is_admin else DRIVER_PANELS

        if section not in panels:
            # Unknown section, or a driver/admin posting the other role's section.
            return HttpResponseForbidden(
                "You are not allowed to change these settings."
            )

        panel = panels[section]

        try:
            if is_admin:
                cfg = SystemSettings.load()
                ADMIN_HANDLERS[section](request, cfg)
                messages.success(
                    request,
                    "Settings reset to defaults."
                    if section == "reset"
                    else "Settings saved.",
                )

            elif section == "password":
                form = PasswordChangeForm(request.user, request.POST)
                if form.is_valid():
                    user = form.save()
                    update_session_auth_hash(request, user)    # stay logged in
                    messages.success(request, "Your password was updated.")
                else:
                    for errs in form.errors.values():
                        for err in errs:
                            messages.error(request, err)

            elif section == "delete_account":
                if Allocation.objects.filter(
                    vehicle__user=request.user, exit_time__isnull=True
                ).exists():
                    messages.error(
                        request,
                        "You are currently parked. Release your slot "
                        "before deleting your account.",
                    )
                else:
                    user = request.user
                    logout(request)
                    delete_account_data(user)
                    messages.success(request, "Your account has been deleted.")
                    return redirect("login")

            else:
                prefs, _ = DriverPreference.objects.get_or_create(user=request.user)
                DRIVER_HANDLERS[section](request, prefs)
                messages.success(request, "Settings saved.")

        except ValidationError as e:
            for msg in e.messages:
                messages.error(request, msg)

        return _settings_back(panel)

    # ---------------- GET: show the page ----------------
    context = {"is_admin": is_admin}

    if is_admin:
        cfg = SystemSettings.load()

        tz_list = list(COMMON_TIMEZONES)
        if cfg.timezone not in tz_list:
            tz_list.insert(0, cfg.timezone)

        context.update(
            cfg=cfg,
            timezones=tz_list,
            auto_release_options=AUTO_RELEASE_OPTIONS,
        )

    else:
        prefs, _ = DriverPreference.objects.get_or_create(user=request.user)

        # settings.html reads {{ profile.phone }}; phone lives on Profile.
        prefs.phone = getattr(getattr(request.user, "profile", None), "phone", "") or ""

        context.update(
            profile=prefs,
            vehicles=request.user.vehicles.filter(is_archived=False).order_by("plate_number"),
            zones=Zone.objects.filter(is_active=True).order_by("name"),
        )

    return render(request, "settings.html", context)