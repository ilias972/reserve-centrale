"""Stockage Django privé remplaçable, sans URL publique ni fournisseur connecté."""

from pathlib import Path

from django.core.files.storage import FileSystemStorage, storages


class PrivatePhotoStorage(FileSystemStorage):
    def __init__(self, **kwargs):
        super().__init__(file_permissions_mode=0o600, directory_permissions_mode=0o700, **kwargs)

    def url(self, name):
        raise ValueError("Photographie privée : utilisez le contrôleur autorisé.")

    def _save(self, name, content):
        root = Path(self.location)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        marker = root / ".inventory-app-private-photos"
        if not marker.exists():
            if any(root.iterdir()) and not marker.exists():
                raise OSError("Stockage privé non vide et non identifié : configuration requise.")
            try:
                with marker.open("x") as stream:
                    stream.write("Private product photo storage\n")
                marker.chmod(0o600)
            except FileExistsError:
                pass
        root.chmod(0o700)
        return super()._save(name, content)


def photo_storage():
    return storages["product_photos"]
