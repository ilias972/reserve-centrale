"""Mêmes contrôles de demande pour les profils assistante et médecin."""

from pathlib import Path
from django.db import migrations

ROOT = Path(__file__).resolve().parent


def guard_reverse(apps, schema_editor):
    # Une rétrogradation ne doit pas invalider des faits historiques de médecin.
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM core_operation WHERE actor_role='DOCTOR') OR EXISTS(SELECT 1 FROM accounts_user WHERE role='DOCTOR')"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError(
                "Des comptes ou opérations médecin existent. Retour arrière refusé sans procédure de reprise vérifiée."
            )


class Migration(migrations.Migration):
    dependencies = [
        ("requests", "0010_m6_integrity"),
        ("accounts", "0004_remove_user_user_valid_role_floor_alter_user_role_and_more"),
    ]
    operations = [
        migrations.RunSQL(
            (ROOT / "doctor_guards_after.sql").read_text(),
            (ROOT / "doctor_guards_before.sql").read_text(),
        ),
        migrations.RunPython(migrations.RunPython.noop, guard_reverse),
    ]
