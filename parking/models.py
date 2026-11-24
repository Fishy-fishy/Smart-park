import datetime
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.utils import timezone


# ---------------------------------------------------------------------------
# Zones & Slots
# ---------------------------------------------------------------------------
class Zone(models.Model):
    """A parking area (e.g. Zone A - Ground Floor)."""

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=255, blank=True)

    distance_rank = models.PositiveIntegerField(
        default=0,
        help_text="Lower = nearer to the entrance."
    )

    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["distance_rank", "name"]

    def __str__(self):
        return self.name

    @property
    def available_count(self):
        return self.slots.filter(
            status=ParkingSlot.Status.AVAILABLE
        ).count()


# ---------------------------------------------------------------------------
# Parking Slots
# ---------------------------------------------------------------------------
class ParkingSlot(models.Model):

    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        OCCUPIED = "occupied", "Occupied"
        RESERVED = "reserved", "Reserved"
        MAINTENANCE = "maintenance", "Maintenance"

    class SlotType(models.TextChoices):
        CAR = "car", "Car"
        MOTORCYCLE = "motorcycle", "Motorcycle"
        VAN = "van", "Van / SUV"
        PWD = "pwd", "PWD"

    zone = models.ForeignKey(
        Zone,
        on_delete=models.CASCADE,
        related_name="slots"
    )

    slot_number = models.CharField(max_length=20)

    slot_type = models.CharField(
        max_length=20,
        choices=SlotType.choices,
        default=SlotType.CAR
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.AVAILABLE,
        db_index=True
    )

    distance_rank = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = [
            "zone__distance_rank",
            "distance_rank",
            "slot_number"
        ]

        constraints = [
            models.UniqueConstraint(
                fields=["zone", "slot_number"],
                name="unique_slot_per_zone"
            ),
        ]

    def __str__(self):
        return f"{self.zone.name} - {self.slot_number}"

    @property
    def is_available(self):
        return self.status == self.Status.AVAILABLE


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

class Vehicle(models.Model):

    class VehicleType(models.TextChoices):
        CAR = "car", "Car"
        MOTORCYCLE = "motorcycle", "Motorcycle"
        VAN = "van", "Van / SUV"
        PWD = "pwd", "PWD"

    plate_number = models.CharField(max_length=20, unique=True)

    vehicle_type = models.CharField(
        max_length=20,
        choices=VehicleType.choices,
        default=VehicleType.CAR
    )

    owner_name = models.CharField(max_length=100, blank=True)

    contact_number = models.CharField(max_length=30, blank=True)

    # Deleting a driver account also deletes their vehicles.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="vehicles"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # Soft deletion / archiving
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["plate_number"]

    def save(self, *args, **kwargs):
        self.plate_number = " ".join(
            self.plate_number.upper().split()
        )
        super().save(*args, **kwargs)

    def __str__(self):
        return self.plate_number


# ---------------------------------------------------------------------------
# Allocations
# ---------------------------------------------------------------------------
class AllocationManager(models.Manager):

    def active(self):
        return self.filter(exit_time__isnull=True)

    @transaction.atomic
    def allocate(self, vehicle, zone=None, allocated_by=None):
        """
        Assign the nearest available slot that fits the vehicle type.
        Returns Allocation or None if no slot is available.
        """

        slots = (
            ParkingSlot.objects.select_for_update()
            .filter(
                status=ParkingSlot.Status.AVAILABLE,
                slot_type=vehicle.vehicle_type,
                zone__is_active=True
            )
            .order_by(
                "zone__distance_rank",
                "distance_rank",
                "slot_number"
            )
        )

        if zone is not None:
            slots = slots.filter(zone=zone)

        slot = slots.first()

        if slot is None:
            return None

        slot.status = ParkingSlot.Status.OCCUPIED
        slot.save(update_fields=["status"])

        return self.create(
            vehicle=vehicle,
            slot=slot,
            allocated_by=allocated_by
        )


