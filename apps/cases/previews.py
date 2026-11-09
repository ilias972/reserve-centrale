"""Empreinte des conséquences calculées côté serveur, jamais preuve fournie par le client."""

import hashlib
import json


def fingerprint(plan):
    # La limite HTTP M0 de32Kio reste appliquée au payload d'intention.
    # Un aperçu serveur inclut aussi les noms et engagements non sélectionnés,
    # et peut légitimement être plus volumineux que ce payload.
    canonical = json.dumps(
        plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
