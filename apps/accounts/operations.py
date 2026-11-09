"""Commandes de compte exécutées par le noyau transactionnel M0."""

import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from django.utils.translation import gettext

from apps.catalog.models import Floor
from apps.core import commands
from apps.core.commands import Command, Effect
from apps.core.errors import BusinessError
from apps.core.payloads import invalid_input, parse_uuid
from apps.core.policies import check_version

from .models import REQUESTER_ROLES, AccountGuard, User, UserSession
from .sessions import checked_session, revoke_all

ALL_ROLES = frozenset(User.Role.values)
ADMIN = frozenset({User.Role.ADMIN})


def user_data(user):
    return {
        "id": str(user.pk),
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "floor_id": str(user.floor_id) if user.floor_id else None,
        "is_active": user.is_active,
        "version": user.version,
        "change_required": user.must_change_password,
    }


def target_user(actor, resource_id):
    if resource_id is None or not User.objects.filter(pk=resource_id).exists():
        raise BusinessError("NOT_FOUND", gettext("Compte introuvable."), status=404)


def create_scope(actor, resource_id):
    if resource_id is not None:
        raise invalid_input()


def session_scope(actor, resource_id):
    sessions = UserSession.objects.filter(pk=resource_id)
    if actor.role != User.Role.ADMIN:
        sessions = sessions.filter(user=actor)
    if not sessions.exists():
        raise BusinessError("NOT_FOUND", gettext("Session introuvable."), status=404)


def lock_accounts(context, resource_id):
    # Tous les changements de permissions suivent cet ordre : garde unique,
    # utilisateurs par UUID, intention, sessions. Aucun acteur partagé acquis avant.
    AccountGuard.objects.select_for_update().get(pk=1)
    list(
        User.objects.select_for_update()
        .filter(pk__in=[context.actor_id, resource_id])
        .order_by("pk")
    )


def lock_session_accounts(context, resource_id):
    target = UserSession.objects.filter(pk=resource_id).values_list("user_id", flat=True).first()
    lock_accounts(context, target)


def no_resources(actor, resource_id):
    return None


def user_resource(actor, resource_id):
    return User.objects.get(pk=resource_id)


def recent_admin(execution):
    context = execution.context
    if context is None or context.session_id is None:
        raise BusinessError(
            "REAUTHENTICATION_REQUIRED",
            gettext("Confirmez votre mot de passe avant de gérer les accès."),
            status=403,
        )
    meta = checked_session(execution.actor, context.session_id)
    if (
        timezone.now() - meta.authenticated_at
    ).total_seconds() > settings.ADMIN_RECENT_AUTH_SECONDS:
        raise BusinessError(
            "REAUTHENTICATION_REQUIRED",
            gettext("Confirmez à nouveau votre mot de passe."),
            status=403,
        )


def fields(payload, allowed):
    if not isinstance(payload, dict) or set(payload) - set(allowed):
        raise invalid_input()


def validated_identity(payload, current=None):
    username = payload.get("username", current.username if current else None)
    name = payload.get("display_name", current.display_name if current else None)
    role = payload.get("role", current.role if current else None)
    floor_id = payload.get(
        "floor_id", str(current.floor_id) if current and current.floor_id else None
    )
    active = payload.get("is_active", current.is_active if current else True)
    if not isinstance(username, str) or not re.fullmatch(
        r"[a-zA-Z0-9_.@+-]{1,150}", username.strip()
    ):
        raise invalid_input()
    if (
        not isinstance(name, str)
        or not 1 <= len(name.strip()) <= 150
        or role not in User.Role.values
        or type(active) is not bool
    ):
        raise invalid_input()
    floor = None
    if role in REQUESTER_ROLES:
        # M2 peut désormais désactiver un unité : sérialiser une nouvelle
        # affectation avec cette configuration, sans perdre les affectations existantes.
        floor = Floor.objects.select_for_update().filter(pk=parse_uuid(floor_id)).first()
        if floor is None or (
            not floor.active and (current is None or current.floor_id != floor.pk)
        ):
            raise BusinessError(
                "FLOOR_UNAVAILABLE", gettext("Sélectionnez un unité actif."), status=400
            )
    elif floor_id is not None:
        raise BusinessError(
            "INVALID_FLOOR",
            gettext(
                "Seuls les comptes demandeur — profil A et demandeur — profil B sont affectés à un unité."
            ),
            status=400,
        )
    return dict(
        username=username.strip().casefold(),
        display_name=name.strip(),
        role=role,
        floor=floor,
        is_active=active,
    )


