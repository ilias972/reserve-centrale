"use strict";
(() => {
  const section = document.querySelector("[data-push-settings]");
  if (!section) return;
  const button = document.getElementById("enable-push");
  const status = document.getElementById("push-status");
  const controls = document.getElementById("push-controls");
  const csrf = () => document.querySelector("[name=csrfmiddlewaretoken]")?.value ?? decodeURIComponent(document.cookie.split("; ").find(c=>c.startsWith("csrftoken="))?.split("=")[1] ?? "");
  async function post(url, body) {
    const response = await fetch(url, {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json", "X-CSRFToken":csrf()}, body:JSON.stringify(body)});
    if (!response.ok) throw new Error(gettext("Enregistrement non confirmé. Rechargez cette page pour vérifier l'abonnement avant de réessayer."));
    return response.json();
  }
  function showStatus(message, className="notice") {
    if (!status) return;
    status.hidden = false;
    status.className = className;
    status.textContent = message;
  }
  function showSubscriptionSuccess() {
    if (!status) return;
    status.hidden = false;
    status.className = "stock-success";
    status.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = gettext("Notifications activées sur cet appareil");
    status.append(title, document.createElement("br"), document.createTextNode(gettext("Votre inscription est enregistrée. La réception dépend aussi des réglages iOS et de la connexion.")));
    status.dataset.subscribed = "true";
  }
  function applicationKey(value) {
    return Uint8Array.from(
      atob(value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=")),
      character => character.charCodeAt(0)
    );
  }
  function keyMatches(subscription, expected) {
    const stored = subscription?.options?.applicationServerKey;
    if (!stored) return null;
    const actual = stored instanceof ArrayBuffer
      ? new Uint8Array(stored)
      : ArrayBuffer.isView(stored)
        ? new Uint8Array(stored.buffer, stored.byteOffset, stored.byteLength)
        : null;
    if (!actual) return null;
    return actual.length === expected.length && actual.every((byte, index) => byte === expected[index]);
  }
  async function getPushConfig() {
    const response = await fetch("/api/v1/push-subscriptions", {credentials:"same-origin",cache:"no-store"});
    if (!response.ok) throw new Error(gettext("Reconnectez-vous puis réessayez."));
    const config = await response.json();
    if (!config.enabled) throw new Error(gettext("Le Web Push n'est pas configuré."));
    return { ...config, applicationServerKey: applicationKey(config.public_key) };
  }
  async function revoke(control) {
    control.disabled=true;
    try {
      // Révocation serveur d'abord : même si le navigateur refuse unsubscribe,
      // aucun futur envoi n'est possible via cet abonnement.
      await post(`/api/v1/push-subscriptions/${control.dataset.subscription}/revoke`, {});
      const registration = await navigator.serviceWorker?.getRegistration?.();
      const subscription = await registration?.pushManager?.getSubscription?.();
      if (subscription) await subscription.unsubscribe();
      window.location.reload();
    } catch {
      showStatus(gettext("Vérifiez l'état de l'abonnement en rechargeant la page."));
      control.disabled=false;
    }
  }
  function bindRevoke(control) {
    control.addEventListener("click", () => revoke(control));
  }
  for (const control of document.querySelectorAll(".revoke-push")) bindRevoke(control);

  const unsupported = !window.isSecureContext || !("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window);
  if (unsupported) {
    if (button) button.hidden = true;
    if (status?.dataset.subscribed === "true") {
      showStatus(gettext("L'abonnement est enregistré, mais ce navigateur ne peut pas afficher les notifications. La boîte reste disponible."));
    } else {
      showStatus(gettext("Ce navigateur ne prend pas en charge les notifications de cette application. La boîte reste disponible."));
    }
    return;
  }

  if (status?.dataset.subscribed === "true") {
    (async () => {
      try {
        const registration = await navigator.serviceWorker.getRegistration("/");
        const subscription = await registration?.pushManager?.getSubscription();
        if (Notification.permission !== "granted" || !subscription) {
          showStatus(gettext("L'abonnement est enregistré, mais les notifications ne sont pas autorisées actuellement sur cet appareil. Vérifiez ses réglages."));
          return;
        }
        const config = await getPushConfig();
        if (keyMatches(subscription, config.applicationServerKey) === false) {
          showStatus(gettext("Cet appareil garde une ancienne inscription. Désactivez cet abonnement, puis réactivez les notifications."), "notice");
        }
      } catch {
        showStatus(gettext("L'abonnement est enregistré. Vérifiez les réglages de notification de cet appareil."));
      }
    })();
  }

  if (button) button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      // Demande directement liée au clic : aucune permission à l'ouverture de page.
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {showStatus(gettext("Notifications non autorisées. Vous pouvez continuer à consulter les demandes dans l'application."));return;}
      const config = await getPushConfig();
      const registration = await navigator.serviceWorker.register("/service-worker.js", {scope:"/"});
      await navigator.serviceWorker.ready;
      let subscription = await registration.pushManager.getSubscription();
      if (subscription && keyMatches(subscription, config.applicationServerKey) === false) {
        const removed = await subscription.unsubscribe();
        if (!removed) throw new Error(gettext("Ancienne inscription non renouvelée. Désactivez-la puis réessayez."));
        subscription = null;
      }
      subscription = subscription || await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:config.applicationServerKey});
      const saved = await post("/api/v1/push-subscriptions", {endpoint:subscription.endpoint,keys:subscription.toJSON().keys,language:document.documentElement.lang});
      button.hidden = true;
      showSubscriptionSuccess();
      if (controls && saved.id) {
        const control = document.createElement("button");
        control.type = "button";
        control.className = "secondary revoke-push";
        control.dataset.subscription = saved.id;
        control.textContent = gettext("Désactiver cet abonnement");
        bindRevoke(control);
        controls.append(control);
      }
    } catch(error) {showStatus(error instanceof Error ? error.message : gettext("Abonnement non confirmé. Rechargez la page."));}
    finally {button.disabled=false;}
  });
})();
