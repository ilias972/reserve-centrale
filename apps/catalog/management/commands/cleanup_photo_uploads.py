"""Remove only abandoned, unreferenced local photo uploads after a safety delay."""

import os
import re
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from apps.catalog.models import PhotoAsset, ProductPhoto
from apps.catalog.storage import PrivatePhotoStorage, photo_storage


class Command(BaseCommand):
    help = "List abandoned photo uploads; deletion requires --execute --confirm-local-test."

    def add_arguments(self, parser):
        parser.add_argument("--execute", action="store_true")
        parser.add_argument("--confirm-local-test", action="store_true")
        parser.add_argument("--age-hours", type=int, default=24)

    def handle(self, *args, **options):
        if options["age_hours"] < 24:
            raise CommandError("The minimum safety delay is 24 hours.")
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            owner = cursor.fetchone()[0]
        if connection.settings_dict["HOST"] not in {
            "127.0.0.1",
            "localhost",
            "::1",
        } or owner != os.environ.get("DATABASE_USER"):
            raise CommandError("Cleanup is limited to the local database owner.")
        execute = options["execute"]
        if execute and not options["confirm_local_test"]:
            raise CommandError("Confirm the local test environment explicitly.")
        storage = photo_storage()
        if not isinstance(storage, PrivatePhotoStorage):
            raise CommandError("This command only supports local private photo storage.")
        root = Path(storage.location)
        if not root.exists():
            self.stdout.write("Simulation : aucun stockage local à nettoyer.")
            return
        if not (root / ".inventory-app-private-photos").is_file():
            raise CommandError("Storage is not identified; no files were changed.")
        cutoff = timezone.now() - timedelta(hours=options["age_hours"])
        removed_assets = 0
        removed_files = 0
        candidates = PhotoAsset.objects.filter(
            created_at__lt=cutoff,
            intent__state__in=["REJECTED", "EXPIRED"],
            productphoto__isnull=True,
        ).values_list("pk", flat=True)
        for identifier in list(candidates):
            with transaction.atomic():
                asset = PhotoAsset.objects.select_for_update().get(pk=identifier)
                if ProductPhoto.objects.filter(asset=asset).exists():
                    continue
                keys = [asset.object_key, asset.thumbnail_key]
                removed_assets += 1
                if execute:
                    asset.delete()
                    for key in keys:
                        transaction.on_commit(lambda key=key: storage.delete(key))
        known = set(PhotoAsset.objects.values_list("object_key", flat=True)) | set(
            PhotoAsset.objects.values_list("thumbnail_key", flat=True)
        )
        try:
            _, files = storage.listdir("objects")
        except FileNotFoundError:
            files = []
        for name in files:
            key = "objects/" + name
            if (
                re.fullmatch(r"[0-9a-f]{32}(?:-thumb)?\.jpg", name)
                and key not in known
                and storage.get_modified_time(key) < cutoff
            ):
                removed_files += 1
                if execute:
                    storage.delete(key)
        self.stdout.write(
            f"{'Nettoyage' if execute else 'Simulation'} : {removed_assets} téléversement(s) abandonné(s), "
            f"{removed_files} fichier(s) orphelin(s). Les photos confirmées et les téléversements prêts sont conservés."
        )
