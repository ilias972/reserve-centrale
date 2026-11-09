import os

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from psycopg import sql


def grant_runtime(role):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        migration_owner = cursor.fetchone()[0]
        if migration_owner == role:
            raise CommandError("Le rôle runtime doit être distinct du propriétaire des migrations.")
        cursor.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=%s", [role]
        )
        privileges = cursor.fetchone()
        if privileges is None or any(privileges):
            raise CommandError("Le rôle runtime doit exister sans privilèges d'administration.")
        cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cursor.execute(
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(sql.Identifier(role))
        )
        cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        cursor.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE')", [role])
        if cursor.fetchone()[0]:
            raise CommandError(
                "Le rôle runtime peut encore créer dans public. Un administrateur PostgreSQL "
                "doit révoquer CREATE de PUBLIC avant de configurer ce rôle."
            )
        cursor.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION public.reserve_lock_actor(uuid) TO {}").format(
                sql.Identifier(role)
            )
        )
        for table, permissions in {
            "django_migrations": "SELECT",
            "accounts_user": "SELECT, INSERT, UPDATE",
            "accounts_accountguard": "SELECT",
            "accounts_usersession": "SELECT, INSERT, UPDATE",
            "accounts_loginthrottle": "SELECT, INSERT, UPDATE, DELETE",
            "catalog_floor": "SELECT, INSERT, UPDATE",
            "catalog_category": "SELECT, INSERT, UPDATE",
            "catalog_supplier": "SELECT, INSERT, UPDATE",
            "catalog_product": "SELECT, INSERT",
            "catalog_floorproduct": "SELECT, INSERT, UPDATE",
            "catalog_productsupplier": "SELECT, INSERT, UPDATE",
            "catalog_photoasset": "SELECT, INSERT",
            "catalog_productphoto": "SELECT, INSERT",
            "inventory_stockbalance": "SELECT, INSERT, UPDATE",
            "inventory_stockentry": "SELECT, INSERT",
            "inventory_inventorybatch": "SELECT, INSERT",
            "inventory_inventoryline": "SELECT, INSERT",
            "cases_stockcase": "SELECT, INSERT",
            "cases_stockcaseproduct": "SELECT, INSERT",
            "cases_stockhold": "SELECT, INSERT, DELETE",
            "cases_case": "SELECT, INSERT, UPDATE",
            "cases_caseevent": "SELECT, INSERT",
            "cases_stockreconciliation": "SELECT, INSERT",
            "cases_reconciledstock": "SELECT, INSERT",
            "requests_requestdecision": "SELECT, INSERT",
            "requests_decisionline": "SELECT, INSERT",
            "requests_deliverycorrection": "SELECT, INSERT",
            "requests_deliverycorrectionline": "SELECT, INSERT",
            "requests_internalrequest": "SELECT, INSERT, UPDATE",
            "requests_requestline": "SELECT, INSERT, UPDATE",
            "requests_reservation": "SELECT, INSERT, UPDATE",
            "requests_requestevent": "SELECT, INSERT",
            "requests_reservationchange": "SELECT, INSERT",
            "requests_delivery": "SELECT, INSERT",
            "requests_deliveryline": "SELECT, INSERT",
            "requests_deliveryissue": "SELECT, INSERT",
            "purchasing_recommendation": "SELECT, INSERT, UPDATE",
            "purchasing_recommendationevent": "SELECT, INSERT",
            "purchasing_purchaseorder": "SELECT, INSERT, UPDATE",
            "purchasing_orderline": "SELECT, INSERT, UPDATE",
            "purchasing_remainderevent": "SELECT, INSERT",
            "purchasing_remainderline": "SELECT, INSERT",
            "purchasing_duplicateacknowledgement": "SELECT, INSERT",
            "receiving_orderevent": "SELECT, INSERT",
            "receiving_receipt": "SELECT, INSERT",
            "receiving_receiptline": "SELECT, INSERT",
            "receiving_arrivalreport": "SELECT, INSERT",
            "receiving_reportline": "SELECT, INSERT",
            "receiving_unknownlink": "SELECT, INSERT",
            "receiving_unknownlinkline": "SELECT, INSERT",
            "receiving_quarantineitem": "SELECT, INSERT",
            "receiving_quarantinedecision": "SELECT, INSERT",
            "receiving_sourceconsumption": "SELECT, INSERT",
            "receiving_reinstatement": "SELECT, INSERT",
            "receiving_reinstatementline": "SELECT, INSERT",
            "receiving_receiptcorrection": "SELECT, INSERT",
            "receiving_receiptcorrectionline": "SELECT, INSERT",
            "receiving_manualreminder": "SELECT, INSERT",
            "django_session": "SELECT, INSERT, UPDATE, DELETE",
            "core_operationintent": "SELECT, INSERT, UPDATE",
            "core_operation": "SELECT, INSERT",
            "audit_auditevent": "SELECT, INSERT",
            "notifications_businessevent": "SELECT, INSERT",
            "notifications_outbox": "SELECT, INSERT",
            "notifications_activation": "SELECT, INSERT",
            "notifications_notification": "SELECT, INSERT, UPDATE",
            "notifications_pushsubscription": "SELECT, INSERT, UPDATE",
            "notifications_workitem": "SELECT, INSERT, UPDATE",
            "notifications_attempt": "SELECT, INSERT",
            "notifications_attemptresult": "SELECT, INSERT",
            "notifications_capturedemail": "SELECT, INSERT",
            "notifications_productalertstate": "SELECT, INSERT, UPDATE",
        }.items():
            cursor.execute(
                sql.SQL("REVOKE ALL ON TABLE {} FROM {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
            cursor.execute(
                sql.SQL("GRANT {} ON TABLE {} TO {}").format(
                    sql.SQL(permissions), sql.Identifier(table), sql.Identifier(role)
                )
            )
        cursor.execute(
            sql.SQL("GRANT UPDATE (version) ON public.receiving_receipt TO {}").format(
                sql.Identifier(role)
            )
        )
        # PostgreSQL exige un privilège UPDATE pour SELECT FOR UPDATE.
        # Ces colonnes restent immuables via les triggers ; aucun droit de réécriture.
        for table in ["receiving_arrivalreport", "receiving_quarantineitem"]:
            cursor.execute(
                sql.SQL("GRANT UPDATE (id) ON TABLE {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
        for sequence in ["receipt_reference_seq", "arrival_reference_seq"]:
            cursor.execute(
                sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(
                    sql.Identifier(sequence), sql.Identifier(role)
                )
            )
        cursor.execute(
            sql.SQL("GRANT UPDATE (id) ON public.requests_delivery TO {}").format(
                sql.Identifier(role)
            )
        )
        cursor.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE public.request_reference_seq TO {}").format(
                sql.Identifier(role)
            )
        )
        cursor.execute(
            sql.SQL("GRANT USAGE ON SEQUENCE public.purchase_reference_seq TO {}").format(
                sql.Identifier(role)
            )
        )
        cursor.execute(
            sql.SQL("GRANT UPDATE (id) ON public.accounts_accountguard TO {}").format(
                sql.Identifier(role)
            )
        )
        cursor.execute(
            sql.SQL("GRANT UPDATE (id) ON public.catalog_photoasset TO {}").format(
                sql.Identifier(role)
            )
        )
        cursor.execute(
            sql.SQL(
                "GRANT UPDATE (name, category_id, unit, minimum, target, status, current_photo_id, activation_operation_id, version, updated_at, last_operation_id, counted_at, initial_inventory_operation_id) ON public.catalog_product TO {}"
            ).format(sql.Identifier(role))
        )


class Command(BaseCommand):
    help = "Accorde les droits SQL M0–M9 ; aucun DELETE des comptes, de l'audit ou des sessions historiques."

    def handle(self, *args, **options):
        role = os.environ.get("RUNTIME_DATABASE_USER")
        if not role:
            raise CommandError("RUNTIME_DATABASE_USER requis.")
        grant_runtime(role)
        self.stdout.write(
            "Droits runtime M0–M9 appliqués à la base configurée ; aucun compte créé."
        )
