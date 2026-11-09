"""Sauvegardes logiques chiffrées et restauration exclusivement dans une base neuve locale.

Archive de confiance créée par ce logiciel : pg_restore exécute du SQL. Ne jamais
restaurer un fichier fourni par un tiers, même si ses empreintes sont correctes.
"""

import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import django
import psycopg
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings
from django.db import connection, transaction
from psycopg import sql

from apps.catalog.models import PhotoAsset
from apps.catalog.storage import photo_storage
from apps.core.postgres_tools import binary

MAGIC = b"RESERVE-M11-1\n"
MAX_ARCHIVE_BYTES = 2 * 1024**3  # Limite locale provisoire A14, explicite sans troncature.
MAX_METADATA_BYTES = 16 * 1024**2


def archive_limit():
    value = getattr(settings, "M11_BACKUP_MAX_BYTES", MAX_ARCHIVE_BYTES)
    if not isinstance(value, int) or not 1 <= value <= MAX_ARCHIVE_BYTES:
        raise RecoveryError("BACKUP_LIMIT_CONFIGURATION")
    return value


class RecoveryError(ValueError):
    pass


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def private_directory(path):
    path = Path(path).absolute()
    if path.exists() or path.is_symlink():
        raise RecoveryError("DESTINATION_EXISTS")
    if any(parent.is_symlink() for parent in path.parents):
        raise RecoveryError("SYMLINK_PARENT")
    path.mkdir(mode=0o700, parents=True)
    return path


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def read_key(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise RecoveryError("KEY_PERMISSIONS")
    value = path.read_bytes()
    if len(value) != 32:
        raise RecoveryError("KEY_FORMAT")
    return value


def keygen(path):
    path = Path(path)
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise RecoveryError("KEY_PARENT_REQUIRED")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(secrets.token_bytes(32))


def db_parameters(name=None):
    config = connection.settings_dict
    if not getattr(settings, "LOCAL_RECOVERY_ENABLED", False) or config["HOST"] not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise RecoveryError("LOCAL_OWNED_CLUSTER_REQUIRED")
    return dict(
        host=config["HOST"],
        hostaddr="::1" if config["HOST"] == "::1" else "127.0.0.1",
        options="",
        port=config["PORT"],
        user=config["USER"],
        password=config["PASSWORD"],
        dbname=name or config["NAME"],
    )


def migrations():
    return {
        str(path.relative_to(settings.BASE_DIR)): sha(path)
        for path in sorted((settings.BASE_DIR / "apps").glob("*/migrations/*.py"))
    }


def release_signature():
    sources = sorted(
        path for folder in ["apps", "config"] for path in (settings.BASE_DIR / folder).rglob("*.py")
    )
    sources += [settings.BASE_DIR / "requirements.txt", settings.BASE_DIR / "pyproject.toml"]
    return hashlib.sha256(
        json.dumps(
            {str(p.relative_to(settings.BASE_DIR)): sha(p) for p in sources}, sort_keys=True
        ).encode()
    ).hexdigest()


def table_snapshot(cursor):
    """Empreintes déterministes en lecture, sans exporter de champs dans le rapport."""
    cursor.execute("SET LOCAL TIME ZONE 'UTC'")
    cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    tables = [row[0] for row in cursor.fetchall()]
    result = {}
    for table in tables:
        digest, count = hashlib.sha256(), 0
        # Curseur serveur : ne pas matérialiser une grande table dans le processus Python.
        with connection.connection.cursor(name="m11_" + uuid.uuid4().hex) as rows:
            rows.itersize = 500
            rows.execute(
                sql.SQL(
                    "SELECT row_to_json(t)::text FROM {} t ORDER BY row_to_json(t)::text"
                ).format(sql.Identifier(table))
            )
            for (value,) in rows:
                digest.update(value.encode())
                digest.update(b"\n")
                count += 1
        result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def pg_run(tool, arguments, database):
    params = db_parameters(database)
    # Le secret n'est ni dans argv, ni dans l'archive, ni imprimé.
    environment = {
        **{k: v for k, v in os.environ.items() if not k.startswith("PG")},
        "PGPASSWORD": params.pop("password"),
        "PGCONNECT_TIMEOUT": "5",
    }
    connection.ensure_connection()
    server_major = int(connection.connection.info.server_version) // 10000
    command = [
        binary(tool, server_major=server_major),
        "--host",
        params["host"],
        "--port",
        str(params["port"]),
        "--username",
        params["user"],
        "--dbname",
        database,
        *arguments,
    ]
    try:
        subprocess.run(  # noqa: S603
            command,
            env=environment,
            check=True,
            capture_output=True,
            timeout=getattr(settings, "M11_TOOL_TIMEOUT_SECONDS", 300),
        )  # noqa: S603
    except (subprocess.SubprocessError, OSError):
        raise RecoveryError("POSTGRES_TOOL_FAILED") from None


def crypt_file(source, destination, key, *, decrypt=False):
    size = Path(source).stat().st_size
    overhead = 0 if decrypt else len(MAGIC) + 28
    if size + overhead > archive_limit():
        raise RecoveryError("ARCHIVE_LIMIT")
    with Path(source).open("rb") as incoming, Path(destination).open("xb") as outgoing:
        os.chmod(destination, 0o600)
        if decrypt:
            if incoming.read(len(MAGIC)) != MAGIC or size < len(MAGIC) + 28:
                raise RecoveryError("ARCHIVE_FORMAT")
            nonce = incoming.read(12)
            incoming.seek(-16, 2)
            tag = incoming.read(16)
            incoming.seek(len(MAGIC) + 12)
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
            remaining = size - len(MAGIC) - 28
        else:
            nonce = secrets.token_bytes(12)
            outgoing.write(MAGIC + nonce)
            cipher = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            remaining = size
        cipher.authenticate_additional_data(MAGIC)
        while remaining:
            block = incoming.read(min(remaining, 1024 * 1024))
            if not block:
                raise RecoveryError("ARCHIVE_INCOMPLETE")
            remaining -= len(block)
            outgoing.write(cipher.update(block))
        try:
            outgoing.write(cipher.finalize())
        except ValueError:
            raise RecoveryError("ARCHIVE_AUTHENTICATION_FAILED") from None
        except Exception:
            raise RecoveryError("ARCHIVE_AUTHENTICATION_FAILED") from None
        if not decrypt:
            outgoing.write(cipher.tag)


def backup(destination, key_file):
    key = read_key(key_file)
    destination = Path(destination).absolute()
    storage_root = Path(photo_storage().location).resolve()
    if destination.resolve().is_relative_to(storage_root) or Path(
        key_file
    ).resolve().is_relative_to(destination.resolve()):
        raise RecoveryError("BACKUP_SEPARATION_REQUIRED")
    db_parameters()
    root = private_directory(destination)
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=root) as temporary:
        temporary = Path(temporary)
        dump, bundle = temporary / "database.dump", temporary / "bundle.zip"
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            cursor.execute(
                "SELECT pg_export_snapshot(), current_setting('server_version_num'), clock_timestamp()"
            )
            snapshot, version, captured = cursor.fetchone()
            assets = list(
                PhotoAsset.objects.values(
                    "object_key", "thumbnail_key", "digest", "size", "thumbnail_size"
                )
            )
            tables = table_snapshot(cursor)
            pg_run(
                "pg_dump",
                [
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--snapshot",
                    snapshot,
                    "--file",
                    str(dump),
                ],
                connection.settings_dict["NAME"],
            )
            dump.chmod(0o600)
            expanded = dump.stat().st_size
            if dump.stat().st_size > archive_limit():
                raise RecoveryError("ARCHIVE_LIMIT")
            meta = {
                "format": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "snapshot_captured_at": captured.astimezone(UTC).isoformat(),
                "release_sha256": release_signature(),
                "postgres_major": int(version) // 10000,
                "django": django.get_version(),
                "timezone": "Europe/Paris",
                "migrations": migrations(),
                "tables": tables,
                "files": {"database.dump": sha(dump)},
                "external_channels": "DISABLED_ON_RESTORE",
            }
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(dump, "database.dump")
                for asset in assets:
                    for column in ["object_key", "thumbnail_key"]:
                        name = asset[column]
                        path = storage_root / name
                        if (
                            path.is_symlink()
                            or not path.resolve().is_relative_to(storage_root)
                            or not path.is_file()
                        ):
                            raise RecoveryError("PHOTO_MISSING_OR_UNSAFE")
                        digest = sha(path)
                        expected_size = asset[
                            "size" if column == "object_key" else "thumbnail_size"
                        ]
                        if path.stat().st_size != expected_size or (
                            column == "object_key" and digest != asset["digest"]
                        ):
                            raise RecoveryError("PHOTO_INTEGRITY")
                        member = "photos/" + name
                        if member not in meta["files"]:
                            expanded += path.stat().st_size
                            if expanded > archive_limit():
                                raise RecoveryError("EXPANDED_ARCHIVE_LIMIT")
                            archive.write(path, member)
                            meta["files"][member] = digest
                metadata = json.dumps(meta)
                if len(metadata.encode()) > MAX_METADATA_BYTES:
                    raise RecoveryError("ARCHIVE_METADATA_LIMIT")
                if (
                    len(meta["files"]) + 1 > 100000
                    or expanded + len(metadata.encode()) > archive_limit()
                ):
                    raise RecoveryError("EXPANDED_ARCHIVE_LIMIT")
                archive.writestr("metadata.json", metadata)
            bundle.chmod(0o600)
        crypt_file(bundle, root / "backup.enc", key)
    manifest = {
        "format": 1,
        "archive_sha256": sha(root / "backup.enc"),
        "created_at": meta["created_at"],
        "restored_and_verified": False,
    }
    write_private(root / "manifest.json", manifest)
    return manifest


