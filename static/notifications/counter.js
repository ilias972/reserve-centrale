"use strict";
(() => {
  const badges = document.querySelectorAll("[data-notification-count]"), labels = document.querySelectorAll("[data-notification-count-label]");
  if (!badges.length || !window.ReserveLive) return;
  window.ReserveLive.subscribe(async () => {
    const data = await window.ReserveLive.read("/api/v1/notifications/unread-count");
    if (!Number.isSafeInteger(data.unread) || data.unread < 0) throw new Error(gettext("Compteur indisponible"));
    for (const badge of badges) {badge.textContent = String(data.unread); badge.hidden = data.unread === 0;}
    for (const label of labels) label.textContent = window.ReserveI18n.plural("%(count)s notification non lue", "%(count)s notifications non lues", data.unread);
  });
  const list = document.getElementById("notification-list");
  if (list) window.ReserveLive.subscribe(async () => {
    // Une lecture automatique ne remplace pas un contrôle en cours d'utilisation.
    if (list.contains(document.activeElement)) return;
    const html = await window.ReserveLive.read(location.href, true);
    const next = new DOMParser().parseFromString(html, "text/html").getElementById("notification-list");
    if (list.contains(document.activeElement)) return;
    if (!next) throw new Error(gettext("Reconnectez-vous pour consulter les notifications"));
    if (list.dataset.revision !== next.dataset.revision) {
      list.replaceChildren(...next.childNodes);
      list.dataset.revision = next.dataset.revision;
    }
  });
})();
