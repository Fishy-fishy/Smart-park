
from django.urls import path
from django.contrib.auth.views import LogoutView
from . import views

urlpatterns = [
    path("", views.SmartLoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),

    path("register/", views.register, name="register"),
    path("admin/register/", views.admin_register, name="admin_register"),

    path("admin/requests/", views.admin_request, name="admin_request"),
    path("admin/approve/<int:pk>/", views.approve_admin, name="approve_admin"),
    path("admin/reject/<int:pk>/", views.reject_admin, name="reject_admin"),

    path("admin/users/", views.user_management, name="user_management"),
    path("admin/users/<int:pk>/edit/", views.edit_user, name="edit_user"),
    path("admin/users/<int:pk>/password/", views.change_user_password, name="change_user_password"),
    path("admin/users/<int:pk>/delete/", views.delete_user, name="delete_user"),

    path("dashboard/", views.dashboard, name="dashboard"),
    path("driver/", views.driver_home, name="driver_home"),

    path("slots/", views.parking_slot, name="parking_slot"),
    path("slots/<int:pk>/status/", views.slot_status, name="slot_status"),
    path("slots/<int:pk>/delete/", views.delete_slot, name="delete_slot"),

    path("zones/add/", views.add_zone, name="add_zone"),
    path("zones/<int:pk>/toggle/", views.toggle_zone, name="toggle_zone"),

    path("api/slot-map/", views.slot_map_data, name="slot_map_data"),

    path("allocate/", views.allocate_slot, name="allocate_slot"),
    path("release/<int:pk>/", views.release_slot, name="release_slot"),

    path("vehicles/", views.vehicles, name="vehicles"),
    path("vehicles/add/", views.vehicle_add, name="vehicle_add"),
    path("vehicles/<int:pk>/edit/", views.vehicle_edit, name="vehicle_edit"),
    path("vehicles/<int:pk>/delete/", views.vehicle_delete, name="vehicle_delete"),

    # RESERVATION URLS
    path("reservation/", views.reservation_page, name="reservation"),
    path("reservations/create/", views.reservation_create, name="reservation_create"),
    path("reservations/<int:pk>/approve/", views.reservation_approve, name="reservation_approve"),
    path("reservations/<int:pk>/reject/", views.reservation_reject, name="reservation_reject"),
    path("reservations/<int:pk>/cancel/", views.reservation_cancel, name="reservation_cancel"),
    path("reservations/<int:pk>/complete/", views.reservation_complete, name="reservation_complete"),

    path("notifications/data/", views.notifications_data, name="notifications_data"),
    path("allocation-history/", views.allocation_history, name="allocation_history"),
    path('my-history/', views.my_history, name='my_history'),
    path("payments/", views.payments, name="payments"),
    path("payments/pay/<int:pk>/", views.make_payment, name="make_payment"),
    path("payments/<int:pk>/confirm/", views.payment_confirm, name="payment_confirm"),
    path("payments/<int:pk>/reject/", views.payment_reject, name="payment_reject"),
    path("settings/", views.settings_page, name="settings"),
    path("reservations/<int:pk>/checkin/", views.reservation_checkin, name="reservation_checkin"),
    
    
]
