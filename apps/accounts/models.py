"""Identités individuelles et métadonnées de sessions révocables (M1)."""

import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy

REQUESTER_ROLES = frozenset({"ASSISTANT", "DOCTOR"})


class UserManager(BaseUserManager):
    def create_user(self, username, password=None, **fields):
        user = self.model(username=username.strip().casefold(), **fields)
        if password is None:
            user.set_unusable_password()
        else:
            user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, *args, **kwargs):
        raise ValueError(
            "Aucun superadministrateur. Utilisez la procédure bootstrap_admin documentée."
        )


class User(AbstractBaseUser):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", gettext_lazy("Gestionnaire")
        REPLENISHER = "REPLENISHER", gettext_lazy("Préparateur")
        ASSISTANT = "ASSISTANT", gettext_lazy("Demandeur — profil A")
        DOCTOR = "DOCTOR", gettext_lazy("Demandeur — profil B")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = models.CharField(max_length=150, unique=True)
    display_name = models.CharField(max_length=150)
    role = models.CharField(max_length=20, choices=Role)
    floor = models.ForeignKey("catalog.Floor", on_delete=models.PROTECT, null=True, blank=True)
    is_active = models.BooleanField(default=True)
    must_change_password = models.BooleanField(default=True)
    auth_version = models.PositiveBigIntegerField(default=1)
    version = models.PositiveBigIntegerField(default=0)
    temporary_expires_at = models.DateTimeField(null=True, blank=True)
    objects = UserManager()
    USERNAME_FIELD = "username"

    @property
    def is_requester(self):
        return self.role in REQUESTER_ROLES

    class Meta:
        constraints = [
            models.UniqueConstraint(Lower("username"), name="user_username_case_insensitive"),
            models.UniqueConstraint(
                fields=["role"],
                condition=models.Q(role="ADMIN", is_active=True),
                name="one_active_admin",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(role__in=sorted(REQUESTER_ROLES), floor__isnull=False)
                    | models.Q(role__in=["ADMIN", "REPLENISHER"], floor__isnull=True)
                ),
                name="user_valid_role_floor",
            ),
            models.CheckConstraint(
                condition=models.Q(auth_version__gte=1), name="user_auth_version"
            ),
        ]


class AccountGuard(models.Model):
    """Verrou de configuration des comptes ; aucun paramètre institutionnel M2."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    initialized = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="account_guard_singleton")
        ]


class UserSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.PROTECT)
    key_digest = models.CharField(max_length=64, unique=True)
    auth_version = models.PositiveBigIntegerField()
    generation_id = models.CharField(max_length=64)
    created_at = models.DateTimeField()
    authenticated_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    idle_expires_at = models.DateTimeField()
    absolute_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    persistent = models.BooleanField(default=False)
    device_label = models.CharField(max_length=120, default="Navigateur")

    class Meta:
        indexes = [models.Index(fields=["user", "revoked_at"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(auth_version__gte=1), name="session_auth_version"
            ),
            models.CheckConstraint(
                condition=models.Q(absolute_expires_at__gt=models.F("created_at")),
                name="session_absolute_deadline",
            ),
            models.CheckConstraint(
                condition=models.Q(idle_expires_at__lte=models.F("absolute_expires_at")),
                name="session_idle_deadline",
            ),
        ]


class LoginThrottle(models.Model):
    """Empreintes HMAC uniquement, partagées entre processus ; pas d'IP en clair."""

    key = models.CharField(primary_key=True, max_length=64)
    failures = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField()
    blocked_until = models.DateTimeField(null=True)
