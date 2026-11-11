
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_save
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

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
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

    vehicle = models.ForeignKey(
        Vehicle,
        on_delete=models.PROTECT,
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
# ---------------------------------------------------------------------------
class Payment(models.Model):

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        EWALLET = "ewallet", "E-Wallet"

    allocation = models.OneToOneField(
        Allocation,
        on_delete=models.CASCADE,
        related_name="payment"
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

    is_paid = models.BooleanField(default=False)

    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-paid_at"]

    def __str__(self):
        return f"{self.allocation} - {self.amount}"


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
