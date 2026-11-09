from django.urls import path

from . import views

urlpatterns = [
    path("notifications", views.inbox, name="notifications"),
    path("notifications/diagnostic", views.diagnostics, name="notification-diagnostics"),
    path("notifications/lire-tout", views.read, name="notifications-read-all"),
    path("notifications/<uuid:notification_id>/lire", views.read, name="notification-read"),
    path(
        "notifications/<uuid:notification_id>/ouvrir",
        views.open_notification,
        name="notification-open",
    ),
    path("api/v1/notifications/unread-count", views.count),
    path("api/v1/push-subscriptions", views.subscriptions),
    path("api/v1/push-subscriptions/<uuid:subscription_id>/revoke", views.revoke_subscription),
    path("service-worker.js", views.service_worker, name="service-worker"),
]
