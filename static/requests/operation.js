"use strict";
// Client M4 du protocole d'intention M0 : aucune file locale de mouvements.
window.ReserveRequestOperation = class {
  constructor({actorId, kind, resourceId = null, csrf, onState, onTerminal}) {
    Object.assign(this, {kind, resourceId, csrf, onState, onTerminal});
    this.storageKey = `reserve.request.pending.${actorId}.${kind}.${resourceId || "new"}`;
    this.busy = false; this.pending = null;
    try {
      const saved = JSON.parse(sessionStorage.getItem(this.storageKey));
      if (saved && /^[0-9a-f-]{36}$/.test(saved.intent_id) && /^[0-9a-f]{32}$/.test(saved.key) && /^[0-9a-f]{64}$/.test(saved.payload_hash) && saved.command_type === kind && (saved.resource_id || null) === resourceId) this.pending = saved;
    } catch { /* pas de quantité ni corps métier conservé */ }
  }
  message(text, error = false) {
    const node = document.createElement("p"); node.textContent = text;
    node.className = error ? "notice error" : "stock-success";
    document.querySelector("#operation-result").replaceChildren(node);
  }
  state() {
    document.getElementById("operation-busy").hidden = !this.busy;
    document.querySelector("#operation-recovery").hidden = !this.pending;
    document.querySelector("#operation-network").hidden = navigator.onLine;
    for (const id of ["operation-check", "operation-resolve"]) document.getElementById(id).disabled = this.busy || !navigator.onLine;
    this.onState();
  }
  save(meta) {
    this.pending = meta;
    try { if (meta) sessionStorage.setItem(this.storageKey, JSON.stringify(meta)); else sessionStorage.removeItem(this.storageKey); }
    catch { this.message(gettext("La reprise après rechargement est indisponible. Gardez cette page ouverte jusqu'au résultat."), true); }
    this.state();
  }
  async fetch(url, payload = null, headers = {}) {
    const response = await fetch(url, {method: payload === null ? "GET" : "POST", credentials: "same-origin", cache: "no-store", headers: {"X-CSRFToken": this.csrf, ...(payload === null ? {} : {"Content-Type":"application/json"}), ...headers}, ...(payload === null ? {} : {body: JSON.stringify(payload)})});
    return {status: response.status, body: await response.json()};
  }
  async check(resolve = false) {
    if (!this.pending || this.busy || !navigator.onLine) return;
    this.busy = true; this.state();
    try {
      if (resolve) {
        // Même liaison uniquement : peut certifier un abandon si le premier POST
        // n'était jamais arrivé ; n'invalide pas une confirmation concurrente.
        const registration = await this.fetch("/api/v1/operation-intents", this.pending);
        if (registration.status !== 200) { this.message(registration.body.error?.message || gettext("Vérification impossible. Reconnectez-vous avec le même compte."), true); return; }
        await this.fetch(`/api/v1/operation-intents/${this.pending.intent_id}/resolve`, {});
      }
      const response = await this.fetch(`/api/v1/operation-intents/${this.pending.intent_id}`);
      if (response.status === 200 && ["CONFIRMED", "REJECTED", "EXPIRED"].includes(response.body.state)) {
        this.save(null);
        if(response.body.state === "CONFIRMED") window.ReserveLive?.notify();
        await this.onTerminal(response.body.response, response.body.http_status);
      } else {
        this.message(response.status === 404 ? gettext("Intention encore introuvable. Résolvez cet envoi avant de recommencer.") : response.body.state === "READY" ? gettext("Résultat encore incertain. Résolvez cet envoi ou vérifiez à nouveau ; aucune nouvelle demande n'a été envoyée automatiquement.") : response.body.error?.message || gettext("Confirmation en cours. Vérifiez à nouveau."), true);
      }
    } catch { this.message(gettext("Connexion indisponible. Vérifiez l'enregistrement avant de recommencer ; aucun envoi automatique."), true); }
    finally { this.busy = false; this.state(); }
  }
  async send(url, payload) {
    if (this.busy || this.pending || !navigator.onLine) return;
    this.busy = true; this.state();
    try {
      const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === "object" ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
      const bytes = new TextEncoder().encode(JSON.stringify(canonical(payload)));
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)), x => x.toString(16).padStart(2,"0")).join("");
      const meta = {intent_id:crypto.randomUUID(), key:crypto.randomUUID().replaceAll("-",""), payload_hash:hash, command_type:this.kind};
      if (this.resourceId) meta.resource_id = this.resourceId;
      this.save(meta);
      const registered = await this.fetch("/api/v1/operation-intents",meta);
      if (registered.status !== 200) { this.message(registered.body.error?.message || gettext("Inscription non confirmée. Vérifiez l'enregistrement."), true); return; }
      const response = await this.fetch(url,payload,{"Operation-Intent":meta.intent_id,"Idempotency-Key":meta.key});
      if (response.status >= 500) { this.message(gettext("Envoi non confirmé. Vérifiez l'enregistrement avant de recommencer."),true); return; }
      this.busy = false; await this.check();
    } catch { this.message(gettext("Connexion indisponible pendant l'envoi. Vérifiez l'enregistrement ; aucun envoi automatique."),true); }
    finally { this.busy = false; this.state(); }
  }
  start() {
    document.querySelector("#operation-check").addEventListener("click",()=>this.check());
    document.querySelector("#operation-resolve").addEventListener("click",()=>this.check(true));
    window.addEventListener("offline",()=>this.state()); window.addEventListener("online",()=>this.state());
    this.state(); if (this.pending) this.check();
  }
};
