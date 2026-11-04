from django.urls import path

from . import web_views

urlpatterns = [
    path("", web_views.home, name="web_home"),
    path("login/", web_views.login_page, name="web_login"),
    path("register/", web_views.register_page, name="web_register"),
    path("logout/", web_views.logout_page, name="web_logout"),
    path("pets/", web_views.pets_list, name="web_pets"),
    path("pets/new/", web_views.pet_create, name="web_pet_create"),
    path("pets/<int:pk>/edit/", web_views.pet_edit, name="web_pet_edit"),
    path("pets/<int:pk>/delete/", web_views.pet_delete, name="web_pet_delete"),
    path("pets/<int:pk>/feed/", web_views.manual_feed, name="web_manual_feed"),
    path("feeding/", web_views.feeding_page, name="web_feeding"),
    path("schedule-events/", web_views.schedule_events_page, name="web_schedule_events"),
    path("history/", web_views.history_page, name="web_history"),
    path("cameras/", web_views.cameras_page, name="web_cameras"),
    path("device/", web_views.device_page, name="web_device"),
    path("device/calibrate/", web_views.calibrate_servo_page, name="web_calibrate_servo"),
    path("device/test/", web_views.test_connection_page, name="web_test_connection"),
    path("device/cleaning/", web_views.dispenser_cleaning_page, name="web_dispenser_cleaning"),
    path('device/control/', web_views.dispenser_control_view, name='web_dispenser_control'),
    path("notifications/", web_views.notifications_page, name="web_notifications"),
    path("profile/", web_views.profile_page, name="web_profile"),
]
