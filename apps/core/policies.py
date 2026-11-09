from dataclasses import dataclass
from uuid import UUID

from django.db import connection
from django.utils.translation import gettext

from apps.accounts.models import User

from .errors import BusinessError


@dataclass(frozen=True)
class ActorContext:
    actor_id: UUID
    auth_version: int
    session_id: UUID | None = None


def lock_actor(context):
    """Un verrou partagé autorise 20 confirmations réellement concurrentes du même acteur."""
    with connection.cursor() as cursor:
        # Fonction SQL bornée : prend uniquement le verrou, sans donner UPDATE
        # sur les comptes au rôle runtime (FOR SHARE requiert ce privilège).
        cursor.execute("SELECT public.reserve_lock_actor(%s)", [context.actor_id])
        found = cursor.fetchone()[0]
    if not found:
        raise BusinessError("AUTHENTICATION_REQUIRED", gettext("Connexion requise."), status=401)
    actor = User.objects.get(pk=context.actor_id)
    if not actor.is_active or actor.auth_version != context.auth_version:
        raise BusinessError(
            "SESSION_REVOKED", gettext("Votre accès n'est plus valide."), status=401
        )
    if context.session_id is not None:
        from apps.accounts.sessions import checked_session

        checked_session(actor, context.session_id)
    if actor.must_change_password:
        raise BusinessError(
            "PASSWORD_CHANGE_REQUIRED", gettext("Changez votre mot de passe."), status=403
        )
    return actor


def check_role(actor, roles):
    if actor.role not in roles:
        raise BusinessError("PERMISSION_DENIED", gettext("Action non autorisée."), status=403)


def check_version(actual, expected):
    if type(expected) is not int or expected < 0:
        raise BusinessError("INVALID_INPUT", gettext("Version reçue invalide."), status=400)
    if actual != expected:
        raise BusinessError(
            "STALE_VERSION", gettext("Les données ont changé. Vérifiez-les avant de confirmer.")
        )
