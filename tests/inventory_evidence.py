"""Traçabilité des obligations GOAL M3 ; résultats détaillés dans le rapport exécuté."""

observations = {}
DEPENDENT_SCENARIOS = {
    "T01_initial_zero_and_activation_without_supplier": "COVERED_M3",
    "T02_one_unit_immutable_after_initial_zero": "COVERED_M3",
    "T03_available_after_internal_request": "COVERED_M4: P/R/D éprouvés avec demande réelle et registre commun",
    "T14_multi_product_single_confirmation": "COVERED_M3",
    "T33_off_pilot_ordinary_adjustment": "COVERED_M3",
    "A03_reservation_arbitration_and_case_resolution": "COVERED_M6: refus/hold M3 et correction, arbitrage ciblé multi-demandes, recomptage et résolution Gestionnaire M6 éprouvés",
    "actual_internal_deliveries_and_supplier_receipts": "PARTIAL: livraisons internes M5 éprouvées, réceptions fournisseurs M8 non développées",
}
