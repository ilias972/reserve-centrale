"""Services d'authentification ; jamais de mot de passe dans l'audit/idempotence."""

import secrets
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import login, logout
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext

from apps.core.commands import Effect
from apps.core.errors import BusinessError
from apps.core.idempotency import finish, record_facts
from apps.core.models import Operation, OperationIntent
from apps.core.payloads import invalid_input, payload_hash
from apps.core.transactions import configure_transaction

from .models import LoginThrottle, User, UserSession
from .operations import temporary_access, user_data
from .sessions import checked_session, revoke_all

DUMMY_PASSWORD_HASH = make_password(secrets.token_urlsafe(24))


def record_action(actor, kind, *, before=None, after=None, resource_id=None, correlation_id=None):
    """Déjà dans la transaction du service ; mêmes faits/contraintes SQL que M0."""
    now = timezone.now()
    intent = OperationIntent.objects.create(
        actor=actor,
        command_type=kind,
        resource_id=resource_id,
        idempotency_key=uuid4().hex,
        payload_hash=payload_hash({}),
        generation_id=settings.OPERATION_GENERATION,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.OPERATION_INTENT_TTL_SECONDS),
    )
    operation = Operation.objects.create(
        intent=intent,
        actor=actor,
        actor_role=actor.role,
        command_type=kind,
        resource_id=resource_id,
        confirmed_at=now,
        correlation_id=correlation_id or uuid4(),
    )
    record_facts(
        operation,
        Effect(
            resource={"id": str(resource_id or actor.pk)}, before=before or {}, after=after or {}
        ),
    )
    finish(
        intent,
        "CONFIRMED",
        {"operation_id": str(operation.pk), "intent_id": str(intent.pk), "result": "CONFIRMED"},
        200,
    )
    return operation


def credential_values(identifier, password):
    if (
        not isinstance(identifier, str)
        or not 1 <= len(identifier) <= 150
        or not isinstance(password, str)
        or not 1 <= len(password) <= 256
    ):
        raise invalid_input()
    return identifier.strip().casefold(), password


def throttle_keys(request, identifier):
    # Pas de confiance X-Forwarded-For sans proxy institutionnel validé.
    return sorted(
        salted_hmac("reserve.login", value).hexdigest()
        for value in ["id:" + identifier, "ip:" + request.META.get("REMOTE_ADDR", "unknown")]
    )


def locked_throttles(request, identifier):
    now = timezone.now()
    result = []
    for key in throttle_keys(request, identifier):
        item, _ = LoginThrottle.objects.get_or_create(key=key, defaults={"window_started_at": now})
        item = LoginThrottle.objects.select_for_update().get(pk=item.pk)
        if (now - item.window_started_at).total_seconds() >= settings.LOGIN_WINDOW_SECONDS:
            item.failures, item.blocked_until, item.window_started_at = 0, None, now
            item.save()
        result.append(item)
    return result


def note_failure(items):
    now = timezone.now()
    for item in items:
        item.failures += 1
        if item.failures >= settings.LOGIN_FAILURE_LIMIT:
            item.blocked_until = now + timedelta(
                seconds=min(300, 2 ** min(8, item.failures - settings.LOGIN_FAILURE_LIMIT))
            )
        item.save()


def sign_in(request, identifier, password, *, persistent=False):
    identifier, password = credential_values(identifier, password)
    if type(persistent) is not bool:
        raise invalid_input()
    error = None
    with transaction.atomic(durable=True):
        configure_transaction()
        throttles = locked_throttles(request, identifier)
        now = timezone.now()
        if any(item.blocked_until and item.blocked_until > now for item in throttles):
            error = BusinessError(
                "LOGIN_TEMPORARILY_LIMITED",
                gettext("Trop d'essais. Patientez avant de réessayer."),
                status=429,
            )
        else:
            target_id = (
                User.objects.filter(username=identifier).values_list("pk", flat=True).first()
            )
            account_ids = [target_id]
            if request.user.is_authenticated:
                account_ids.append(request.user.pk)
            list(User.objects.select_for_update().filter(pk__in=account_ids).order_by("pk"))
            user = User.objects.filter(pk=target_id, username=identifier).first()
            # Même famille de coût pour un identifiant inconnu ; aucun setter
            # automatique ne sauvegarde un ancien hash hors transaction.
            valid = check_password(password, user.password if user else DUMMY_PASSWORD_HASH)
            if (
                not user
                or not valid
                or not user.is_active
                or (
                    user.must_change_password
                    and (not user.temporary_expires_at or user.temporary_expires_at <= now)
                )
            ):
                note_failure(throttles)
                error = BusinessError(
                    "INVALID_CREDENTIALS",
                    gettext("Identifiant ou mot de passe invalide, ou accès indisponible."),
                    status=401,
                )
            else:
                for item in throttles:
                    # Une connexion ne retire pas les échecs d'autres comptes de
                    # l'origine. Seul le compteur de cet identifiant est remis à zéro.
                    id_key = salted_hmac("reserve.login", "id:" + identifier).hexdigest()
                    if item.pk == id_key:
                        item.failures, item.blocked_until = 0, None
                        item.save()
                request.persistent_login = persistent
                if request.user.is_authenticated:
                    UserSession.objects.filter(
                        pk=getattr(request, "user_session_id", None), revoked_at__isnull=True
                    ).update(revoked_at=now)
                # Django conserve sinon la clé lors d'un second login du même
                # utilisateur : rotation explicite et ancienne métadonnée révoquée.
                request.session.flush()
                login(request, user, backend="django.contrib.auth.backends.ModelBackend")
                record_action(
                    user,
                    "auth.login",
                    after={"session_id": str(request.user_session_id)},
                    correlation_id=request.correlation_id,
                )
    if error:
        raise error
    return user


