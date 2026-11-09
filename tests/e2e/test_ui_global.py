"""Inventaire et recette UI globale, PostgreSQL isolé et Chromium réel."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from playwright.sync_api import expect, sync_playwright

from apps.accounts.models import User
from apps.requests.models import InternalRequest
from tests.e2e import test_deliveries as delivery_browser
from tests.e2e import test_ui_fix as ui_helpers
from tests.test_accounts import FICTIVE_PASSWORD
from tests.test_notifications import NotificationFixtures

ROOT = Path("docs/recette/UI-UX-GLOBAL")
PHASE = os.environ.get("UI_GLOBAL_PHASE", "after")
SIZES = [
    (320, 700),
    (375, 812),
    (390, 844),
    (430, 932),
    (768, 1024),
    (1280, 800),
    (1440, 900),
    (1920, 1080),
]


class GlobalUIBrowserTests(NotificationFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login
    audit_data = ui_helpers.UIFixBrowserTests.audit_data
    surfaces = ui_helpers.UIFixBrowserTests.surfaces

    @classmethod
    def setUpClass(cls):
        cls.original_frontend = None
        if os.environ.get("UI_GLOBAL_ORIGINAL") == "1":
            from copy import deepcopy

            from django.conf import settings
            from django.test import override_settings

            snapshot = settings.BASE_DIR / "docs/recette/UI-UX-GLOBAL/source-before"
            templates = deepcopy(settings.TEMPLATES)
            templates[0]["DIRS"] = [snapshot / "templates"]
            cls.original_frontend = override_settings(
                TEMPLATES=templates,
                STATICFILES_DIRS=[snapshot / "static", settings.BASE_DIR / "static"],
            )
            cls.original_frontend.enable()
        try:
            super().setUpClass()
        except Exception:
            if cls.original_frontend:
                cls.original_frontend.disable()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            if cls.original_frontend:
                cls.original_frontend.disable()

    def data(self):
        d = self.audit_data()
        self.doctor = User.objects.create_user(
            "medecin-audit",
            FICTIVE_PASSWORD,
            display_name="Demandeur B de recette",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        old = self.user_client
        self.user_client = self.logged(self.doctor)
        result = self.send([d["p"]], [1])
        self.assertEqual(result.status_code, 201, result.content)
        d["doctor_request"] = InternalRequest.objects.get(pk=result.json()["resource"]["id"])
        self.user_client = old
        closed_product = self.demandable_product(code="GLOBAL-CLOSE", physical=10)
        result = self.send([closed_product], [2])
        req = InternalRequest.objects.get(pk=result.json()["resource"]["id"])
        self.assertEqual(self.claim(req).status_code, 200)
        req.refresh_from_db()
        self.assertEqual(self.deliver(req).status_code, 201)
        d["closed"] = req
        missing_product = self.demandable_product(code="GLOBAL-MANQUE")
        result = self.send([missing_product], [3])
        shortage_req = InternalRequest.objects.get(pk=result.json()["resource"]["id"])
        self.assertEqual(self.claim(shortage_req).status_code, 200)
        shortage_req.refresh_from_db()
        self.assertEqual(self.deliver(shortage_req, [1], "SHORTAGE").status_code, 201)
        from apps.cases.models import Case

        d["shortage"] = Case.objects.get(kind="SHORTAGE")
        deferred = self.demandable_product(code="GLOBAL-REPORTE", physical=1)
        self.modify(deferred, minimum=2, target=10)
        self.assertEqual(self.rec_action(deferred, "defer").status_code, 200)
        d["deferred"] = deferred
        self.process()
        return d

    def pages(self, d):
        sets = self.surfaces(d)
        for user, paths in sets:
            paths += ["/notifications", "/historique-personnel"]
            if user.role == "ADMIN":
                paths += [
                    "/notifications/diagnostic",
                    "/tableau-de-bord",
                    f"/dossiers/{d['shortage'].pk}/verifier-manque",
                    f"/dossiers/livraison/{d['delivery'].pk}",
                ]
                paths += [
                    "/analyses/" + name
                    for name in [
                        "stocks",
                        "movements",
                        "requests",
                        "deliveries",
                        "purchases",
                        "orderlines",
                        "receipts",
                        "surplus",
                        "cases",
                        "history",
                    ]
                ]
                paths += [
                    f"/achats/besoins/{d['deferred'].pk}/reactiver",
                    f"/demandes/{d['closed'].pk}/administrer/reopen",
                    f"/receptions/recus/{d['receipt'].pk}/open_receipt_case",
                ]
            elif user.role == "REPLENISHER":
                paths += [
                    f"/receptions/recus/{d['receipt'].pk}/report_error",
                    f"/dossiers/{d['case'].pk}",
                    f"/catalogue/{d['p'].pk}",
                ]
        doctor = [
            "/catalogue",
            "/demandes/nouvelle",
            "/demandes",
            "/demandes/historique",
            f"/demandes/{d['doctor_request'].pk}",
            "/notifications",
            "/historique-personnel",
            "/mon-compte",
            "/mot-de-passe",
        ]
        return sets + [(self.doctor, doctor)]

    def discover(self, pages):
        """Comparer tous les liens d'écran des fixtures aux permissions serveur réelles."""
        candidates = list(dict.fromkeys(path for _, paths in pages for path in paths))
        discovery = []
        expanded = []
        for user, paths in pages:
            client = self.logged(user)
            allowed = list(paths)
            for path in candidates:
                response = client.get(path, follow=True)
                row = {
                    "role": user.role,
                    "path": path,
                    "status": response.status_code,
                    "templates": sorted({t.name for t in response.templates if t.name}),
                    "redirects": response.redirect_chain,
                }
                discovery.append(row)
                self.assertLess(response.status_code, 500, (user.role, path))
                if response.status_code == 200 and "text/html" in response.get("Content-Type", ""):
                    allowed.append(path)
            expanded.append((user, list(dict.fromkeys(allowed))))
        ROOT.mkdir(parents=True, exist_ok=True)
        (ROOT / "role-discovery.json").write_text(
            json.dumps(discovery, ensure_ascii=False, indent=2)
        )
        return expanded

    def test_all_pages_responsive_inventory(self):
        d = self.data()
        pages = self.discover(self.pages(d))
        out = ROOT / PHASE / "sweep"
        out.mkdir(parents=True, exist_ok=True)
        records, console, network, events = [], [], [], []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for user, paths in pages:
                    page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
                    page.on(
                        "console",
                        lambda e: (
                            console.append({"type": e.type, "text": e.text})
                            if e.type in {"warning", "error"}
                            else None
                        ),
                    )
                    page.on(
                        "pageerror", lambda e: console.append({"type": "pageerror", "text": str(e)})
                    )
                    page.on(
                        "response",
                        lambda r: (
                            network.append({"path": urlsplit(r.url).path, "status": r.status})
                            if r.status >= 400
                            else None
                        ),
                    )
                    self.login(page, user)
                    selected_language = os.environ.get("UI_GLOBAL_LANGUAGE")
                    if selected_language:
                        language_select = page.locator(".language-switcher:visible select").first
                        language_select.select_option(selected_language)
                        page.locator(".language-switcher:visible button").first.click()
                        page.wait_for_load_state("networkidle")
                    for index, path in enumerate(dict.fromkeys(paths)):
                        if user.role == "ADMIN" and index < int(
                            os.environ.get("UI_GLOBAL_START", "0")
                        ):
                            continue
                        at = datetime.now(UTC).isoformat()
                        response = page.goto(self.live_server_url + path)
                        page.wait_for_load_state("networkidle")
                        page.evaluate("document.fonts.ready")
                        name = f"{user.role}-{index:03}"
                        before = str(out / (name + "-before.png"))
                        page.screenshot(
                            path=before,
                            full_page=True,
                            mask=[page.locator('input[type="password"]')],
                        )
                        controls = page.locator(
                            "main a,main button,main input,main select,main textarea,main summary"
                        ).evaluate_all(
                            "ns=>ns.map(n=>({tag:n.tagName,id:n.id,type:n.type,label:n.getAttribute('aria-label')||[...n.labels||[]].map(l=>l.textContent.trim()).join(' ')||n.textContent.trim().slice(0,130),href:n.getAttribute('href'),hidden:!n.getClientRects().length,disabled:!!n.disabled}))"
                        )
                        # Interaction de consultation réelle, sans confirmer un fait métier pendant le balayage.
                        search = page.locator(
                            'main input[name="q"]:visible,main input[type="search"]:visible'
                        ).first
                        if search.count():
                            search.fill("fictif")
                            page.keyboard.press("Tab")
                            events.append(
                                {
                                    "at": datetime.now(UTC).isoformat(),
                                    "path": path,
                                    "role": user.role,
                                    "action": "Saisir une recherche puis déplacer le focus",
                                    "selector": search.get_attribute("id") or "input[name=q]",
                                }
                            )
                            search.fill("")
                        summary = page.locator("main details:not([open]) > summary").first
                        if summary.count():
                            summary.click()
                            expect(page.locator("main details[open]").first).to_be_visible()
                            events.append(
                                {
                                    "at": datetime.now(UTC).isoformat(),
                                    "path": path,
                                    "role": user.role,
                                    "action": "Ouvrir le détail",
                                    "selector": "details > summary",
                                }
                            )
                        # Aller-retour réel depuis chaque écran lorsqu'un lien interne existe.
                        primary_link = page.locator('main a[href^="/"]:visible').first
                        interaction = {"type": "lecture", "mutation": "Parcours métier séparés"}
                        if primary_link.count() and PHASE != "before":
                            target = primary_link.get_attribute("href")
                            label = primary_link.inner_text()
                            primary_link.click()
                            page.wait_for_load_state("networkidle")
                            interaction = {
                                "type": "lien interne cliqué",
                                "label": label,
                                "target": target,
                                "actual": urlsplit(page.url).path,
                                "verified": urlsplit(page.url).path == urlsplit(target).path,
                            }
                            action_shot = str(out / (name + "-action.png"))
                            page.screenshot(
                                path=action_shot,
                                full_page=True,
                                mask=[page.locator('input[type="password"]')],
                            )
                            interaction["capture"] = action_shot
                            events.append(
                                {
                                    "at": datetime.now(UTC).isoformat(),
                                    "path": path,
                                    "role": user.role,
                                    **interaction,
                                }
                            )
                            page.goto(self.live_server_url + path)
                            page.wait_for_load_state("networkidle")
                        page.keyboard.press("Tab")
                        page.wait_for_timeout(100)
                        page.evaluate(Path("tests/assets/axe.min.js").read_text())
                        axe = page.evaluate(
                            "async()=>{let r=await axe.run();return r.violations.map(v=>({id:v.id,impact:v.impact,nodes:v.nodes.map(n=>({target:n.target,summary:n.failureSummary}))}))}"
                        )
                        sizes = [(1440, 900), (390, 844)] if PHASE == "before" else SIZES
                        layouts = []
                        for width, height in sizes:
                            page.set_viewport_size({"width": width, "height": height})
                            page.evaluate("window.scrollTo(0,0)")
                            page.wait_for_timeout(50)
                            layout = page.evaluate(
                                """()=>{let controls=[...document.querySelectorAll('main button,main a.button,main summary')].filter(n=>n.getClientRects().length&&!n.disabled);return {width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth,smallTargets:controls.map(n=>({label:n.textContent.trim(),r:n.getBoundingClientRect()})).filter(o=>o.r.width<24||o.r.height<24).map(o=>({label:o.label,w:o.r.width,h:o.r.height})),actions:controls.length,touchingActions:[...document.querySelectorAll('main .actions, main .mobile-actions, main .confirmation-buttons, main .basket-controls')].flatMap(g=>{let ns=[...g.children].filter(n=>n.matches('a,button,form')&&n.getClientRects().length);let bad=[];for(let i=0;i<ns.length;i++)for(let j=i+1;j<ns.length;j++){let a=ns[i].getBoundingClientRect(),b=ns[j].getBoundingClientRect();let dx=Math.max(0,a.left-b.right,b.left-a.right),dy=Math.max(0,a.top-b.bottom,b.top-a.bottom);if(dx<7&&dy<7)bad.push({a:ns[i].textContent.trim(),b:ns[j].textContent.trim(),dx,dy})}return bad}),orphanLinks:[...document.querySelectorAll('a[href="#"],a[href^="javascript:"]')].length}}"""
                            )
                            shot = str(out / f"{name}-{width}.png")
                            page.screenshot(
                                path=shot,
                                full_page=True,
                                mask=[page.locator('input[type="password"]')],
                            )
                            layouts.append({**layout, "screenshot": shot})
                        record = {
                            "role": user.role,
                            "path": path,
                            "final_path": urlsplit(page.url).path,
                            "at": at,
                            "status": response.status,
                            "controls": controls,
                            "axe": axe,
                            "layouts": layouts,
                            "initial_capture": before,
                            "interaction": interaction,
                            "interaction_limit": "Mutations couvertes séparément par les parcours et la suite complète.",
                            "console_reads": len(console),
                            "network_reads": len(network),
                        }
                        records.append(record)
                        (out / "manifest.json").write_text(
                            json.dumps(
                                {
                                    "pages": records,
                                    "events": events,
                                    "console": console,
                                    "network": network,
                                    "physical_iphone": False,
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                        )
                    page.close()
            finally:
                browser.close()
        self.assertGreater(len(records), 80)
        self.assertFalse([r for r in records if r["status"] >= 400])
        self.assertEqual(console, [])
        self.assertEqual(network, [])
        self.assertFalse(
            [
                (r["path"], v["id"])
                for r in records
                for v in r["axe"]
                if v["impact"] in {"serious", "critical"}
            ]
        )
        if PHASE != "before":
            self.assertFalse(
                [
                    (r["path"], layout["width"], layout["scroll"])
                    for r in records
                    for layout in r["layouts"]
                    if layout["scroll"] > layout["width"]
                ]
            )
            self.assertFalse(
                [
                    (r["path"], layout["width"], layout["touchingActions"])
                    for r in records
                    for layout in r["layouts"]
                    if layout["touchingActions"]
                ]
            )
            self.assertFalse(
                [
                    (r["path"], layout["width"], layout["smallTargets"])
                    for r in records
                    for layout in r["layouts"]
                    if layout["smallTargets"]
                ]
            )

    def test_receipts_round_trip_spacing_keyboard_and_zoom(self):
        from tests.e2e import recette_support

        product = self.demandable_product(code="GLOBAL-RECU", physical=0)
        order = self.buy([product], [20])
        self.assertEqual(self.receive(order, [23], [1]).status_code, 201)
        from apps.receiving.models import Receipt

        receipt = Receipt.objects.get()
        case = self.receipt_case(receipt)
        walk = recette_support.Walk("recu-responsable")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = walk.attach(
                browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR"), "ADMIN"
            )
            self.login(page, self.admin)
            page.get_by_role("link", name="Réceptions fournisseurs", exact=True).click()
            walk.step(
                page,
                "Ouvrir les reçus confirmés",
                lambda: page.get_by_role("link", name="Reçus", exact=True).click(),
                lambda: expect(
                    page.get_by_role("heading", name="Reçus", exact=True)
                ).to_be_visible(),
            )
            expect(page.locator("main")).to_contain_text(product.name)
            expect(page.locator("main")).to_contain_text("20 carton")
            walk.step(
                page,
                "Ouvrir le reçu",
                lambda: page.get_by_role("link", name="Voir le reçu", exact=True).click(),
                lambda: expect(
                    page.get_by_role("heading", name=receipt.reference, exact=True)
                ).to_be_visible(),
            )
            for width, height in SIZES:
                page.set_viewport_size({"width": width, "height": height})
                walk.inspect(page, "ADMIN", "recu-" + str(width))
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
                for box in page.locator("main .actions").all():
                    self.assertGreaterEqual(
                        float(box.evaluate("n=>parseFloat(getComputedStyle(n).gap)")), 8
                    )
            page.set_viewport_size({"width": 1280, "height": 800})
            page.evaluate("document.documentElement.style.zoom='200%'")
            walk.inspect(page, "ADMIN", "recu-zoom-200")
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 1280)
            page.evaluate("document.documentElement.style.zoom=''")
            walk.step(
                page,
                "Revenir aux reçus",
                lambda: page.get_by_role("link", name="Retour aux reçus", exact=True).click(),
                lambda: expect(
                    page.get_by_role("link", name="Voir le reçu", exact=True)
                ).to_be_visible(),
            )
            page.goto(self.live_server_url + f"/receptions/dossiers/{case.pk}")
            before = self.db(lambda: self.stock(product)[:3])
            walk.step(
                page,
                "Vérifier le reçu sans changer les quantités",
                lambda: page.get_by_role(
                    "link", name="Commencer la vérification", exact=True
                ).click(),
                lambda: expect(page.locator("#arrival-message")).to_be_visible(),
            )
            walk.step(
                page,
                "Décrire la vérification",
                lambda: page.locator("#arrival-message").fill(
                    "Vérification fictive du reçu et des articles reçus."
                ),
                lambda: expect(page.locator("#arrival-message")).to_have_value(
                    "Vérification fictive du reçu et des articles reçus."
                ),
            )
            walk.step(
                page,
                "Confirmer le début de vérification",
                lambda: page.get_by_role("button", name="Confirmer", exact=True).click(),
                lambda: expect(
                    page.get_by_role("link", name="Consulter le résultat", exact=True)
                ).to_be_visible(),
            )
            page.get_by_role("link", name="Consulter le résultat", exact=True).click()
            expect(page.locator("main")).to_contain_text("En vérification")
            self.assertEqual(self.db(lambda: self.stock(product)[:3]), before)
            walk.inspect(page, "ADMIN", "dossier-verifie")
            page.get_by_role("link", name="Prévisualiser une correction", exact=True).focus()
            expect(
                page.get_by_role("link", name="Prévisualiser une correction", exact=True)
            ).to_be_focused()
            walk.clean(self)
            browser.close()

    def test_doctor_assistant_mobile_real_partial_delivery(self):
        from tests.e2e import recette_support

        products = [self.demandable_product(code=f"GLOBAL-ROLE-{i}") for i in range(2)]
        doctor = User.objects.create_user(
            "medecin-parcours",
            FICTIVE_PASSWORD,
            display_name="Demandeur B fictif",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                for user in [self.assistant, doctor]:
                    walk = recette_support.Walk("demande-" + user.role)
                    pc = walk.attach(
                        browser.new_page(viewport={"width": 1440, "height": 900}), user.role
                    )
                    self.login(pc, user)
                    walk.step(
                        pc,
                        "Ouvrir le catalogue autorisé",
                        lambda pc=pc: pc.get_by_role(
                            "link", name="Faire une demande", exact=True
                        ).click(),
                        lambda pc=pc: expect(pc.locator(".request-product")).to_have_count(2),
                    )
                    for product in products:
                        walk.step(
                            pc,
                            "Choisir deux articles",
                            lambda p=product, pc=pc: pc.get_by_label(
                                f"Quantité demandée — {p.name}", exact=True
                            ).fill("2"),
                            lambda p=product, pc=pc: expect(
                                pc.get_by_label(f"Quantité demandée — {p.name}", exact=True)
                            ).to_have_value("2"),
                        )
                    walk.step(
                        pc,
                        "Vérifier le récapitulatif",
                        lambda pc=pc: pc.get_by_role(
                            "button", name="Vérifier ma demande", exact=True
                        ).click(),
                        lambda pc=pc: expect(pc.locator("#request-review")).to_be_visible(),
                    )
                    walk.inspect(pc, user.role, "confirmation")
                    walk.step(
                        pc,
                        "Envoyer la demande réelle",
                        lambda pc=pc: pc.get_by_role(
                            "button", name="Envoyer la demande", exact=True
                        ).click(),
                        lambda pc=pc: expect(
                            pc.get_by_role("link", name="Suivre cette demande", exact=True)
                        ).to_be_visible(),
                    )
                    pc.get_by_role("link", name="Suivre cette demande", exact=True).click()
                    request_id = urlsplit(pc.url).path.rsplit("/", 1)[-1]
                    phone = walk.attach(
                        browser.new_page(
                            viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
                        ),
                        "REPLENISHER",
                    )
                    self.login(phone, self.replenisher)
                    phone.get_by_role("link", name="Demandes", exact=True).click()
                    reference = self.db(
                        lambda request_id=request_id: (
                            InternalRequest.objects.get(pk=request_id).reference
                        )
                    )
                    phone.get_by_role("link", name=reference, exact=True).click()
                    walk.step(
                        phone,
                        "Prendre en charge",
                        lambda phone=phone: phone.get_by_role(
                            "button", name="Prendre en charge", exact=True
                        ).click(),
                        lambda phone=phone: expect(phone.locator("#request-status")).to_have_text(
                            "En préparation"
                        ),
                    )
                    for quantity in phone.locator(".handed-quantity").all():
                        quantity.fill("1")
                    phone.get_by_label("Le reste sera apporté plus tard", exact=True).check()
                    walk.step(
                        phone,
                        "Préparer une remise partielle",
                        lambda phone=phone: phone.get_by_role(
                            "button", name="Vérifier la remise", exact=True
                        ).click(),
                        lambda phone=phone: expect(
                            phone.locator("#delivery-summary")
                        ).to_be_visible(),
                    )
                    walk.inspect(phone, "REPLENISHER", "confirmation-partielle")
                    walk.step(
                        phone,
                        "Confirmer la remise partielle",
                        lambda phone=phone: phone.get_by_role(
                            "button", name="Confirmer la remise", exact=True
                        ).click(),
                        lambda phone=phone: expect(phone.locator("#request-status")).to_have_text(
                            "Partiellement livrée — reste à livrer"
                        ),
                    )
                    # La page se met déjà à jour automatiquement : ne pas lancer un
                    # second reload qui peut interrompre cette navigation de lecture.
                    expect(pc.locator("#request-status")).to_have_text(
                        "Partiellement livrée — reste à livrer", timeout=15000
                    )
                    walk.inspect(pc, user.role, "suivi-partiel")
                    walk.clean(self)
                    pc.close()
                    phone.close()
            finally:
                browser.close()

    def test_empty_long_data_validation_errors_and_keyboard(self):
        from apps.catalog.models import Product
        from tests.e2e import recette_support

        walk = recette_support.Walk("etats-vides-longs-erreurs")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = walk.attach(
                browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR"), "ADMIN"
            )
            self.login(page, self.admin)
            for path in [
                "/catalogue",
                "/catalogue?status=COUNTED_PENDING_ACTIVATION",
                "/achats",
                "/receptions/historique",
                "/receptions/signalements",
                "/receptions",
                "/stocks/signalements",
                "/livraisons/signalements",
                "/achats/besoins",
                "/demandes",
                "/notifications",
                "/analyses/receipts",
            ]:
                page.goto(self.live_server_url + path)
                walk.inspect(page, "ADMIN", "vide-" + path.replace("/", "-"))
            page.goto(self.live_server_url + "/catalogue/creer")
            page.get_by_role("button", name="Créer le produit", exact=True).click()
            self.assertTrue(page.locator("input:invalid").count())
            walk.inspect(page, "ADMIN", "formulaire-incomplet")

            def seed():
                Product.objects.bulk_create(
                    [
                        Product(
                            code=f"VOLUME-{i:04}",
                            name=(
                                "Produit fictif à intitulé long — étiquettes et accessoires "
                                + str(i)
                            ),
                            unit="paquet de démonstration",
                            minimum=0,
                            target=1000000000,
                        )
                        for i in range(501)
                    ]
                )

            self.db(seed)
            page.goto(self.live_server_url + "/catalogue")
            page.get_by_label("Rechercher un produit", exact=True).fill("VOLUME")
            page.get_by_role("button", name="Rechercher", exact=True).click()
            expect(page.get_by_role("link", name="Produits suivants", exact=True)).to_be_visible()
            for width, height in SIZES:
                page.set_viewport_size({"width": width, "height": height})
                walk.inspect(page, "ADMIN", "501-produits-" + str(width))
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
            walk.step(
                page,
                "Passer à la suite du catalogue",
                lambda: page.get_by_role("link", name="Produits suivants", exact=True).click(),
                lambda: expect(page).to_have_url(__import__("re").compile("offset=")),
            )
            # Un refus attendu est distingué d'une erreur du parcours normal.
            walk.expected_incident = True
            page.goto(self.live_server_url + "/administration/utilisateurs/creer")
            page.context.set_offline(True)
            walk.inspect(page, "ADMIN", "hors-ligne")
            page.context.set_offline(False)
            walk.expected_incident = False
            # La coupure réseau provoque volontairement l'échec du GET de compteur.
            # Conserver les événements bruts ; seule cette signature précise est attendue.
            raw_console, raw_network = walk.console, walk.network
            counter_paths = {
                "/api/v1/notifications/unread-count",
                "/api/v1/recommendations/pending-count",
                "/api/v1/requests/pending-count",
            }
            allowed_console = [
                e
                for e in raw_console
                if e["expected_incident"]
                and e["message"] == "Failed to load resource: net::ERR_INTERNET_DISCONNECTED"
                and urlsplit(e.get("location", {}).get("url", "")).path in counter_paths
            ]
            allowed_network = [
                e
                for e in raw_network
                if e["expected_incident"]
                and e["method"] == "GET"
                and e["path"] in counter_paths
                and e.get("failure") == "net::ERR_INTERNET_DISCONNECTED"
            ]
            try:
                walk.console = [e for e in raw_console if e not in allowed_console]
                walk.network = [e for e in raw_network if e not in allowed_network]
                walk.clean(self)
            finally:
                walk.console, walk.network = raw_console, raw_network
                walk.save()
                (walk.folder / "intentional-offline.json").write_text(
                    json.dumps(
                        {
                            "console": allowed_console,
                            "network": allowed_network,
                            "reason": "Coupure réseau volontaire ; aucun incident normal supprimé.",
                        },
                        indent=2,
                    )
                )
            browser.close()

    def test_login_upload_error_and_permission_pages(self):
        from tests.e2e import recette_support

        p = self.demandable_product(code="GLOBAL-ERREUR")
        walk = recette_support.Walk("erreurs-et-acces")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = walk.attach(
                browser.new_page(viewport={"width": 390, "height": 844}), "ASSISTANT"
            )
            page.goto(self.live_server_url + "/connexion")
            for width, height in SIZES:
                page.set_viewport_size({"width": width, "height": height})
                walk.inspect(page, "ANONYMOUS", "connexion-" + str(width))
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)
            page.set_viewport_size({"width": 390, "height": 844})
            page.get_by_label("Identifiant", exact=True).fill("inconnu-fictif")
            page.get_by_label("Mot de passe", exact=True).fill("Identifiants incorrects fictifs")
            walk.expected_incident = True
            walk.step(
                page,
                "Refus des identifiants invalides",
                lambda: page.get_by_role("button", name="Se connecter", exact=True).click(),
                lambda: expect(page.locator(".errorlist")).to_be_visible(),
            )
            walk.inspect(page, "ANONYMOUS", "connexion-refusee")
            walk.expected_incident = False
            self.login(page, self.assistant)
            walk.expected_incident = True
            response = page.goto(self.live_server_url + "/administration/utilisateurs")
            self.assertEqual(response.status, 403)
            walk.inspect(page, "ASSISTANT", "acces-refuse")
            walk.expected_incident = False
            admin = walk.attach(browser.new_page(viewport={"width": 1440, "height": 900}), "ADMIN")
            self.login(admin, self.admin)
            admin.goto(self.live_server_url + f"/catalogue/{p.pk}")
            admin.locator('input[type="file"]').set_input_files(
                {
                    "name": "document.png",
                    "mimeType": "image/png",
                    "buffer": b"document invalide fictif",
                }
            )
            walk.expected_incident = True
            walk.step(
                admin,
                "Refuser une photographie invalide",
                lambda: admin.get_by_role(
                    "button", name="Remplacer la photographie", exact=True
                ).click(),
                lambda: expect(
                    admin.get_by_role("heading", name="Photographie non enregistrée", exact=True)
                ).to_be_visible(),
            )
            walk.inspect(admin, "ADMIN", "photographie-refusee")
            walk.expected_incident = False
            admin.goto(self.live_server_url + f"/catalogue/{p.pk}")
            archive = admin.locator('form[action$="/archiver"]')
            archive.get_by_role("checkbox").check()
            renamed = self.db(
                lambda: self.modify(p, name="Produit fictif vérifié depuis un autre onglet")
            )
            self.assertEqual(renamed.status_code, 200, renamed.content)
            before_stock = self.db(lambda: self.stock(p)[:3])
            walk.expected_incident = True
            walk.step(
                admin,
                "Refuser une confirmation devenue obsolète",
                lambda: archive.get_by_role(
                    "button", name="Archiver le produit", exact=True
                ).click(),
                lambda: expect(
                    admin.get_by_role("heading", name="Action non confirmée", exact=True)
                ).to_be_visible(),
            )
            for width, height in SIZES:
                admin.set_viewport_size({"width": width, "height": height})
                walk.inspect(admin, "ADMIN", "confirmation-obsolete-" + str(width))
                self.assertLessEqual(admin.evaluate("document.documentElement.scrollWidth"), width)
            self.assertEqual(self.db(lambda: self.stock(p)[:3]), before_stock)
            self.assertEqual(self.db(lambda: type(p).objects.get(pk=p.pk).status), "ACTIVE")
            raw_network = walk.network
            expected_refusal = [
                event
                for event in raw_network
                if event["expected_incident"]
                and event["method"] == "POST"
                and event["path"] == f"/catalogue/{p.pk}/archiver"
                and event.get("status") == 409
            ]
            self.assertEqual(len(expected_refusal), 1)
            try:
                walk.network = [event for event in raw_network if event not in expected_refusal]
                walk.clean(self)
            finally:
                walk.network = raw_network
                walk.save()
                (walk.folder / "intentional-stale.json").write_text(
                    json.dumps(expected_refusal, indent=2)
                )
            browser.close()

    def test_catalogue_performance_and_visible_keyboard_focus(self):
        self.demandable_product(code="GLOBAL-PERFORMANCE")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            self.login(page, self.admin)
            client = page.context.new_cdp_session(page)
            client.send("Emulation.setCPUThrottlingRate", {"rate": 4})
            client.send("Network.enable")
            client.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "latency": 50,
                    "downloadThroughput": 200000,
                    "uploadThroughput": 100000,
                },
            )
            page.add_init_script("""window.uiPerf={lcp:0,cls:0,eventMax:0};
              new PerformanceObserver(l=>{for(const e of l.getEntries())window.uiPerf.lcp=e.startTime}).observe({type:'largest-contentful-paint',buffered:true});
              new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput)window.uiPerf.cls+=e.value}).observe({type:'layout-shift',buffered:true});
              new PerformanceObserver(l=>{for(const e of l.getEntries())window.uiPerf.eventMax=Math.max(window.uiPerf.eventMax,e.duration)}).observe({type:'event',buffered:true,durationThreshold:16});""")
            page.goto(self.live_server_url + "/catalogue")
            page.wait_for_load_state("networkidle")
            field = page.get_by_label("Rechercher un produit", exact=True)
            field.focus()
            field.press_sequentially("GLOBAL", delay=70)
            page.keyboard.press("Tab")
            page.wait_for_timeout(1600)
            measurements = page.evaluate("window.uiPerf")
            measurements.update(
                cpu_rate=4,
                latency_ms=50,
                download_bytes_second=200000,
                input_events="Clavier réel ; eventMax est une mesure de laboratoire, pas l’INP terrain.",
            )
            (ROOT / "performance.json").write_text(
                json.dumps(measurements, indent=2, ensure_ascii=False)
            )
            self.assertLess(measurements["lcp"], 4000)
            self.assertLess(measurements["cls"], 0.25)
            self.assertLess(measurements["eventMax"], 500)
            self.assertNotEqual(page.evaluate("document.activeElement.tagName"), "BODY")
            browser.close()

    def test_export_loading_server_failure_and_real_retry(self):
        from apps.core.models import Operation

        self.demandable_product(code="GLOBAL-ERREUR-SERVEUR")
        out = ROOT / "export-incident"
        out.mkdir(parents=True, exist_ok=True)
        console, responses, pending = [], [], []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.on(
                "console",
                lambda e: (
                    console.append({"type": e.type, "message": e.text})
                    if e.type in {"error", "warning"}
                    else None
                ),
            )
            page.on(
                "response",
                lambda r: (
                    responses.append({"url": urlsplit(r.url).path, "status": r.status})
                    if r.status >= 400
                    else None
                ),
            )
            self.login(page, self.admin)
            page.goto(self.live_server_url + "/analyses/stocks")
            page.route("**/analyses/stocks/export.xlsx*", lambda route: pending.append(route))
            button = page.get_by_role("button", name="Exporter ces données en Excel", exact=True)
            before = self.db(lambda: Operation.objects.count())
            button.click()
            expect(button).to_be_disabled()
            page.screenshot(path=str(out / "chargement.png"), full_page=True)
            self.assertEqual(len(pending), 1)
            pending[0].fulfill(
                status=500,
                content_type="text/html",
                body="<main><p>Service indisponible. Réessayez dans un instant.</p></main>",
            )
            expect(page.locator("#export-status")).to_have_text(
                "Service indisponible. Réessayez dans un instant."
            )
            expect(button).to_be_enabled()
            self.assertEqual(self.db(lambda: Operation.objects.count()), before)
            page.screenshot(path=str(out / "erreur-500-attendue.png"), full_page=True)
            page.unroute("**/analyses/stocks/export.xlsx*")
            with page.expect_download() as downloaded:
                button.click()
            downloaded.value.save_as(out / "reprise-reelle.xlsx")
            expect(button).to_be_enabled()
            self.assertEqual(self.db(lambda: Operation.objects.count()), before + 1)
            page.screenshot(path=str(out / "reprise.png"), full_page=True)
            (out / "manifest.json").write_text(
                json.dumps(
                    {
                        "intentional_fault": "Réponse 500 injectée sur l’export uniquement, aucune réussite simulée ; reprise réelle ensuite.",
                        "console": console,
                        "responses": responses,
                        "failed_attempt_operations": 0,
                        "successful_retry_operations": 1,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            self.assertEqual(responses, [{"url": "/analyses/stocks/export.xlsx", "status": 500}])
            self.assertTrue(all(e["type"] == "error" and "500" in e["message"] for e in console))
            browser.close()

    def test_long_supplier_fifty_receipt_lines_and_large_quantities(self):
        from apps.catalog.models import Supplier

        supplier_response = self.post_json(
            "/api/v1/suppliers",
            {
                "code": "GLOBAL-LONG",
                "website": "https://example.invalid/catalogue",
                "name": "Fournisseur fictif — " + "Démonstration " * 8,
                "active": True,
            },
        )
        self.assertEqual(supplier_response.status_code, 201, supplier_response.content)
        supplier = Supplier.objects.get(pk=supplier_response.json()["resource"]["id"])
        products = [self.demandable_product(code=f"GLOBAL-LIGNES-{i:02}") for i in range(50)]
        renamed = self.modify(products[0], name="ArticleFictif" * 11)
        self.assertEqual(renamed.status_code, 200, renamed.content)
        order = self.buy(products, [999999999] * 50, supplier_id=str(supplier.pk))
        out = ROOT / "long-receipt"
        out.mkdir(parents=True, exist_ok=True)
        layouts = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            self.login(page, self.replenisher)
            page.goto(self.live_server_url + f"/receptions/commandes/{order.pk}/recevoir")
            expect(page.locator(".arrival-row")).to_have_count(50)
            for width, height in SIZES:
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_timeout(100)
                page.screenshot(path=str(out / f"debut-{width}.png"))
                page.locator("#arrival-submit").scroll_into_view_if_needed()
                expect(page.locator("#arrival-submit")).to_be_visible()
                page.screenshot(path=str(out / f"confirmation-{width}.png"))
                layouts.append(
                    page.evaluate(
                        "({width:innerWidth,scroll:document.documentElement.scrollWidth})"
                    )
                )
                page.evaluate("scrollTo(0,0)")
            (out / "manifest.json").write_text(
                json.dumps(
                    {
                        "lines": 50,
                        "supplier_name_length": len(supplier.name),
                        "product_name_length": len(products[0].name),
                        "waiting_per_line": 999999999,
                        "layouts": layouts,
                        "receipt_confirmed": False,
                    },
                    indent=2,
                )
            )
            self.assertFalse([row for row in layouts if row["scroll"] > row["width"]])
            self.assertTrue(
                all(
                    value == "0"
                    for value in page.locator('[data-quantity="conforming"]').evaluate_all(
                        "ns=>ns.map(n=>n.value)"
                    )
                )
            )
            self.assertEqual(
                self.db(lambda: [self.stock(product)[:3] for product in products]),
                [(10, 0, 10)] * 50,
            )
            browser.close()

    def test_catalogue_context_titles_remain_appropriate_for_each_role(self):
        self.demandable_product(code="GLOBAL-CONTEXTE")
        doctor = User.objects.create_user(
            "medecin-contexte",
            FICTIVE_PASSWORD,
            display_name="Demandeur B fictif",
            role="DOCTOR",
            floor=self.floor,
            must_change_password=False,
        )
        out = ROOT / "catalogue-context"
        out.mkdir(parents=True, exist_ok=True)
        scenes = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for user in [self.admin, self.assistant, doctor, self.replenisher]:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                self.login(page, user)
                for mode, expected in [
                    ("configuration=droits", "Autorisations & plafonds"),
                    ("configuration=stock", "Paramètres de stock"),
                    ("status=COUNTED_PENDING_ACTIVATION", "Produits à activer"),
                ]:
                    page.goto(self.live_server_url + "/catalogue?" + mode)
                    title = expected if user.role == "ADMIN" else "Catalogue"
                    expect(page.get_by_role("heading", level=1)).to_have_text(title)
                    for width, height in SIZES:
                        page.set_viewport_size({"width": width, "height": height})
                        shot = out / f"{user.role}-{mode.replace('=', '-')}-{width}.png"
                        page.screenshot(path=str(shot), full_page=True)
                        self.assertLessEqual(
                            page.evaluate("document.documentElement.scrollWidth"), width
                        )
                        scenes.append(
                            {
                                "role": user.role,
                                "mode": mode,
                                "title": title,
                                "width": width,
                                "screenshot": str(shot),
                            }
                        )
                page.close()
            browser.close()
        (out / "manifest.json").write_text(json.dumps(scenes, indent=2, ensure_ascii=False))