class Allocation(models.Model):
    """A vehicle parked in a slot."""

    # CASCADE: deleting a vehicle (or its driver) that has parking history
    # does not raise ProtectedError.
    vehicle = models.ForeignKey(
        Vehicle,
        on_delete=models.CASCADE,
        related_name="allocations"
    )

    slot = models.ForeignKey(
        ParkingSlot,
        on_delete=models.PROTECT,
        related_name="allocations"
    )

    allocated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="allocations_made"
    )

    entry_time = models.DateTimeField(default=timezone.now)

    exit_time = models.DateTimeField(null=True, blank=True)

    objects = AllocationManager()

    class Meta:
        ordering = ["-entry_time"]

        indexes = [
            models.Index(fields=["exit_time"])
        ]

    def __str__(self):
        return f"{self.vehicle} @ {self.slot}"

    @property
    def is_active(self):
        return self.exit_time is None

    @property
    def duration(self):
        end = self.exit_time or timezone.now()
        return end - self.entry_time

    @transaction.atomic
    def release(self):
        """Mark the vehicle as exited and free up the slot."""

        if self.exit_time is not None:
            return self

        self.exit_time = timezone.now()
        self.save(update_fields=["exit_time"])

        self.slot.status = ParkingSlot.Status.AVAILABLE
        self.slot.save(update_fields=["status"])

        return self


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------
class Reservation(models.Model):

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    vehicle = models.ForeignKey(
        Vehicle,
        on_delete=models.CASCADE,
        related_name="reservations"
    )

    slot = models.ForeignKey(
        ParkingSlot,
        on_delete=models.CASCADE,
        related_name="reservations"
    )

    start_time = models.DateTimeField()

    end_time = models.DateTimeField()

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reservations"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start_time"]

    def __str__(self):
        return (
            f"{self.vehicle} -> {self.slot} "
            f"({self.start_time:%b %d %H:%M})"
        )


# ---------------------------------------------------------------------------
# Payments
#
# - A parking session (Allocation) can have several payments, so a driver
#   can pay again if they stay longer after paying.
# - "Pending" simply means is_paid = False (e.g. cash waiting for an
#   administrator to confirm it).
# - Card numbers are never stored.
# ---------------------------------------------------------------------------
class Payment(models.Model):

    class Method(models.TextChoices):
        CASH = "cash", "Cash Counter"
        GCASH = "gcash", "GCash"
        CARD = "card", "Card"  # Keep for old payment records
        WALLET = "ewallet", "E-Wallet"  # Keep for old records
        MAYA = "maya", "Maya"  # Keep for old records
        FREE = "free", "Free"  # (label changed: free minutes are now configurable)

    allocation = models.ForeignKey(
        Allocation,
        on_delete=models.CASCADE,
        related_name="payments"
    )

    amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00")
    )

    method = models.CharField(
        max_length=20,
        choices=Method.choices,
        default=Method.CASH
    )

    # False = submitted but not confirmed yet
    is_paid = models.BooleanField(default=False)

    paid_at = models.DateTimeField(null=True, blank=True)

    # GCash / Maya reference number
    reference = models.CharField(max_length=60, blank=True)

    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_made"
    )

    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_confirmed"
    )

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.allocation} - {self.amount}"

    @property
    def status(self):
        """Used by payment.html: 'paid' or 'pending'."""
        return "paid" if self.is_paid else "pending"


# ---------------------------------------------------------------------------
# User Profile & Administrator Approval
# ---------------------------------------------------------------------------
class Profile(models.Model):

    class Role(models.TextChoices):
        ADMIN = "admin", "Administrator"
        DRIVER = "driver", "Driver"

    class ApprovalStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile"
    )

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.DRIVER
    )

    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.APPROVED
    )

    phone = models.CharField(max_length=30, blank=True)

    # Identifies the Main Administrator.
    is_main_admin = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} ({self.get_role_display()})"

    @property
    def is_admin(self):
        return (
            self.role == self.Role.ADMIN
            and self.approval_status == self.ApprovalStatus.APPROVED
        )

    @property
    def is_driver(self):
        return self.role == self.Role.DRIVER


