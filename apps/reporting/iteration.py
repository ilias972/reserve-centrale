"""Lecture bornée du lecteur XLSX ; les vues ordinaires conservent leurs curseurs."""

from django.conf import settings

from .filters import limit


def stream(query, *, post_filter=False):
    if not getattr(settings, "REPORTING_EXPORT_READER", False):
        yield from query.iterator(chunk_size=100)
        return
    if not post_filter:
        # Une ligne SQL donne une ligne exportée : lire au plus la borne+1,
        # pour refuser l'excédent sans tronquer silencieusement. Le curseur
        # ordinaire charge cette sélection bornée pendant execute(), donc le
        # statement_timeout couvre la vraie requête, sans OFFSET croissant.
        maximum = min(limit("EXPORT_MAX_ROWS", 20000), 20000)
        yield from query[: maximum + 1].iterator(chunk_size=100)
        return
    # Si un filtre Python peut retirer des objets, ne pas limiter les candidats
    # avant ce filtre. Le lecteur reste interruptible et le snapshot RR stable.
    offset = 0
    while True:
        batch = list(query[offset : offset + 100])
        yield from batch
        if len(batch) < 100:
            return
        offset += 100
