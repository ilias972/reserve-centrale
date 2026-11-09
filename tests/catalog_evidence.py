observations = {}
DEPENDENT_SCENARIOS = {
    "inventory_zero_distinction": {
        "status": "COVERED_M3",
        "reason": "Complété par tests M3 de comptage réel zéro/NULL ; voir dependent_inventory_scenarios.",
    },
    "unit_locked_after_real_inventory": {
        "status": "COVERED_M3",
        "reason": "Depuis M3, la fixture utilise le véritable comptage R ; verrou unité vérifié avec registre réel.",
    },
    "activation_without_supplier": {
        "status": "COVERED_M3",
        "reason": "Complété M3 par parcours réel R comptage puis N activation sans fournisseur.",
    },
    "historical_commitments_after_rights_change": {
        "status": "COVERED_M4",
        "reason": "Snapshots et engagements M4 réellement conservés après changement de droits, nom et unité.",
    },
    "archive_existing_commitments": {
        "status": "PARTIAL",
        "reason": "Engagements M4 conservés après archivage, annulables avant remise et livraison M5 du produit archivé éprouvée ; scénario composite archivage + correction M6 non exécuté, distinct des corrections M6 ordinaires testées.",
    },
    "send_request_with_floor_ceiling": {
        "status": "COVERED_M4",
        "reason": "Création/refus de demandes réelles M4 selon plafond courant, vérifiés côté serveur et navigateur.",
    },
}
