"""Capture Django locale et transactionnelle ; aucun socket ni fichier public."""

from django.core.mail.backends.base import BaseEmailBackend
from django.db import connection

from .models import CapturedEmail, WorkItem


class CaptureBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        if not connection.in_atomic_block:
            raise RuntimeError("La capture doit partager la transaction de résultat du worker.")
        count = 0
        for message in email_messages:
            work = WorkItem.objects.get(pk=message.extra_headers["X-Reserve-Work"])
            if work.kind != "EMAIL" or work.mode != "CAPTURE" or message.to != [work.email_to]:
                raise ValueError("Capture incompatible avec la tâche de notification.")
            CapturedEmail.objects.get_or_create(
                work=work,
                defaults={
                    "message_id": message.extra_headers["Message-ID"],
                    "recipient": work.email_to,
                    "subject": message.subject,
                    "body": message.body,
                },
            )
            count += 1
        return count
