"""Résultats observés et limites cumulées M4–M6 ; canaux externes M9 distincts."""

observations = {}
DEPENDENT_SCENARIOS = {
    "T03_physical_reserved_available": "COVERED_M4: P10,R4,D6 après vraie demande",
    "T04_last_stock_concurrent_assistants": "COVERED_M4",
    "T05_floor_ceiling_server_refusal": "COVERED_M4",
    "T06_multi_product_request": "PARTIAL: création/réservation/audit/événement/outbox M4 éprouvés ; délivrance de notification M9 non exécutée",
    "T10_cancel_before_delivery": "PARTIAL: NEW/PREPARING et course livraison M5 éprouvés ; délivrance externe M9 non exécutée",
    "T27_permissions": "COVERED_M4: pages, API, propriétaire, rôle et accès photo historique",
    "T30_network_failure": "COVERED_M4: inscription perdue, réponse perdue après commit, annulation, pas de rejeu automatique",
    "T07_T13_deliveries_and_corrections": "PARTIAL: T07-T12 M5 homogènes et T13 corrections M6 éprouvés ; PT01 mixte reste non exécuté",
    "request_push_email_external_channels": "NOT_EXECUTED: événements/outbox uniquement, M9 différé",
}
