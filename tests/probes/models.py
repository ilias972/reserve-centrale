"""Objets techniques exclusivement installés par config.settings.test, jamais de stock métier."""

import uuid

from django.conf import settings
from django.db import models


class Counter(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    value = models.PositiveBigIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=0)


class RejectionRecord(models.Model):
    intent_id = models.UUIDField(unique=True)
    code = models.CharField(max_length=80)