def unpack_verified(root, key_file, temporary):
    root, temporary = Path(root), Path(temporary)
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("format") != 1 or sha(root / "backup.enc") != manifest.get("archive_sha256"):
        raise RecoveryError("ARCHIVE_INTEGRITY")
    bundle = temporary / "bundle.zip"
    crypt_file(root / "backup.enc", bundle, read_key(key_file), decrypt=True)
    with zipfile.ZipFile(bundle) as archive:
        if (
            len(archive.infolist()) > 100000
            or sum(item.file_size for item in archive.infolist()) > archive_limit()
        ):
            raise RecoveryError("EXPANDED_ARCHIVE_LIMIT")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise RecoveryError("ARCHIVE_DUPLICATE_MEMBER")
        if (
            "metadata.json" not in names
            or archive.getinfo("metadata.json").file_size > MAX_METADATA_BYTES
        ):
            raise RecoveryError("ARCHIVE_METADATA")
        meta = json.loads(archive.read("metadata.json"))
        if (
            not isinstance(meta, dict)
            or meta.get("format") != 1
            or not isinstance(meta.get("files"), dict)
            or "database.dump" not in meta["files"]
        ):
            raise RecoveryError("ARCHIVE_INCOMPLETE")
        if set(names) != {"metadata.json", *meta["files"]}:
            raise RecoveryError("ARCHIVE_MEMBERS")
        for name, digest in meta["files"].items():
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not (name == "database.dump" or name.startswith("photos/"))
            ):
                raise RecoveryError("ARCHIVE_PATH")
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with archive.open(name) as incoming, target.open("xb") as outgoing:
                while block := incoming.read(1024 * 1024):
                    outgoing.write(block)
            target.chmod(0o600)
            if sha(target) != digest:
                raise RecoveryError("MEMBER_INTEGRITY")
    return meta


