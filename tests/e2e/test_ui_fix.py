"""UI-FIX : vraie navigation et opérations sur PostgreSQL jetable, jamais la démonstration."""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import tag
from playwright.sync_api import expect, sync_playwright

from apps.cases.models import Case
from apps.catalog.models import Category
from apps.receiving.models import ArrivalReport, QuarantineItem, Receipt
from apps.requests.models import Delivery, InternalRequest
from tests.e2e import test_deliveries as delivery_browser
from tests.test_corrections import CorrectionFixtures
from tests.test_receiving import ReceivingFixtures

PHASE = os.environ.get("UI_FIX_PHASE", "after")
ROOT = Path("docs/recette/UI-FIX") / PHASE / ("browser" + os.environ.get("UI_FIX_BATCH", ""))
SIZES = [(1440, 900), (1243, 954), (1280, 800), (768, 1024), (390, 844), (320, 700)]


class UIFixBrowserTests(ReceivingFixtures, CorrectionFixtures, StaticLiveServerTestCase):
    db = delivery_browser.BrowserDeliveryTests.db
    login = delivery_browser.BrowserDeliveryTests.login

    def audit_data(self):
        p = self.demandable_product(code="UI-BESOIN", physical=7)
        self.assertEqual(
            self.modify(
                p,
                name="Gants de protection fictifs pour la réserve interne",
                minimum=10,
                target=100,
            ).status_code,
            200,
        )
        pending = self.create_product(code="UI-ACTIVER")
        self.initial([pending], [0])
        self.upload(pending)
        category = Category.objects.create(name="Protection et hygiène de démonstration")
        request, products = self.prepare((4, 3), physical=20)
        self.assertEqual(self.deliver(request, [2, 1], "LATER").status_code, 201)
        delivery = Delivery.objects.get(request=request)
        case = self.error_case(delivery)
        conflict_product = self.demandable_product(code="UI-CONFLIT", physical=10, maximum=10)
        self.send([conflict_product], [8])
        response = self.adjust([conflict_product], [6])
        self.assertEqual(response.status_code, 409)
        conflict = Case.objects.get(pk=response.json()["case_id"])
        incoming = self.demandable_product(code="UI-ARRIVEE", physical=0)
        order = self.buy([incoming], [20])
        self.assertEqual(self.receive(order, [12], [2]).status_code, 201)
        receipt = Receipt.objects.get()
        rcase = self.receipt_case(receipt)
        surplus_product = self.demandable_product(code="UI-SURPLUS", physical=0)
        surplus_order = self.buy([surplus_product], [20])
        self.assertEqual(self.receive(surplus_order, [23]).status_code, 201)
        surplus = QuarantineItem.objects.get(kind="SURPLUS")
        unknown = self.unknown(message="Colis fictif sans commande identifiée")
        self.cancel_remainder(order, 4)
        order.refresh_from_db()
        response = self.post_json(
            f"/api/v1/orders/{order.pk}/late-delivery-reports",
            {
                "expected_version": order.version,
                "lines": [{"order_line_id": str(order.lines.get().pk), "observed_quantity": 2}],
            },
            self.operator,
        )
        self.assertEqual(response.status_code, 201, response.content)
        late = ArrivalReport.objects.get(pk=response.json()["resource"]["id"])
        new_p = self.demandable_product(code="UI-NOUVELLE", physical=10)
        response = self.send([new_p], [1])
        new = InternalRequest.objects.get(pk=response.json()["resource"]["id"])
        return locals()

    def surfaces(self, d):
        p = d["p"]
        r = d["request"]
        c = d["case"]
        sc = d["conflict"]
        o = d["order"]
        receipt = d["receipt"]
        rc = d["rcase"]
        s = d["surplus"]
        u = d["unknown"]
        late = d["late"]
        new = d["new"]
        admin = [
            "/catalogue",
            "/catalogue?configuration=droits",
            "/catalogue?configuration=stock",
            "/catalogue?status=COUNTED_PENDING_ACTIVATION",
            "/catalogue?status=COUNTED_PENDING_ACTIVATION&q=inexistant",
            "/catalogue/creer",
            f"/catalogue/{p.pk}",
            f"/catalogue/{p.pk}/modifier",
            f"/catalogue/{p.pk}/droits",
            f"/catalogue/{p.pk}/fournisseurs",
            f"/catalogue/{p.pk}/historique",
            "/administration/utilisateurs",
            "/administration/utilisateurs/creer",
            f"/administration/utilisateurs/{self.assistant.pk}",
            f"/administration/utilisateurs/{self.assistant.pk}/reinitialiser",
            "/configuration/floor",
            "/configuration/floor/creer",
            f"/configuration/floor/{self.floor.pk}",
            "/configuration/category",
            "/configuration/category/creer",
            f"/configuration/category/{d['category'].pk}",
            "/configuration/supplier",
            "/configuration/supplier/creer",
            f"/configuration/supplier/{self.supplier.pk}",
            "/stocks",
            "/stocks/mouvements",
            "/stocks/signalements",
            f"/stocks/signalements/{sc.pk}",
            "/livraisons/signalements",
            "/dossiers",
            f"/dossiers/{c.pk}",
            f"/dossiers/{c.pk}/corriger",
            f"/dossiers/{sc.pk}",
            f"/dossiers/{sc.pk}/regulariser",
            "/demandes",
            f"/demandes/{r.pk}",
            f"/demandes/{r.pk}/administrer/close-remainder",
            f"/demandes/{r.pk}/administrer/reassign",
            f"/demandes/{r.pk}/administrer/reopen",
            "/achats/besoins",
            f"/achats/besoins/{p.pk}",
            f"/achats/besoins/{p.pk}/reporter",
            "/achats",
            f"/achats/{o.pk}",
            f"/achats/{o.pk}/annuler-reliquat",
            "/achats/enregistrer",
            f"/achats/enregistrer?product={p.pk}&supplier={self.supplier.pk}",
            "/receptions",
            "/receptions/historique",
            "/receptions/signalements",
            f"/receptions/commandes/{o.pk}",
            f"/receptions/recus/{receipt.pk}",
            f"/receptions/dossiers/{rc.pk}",
            f"/receptions/dossiers/{rc.pk}/correction",
            f"/receptions/dossiers/{rc.pk}/review",
            f"/receptions/dossiers/{s.case_id}",
            f"/receptions/isolation/{s.pk}/accept",
            f"/receptions/isolation/{s.pk}/refuse",
            f"/receptions/dossiers/{u.case_id}",
            f"/receptions/dossiers/{u.case_id}/link",
            f"/receptions/dossiers/{u.case_id}/link?order={o.pk}",
            f"/receptions/dossiers/{u.case_id}/resolve",
            f"/receptions/dossiers/{late.case_id}",
            f"/receptions/dossiers/{late.case_id}/reinstate",
            "/mon-compte",
            "/mot-de-passe",
        ]
        assistant = [
            "/demandes/nouvelle",
            "/demandes",
            "/demandes/historique",
            f"/demandes/{r.pk}",
            f"/demandes/{new.pk}",
            "/catalogue",
            "/mon-compte",
            "/mot-de-passe",
        ]
        operator = [
            "/stocks",
            "/stocks/inventaire-initial",
            "/stocks/mouvements",
            "/stocks/signalements",
            f"/stocks/signalements/{sc.pk}",
            "/livraisons/signalements",
            "/demandes",
            f"/demandes/{r.pk}",
            f"/demandes/{new.pk}",
            f"/demandes/{new.pk}/refuser",
            "/demandes/historique",
            f"/livraisons/{d['delivery'].pk}/signaler",
            "/catalogue",
            "/mon-compte",
            "/mot-de-passe",
            "/receptions",
            "/receptions/historique",
            "/receptions/signalements",
            "/receptions/commande-introuvable",
            f"/receptions/commandes/{o.pk}",
            f"/receptions/commandes/{o.pk}/recevoir",
            f"/receptions/commandes/{o.pk}/reliquat-arrive",
            f"/receptions/commandes/{o.pk}/rappel",
            f"/receptions/commandes/{d['surplus_order'].pk}/surplus",
            f"/receptions/recus/{receipt.pk}",
            f"/receptions/dossiers/{u.case_id}",
        ]
        # Réouverture d’une demande ouverte est volontairement indisponible : son
        # écran ne doit pas être présenté comme un parcours valide à cet état.
        admin.remove(f"/demandes/{r.pk}/administrer/reopen")
        return [(self.admin, admin), (self.assistant, assistant), (self.replenisher, operator)]

    def instrument(self, page, role):
        page.on(
            "pageerror",
            lambda e: self.console.append(
                {
                    "role": role,
                    "path": urlsplit(page.url).path,
                    "type": "pageerror",
                    "message": str(e),
                }
            ),
        )
        page.on(
            "console",
            lambda e: (
                self.console.append(
                    {
                        "role": role,
                        "path": urlsplit(page.url).path,
                        "type": e.type,
                        "message": e.text,
                    }
                )
                if e.type in {"warning", "error"}
                else None
            ),
        )
        page.on(
            "response",
            lambda r: self.network.append(
                {
                    "role": role,
                    "path": urlsplit(r.url).path,
                    "method": r.request.method,
                    "status": r.status,
                }
            ),
        )

    def shot(self, page, name):
        path = ROOT / (name + ".png")
        page.screenshot(
            path=str(path), full_page=False, mask=[page.locator("input[type=password]")]
        )
        return str(path)

    def step(self, page, label, action, check):
        before = self.shot(page, f"{self._testMethodName}-step-{len(self.steps):04}-before")
        at = datetime.now(UTC).isoformat()
        started = time.monotonic()
        action()
        check()
        page.wait_for_timeout(650)
        after = self.shot(page, f"{self._testMethodName}-step-{len(self.steps):04}-after")
        self.steps.append(
            {
                "at": at,
                "path": urlsplit(page.url).path,
                "action": label,
                "before": before,
                "after": after,
                "seconds": round(time.monotonic() - started, 3),
                "expected_state_verified": True,
                "console_read": len(self.console),
                "network_read": len(self.network),
            }
        )

    @tag("ui-fix-matrix")
    def test_01_complete_sitemap_and_responsive_walk(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.console = []
        self.network = []
        self.steps = []
        scenes = []
        d = self.audit_data()
        surfaces = self.surfaces(d)
        if os.environ.get("UI_FIX_ROLES"):
            surfaces = [
                (u, paths)
                for u, paths in surfaces
                if u.role in os.environ["UI_FIX_ROLES"].split(",")
            ]
        (ROOT / "surfaces.json").write_text(
            json.dumps([{"role": u.role, "routes": p} for u, p in surfaces], indent=2) + "\n"
        )
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                for user, paths in surfaces:
                    page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
                    self.instrument(page, user.role)
                    self.login(page, user)
                    for index, path in enumerate(paths):
                        response = page.goto(self.live_server_url + path)
                        page.wait_for_load_state("networkidle")
                        self.assertEqual(response.status, 200, (path, response.status))
                        self.assertNotIn("/connexion", page.url)
                        for width, height in SIZES:
                            page.set_viewport_size({"width": width, "height": height})
                            page.evaluate("window.scrollTo(0,0)")
                            page.wait_for_timeout(75)
                            controls = page.locator(
                                "a:visible,button:visible,input:visible,select:visible,textarea:visible,summary:visible"
                            ).evaluate_all(
                                "ns=>ns.map(n=>{const r=n.getBoundingClientRect();return {tag:n.tagName,id:n.id,label:n.getAttribute('aria-label')||[...(n.labels||[])].map(l=>l.textContent.trim()).join(' ')||n.textContent.trim().slice(0,180),href:n.getAttribute('href'),type:n.type,disabled:!!n.disabled,box:[r.x,r.y,r.width,r.height],clipped:!!n.closest('main') && (r.left<0 || r.right>innerWidth)}})"
                            )
                            screenshot = self.shot(page, f"{user.role}-{index:02}-{width}")
                            if width in {1440, 390, 320}:
                                page.evaluate(Path("tests/assets/axe.min.js").read_text())
                                axe = page.evaluate(
                                    "async()=>{let r=await axe.run();return r.violations.map(v=>({id:v.id,impact:v.impact,targets:v.nodes.map(n=>n.target)}))}"
                                )
                            else:
                                axe = None
                            scenes.append(
                                {
                                    "role": user.role,
                                    "route": path,
                                    "width": width,
                                    "height": height,
                                    "screenshot": screenshot,
                                    "controls": controls,
                                    "scroll_width": page.evaluate(
                                        "document.documentElement.scrollWidth"
                                    ),
                                    "axe": axe,
                                    "current_tabs": page.locator(
                                        "nav.tabs [aria-current=page]"
                                    ).count(),
                                }
                            )
                        page.set_viewport_size({"width": 1440, "height": 900})
                        field = page.locator(
                            "main input[type=text]:visible,main input:not([type]):visible,main textarea:visible"
                        ).first
                        if field.count() and field.is_editable():
                            previous = field.input_value()
                            self.step(
                                page,
                                "Saisir puis conserver une valeur provisoire",
                                lambda field=field: field.fill("Test ergonomique fictif"),
                                lambda field=field: expect(field).to_have_value(
                                    "Test ergonomique fictif"
                                ),
                            )
                            field.fill(previous)
                        search = page.locator(
                            "main form[method=get] button[type=submit]:visible,main form[method=get] button:not([type]):visible"
                        ).first
                        if search.count():
                            self.step(
                                page,
                                "Appliquer le filtre réel",
                                lambda search=search: search.click(),
                                lambda page=page: expect(page.locator("main h1")).to_be_visible(),
                            )
                        else:
                            link = page.locator('main a[href^="/"]:visible').first
                            if link.count():
                                self.step(
                                    page,
                                    "Suivre le lien de navigation puis revenir",
                                    lambda link=link: link.click(),
                                    lambda page=page: expect(
                                        page.locator("main h1")
                                    ).to_be_visible(),
                                )
                                page.goto(self.live_server_url + path)
                                page.wait_for_load_state("networkidle")
                        for details in page.locator("main details:visible").all():
                            summary = details.locator("summary").first
                            if details.evaluate("n=>n.open"):
                                summary.click()
                            self.step(
                                page,
                                "Ouvrir les actions secondaires",
                                lambda s=summary: s.click(),
                                lambda n=details: expect(n).to_have_attribute("open", ""),
                            )
                            summary.click()
                    page.context.close()
                browser.close()
        finally:
            (ROOT / "scenes.json").write_text(
                json.dumps(scenes, indent=2, ensure_ascii=False) + "\n"
            )
            (ROOT / "manifest.json").write_text(
                json.dumps(self.steps, indent=2, ensure_ascii=False) + "\n"
            )
            (ROOT / "console.json").write_text(
                json.dumps(self.console, indent=2, ensure_ascii=False) + "\n"
            )
            (ROOT / "network.json").write_text(json.dumps(self.network, indent=2) + "\n")
        if PHASE != "before":
            self.assertFalse(
                [s for s in scenes if s["scroll_width"] > s["width"]],
                "Débordement horizontal de page",
            )
            self.assertFalse(
                [
                    s
                    for s in scenes
                    if s["axe"] and any(v["impact"] in {"critical", "serious"} for v in s["axe"])
                ],
                "Violation axe sérieuse/critique",
            )
            self.assertEqual(self.console, [])
            self.assertFalse([r for r in self.network if r["status"] >= 400])

    def test_02_critical_purchase_quantity_and_receiving_tabs(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        self.console = []
        self.network = []
        self.steps = []
        p = self.demandable_product(physical=7)
        self.modify(p, minimum=10, target=100)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1243, "height": 954}, locale="fr-FR")
            self.instrument(page, "ADMIN")
            self.login(page, self.admin)
            page.goto(
                self.live_server_url
                + f"/achats/enregistrer?product={p.pk}&supplier={self.supplier.pk}"
            )
            page.wait_for_load_state("networkidle")
            field = page.get_by_label("Quantité réellement achetée", exact=True)
            value = field.input_value()
            field.scroll_into_view_if_needed()
            self.shot(page, "critical-purchase-quantity")
            tabs = []
            for path in ["/receptions", "/receptions/historique", "/receptions/signalements"]:
                page.goto(self.live_server_url + path)
                tabs.append(
                    {"path": path, "active": page.locator("nav.tabs [aria-current=page]").count()}
                )
            (ROOT / "initial-defects.json").write_text(
                json.dumps(
                    {"purchase_value": value, "expected_recommendation": 93, "tabs": tabs}, indent=2
                )
                + "\n"
            )
            if PHASE == "before":
                self.assertEqual(value, "93")
            else:
                self.assertEqual(value, "93")
                self.assertTrue(all(row["active"] == 1 for row in tabs))
            browser.close()

    def test_03_empty_states(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        scenes = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            for user, paths in [
                (
                    self.admin,
                    [
                        "/catalogue?status=COUNTED_PENDING_ACTIVATION",
                        "/catalogue?q=aucun-resultat",
                        "/administration/utilisateurs?q=aucun-resultat",
                        "/configuration/floor?q=aucun-resultat",
                        "/configuration/supplier?q=aucun-resultat",
                        "/configuration/category",
                        "/stocks/mouvements",
                        "/stocks/signalements",
                        "/livraisons/signalements",
                        "/dossiers",
                        "/demandes",
                        "/achats/besoins",
                        "/achats",
                        "/receptions",
                        "/receptions/historique",
                        "/receptions/signalements",
                    ],
                ),
                (self.assistant, ["/demandes/nouvelle", "/demandes", "/demandes/historique"]),
                (
                    self.replenisher,
                    [
                        "/stocks",
                        "/stocks/inventaire-initial",
                        "/demandes",
                        "/receptions",
                        "/receptions/historique",
                        "/receptions/signalements",
                    ],
                ),
            ]:
                page = browser.new_page(viewport={"width": 390, "height": 844}, locale="fr-FR")
                self.login(page, user)
                for i, path in enumerate(paths):
                    page.goto(self.live_server_url + path)
                    page.wait_for_load_state("networkidle")
                    screenshot = self.shot(page, f"empty-{user.role}-{i}")
                    scenes.append(
                        {
                            "role": user.role,
                            "path": path,
                            "screenshot": screenshot,
                            "text": page.locator("main").inner_text(),
                        }
                    )
                    if PHASE != "before" and path == "/catalogue?status=COUNTED_PENDING_ACTIVATION":
                        expect(page.locator("main")).to_contain_text(
                            "Aucun produit en attente d’activation"
                        )
                        expect(
                            page.get_by_role("link", name="Créer un produit", exact=True)
                        ).to_have_count(0)
                page.context.close()
            browser.close()
        (ROOT / "empty-states.json").write_text(
            json.dumps(scenes, indent=2, ensure_ascii=False) + "\n"
        )

    def test_04_explicit_actual_quantity_and_safe_round_trips(self):
        from apps.purchasing.models import PurchaseOrder

        ROOT.mkdir(parents=True, exist_ok=True)
        self.console, self.network, self.steps = [], [], []
        d = self.audit_data()
        p = d["p"]
        count = self.db(lambda: PurchaseOrder.objects.count())
        stock = self.db(lambda: self.stock(p))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1243, "height": 954}, locale="fr-FR")
            self.instrument(page, "ADMIN")
            self.login(page, self.admin)
            page.goto(self.live_server_url + f"/achats/besoins/{p.pk}")
            page.get_by_role(
                "link", name="Enregistrer un achat avec un autre fournisseur", exact=True
            ).click()
            page.reload()
            expect(page.locator("a.back-link")).to_have_attribute("href", f"/achats/besoins/{p.pk}")
            field = page.get_by_label("Quantité réellement achetée", exact=True)
            self.db(lambda: p.refresh_from_db())
            expect(field).to_have_value(str(max(0, p.target - stock[0])))
            field.fill("")
            page.locator("#purchase-supplier").select_option(str(self.supplier.pk))
            page.locator("#purchase-attestation").check()
            self.step(
                page,
                "Essayer le récapitulatif sans quantité réellement saisie",
                lambda: page.locator("#purchase-submit").click(),
                lambda: expect(page.locator("#purchase-review")).to_be_hidden(),
            )
            self.assertFalse(field.evaluate("n=>n.checkValidity()"))
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), count)
            self.step(
                page,
                "Saisir les 2 cartons réellement achetés",
                lambda: field.fill("2"),
                lambda: expect(field).to_have_value("2"),
            )
            self.step(
                page,
                "Vérifier puis enregistrer le véritable achat fictif",
                lambda: page.locator("#purchase-submit").click(),
                lambda: expect(page.locator("#purchase-review")).to_be_visible(),
            )
            page.locator("#purchase-confirm").click()
            page.get_by_role("link", name="Consulter les données à jour", exact=True).click()
            self.assertEqual(self.db(lambda: PurchaseOrder.objects.count()), count + 1)
            self.assertEqual(self.db(lambda: self.stock(p)), stock)
            page.goto(self.live_server_url + "/livraisons/signalements")
            page.get_by_role("link", name="Examiner le signalement", exact=True).first.click()
            page.reload()
            expect(page.locator("a.back-link").first).to_have_attribute(
                "href", "/livraisons/signalements?state=pending&offset=0"
            )
            page.locator("a.back-link").first.click()
            expect(page).to_have_url(
                self.live_server_url + "/livraisons/signalements?state=pending&offset=0"
            )
            page.goto(self.live_server_url + f"/dossiers/{d['case'].pk}?from=//evil.invalid")
            expect(page.locator("a.back-link").first).to_have_attribute(
                "href", "/livraisons/signalements"
            )
            page.goto(self.live_server_url + "/achats/enregistrer")
            expect(page.locator("a.back-link")).to_have_attribute("href", "/achats")
            browser.close()
        (ROOT / "purchase-roundtrip-manifest.json").write_text(
            json.dumps(self.steps, indent=2) + "\n"
        )
        self.assertEqual(self.console, [])

    def test_05_action_visibility_keyboard_live_stability_and_performance(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        p = self.demandable_product(physical=7)
        self.modify(p, minimum=10, target=100)
        rows = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="fr-FR")
            self.login(page, self.admin)
            page.add_init_script("""window.uiPerf={lcp:0,cls:0,events:[]};
            new PerformanceObserver(l=>{for(const e of l.getEntries())uiPerf.lcp=e.startTime;}).observe({type:'largest-contentful-paint',buffered:true});
            new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput)uiPerf.cls+=e.value;}).observe({type:'layout-shift',buffered:true});
            new PerformanceObserver(l=>{for(const e of l.getEntries())if(e.interactionId)uiPerf.events.push(e.duration);}).observe({type:'event',durationThreshold:16,buffered:true});""")
            page.goto(self.live_server_url + "/catalogue")
            page.wait_for_load_state("networkidle")
            page.get_by_role("link", name="Ouvrir " + p.name, exact=True).focus()
            self.assertTrue(page.evaluate("document.activeElement.matches('a')"))
            page.keyboard.press("Enter")
            expect(page).to_have_url(self.live_server_url + f"/catalogue/{p.pk}")
            page.goto(self.live_server_url + "/catalogue")
            page.locator("table[data-action-table] tbody tr").first.wait_for()
            page.evaluate(
                "window.uiOriginalRow=document.querySelector('table[data-action-table] tbody tr')"
            )
            page.wait_for_timeout(4600)
            self.assertTrue(
                page.evaluate(
                    "uiOriginalRow===document.querySelector('table[data-action-table] tbody tr')"
                ),
                "Actualisation inchangée doit préserver le DOM et le focus",
            )
            for path in [
                "/catalogue",
                "/administration/utilisateurs",
                "/achats/besoins",
                "/receptions",
            ]:
                page.goto(self.live_server_url + path)
                for w, h in SIZES:
                    page.set_viewport_size({"width": w, "height": h})
                    page.wait_for_timeout(100)
                    actions = page.locator(
                        "table[data-action-table] tbody tr td:last-child a:visible"
                    ).evaluate_all(
                        "ns=>ns.map(n=>{let r=n.getBoundingClientRect();let w=n.closest('.table-scroll,.table-wrap').getBoundingClientRect();return {label:n.textContent.trim(),inside:r.left>=w.left-1&&r.right<=w.right+1,left:r.left,right:r.right}})"
                    )
                    self.assertTrue(all(a["inside"] for a in actions), (path, w, actions))
                    rows.append({"path": path, "width": w, "actions": actions})
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto(self.live_server_url + "/catalogue")
            page.get_by_label("Rechercher un produit", exact=True).fill("Gants")
            page.wait_for_timeout(500)
            perf = page.evaluate("uiPerf")
            perf["interaction_note"] = (
                "Durées Event Timing locales ; absence d’événement lent signifie <16ms, pas un INP terrain."
            )
            (ROOT / "performance.json").write_text(json.dumps(perf, indent=2) + "\n")
            self.assertGreater(perf["lcp"], 0)
            self.assertLess(perf["lcp"], 4000)
            self.assertLess(perf["cls"], 0.25)
            self.assertLess(max(perf["events"], default=0), 500)
            self.shot(page, "keyboard-and-performance")
            browser.close()
        (ROOT / "action-visibility.json").write_text(json.dumps(rows, indent=2) + "\n")

    def test_06_real_browser_zoom_200_percent_three_roles(self):
        from tempfile import TemporaryDirectory

        ROOT.mkdir(parents=True, exist_ok=True)
        extension = Path("tests/assets/zoom-extension").resolve()
        records = []
        with sync_playwright() as pw:
            for user, path in [
                (self.admin, "/catalogue"),
                (self.assistant, "/demandes/nouvelle"),
                (self.replenisher, "/stocks"),
            ]:
                with TemporaryDirectory() as profile:
                    context = pw.chromium.launch_persistent_context(
                        profile,
                        channel="chromium",
                        headless=True,
                        viewport={"width": 1440, "height": 900},
                        args=[
                            f"--disable-extensions-except={extension}",
                            f"--load-extension={extension}",
                        ],
                    )
                    page = context.new_page()
                    self.login(page, user)
                    page.goto(self.live_server_url + path)
                    worker = (
                        context.service_workers[0]
                        if context.service_workers
                        else context.wait_for_event("serviceworker")
                    )
                    self.assertEqual(worker.evaluate("(f)=>setRecetteZoom(f)", 2), 2)
                    page.wait_for_timeout(400)
                    self.assertLessEqual(
                        page.evaluate("document.documentElement.scrollWidth"),
                        page.evaluate("innerWidth"),
                    )
                    shot = self.shot(page, "zoom-200-" + user.role)
                    records.append(
                        {
                            "role": user.role,
                            "chrome_zoom": 2,
                            "innerWidth": page.evaluate("innerWidth"),
                            "screenshot": shot,
                        }
                    )
                    context.close()
        (ROOT / "zoom.json").write_text(json.dumps(records, indent=2) + "\n")

    def test_07_only_one_current_bottom_navigation_entry(self):
        ROOT.mkdir(parents=True, exist_ok=True)
        states = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 390, "height": 844})
            self.login(page, self.replenisher)
            for path, expected in [
                ("/receptions", "/receptions"),
                ("/receptions/historique", "/receptions"),
                ("/receptions/signalements", "/receptions"),
                ("/stocks/signalements", "/stocks/signalements"),
                ("/livraisons/signalements", "/stocks/signalements"),
            ]:
                page.goto(self.live_server_url + path)
                links = page.locator(".bottom-navigation a[aria-current=page]")
                expect(links).to_have_count(1)
                expect(links).to_have_attribute("href", expected)
                states.append({"path": path, "active": expected})
                self.shot(page, "navigation-" + path.strip("/").replace("/", "-"))
            browser.close()
        (ROOT / "bottom-navigation.json").write_text(json.dumps(states, indent=2) + "\n")