# ---------------------------------------------------------------------------
# NEW: System settings (administrators)
# One row only. Always use SystemSettings.load().
# ---------------------------------------------------------------------------
class SystemSettings(models.Model):

    # General
    facility_name = models.CharField(max_length=120, default="SmartPark")
    facility_code = models.CharField(max_length=20, blank=True)
    facility_address = models.CharField(max_length=255, blank=True)
    contact_email = models.EmailField(blank=True)
    timezone = models.CharField(max_length=64, default="Asia/Manila")
    open_24_7 = models.BooleanField(default=True)
    open_time = models.TimeField(default=datetime.time(6, 0))
    close_time = models.TimeField(default=datetime.time(22, 0))

    # Allocation
    alloc_mode = models.CharField(max_length=20, default="nearest_entrance")
    match_vehicle_type = models.BooleanField(default=True)
    reserve_pwd = models.BooleanField(default=True)
    driver_can_choose = models.BooleanField(default=True)
    emergency_buffer = models.PositiveSmallIntegerField(default=2)
    auto_release_hours = models.PositiveSmallIntegerField(default=24)
    priority_order = models.CharField(
        max_length=80,
        default="reservation,pwd,subscriber,walkin"
    )

    # Pricing: first N minutes free, then every started hour is charged
    free_minutes = models.PositiveSmallIntegerField(default=60)
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, default=20)
    pay_gcash = models.BooleanField(default=True)
    pay_cash = models.BooleanField(default=True)       # "Cash Counter"

    # Reservations
    res_enabled = models.BooleanField(default=True)
    res_advance_days = models.PositiveSmallIntegerField(default=14)
    res_max_hours = models.PositiveSmallIntegerField(default=12)
    res_hold_minutes = models.PositiveSmallIntegerField(default=15)
    res_cancel_minutes = models.PositiveSmallIntegerField(default=60)
    res_per_driver = models.PositiveSmallIntegerField(default=3)
    no_show_action = models.CharField(max_length=10, default="warn")

    # Alerts
    al_occupancy = models.BooleanField(default=True)
    al_occupancy_pct = models.PositiveSmallIntegerField(default=90)
    al_overstay = models.BooleanField(default=True)
    al_new_driver = models.BooleanField(default=False)
    al_payment = models.BooleanField(default=True)
    al_recipients = models.CharField(max_length=500, blank=True)
    dr_assigned = models.BooleanField(default=True)
    dr_reminder = models.BooleanField(default=True)
    dr_reminder_min = models.PositiveSmallIntegerField(default=30)

    # Security & data
    require_2fa = models.BooleanField(default=False)
    session_timeout = models.PositiveSmallIntegerField(default=30)
    min_password = models.PositiveSmallIntegerField(default=8)
    lock_attempts = models.PositiveSmallIntegerField(default=5)
    self_register = models.BooleanField(default=True)
    retain_history = models.PositiveIntegerField(default=365)
    mask_plates = models.BooleanField(default=False)
    audit_log = models.BooleanField(default=True)

    class Meta:
        verbose_name = "System settings"
        verbose_name_plural = "System settings"

    def __str__(self):
        return "System settings"

    def save(self, *args, **kwargs):
        self.pk = 1                      # enforce a single row
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        # First time only: start from your existing settings.py values.
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "free_minutes": int(getattr(settings, "PARKING_FREE_MINUTES", 60)),
                "hourly_rate": getattr(settings, "PARKING_HOURLY_RATE", 20),
            },
        )
        return obj


# ---------------------------------------------------------------------------
# NEW: Driver preferences (one row per driver, created on first visit
# to the settings page). Phone stays on Profile.
# ---------------------------------------------------------------------------
class DriverPreference(models.Model):

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preferences"
    )

    # Parking preferences
    default_vehicle = models.ForeignKey(
        Vehicle, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+"
    )
    preferred_zone = models.ForeignKey(
        Zone, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+"
    )
    prefer_covered = models.BooleanField(default=False)
    prefer_exit = models.BooleanField(default=False)
    needs_pwd = models.BooleanField(default=False)
    needs_ev = models.BooleanField(default=False)
    auto_release = models.PositiveSmallIntegerField(default=0)     # hours, 0 = never
    default_payment = models.CharField(max_length=10, default="gcash")

    # Notifications
    ch_app = models.BooleanField(default=True)
    ch_email = models.BooleanField(default=True)
    ch_sms = models.BooleanField(default=False)
    n_assigned = models.BooleanField(default=True)
    n_reminder = models.BooleanField(default=True)
    n_expiry = models.PositiveSmallIntegerField(default=15)        # minutes, 0 = off
    n_receipt = models.BooleanField(default=True)
    n_promo = models.BooleanField(default=False)

    # Privacy
    mask_plate = models.BooleanField(default=False)
    signin_alerts = models.BooleanField(default=True)

    def __str__(self):
        return f"Preferences of {self.user}"


# ---------------------------------------------------------------------------
# Automatically Create User Profile
# ---------------------------------------------------------------------------
@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_profile(sender, instance, created, raw=False, **kwargs):

    if raw or not created:
        return

    if instance.is_superuser:
        # Django superusers are initially created as main administrators.
        Profile.objects.create(
            user=instance,
            role=Profile.Role.ADMIN,
            approval_status=Profile.ApprovalStatus.APPROVED,
            is_main_admin=True
        )

    else:
        # Normal users are drivers by default.
        # Administrator registration is handled separately in views.py.
        Profile.objects.create(
            user=instance,
            role=Profile.Role.DRIVER,
            approval_status=Profile.ApprovalStatus.APPROVED,
            is_main_admin=False
        )


# ---------------------------------------------------------------------------
# Free the parking slot when an active allocation is deleted
# (e.g. when a driver who is currently parked has their account deleted).
# ---------------------------------------------------------------------------
@receiver(pre_delete, sender=Allocation)
def free_slot_on_allocation_delete(sender, instance, **kwargs):
    if instance.exit_time is None:
        ParkingSlot.objects.filter(pk=instance.slot_id).update(
            status=ParkingSlot.Status.AVAILABLE
        ) 