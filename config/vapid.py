"""Validation VAPID sans exposer les éléments privés."""

import base64
import hashlib
import re
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def validate_vapid_key_pair(public_key: str, private_key_file: str | Path) -> str:
    """Valide une paire P-256 VAPID et retourne uniquement l'empreinte SHA-256 publique."""
    try:
        if not isinstance(public_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{87}", public_key):
            raise ValueError
        encoded_public = base64.b64decode(public_key + "=", altchars=b"-_", validate=True)
        if len(encoded_public) != 65 or encoded_public[0] != 4:
            raise ValueError
        configured_public = ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), encoded_public
        )

        path = Path(private_key_file)
        if path.is_symlink() or not path.is_file():
            raise ValueError
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError
        private = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(private, ec.EllipticCurvePrivateKey):
            raise ValueError
        if private.curve.name != "secp256r1":
            raise ValueError
        derived_public = private.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        if derived_public != encoded_public:
            raise ValueError
        # Force validation of the configured point; the returned fingerprint is public only.
        configured_public.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    except (OSError, TypeError, ValueError, base64.binascii.Error):
        raise ValueError("VAPID_KEY_PAIR_INVALID") from None
    return hashlib.sha256(encoded_public).hexdigest()
