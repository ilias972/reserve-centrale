import threading

from django.db import connection

from apps.accounts.models import User
from apps.core.commands import Command, Effect
from apps.core.errors import BusinessError
from apps.core.policies import check_version

from .models import Counter, RejectionRecord

entered = threading.Event()
release = threading.Event()
committed = threading.Event()
backend_ids = []
observations = {}


def authorize(actor, resource_id):
    if not Counter.objects.filter(pk=resource_id, owner=actor).exists():
        raise BusinessError("NOT_FOUND", "Objet technique introuvable.", status=404)


def lock_resources(actor, resource_id):
    return Counter.objects.select_for_update().get(pk=resource_id, owner=actor)


def apply(execution, counter, payload):
    check_version(counter.version, payload.get("expected_version"))
    delta = payload.get("delta")
    if type(delta) is not int:
        raise BusinessError("INVALID_INPUT", "Quantité technique invalide.", status=422)
    before = {"value": counter.value, "version": counter.version}
    counter.value += delta
    counter.version += 1
    counter.save(update_fields=["value", "version"])
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        backend_ids.append(cursor.fetchone()[0])
    if payload.get("pause"):
        entered.set()
        if not release.wait(timeout=10):
            raise TimeoutError("Barrière de test non libérée.")
    if payload.get("fail") == "business":
        raise BusinessError("DELIBERATE_REJECTION", "Refus métier de test.")
    if payload.get("fail") == "technical":
        raise RuntimeError("detail-interne-ne-doit-pas-apparaitre")
    if payload.get("fail") == "database":
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 / 0")
    after = {"value": counter.value, "version": counter.version}
    return Effect(resource={"id": str(counter.pk), **after}, before=before, after=after)


def record_rejection(execution, resources, error):
    if error.code == "DELIBERATE_REJECTION":
        record = RejectionRecord.objects.create(intent_id=execution.intent.pk, code=error.code)
        return {"incident_recorded": True, "incident_id": record.pk}
    return {}


COMMAND = Command(
    "m0.test.counter",
    frozenset({User.Role.REPLENISHER}),
    authorize,
    lock_resources,
    apply,
    record_rejection=record_rejection,
)