def temporary_access(user):
    temporary = secrets.token_urlsafe(24)
    user.set_password(temporary)
    user.must_change_password = True
    user.temporary_expires_at = timezone.now() + timedelta(
        seconds=settings.TEMPORARY_PASSWORD_SECONDS
    )
    return temporary


def create_user(execution, resources, payload):
    recent_admin(execution)
    fields(payload, {"username", "display_name", "role", "floor_id"})
    values = validated_identity(payload)
    if values["role"] == User.Role.ADMIN:
        raise BusinessError(
            "ADMIN_REPLACEMENT_CONTROLLED",
            gettext("Le remplacement de la gestionnaire suit la procédure informatique contrôlée."),
        )
    user = User(**values)
    temporary = temporary_access(user)
    try:
        user.save(force_insert=True)
    except IntegrityError as exc:
        raise BusinessError("USERNAME_CONFLICT", gettext("Cet identifiant existe déjà.")) from exc
    return Effect(
        user_data(user),
        after=user_data(user),
        http_status=201,
        once={"temporary_password": temporary},
    )


def update_user(execution, user, payload):
    recent_admin(execution)
    fields(payload, {"expected_version", "display_name", "role", "floor_id", "is_active"})
    check_version(user.version, payload.get("expected_version"))
    values = validated_identity(payload, user)
    if user.role == User.Role.ADMIN and not user.is_active and values["is_active"]:
        raise BusinessError(
            "ADMIN_REPLACEMENT_CONTROLLED",
            gettext(
                "Une ancienne Gestionnaire ne peut être réactivée par cette API. Utilisez la procédure informatique contrôlée."
            ),
        )
    if (
        user.role == User.Role.ADMIN
        and (values["role"] != User.Role.ADMIN or not values["is_active"])
    ) or (user.role != User.Role.ADMIN and values["role"] == User.Role.ADMIN):
        raise BusinessError(
            "LAST_ADMIN_PROTECTED",
            gettext(
                "L'accès Gestionnaire est protégé. Utilisez la procédure informatique de remplacement."
            ),
        )
    before = user_data(user)
    changed_access = any(
        getattr(user, field) != values[field] for field in ("role", "floor", "is_active")
    )
    for field, value in values.items():
        setattr(user, field, value)
    user.version += 1
    if changed_access:
        user.auth_version += 1
        revoke_all(user)
    user.save()
    return Effect(user_data(user), before=before, after=user_data(user))


def reset_access(execution, user, payload):
    recent_admin(execution)
    fields(payload, {"expected_version", "identity_verified"})
    check_version(user.version, payload.get("expected_version"))
    if payload.get("identity_verified") is not True:
        raise BusinessError(
            "IDENTITY_VERIFICATION_REQUIRED",
            gettext("Vérifiez l'identité puis confirmez cette vérification."),
            status=400,
        )
    before = user_data(user)
    temporary = temporary_access(user)
    user.auth_version += 1
    user.version += 1
    revoke_all(user)
    user.save()
    return Effect(
        user_data(user),
        before=before,
        after=user_data(user),
        details=[{"identity_verified": True}],
        once={"temporary_password": temporary},
    )


def revoke_session(execution, resources, payload):
    fields(payload, set())
    meta = UserSession.objects.get(pk=execution.intent.resource_id)
    if execution.actor.pk != meta.user_id:
        recent_admin(execution)
    if meta.revoked_at is None:
        meta.revoked_at = timezone.now()
        meta.save(update_fields=["revoked_at"])
    return Effect(
        {"id": str(meta.pk), "revoked": True}, after={"session_id": str(meta.pk), "revoked": True}
    )


COMMANDS = (
    Command(
        "accounts.create",
        ADMIN,
        create_scope,
        no_resources,
        create_user,
        lock_accounts=lock_accounts,
    ),
    Command(
        "accounts.update",
        ADMIN,
        target_user,
        user_resource,
        update_user,
        lock_accounts=lock_accounts,
    ),
    Command(
        "accounts.reset",
        ADMIN,
        target_user,
        user_resource,
        reset_access,
        lock_accounts=lock_accounts,
    ),
    Command(
        "accounts.revoke_session",
        ALL_ROLES,
        session_scope,
        no_resources,
        revoke_session,
        lock_accounts=lock_session_accounts,
    ),
)


def ensure_registered():
    for command in COMMANDS:
        if command.name not in commands._registry:
            commands.register_command(command)
