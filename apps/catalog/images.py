import hashlib
import io
from dataclasses import dataclass
from pathlib import PurePath
from uuid import uuid4, uuid5

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.translation import gettext
from PIL import Image, ImageOps, UnidentifiedImageError

from apps.accounts.models import User
from apps.core.api import actor_context
from apps.core.errors import BusinessError
from apps.core.idempotency import execute_intent, register_intent
from apps.core.payloads import parse_uuid, payload_hash
from apps.core.policies import check_role, lock_actor
from apps.core.transactions import configure_transaction

from .models import PhotoAsset
from .services import ensure_registered
from .storage import photo_storage

ALLOWED = {
    "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
    "PNG": ("image/png", {".png"}),
    "WEBP": ("image/webp", {".webp"}),
}


@dataclass(frozen=True)
class EncodedPhoto:
    main: bytes
    thumbnail: bytes
    width: int
    height: int
    source_digest: str


def invalid_photo(message=None):
    if message is None:
        message = gettext("Fichier refusé. Choisissez une photographie JPEG, PNG ou WebP valide.")
    else:
        message = gettext(message)
    return BusinessError("INVALID_PHOTO", message, status=400)


def encode_photo(upload):
    if upload is None:
        raise invalid_photo()
    name = upload.name
    if (
        not isinstance(name, str)
        or any(char in name for char in ["/", "\\", "\x00"])
        or name in {".", ".."}
    ):
        raise invalid_photo()
    raw = upload.read(settings.PHOTO_MAX_BYTES + 1)
    if not raw or len(raw) > settings.PHOTO_MAX_BYTES:
        raise invalid_photo(gettext("Photographie trop volumineuse (5 Mio maximum par défaut)."))
    try:
        with Image.open(io.BytesIO(raw)) as image:
            kind = image.format
            if (
                kind not in ALLOWED
                or upload.content_type != ALLOWED[kind][0]
                or PurePath(name).suffix.lower() not in ALLOWED[kind][1]
            ):
                raise invalid_photo()
            if (
                image.width * image.height > settings.PHOTO_MAX_PIXELS
                or max(image.size) > settings.PHOTO_MAX_DIMENSION
                or min(image.size) < 1
            ):
                raise invalid_photo("Dimensions trop importantes pour cette photographie.")
            if getattr(image, "n_frames", 1) != 1:
                raise invalid_photo("Utilisez une photographie fixe, sans animation.")
            image.verify()
        with Image.open(io.BytesIO(raw)) as original:
            original.load()
            image = ImageOps.exif_transpose(original).convert("RGBA")
            clean = Image.new("RGB", image.size, (255, 255, 255))
            clean.paste(image, mask=image.getchannel("A"))
            clean.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            main = io.BytesIO()
            clean.save(main, "JPEG", quality=85, optimize=True)
            width, height = clean.size
            clean.thumbnail((240, 240), Image.Resampling.LANCZOS)
            thumbnail = io.BytesIO()
            clean.save(thumbnail, "JPEG", quality=80, optimize=True)
        return EncodedPhoto(
            main.getvalue(), thumbnail.getvalue(), width, height, hashlib.sha256(raw).hexdigest()
        )
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        OSError,
        ValueError,
        SyntaxError,
    ) as exc:
        raise invalid_photo() from exc


def stage_asset(context, *, intent_id, product_id, encoded, asset_id):
    storage = photo_storage()
    written = []
    try:
        with transaction.atomic(durable=True):
            configure_transaction()
            actor = lock_actor(context)
            check_role(actor, {User.Role.ADMIN})
            # Une clé d'intention déjà inscrite lie l'empreinte avant toute
            # écriture de fichier ; FOR UPDATE sérialise les doubles uploads.
            from apps.core.models import OperationIntent

            intent = OperationIntent.objects.select_for_update().get(pk=intent_id, actor=actor)
            if intent.state != "READY":
                return
            existing = PhotoAsset.objects.filter(pk=asset_id).first()
            if existing:
                if (
                    existing.source_digest != encoded.source_digest
                    or existing.product_id != product_id
                ):
                    raise BusinessError(
                        "IDEMPOTENCY_MISMATCH",
                        gettext("Ce téléversement correspond à une autre photographie."),
                    )
                if not storage.exists(existing.object_key) or not storage.exists(
                    existing.thumbnail_key
                ):
                    raise BusinessError(
                        "PHOTO_STORAGE_UNAVAILABLE",
                        gettext(
                            "Photographie temporaire indisponible. Vérifiez l'opération puis téléversez de nouveau avec une nouvelle confirmation."
                        ),
                        status=503,
                    )
                return
            # Les clés d'objet ne proviennent jamais du nom envoyé par le client.
            prefix = uuid4().hex
            main_key = storage.save(f"objects/{prefix}.jpg", ContentFile(encoded.main))
            written.append(main_key)
            thumb_key = storage.save(f"objects/{prefix}-thumb.jpg", ContentFile(encoded.thumbnail))
            written.append(thumb_key)
            PhotoAsset.objects.create(
                id=asset_id,
                intent=intent,
                product_id=product_id,
                uploader=actor,
                source_digest=encoded.source_digest,
                digest=hashlib.sha256(encoded.main).hexdigest(),
                object_key=main_key,
                thumbnail_key=thumb_key,
                width=encoded.width,
                height=encoded.height,
                size=len(encoded.main),
                thumbnail_size=len(encoded.thumbnail),
            )
    except Exception:
        # Compensation des seuls nouveaux fichiers non validés, jamais d'une
        # version historique. Un arrêt du processus est traité par le GC différé.
        for name in written:
            storage.delete(name)
        raise


def upload_photo(request, product_id, upload, *, expected_version, identifier, key):
    check_role(request.user, {User.Role.ADMIN})
    ensure_registered()
    encoded = encode_photo(upload)  # CPU hors transaction métier
    identifier = parse_uuid(identifier)
    asset_id = uuid5(identifier, "product-photo")
    payload = {
        "expected_version": expected_version,
        "asset_id": str(asset_id),
        "source_digest": encoded.source_digest,
    }
    context = actor_context(request)
    register_intent(
        context,
        intent_id=identifier,
        key=key,
        fingerprint=payload_hash(payload),
        command_type="catalog.product.photo",
        resource_id=product_id,
    )
    stage_asset(
        context, intent_id=identifier, product_id=product_id, encoded=encoded, asset_id=asset_id
    )
    return execute_intent(
        context,
        intent_id=identifier,
        key=key,
        payload=payload,
        correlation_id=request.correlation_id,
    )
