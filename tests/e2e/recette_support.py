"""Instrumentation de recette uniquement ; jamais chargée par l'application."""

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect

ROOT = Path("reports/recette-pc")
AXE = Path("tests/assets/axe.min.js")


class Walk:
    def __init__(self, test_name):
        self.folder = ROOT / test_name
        self.folder.mkdir(parents=True, exist_ok=True)
        # Ces artefacts de travail appartiennent à ce test ; les preuves archivées
        # et les données manuelles sont dans des répertoires distincts.
        for previous in self.folder.iterdir():
            if previous.is_file() and previous.suffix in {".png", ".json"}:
                previous.unlink()
        self.steps, self.pages, self.console, self.network = [], [], [], []
        self.events = []
        self.expected_incident = False
        self.start = time.monotonic()

    def attach(self, page, role):
        page.expose_binding(
            "recetteEvent",
            lambda source, event: self.events.append(
                {"role": role, "at": datetime.now(UTC).isoformat(), **event}
            ),
        )
        page.add_init_script("""(() => {
            for(const type of ['click','input','change','submit'])document.addEventListener(type,event=>{
                const n=event.target.closest?.('a,button,input,select,textarea,summary,form');if(!n)return;
                window.recetteEvent({event:type,path:location.pathname,tag:n.tagName,id:n.id,name:n.name||'',label:n.getAttribute('aria-label')||[...n.labels||[]].map(l=>l.textContent.trim()).join(' ')||(n.tagName==='INPUT'?'':n.textContent.trim().slice(0,160)),inputType:n.type||'',href:n.getAttribute('href'),trusted:event.isTrusted});
            },true);
        })();""")
        page.on(
            "pageerror",
            lambda error: self.console.append(
                {
                    "role": role,
                    "type": "pageerror",
                    "message": str(error),
                    "expected_incident": self.expected_incident,
                }
            ),
        )
        page.on(
            "console",
            lambda event: (
                self.console.append(
                    {
                        "role": role,
                        "type": event.type,
                        "message": event.text,
                        "location": event.location,
                        "expected_incident": self.expected_incident,
                    }
                )
                if event.type in {"warning", "error"}
                else None
            ),
        )
        page.on(
            "response",
            lambda response: self.network.append(
                {
                    "role": role,
                    "method": response.request.method,
                    "path": urlsplit(response.url).path,
                    "status": response.status,
                    "background_read": response.request.method == "GET"
                    and response.request.headers.get("x-reserve-background") == "1",
                    "navigation": response.request.is_navigation_request(),
                    "expected_incident": self.expected_incident,
                }
            ),
        )
        page.on(
            "requestfailed",
            lambda request: self.network.append(
                {
                    "role": role,
                    "method": request.method,
                    "path": urlsplit(request.url).path,
                    "failure": request.failure,
                    "background_read": request.method == "GET"
                    and request.headers.get("x-reserve-background") == "1",
                    "navigation": request.is_navigation_request(),
                    "expected_incident": self.expected_incident,
                }
            ),
        )
        return page

    def shot(self, page, suffix):
        name = f"{len(self.steps):03}-{suffix}.png"
        for attempt in range(2):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
                page.screenshot(
                    path=str(self.folder / name),
                    full_page=True,
                    timeout=15_000,
                    mask=[
                        page.locator(
                            'input[type="password"],input[autocomplete="current-password"],input[autocomplete="new-password"],#temporary'
                        )
                    ],
                )
                break
            except PlaywrightError as error:
                if attempt or "Execution context was destroyed" not in str(error):
                    raise
                # A concurrent live refresh can replace the page while Chromium
                # waits for fonts during a screenshot. Capture the settled page once.
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
        return name

    def step(self, page, title, action, check=None):
        before = self.shot(page, "before")
        started = datetime.now(UTC).isoformat()
        action()
        if check:
            check()
        # Regarder l'état réellement stabilisé, y compris les annonces et transitions.
        page.wait_for_timeout(650)
        after = self.shot(page, "after")
        self.steps.append(
            {
                "at": started,
                "path": urlsplit(page.url).path,
                "action": title,
                "before": before,
                "after": after,
                "verified": check is not None,
                "console_read": len(self.console),
                "network_read": len(self.network),
            }
        )
        self.save()

    def inspect(self, page, role, label):
        page.wait_for_load_state("networkidle")
        page.evaluate("document.fonts.ready")
        page.evaluate(AXE.read_text())
        accessibility = page.evaluate(
            "async()=>{const r=await axe.run();return {violations:r.violations.map(v=>({id:v.id,impact:v.impact,help:v.help,nodes:v.nodes.map(n=>({target:n.target,html:n.html,summary:n.failureSummary}))})),passes:r.passes.length,incomplete:r.incomplete.map(v=>v.id)}}"
        )
        controls = page.locator(
            "a:visible,button:visible,input:visible,select:visible,textarea:visible,summary:visible"
        ).evaluate_all(
            "ns=>ns.map(n=>({tag:n.tagName,label:n.getAttribute('aria-label')||Array.from(n.labels||[]).map(l=>l.textContent.trim()).join(' '),text:n.tagName==='INPUT'?'':n.textContent?.trim().slice(0,180),id:n.id,type:n.type,href:n.getAttribute('href'),disabled:!!n.disabled}))"
        )
        layout = page.evaluate(
            "({width:innerWidth,height:innerHeight,scroll:document.documentElement.scrollWidth,viewport:visualViewport?{width:visualViewport.width,scale:visualViewport.scale}:null})"
        )
        screenshot = self.shot(page, re.sub(r"[^a-z0-9-]", "-", role.lower() + "-" + label.lower()))
        self.pages.append(
            {
                "role": role,
                "label": label,
                "path": urlsplit(page.url).path,
                "viewport": layout,
                "controls": controls,
                "axe": accessibility,
                "screenshot": screenshot,
                "console_read": len(self.console),
            }
        )
        self.save()
        return accessibility

    def save(self):
        (self.folder / "manifest.json").write_text(
            json.dumps(
                {
                    "duration_seconds": round(time.monotonic() - self.start, 3),
                    "steps": self.steps,
                    "events": self.events,
                    "pages": self.pages,
                    "console": self.console,
                    "network": self.network,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )

    def clean(self, test):
        self.save()
        test.assertEqual(
            [
                (p["role"], p["path"], v["id"])
                for p in self.pages
                for v in p["axe"]["violations"]
                if v["impact"] in {"serious", "critical"}
            ],
            [],
        )
        expected_console = re.compile(
            r"Failed to load resource: (?:net::ERR_FAILED|the server responded with a status of (?:400|401|403|404|409|422) )"
        )
        test.assertEqual(
            [
                e
                for e in self.console
                if not (e["expected_incident"] and expected_console.search(e["message"]))
            ],
            [],
        )
        unexpected = []
        for event in self.network:
            status = event.get("status", 0)
            path = event["path"]
            expected = event["expected_incident"] and (
                (status == 400 and path.endswith("/photographie"))
                or (status == 401 and path == "/connexion")
                or (
                    status == 401
                    and path == "/api/v1/notifications/unread-count"
                    and event.get("role") == "ASSISTANT"
                    and event["method"] == "GET"
                    and event.get("background_read") is True
                )
                or (status == 403 and path.startswith("/administration/"))
                or (status == 404 and path.startswith(("/demandes/", "/api/v1/operation-intents/")))
                or (
                    status == 409
                    and path.startswith(("/api/v1/requests", "/api/v1/stocks/adjustments"))
                )
                or (
                    status == 409
                    and event["method"] == "POST"
                    and path.startswith("/api/v1/operation-intents/")
                    and path.endswith("/resolve")
                )
                or (
                    "failure" in event
                    and path.startswith(("/api/v1/operation-intents", "/api/v1/requests"))
                )
            )
            # Une navigation/fermeture annule légitimement un GET de fond.
            # Jamais un POST, une navigation principale, un 4xx/5xx ou ERR_FAILED.
            cancelled_read = (
                event.get("background_read") is True
                and event.get("navigation") is False
                and event.get("failure") == "net::ERR_ABORTED"
            )
            if (status >= 400 or "failure" in event) and not (expected or cancelled_read):
                unexpected.append(event)
        test.assertEqual(unexpected, [])


def login(walk, page, base, user, password):
    page.goto(base + "/connexion")
    walk.step(
        page,
        "Connexion par le formulaire individuel",
        lambda: (
            page.get_by_label("Identifiant", exact=True).fill(user.username),
            page.get_by_label("Mot de passe", exact=True).fill(password),
            page.get_by_role("button", name="Se connecter", exact=True).click(),
        ),
        lambda: expect(page.get_by_role("heading", name="Mon compte", exact=True)).to_be_visible(),
    )


def browser_command(page, path, kind, payload, resource_id):
    """Attaque de contournement contrôlée : vrai fetch, CSRF et intention M0."""
    return page.evaluate(
        """async ({path,kind,payload,resource})=>{
        const canonical=v=>Array.isArray(v)?v.map(canonical):v&&typeof v==='object'?Object.fromEntries(Object.keys(v).sort().map(k=>[k,canonical(v[k])])):v;
        const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(canonical(payload))))),x=>x.toString(16).padStart(2,'0')).join('');
        const meta={intent_id:crypto.randomUUID(),key:crypto.randomUUID().replaceAll('-',''),payload_hash:hash,command_type:kind,resource_id:resource};
        const headers={'Content-Type':'application/json','X-CSRFToken':document.querySelector('[name=csrfmiddlewaretoken]').value};
        const registered=await fetch('/api/v1/operation-intents',{method:'POST',headers,body:JSON.stringify(meta)});
        if(registered.status!==200)return {status:registered.status,registration:await registered.json()};
        const response=await fetch(path,{method:'POST',headers:{...headers,'Operation-Intent':meta.intent_id,'Idempotency-Key':meta.key},body:JSON.stringify(payload)});
        return {status:response.status,body:await response.json()};
    }""",
        {"path": path, "kind": kind, "payload": payload, "resource": resource_id},
    )
