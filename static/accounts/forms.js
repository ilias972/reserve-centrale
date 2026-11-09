"use strict";
for (const form of document.querySelectorAll("form[method='post']:not([data-file-download])")) {
  form.addEventListener("submit", (event) => {
    if (form.action.endsWith("/deconnexion")) {
      for (const key of Object.keys(sessionStorage)) if ((key.startsWith("reserve.stock.pending.") || key.startsWith("reserve.request.pending."))) sessionStorage.removeItem(key);
    }
    const status = form.querySelector(".submit-status");
    if (!navigator.onLine) {
      event.preventDefault();
      if (status) status.textContent = gettext("Connexion indisponible. Aucun envoi effectué. Reconnectez-vous avant de confirmer.");
      return;
    }
    if (status) status.textContent = gettext("Vérification en cours… En cas d'interruption, consultez l'état enregistré avant une nouvelle action.");
    for (const button of form.querySelectorAll("button[type='submit']")) button.disabled = true;
  });
}
window.addEventListener("pageshow", () => {
  for (const button of document.querySelectorAll("form[method=post] button[disabled]")) button.disabled = false;
});
for (const field of document.querySelectorAll("input[type='password']")) {
  const toggle = document.createElement("button");
  const name = field.labels[0]?.textContent || gettext("mot de passe");
  toggle.type = "button";
  toggle.className = "secondary password-toggle";
  toggle.textContent = gettext("Afficher");
  toggle.setAttribute("aria-label", window.ReserveI18n.format(gettext("Afficher : %(name)s"), {name}));
  toggle.setAttribute("aria-controls", field.id);
  toggle.setAttribute("aria-pressed", "false");
  toggle.addEventListener("click", () => {
    const visible = field.type === "password";
    field.type = visible ? "text" : "password";
    toggle.textContent = visible ? gettext("Masquer") : gettext("Afficher");
    toggle.setAttribute("aria-label", window.ReserveI18n.format(gettext(visible ? "Masquer : %(name)s" : "Afficher : %(name)s"), {name}));
    toggle.setAttribute("aria-pressed", String(visible));
  });
  field.after(toggle);
}
window.addEventListener("pagehide", () => {
  const temporary = document.querySelector("#temporary");
  if (temporary) temporary.value = "";
});
