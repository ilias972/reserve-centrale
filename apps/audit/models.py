import uuid

from django.db import models


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.OneToOneField("core.Operation", on_delete=models.PROTECT)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    details = models.JSONField(default=list)

    # Auteur, rôle, action et date sont immuables et accessibles par operation.
