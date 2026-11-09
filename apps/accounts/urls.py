from django.urls import path

from . import api, views

urlpatterns = [
    path("connexion", views.login, name="login"),
    path("mot-de-passe", views.change_password, name="change-password"),
    path("deconnexion", views.logout, name="logout"),
    path("mon-compte", views.account, name="account"),
    path(
        "mon-compte/sessions/<uuid:session_id>/revoquer",
        views.revoke_session,
        name="revoke-session",
    ),
    path("administration/utilisateurs", views.users, name="users"),
    path("administration/utilisateurs/creer", views.create_user, name="create-user"),
    path("administration/utilisateurs/<uuid:user_id>", views.update_user, name="update-user"),
    path(
        "administration/utilisateurs/<uuid:user_id>/reinitialiser",
        views.reset_access,
        name="reset-access",
    ),
    path("api/v1/auth/login", api.login),
    path("api/v1/auth/change-password", api.change_password),
    path("api/v1/auth/logout", api.logout),
    path("api/v1/auth/reauthenticate", api.reauthenticate),
    path("api/v1/auth/sessions", api.sessions),
    path("api/v1/auth/sessions/<uuid:session_id>/revoke", api.revoke_session),
    path("api/v1/me", api.me),
    path("api/v1/users", api.users),
    path("api/v1/users/<uuid:user_id>", api.update_user),
    path("api/v1/users/<uuid:user_id>/reset-password", api.reset_password),
]
