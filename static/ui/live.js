"use strict";
// Lecture automatique seulement. Aucune confirmation ni quantité en file d'attente.
window.ReserveLive = (() => {
  const subscribers = new Set();
  let timer, running = false, stopped = false, controller, failures = 0;
  const status = document.getElementById("live-status");
  function message(text = "") {
    if (!status) return;
    status.hidden = !text;
    if (status.textContent !== text) status.textContent = text;
  }
  function schedule(delay = 2000) {
    clearTimeout(timer);
    if (!stopped && subscribers.size && !document.hidden) timer = setTimeout(tick, delay);
  }
  async function tick() {
    if (running || stopped || document.hidden) return;
    if (!navigator.onLine) { message(gettext("Hors connexion — données potentiellement obsolètes. Aucun envoi automatique.")); return; }
    running = true;
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      for (const refresh of subscribers) await refresh();
      failures=0; message();
    } catch {
      failures+=1;
      if (!stopped) message(gettext("Actualisation indisponible — données potentiellement obsolètes. Vérifiez votre connexion ou reconnectez-vous."));
    } finally {
      clearTimeout(timeout); running = false; schedule(Math.min(30000,2000 * 2 ** Math.min(failures,4)));
    }
  }
  async function read(url, html = false) {
    const target = new URL(url, location.href);
    if (target.origin !== location.origin) throw new Error(gettext("Lecture extérieure interdite"));
    const response = await fetch(target, {credentials:"same-origin",cache:"no-store",headers:{"X-Reserve-Background":"1"},signal:controller?.signal});
    if (!response.ok || (html && new URL(response.url).pathname !== target.pathname)) throw new Error(gettext("Lecture non autorisée ou indisponible"));
    return html ? response.text() : response.json();
  }
  async function products(url, params, count) {
    const result = {products:[],next_offset:null};
    params = new URLSearchParams(params);
    do {
      const page = await read(url + "?" + params);
      result.products.push(...page.products); result.next_offset = page.next_offset;
      params.set("offset", String(page.next_offset));
    } while (result.next_offset !== null && result.products.length < count);
    return result;
  }
  function subscribe(refresh) { subscribers.add(refresh); schedule(); }
  function reload(interactive = false) {
    if(stopped)return;
    stopped=true;clearTimeout(timer);controller?.abort();
    const url=new URL(location.href);
    if(interactive)url.searchParams.delete("_live");else url.searchParams.set("_live","1");
    location.replace(url);
  }
  function notify() { channel?.postMessage("changed"); schedule(0); }
  const channel = typeof BroadcastChannel === "function" ? new BroadcastChannel("reserve-view-update") : null;
  if (channel) channel.onmessage = () => schedule(0);
  window.addEventListener("online", () => schedule(0));
  window.addEventListener("offline", () => { if (subscribers.size) message(gettext("Hors connexion — données potentiellement obsolètes. Aucun envoi automatique.")); });
  window.addEventListener("focus", () => schedule(0));
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(timer); else schedule(0); });
  // Priorité à une navigation manuelle, dès son départ (pagehide arrive trop tard).
  // Les éditeurs stock/panier ont un dialogue d'abandon et ne naviguent jamais
  // automatiquement ; leurs gardes restent responsables du choix « Rester ».
  if(!document.querySelector("#stock-editor,#request-cart"))window.addEventListener("beforeunload",()=>{stopped=true;clearTimeout(timer);controller?.abort();});
  window.addEventListener("pagehide", () => { stopped = true; clearTimeout(timer); controller?.abort(); });
  window.addEventListener("pageshow", () => { stopped = false; schedule(0); });
  // Les régions désignées ne contiennent que des listes en lecture ; ni formulaires
  // d'édition ni scripts. Les filtres et leur saisie restent en dehors.
  const regions = [...document.querySelectorAll("[data-live-region]")];
  if (regions.length) subscribe(async () => {
    const html = await read(location.href, true);
    const documentNext = new DOMParser().parseFromString(html, "text/html");
    for (const region of regions) {
      const next = documentNext.getElementById(region.id);
      if (!next) throw new Error(gettext("Session à revérifier"));
      if (next.innerHTML === (window.ReserveUI.tables?.comparableHTML(region) ?? region.innerHTML)) continue;
      const expanded = [...region.querySelectorAll("details[open]")].map(node => node.dataset.liveKey);
      const focused = region.contains(document.activeElement) ? document.activeElement.getAttribute("href") : null;
      region.replaceChildren(...next.childNodes);
      for (const node of region.querySelectorAll("details")) if (expanded.includes(node.dataset.liveKey)) node.open = true;
      if (focused) [...region.querySelectorAll("a")].find(node => node.getAttribute("href") === focused)?.focus({preventScroll:true});
    }
  });
  return {subscribe, read, products, notify, reload};
})();