def restore(root, key_file, *, database, destination):
    if not re.fullmatch(r"restore_m11_[a-z0-9_]{1,40}", database):
        raise RecoveryError("RESTORE_NEW_DATABASE_PREFIX_REQUIRED")
    params = db_parameters("postgres")
    destination = Path(destination).absolute()
    if destination.resolve().is_relative_to(Path(photo_storage().location).resolve()):
        raise RecoveryError("RESTORE_ACTIVE_STORAGE_FORBIDDEN")
    if destination.exists() or destination.is_symlink():
        raise RecoveryError("RESTORE_NEW_DIRECTORY_REQUIRED")
    # Toutes les validations avant création de la base ; aucun DROP/--clean.
    with tempfile.TemporaryDirectory(prefix="reserve-restore-") as folder:
        os.chmod(folder, 0o700)
        meta = unpack_verified(root, key_file, folder)
        with psycopg.connect(**params, autocommit=True) as admin:
            major = int(admin.execute("SHOW server_version_num").fetchone()[0]) // 10000
            if (
                meta.get("migrations") != migrations()
                or meta.get("release_sha256") != release_signature()
                or meta.get("postgres_major") != major
                or meta.get("django") != django.get_version()
            ):
                raise RecoveryError("RESTORE_INCOMPATIBLE")
            if admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", [database]).fetchone():
                raise RecoveryError("RESTORE_DATABASE_EXISTS")
            target = private_directory(destination)
            write_private(
                target / "status.json", {"status": "TARGET_ALLOCATED", "database": database}
            )
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
            )
        try:
            pg_run(
                "pg_restore",
                [
                    "--no-owner",
                    "--no-acl",
                    "--single-transaction",
                    "--exit-on-error",
                    str(Path(folder) / "database.dump"),
                ],
                database,
            )
            photos = target / "photos"
            photos.mkdir(mode=0o700)
            for name in meta["files"]:
                if name.startswith("photos/"):
                    src = Path(folder) / name
                    dst = target / name
                    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    dst.write_bytes(src.read_bytes())
                    dst.chmod(0o600)
            write_private(
                target / "recovery.json",
                {
                    "database": database,
                    "generation": str(uuid.uuid4()),
                    "external_channels": False,
                    "source_created_at": meta["created_at"],
                    "archive_sha256": sha(Path(root) / "backup.enc"),
                    "status": "RESTORED_REQUIRES_CONSISTENCY_AND_RECONCILIATION",
                    "tables": meta["tables"],
                },
            )
        except Exception:
            write_private(
                target / "failure.json",
                {"code": "RESTORE_FAILED", "database_retained_isolated": True},
            )
            raise RecoveryError("RESTORE_FAILED_RETAINED_ISOLATED") from None
    return {"database": database, "files": len(meta["files"]) - 1, "consistency_verified": False}
