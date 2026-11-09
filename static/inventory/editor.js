"use strict";
(() => {
  const config = JSON.parse(document.querySelector("#stock-data").textContent);
  const form = document.querySelector("#stock-editor");
  const list = document.querySelector("#stock-lines");
  const result = document.querySelector("#stock-result");
  const recovery = document.querySelector("#stock-recovery");
  const conflicts = document.querySelector("#stock-conflicts");
  const csrf = form.querySelector("[name=csrfmiddlewaretoken]").value;
  const confirm = document.querySelector("#stock-confirm");
  const cancel = document.querySelector("#stock-cancel");
  const storageKey = "reserve.stock.pending." + config.actor_id;
  const known = new Map(config.rows.map(row => [row.product_id, row]));
  const drafts = new Map();
  let visible = config.rows.map(row => row.product_id);
  let nextOffset = config.next_offset;
  let busy = false;
  let reviewRows = null;
  let pending = null;
  let modifiedOnly=false, loadSequence=0;
  let appliedParams=new URLSearchParams(new FormData(document.getElementById("stock-filter")));
  try { pending = JSON.parse(sessionStorage.getItem(storageKey)); } catch { /* aucune donnée de mouvement persistée */ }
  if (pending && (!/^[0-9a-f-]{36}$/.test(pending.intent_id || "") || !/^[0-9a-f]{32}$/.test(pending.key || "") || !/^[0-9a-f]{64}$/.test(pending.payload_hash || "") || !["inventory.initial", "inventory.adjust"].includes(pending.command_type))) {
    pending = null; sessionStorage.removeItem(storageKey);
  }
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function notice(text, error = false, caseUrl = null) {
    result.replaceChildren(element("p", text, error ? "notice" : "stock-success"));
    if (caseUrl && /^\/stocks\/signalements\/[0-9a-f-]+$/.test(caseUrl)) {
      const link = element("a", gettext("Ouvrir le signalement")); link.href = caseUrl; result.append(link);
    }
  }
  function updateActions() {
    document.getElementById("stock-busy").hidden = !busy;
    const invalid=Boolean(form.querySelector("input:invalid"));
    if (confirm) confirm.disabled = !drafts.size || invalid || busy || Boolean(pending) || !navigator.onLine;
    if (cancel) cancel.disabled = (!drafts.size && !invalid) || busy || Boolean(pending);
    recovery.hidden = !pending;
    document.querySelector("#stock-network").hidden = navigator.onLine;
    document.querySelector("#stock-more").hidden = nextOffset === null;
    document.querySelectorAll(".stock-row").forEach(card=>card.hidden=modifiedOnly && !drafts.has(card.dataset.productId) && !card.querySelector("input:invalid"));
    document.querySelector("#stock-empty").hidden=Boolean(document.querySelector(".stock-row:not([hidden])"));
    const count=document.getElementById("stock-filter-count");if(count)count.textContent=String(drafts.size);
    const toolbar=document.querySelector(".stock-confirmation");if(toolbar)toolbar.hidden=!drafts.size && !invalid;
    if (config.can_edit) {
      document.querySelector("#stock-modified-count").textContent = invalid ? gettext("Corrigez les quantités signalées avant confirmation") : drafts.size ? window.ReserveI18n.plural("%(count)s produit modifié — non enregistré","%(count)s produits modifiés — non enregistrés",drafts.size) : gettext("Aucune modification provisoire");
      const summary = document.querySelector("#stock-summary"); summary.replaceChildren();
      for (const draft of drafts.values()) summary.append(element("li", window.ReserveI18n.format(gettext("%(name)s : %(physical)s → %(target)s %(unit)s"),{name:draft.base.name,physical:draft.base.physical===null?gettext("Non inventorié"):draft.base.physical,target:draft.target,unit:draft.base.unit})));
    }
  }
  function mark(row, input, card) {
    const text = input.value.trim();
    const target = Number(text);
    if (!text || !/^\d+$/.test(text) || !Number.isSafeInteger(target) || target > 1000000000) {
      input.setCustomValidity(text ? gettext("Saisissez un entier positif ou nul.") : gettext("Saisissez explicitement une quantité, zéro compris."));
      drafts.delete(row.product_id); card.classList.remove("stock-dirty");card.classList.add("stock-invalid");
      const error=card.querySelector(".stock-delta");if(error){error.hidden=false;error.textContent=gettext("Saisissez une quantité entière, zéro compris, avant confirmation.");}
      updateActions(); return;
    }
    input.setCustomValidity("");card.classList.remove("stock-invalid");
    const existing = drafts.get(row.product_id);
    const base = existing ? existing.base : {...row};
    if (!config.initial && target === base.physical) drafts.delete(row.product_id);
    else drafts.set(row.product_id, {base, target});
    card.classList.toggle("stock-dirty", drafts.has(row.product_id));
    const delta=card.querySelector(".stock-delta");
    if(delta){delta.hidden=!drafts.has(row.product_id);delta.textContent=window.ReserveI18n.format(gettext("%(physical)s → %(target)s"),{physical:base.physical===null?gettext("Non inventorié"):base.physical,target});}
    updateActions();
  }
  function render() {
    list.replaceChildren();
    for (const identifier of visible) {
      const row = known.get(identifier), draft = drafts.get(identifier);
      const card = element("article", undefined, "card stock-row");
      card.dataset.productId = identifier;
      card.classList.toggle("stock-dirty", Boolean(draft));
      const info = element("div", undefined, "stock-product");
      if (row.photo_url) {
        const image = document.createElement("img"); image.src = row.photo_url + "?thumbnail=1";
        image.alt = `${row.name} — ${row.unit}`; image.width = 70; image.height = 70; image.loading = "lazy"; info.append(image);
      }
      const details = element("div"); const heading=element("h2");const productLink=element("a",row.name);productLink.href=`/catalogue/${identifier}`;heading.append(row.status === "ARCHIVED" && config.can_edit ? document.createTextNode(row.name) : productLink);details.append(heading, element("p", `${row.code} · ${row.unit}`, "hint"));
      const liveQuantities=element("p", row.physical === null ? gettext("Non inventorié — quantité inconnue") : window.ReserveI18n.format(gettext("Réservé %(reserved)s · Disponible %(available)s %(unit)s"),{reserved:row.reserved,available:row.available,unit:row.unit})); liveQuantities.dataset.liveQuantities=""; details.append(liveQuantities);
      if (row.on_hold) details.append(element("p", gettext("Stock à vérifier par la gestionnaire"), "notice"));
      if (row.status === "ARCHIVED") details.append(element("p", gettext("Produit archivé"), "badge"));
      const delta=element("p",draft?window.ReserveI18n.format(gettext("%(physical)s → %(target)s"),{physical:draft.base.physical===null?gettext("Non inventorié"):draft.base.physical,target:draft.target}):"","stock-delta");
      delta.hidden=!draft;details.append(delta);
      info.append(details); card.append(info);
      if (!config.can_edit) {
        const quantities = element("div", undefined, "stock-values"); quantities.append(element("strong", row.physical === null ? gettext("Non inventorié") : window.ReserveI18n.format(gettext("%(quantity)s %(unit)s"),{quantity:row.physical,unit:row.unit})));
        const link = element("a", row.counted_at ? gettext("Ouvrir la fiche produit") : gettext("Fiche produit")); link.href = `/catalogue/${identifier}`; quantities.append(link); card.append(quantities);
      } else if (!config.initial && row.physical === null) {
        const link = element("a", gettext("Compter ce produit")); link.href = "/stocks/inventaire-initial"; card.append(link);
      } else {
        const counter = element("div", undefined, "stock-counter");
        const input = document.createElement("input"); input.type = "text"; input.inputMode = "numeric";
        input.pattern = "[0-9]+"; input.setAttribute("aria-label", window.ReserveI18n.format(gettext("Quantité physique — %(name)s"), {name: row.name}));
        input.value = draft ? String(draft.target) : config.initial ? "" : String(row.physical);
        input.autocomplete = "off"; input.disabled = busy || Boolean(pending);
        input.addEventListener("input", () => mark(row, input, card));
        const minus = element("button", gettext("−"), "secondary"); minus.type = "button";
        minus.setAttribute("aria-label", window.ReserveI18n.format(gettext("Diminuer — %(name)s"), {name: row.name})); minus.disabled = busy || Boolean(pending) || Boolean(row.on_hold);
        const plus = element("button", gettext("+"), "secondary"); plus.type = "button";
        plus.setAttribute("aria-label", window.ReserveI18n.format(gettext("Augmenter — %(name)s"), {name: row.name})); plus.disabled = busy || Boolean(pending);
        minus.addEventListener("click", () => {
          const value = input.value === "" ? 0 : Number(input.value);
          if (!config.initial && value <= row.reserved) {
            notice(window.ReserveI18n.format(gettext("%(reserved)s %(unit)s sont réservés. Si le stock réel est inférieur, saisissez le total constaté puis confirmez le lot pour signaler l'écart à la gestionnaire."),{reserved:row.reserved,unit:row.unit}), true); input.focus(); return;
          }
          input.value = String(Math.max(0, value - 1)); mark(row, input, card);
        });
        plus.addEventListener("click", () => { input.value = String(Math.min(1000000000, (Number(input.value) || 0) + 1)); mark(row, input, card); });
        counter.append(minus, input, plus); card.append(counter);
      }
      list.append(card);
    }
    document.querySelector("#stock-empty").hidden = visible.length > 0;
    updateActions();
  }
  async function request(url, options = {}) {
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store", ...options,
      headers: {"X-CSRFToken": csrf, ...(options.headers || {})}});
    let body;
    try { body = await response.json(); } catch { throw new Error(gettext("Réponse non vérifiable. Consultez le statut avant de recommencer.")); }
    return {status: response.status, body};
  }
  function post(url, payload, headers = {}) {
    return request(url, {method: "POST", headers: {"Content-Type": "application/json", ...headers}, body: JSON.stringify(payload)});
  }
  function setPending(value) {
    pending = value;
    try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); }
    catch { notice(gettext("La récupération après rechargement est indisponible sur ce navigateur. Gardez cette page ouverte jusqu'au résultat."), true); }
    updateActions();
  }
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
    return value;
  }
  async function fingerprint(payload) {
    const bytes = new TextEncoder().encode(JSON.stringify(canonical(payload)));
    return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), byte => byte.toString(16).padStart(2, "0")).join("");
  }
  async function load(append = false) {
    const job=++loadSequence;
    const params = append ? new URLSearchParams(appliedParams) : new URLSearchParams(new FormData(document.querySelector("#stock-filter")));
    if(!append)appliedParams=new URLSearchParams(params);
    params.set("mode", config.initial ? "initial" : "all");
    if (append && nextOffset !== null) params.set("offset", String(nextOffset));
    const response = await request("/api/v1/stocks?" + params);
    if(job!==loadSequence)return;
    if (response.status !== 200) { notice(response.body.error?.message || gettext("Chargement impossible."), true); return; }
    const identifiers = [];
    for (const row of response.body.products) { known.set(row.product_id, row); identifiers.push(row.product_id); }
    visible = append ? [...new Set([...visible, ...identifiers])] : identifiers;
    nextOffset = response.body.next_offset; render();
  }
  function showConflict(current) {
    reviewRows = current; conflicts.hidden = false;
    const content = document.querySelector("#conflict-values"); content.replaceChildren();
    for (const row of current) {
      const draft = drafts.get(row.product_id);
      if (draft) content.append(element("p", window.ReserveI18n.format(gettext("%(name)s : votre saisie %(target)s, serveur %(physical)s, réservé %(reserved)s %(unit)s"),{name:row.name,target:draft.target,physical:row.physical,reserved:row.reserved,unit:row.unit})));
    }
  }
  async function terminal(body, status) {
    setPending(null);
    if (body.result === "CONFIRMED") {
      window.ReserveLive?.notify();
      drafts.clear(); conflicts.hidden = true; reviewRows = null;
      notice(window.ReserveI18n.format(gettext("Lot enregistré une seule fois. Opération %(operation)s, %(confirmed_at)s."),{operation:body.operation_id,confirmed_at:new Intl.DateTimeFormat(document.documentElement.lang,{dateStyle:"short",timeStyle:"short",timeZone:"Europe/Paris"}).format(new Date(body.resource.confirmed_at))}));
    } else {
      notice(body.error?.message || gettext("Aucune mise à jour confirmée."), true, body.case_url);
      if (body.error?.current) showConflict(body.error.current);
    }
    render();
    try { await load(); } catch { result.append(element("p",gettext("Statut définitif reçu, mais actualisation indisponible. Rechargez les quantités avant une nouvelle saisie."),"notice")); }
  }
  async function check(resolve = false) {
    if (!pending) return;
    busy = true; updateActions();
    try {
      if (resolve) {
        // Réinscrire uniquement la même liaison résout aussi une perte AVANT
        // l'inscription. Une confirmation concurrente conserve le même verrou.
        const registration = await post("/api/v1/operation-intents", pending);
        if (registration.status !== 200) { notice(registration.body.error?.message || gettext("Vérification impossible. Reconnectez-vous avec le même compte."), true); return; }
      }
      const response = resolve ? await post(`/api/v1/operation-intents/${pending.intent_id}/resolve`, {}) : await request(`/api/v1/operation-intents/${pending.intent_id}`);
      if (resolve && response.status !== 202) {
        // Le résultat de resolve peut aussi être une erreur d'accès : seul GET
        // certifie l'état terminal avant d'autoriser une nouvelle intention.
        if (response.status === 401 || response.status === 403 || response.status === 404) { notice(response.body.error?.message || gettext("Accès à revérifier."), true); return; }
        busy = false; await check(false); return;
      }
      if (response.body.state && ["CONFIRMED", "REJECTED", "EXPIRED"].includes(response.body.state)) {
        await terminal(response.body.response, response.body.http_status); return;
      }
      notice(response.body.state === "CHECKING" ? gettext("Confirmation encore en cours. Vérifiez à nouveau.") : response.body.state === "READY" ? gettext("Intention retrouvée, résultat encore incertain. Résolvez cette confirmation avant de recommencer.") : response.status === 404 ? gettext("Intention encore introuvable. Résolvez cette confirmation pour vérifier sûrement son abandon.") : response.body.error?.message || gettext("Statut encore non vérifiable. Vérifiez à nouveau."), true);
    } catch { notice(gettext("Connexion indisponible. La confirmation reste à vérifier ; aucun nouvel envoi automatique."), true); }
    finally { busy = false; render(); }
  }
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!config.can_edit || !drafts.size || busy || pending || !navigator.onLine) return;
    busy = true; render();
    const lines = [...drafts.values()].map(draft => config.initial ? {
      product_id: draft.base.product_id, expected_stock_version: draft.base.stock_version,
      expected_product_version: draft.base.product_version, counted_quantity: draft.target
    } : {product_id: draft.base.product_id, expected_stock_version: draft.base.stock_version, target_physical: draft.target});
    const payload = {lines};
    const meta = {intent_id: crypto.randomUUID(), key: crypto.randomUUID().replaceAll("-", ""), command_type: config.initial ? "inventory.initial" : "inventory.adjust"};
    try {
      meta.payload_hash = await fingerprint(payload);
      setPending(meta);
      const registered = await post("/api/v1/operation-intents", meta);
      if (registered.status !== 200) { notice(registered.body.error?.message || gettext("Intention non inscrite."), true); await check(); return; }
      const response = await post(config.initial ? "/api/v1/inventories/initial" : "/api/v1/stocks/adjustments", payload, {"Operation-Intent": meta.intent_id, "Idempotency-Key": meta.key});
      if (response.status >= 500) { notice(gettext("Enregistrement non confirmé. Vérifiez son statut avant toute nouvelle action."), true); return; }
      if (response.body.result || response.body.error) await check();
    } catch { notice(gettext("Connexion indisponible pendant la confirmation. Vérifiez l'enregistrement ; aucun nouvel envoi automatique."), true); }
    finally { busy = false; render(); }
  });
  if (cancel) cancel.addEventListener("click", () => {
    if (busy || pending) return;
    drafts.clear(); reviewRows = null; conflicts.hidden = true; notice(gettext("Modifications provisoires abandonnées. Aucun mouvement envoyé.")); render();
  });
  document.querySelector("#stock-filter").addEventListener("submit", async event => { event.preventDefault(); try { await load(); } catch { notice(gettext("Recherche indisponible. Les modifications provisoires sont conservées en mémoire."), true); } });
  document.querySelector("#stock-more").addEventListener("click", async () => { try { await load(true); } catch { notice(gettext("Chargement indisponible."), true); } });
  document.querySelector("#check-operation").addEventListener("click", () => check());
  document.querySelector("#resolve-operation").addEventListener("click", () => check(true));
  document.querySelector("#review-values").addEventListener("click", () => {
    if (!reviewRows || pending || busy) return;
    for (const row of reviewRows) { const draft = drafts.get(row.product_id); if (draft) draft.base = {...draft.base, ...row}; }
    reviewRows = null; conflicts.hidden = true; notice(gettext("Valeurs serveur reprises après votre contrôle. Reconfirmez explicitement le lot si la saisie est correcte.")); render();
  });
  window.addEventListener("offline", updateActions); window.addEventListener("online", updateActions);
  window.addEventListener("beforeunload", event => { if (drafts.size && !pending) { event.preventDefault(); event.returnValue = ""; } });
  for(const [id,modified] of [["stock-show-all",false],["stock-show-modified",true]]){
    document.getElementById(id)?.addEventListener("click",()=>{modifiedOnly=modified;document.getElementById("stock-show-all").setAttribute("aria-pressed",String(!modified));document.getElementById("stock-show-modified").setAttribute("aria-pressed",String(modified));updateActions();});
  }
  document.getElementById("stock-category").addEventListener("change",()=>document.getElementById("stock-filter").requestSubmit());
  window.ReserveLive.subscribe(async () => {
    if (busy || pending) return;
    const job=loadSequence;
    const params=new URLSearchParams(appliedParams);
    params.set("mode",config.initial ? "initial" : "all");
    const data=await window.ReserveLive.products("/api/v1/stocks",params,visible.length);
    if(job!==loadSequence || busy || pending)return;
    const changed=JSON.stringify(data.products)!==JSON.stringify(visible.map(id=>known.get(id)));
    const editing=drafts.size || form.querySelector("input:invalid") || list.contains(document.activeElement);
    if(editing){
      // Ne jamais remplacer la base/version du comptage provisoire ni la saisie.
      for(const row of data.products){
        const card=[...list.children].find(node=>node.dataset.productId===row.product_id);
        const text=card?.querySelector("[data-live-quantities]");
        if(text)text.textContent=row.physical===null ? gettext("Non inventorié — quantité inconnue") : window.ReserveI18n.format(gettext("Réservé %(reserved)s · Disponible %(available)s %(unit)s"),{reserved:row.reserved,available:row.available,unit:row.unit});
      }
      return;
    }
    if(changed){for(const row of data.products)known.set(row.product_id,row);visible=data.products.map(row=>row.product_id);nextOffset=data.next_offset;render();}
  });
  render();
  if (pending) check();
})();