def locked_current(request):
    user = User.objects.select_for_update().get(pk=request.user.pk)
    meta = checked_session(user, request.user_session_id, key=request.session.session_key)
    return user, meta


def change_password(request, current_password, new_password):
    if (
        not isinstance(current_password, str)
        or not isinstance(new_password, str)
        or len(current_password) > 256
        or not 15 <= len(new_password) <= 256
    ):
        raise BusinessError(
            "PASSWORD_INVALID",
            gettext("Choisissez un mot de passe de 15 à 256 caractères."),
            status=400,
        )
    error = None
    with transaction.atomic(durable=True):
        configure_transaction()
        limits = locked_throttles(request, request.user.username)
        user, meta = locked_current(request)
        if any(item.blocked_until and item.blocked_until > timezone.now() for item in limits):
            error = BusinessError(
                "LOGIN_TEMPORARILY_LIMITED", gettext("Patientez avant de réessayer."), status=429
            )
        elif not check_password(current_password, user.password) or (
            user.must_change_password
            and (not user.temporary_expires_at or user.temporary_expires_at <= timezone.now())
        ):
            note_failure(limits)
            error = BusinessError(
                "INVALID_CREDENTIALS",
                gettext("Mot de passe actuel invalide ou expiré."),
                status=400,
            )
        else:
            try:
                validate_password(new_password, user)
            except ValidationError as exc:
                raise BusinessError("PASSWORD_INVALID", " ".join(exc.messages), status=400) from exc
            if check_password(new_password, user.password):
                raise BusinessError(
                    "PASSWORD_UNCHANGED",
                    gettext("Choisissez un mot de passe différent du temporaire ou de l'ancien."),
                    status=400,
                )
            user.set_password(new_password)
            user.must_change_password = False
            user.temporary_expires_at = None
            user.auth_version += 1
            user.version += 1
            user.save()
            revoke_all(user)
            request.session.flush()
            request.persistent_login = meta.persistent
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            record_action(
                user,
                "auth.change_access",
                after={"change_required": False},
                correlation_id=request.correlation_id,
            )
    if error:
        raise error
    return user


def reauthenticate(request, password):
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise invalid_input()
    error = None
    with transaction.atomic(durable=True):
        configure_transaction()
        # Même ordre que login : limiteurs, puis utilisateur.
        limits = locked_throttles(request, request.user.username)
        user, meta = locked_current(request)
        if any(t.blocked_until and t.blocked_until > timezone.now() for t in limits):
            error = BusinessError(
                "LOGIN_TEMPORARILY_LIMITED", gettext("Patientez avant de réessayer."), status=429
            )
        elif not check_password(password, user.password):
            note_failure(limits)
            error = BusinessError(
                "INVALID_CREDENTIALS", gettext("Mot de passe invalide."), status=400
            )
        else:
            meta.authenticated_at = timezone.now()
            meta.save(update_fields=["authenticated_at"])
            record_action(user, "auth.reauthenticate", correlation_id=request.correlation_id)
    if error:
        raise error


def sign_out(request):
    with transaction.atomic(durable=True):
        configure_transaction()
        user, meta = locked_current(request)
        meta.revoked_at = timezone.now()
        meta.save(update_fields=["revoked_at"])
        record_action(
            user,
            "auth.logout",
            after={"session_id": str(meta.pk)},
            correlation_id=request.correlation_id,
        )
        logout(request)


def bootstrap_admin(*, username, display_name, replace=None, reason=None):
    from .models import AccountGuard
    from .operations import validated_identity

    values = validated_identity(
        {"username": username, "display_name": display_name, "role": "ADMIN"}
    )
    with transaction.atomic(durable=True):
        configure_transaction()
        guard = AccountGuard.objects.select_for_update().get(pk=1)
        old = None
        old_before = {}
        if guard.initialized:
            if replace is None or not reason or len(reason.strip()) < 10:
                raise BusinessError(
                    "CONTROLLED_REPLACEMENT_REQUIRED",
                    gettext("Remplacement : ancien UUID et motif explicite requis."),
                )
            old = User.objects.select_for_update().get(pk=replace, role="ADMIN", is_active=True)
            old_before = user_data(old)
            old.is_active = False
            old.auth_version += 1
            old.version += 1
            old.save()
            revoke_all(old)
        elif User.objects.filter(role="ADMIN", is_active=True).exists() or replace is not None:
            raise BusinessError(
                "ADMIN_ALREADY_EXISTS",
                gettext("Accès Gestionnaire existant : vérification informatique requise."),
            )
        user = User(**values)
        temporary = temporary_access(user)
        user.save(force_insert=True)
        guard.initialized = True
        guard.save(update_fields=["initialized"])
        record_action(
            user,
            "accounts.replace_admin" if old else "accounts.initialize_admin",
            before=old_before,
            after=user_data(user),
            resource_id=user.pk,
        )
        if old:
            # Motif contrôlé, sans donnée d'identité réelle dans le prototype.
            record_action(
                user,
                "accounts.replacement_reason",
                after={"reason": reason.strip(), "previous_admin_id": str(old.pk)},
            )
    return user, temporary
