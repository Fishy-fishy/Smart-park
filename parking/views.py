from datetime import timedelta
from functools import wraps
from django.utils.dateparse import parse_date, parse_datetime
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.views import LoginView
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import (
    Avg,
    Count,
    DurationField,
    ExpressionWrapper,
    F,
    Prefetch,
    Q,
)
from django.db.models.deletion import ProtectedError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timesince import timesince, timeuntil
from django.views.decorators.http import require_POST

from .forms import DriverSignUpForm
from .models import (
    Allocation,
    ParkingSlot,
    Profile,
    Vehicle,
    Zone,
    Reservation,
)

User = get_user_model()


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

        # Allocation.vehicle is PROTECT, so remove allocations first.
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

    messages.success(
        request,
        f"Account '{username}' and all of its vehicles and "
        "allocation history have been deleted."
    )

    return redirect("admin_request")


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

    Plate number, vehicle type and parked-since time are only included for
    approved administrators, or for the driver who owns that vehicle, so
    drivers cannot see other drivers' vehicle details.
    """

    # Free slots whose reservations have ended, so the map is always current.
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
        .order_by("zone__distance_rank", "zone_id", "distance_rank", "slot_number")
    )

    counts = {
        ParkingSlot.Status.AVAILABLE: 0,
        ParkingSlot.Status.OCCUPIED: 0,
        ParkingSlot.Status.RESERVED: 0,
        ParkingSlot.Status.MAINTENANCE: 0,
    }

    # Keep every active zone, even if it has no slots yet.
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

    total = sum(counts.values())
    occupied = counts[ParkingSlot.Status.OCCUPIED]

    return {
        "total": total,
        "available": counts[ParkingSlot.Status.AVAILABLE],
        "occupied": occupied,
        "reserved": counts[ParkingSlot.Status.RESERVED],
        "maintenance": counts[ParkingSlot.Status.MAINTENANCE],
        "occupancy_rate": round(occupied * 100 / total) if total else 0,
        "zones": list(zones.values()),
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

    if total and occupied * 100 >= total * 90:
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

        # The vehicle must belong to this driver.
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

        # Find a slot (rows are locked so two drivers can't take the same one).
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

            # A slot picked on the Live Slot Map.
            slot = candidates.filter(pk=slot_id).first()

            if slot is None:

                messages.error(
                    request,
                    "That slot is no longer available, or it does "
                    "not fit this vehicle type. Please pick another one."
                )

                return redirect("driver_home")

        else:

            # Auto allocation: nearest available slot (lowest distance rank).
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

        # Register the vehicle the first time it is used.
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
                contact_number=getattr(profile, "phone", "") or "",
            )

        Allocation.objects.create(
            vehicle=vehicle,
            slot=slot,
            allocated_by=request.user,
            entry_time=timezone.now(),
        )

        slot.status = ParkingSlot.Status.OCCUPIED
        slot.save(update_fields=["status"])

    messages.success(
        request,
        f"Slot {slot.slot_number} ({slot.zone.name}) "
        f"has been allocated to {plate}."
    )

    return redirect("driver_home")


# ==========================================================
# RELEASE A SLOT (vehicle leaves)
# The owner of the vehicle or an approved administrator.
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

        allocation.exit_time = timezone.now()
        allocation.save(update_fields=["exit_time"])

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

    context = {

        "is_admin": False,

        "vehicles": request.user.vehicles.filter(
            is_archived=False
        ),

        "current": mine.filter(
            exit_time__isnull=True
        ),

        "history": mine.filter(
            exit_time__isnull=False
        )[:10],

        "recent_allocations": mine.order_by("-entry_time")[:5],

        # Live Slot Map / stat cards (first paint; JS keeps them fresh)
        "zones": Zone.objects.filter(is_active=True),
        "total_slots": slot_map["total"],
        "available_count": slot_map["available"],
        "occupied_count": slot_map["occupied"],
        "reserved_count": slot_map["reserved"],
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
        allocations = allocations.filter(entry_time__date__gte=date_from)

    if date_to:
        allocations = allocations.filter(entry_time__date__lte=date_to)

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

    # Complete reservations whose end time has passed
    # and free their slots before building the page.
    auto_complete_expired_reservations()

    user_role = get_role(request.user)
    is_admin = is_approved_admin(request.user)

    # Prevent unapproved administrators from accessing reservations.
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

    # Administrators can see all reservations.
    # Drivers can only see their own reservations.
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

    # Statistics
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

        "reservations": reservations,

        # Drivers can only select their own registered vehicles.
        "vehicles": Vehicle.objects.filter(
            user=request.user,
            is_archived=False
        ).order_by("plate_number"),

        # Only currently available slots are offered.
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