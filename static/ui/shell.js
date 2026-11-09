"use strict";
window.ReserveUI = {
  date(value) {
    const day = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? value+"T12:00:00Z" : value);
    return Number.isNaN(day.getTime()) ? gettext("Date indisponible") : new Intl.DateTimeFormat(document.documentElement.lang,{timeZone:"Europe/Paris"}).format(day);
  },
  icon(name) {
    const ns = "http://www.w3.org/2000/svg",
      svg = document.createElementNS(ns, "svg"),
      use = document.createElementNS(ns, "use");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-width", "2");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    use.setAttribute("href", document.body.dataset.uiIcons + "#" + name);
    svg.append(use);
    return svg;
  },
};
(() => {
  const toggle = document.getElementById("menu-toggle"),
    scrim = document.getElementById("nav-scrim");
  if (toggle && scrim) {
    const close = () => {
      document.body.classList.remove("nav-open");
      scrim.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("aria-label", gettext("Ouvrir la navigation"));
    };
    toggle.addEventListener("click", () => {
      const opened = toggle.getAttribute("aria-expanded") === "true";
      if (opened) close();
      else {
        document.body.classList.add("nav-open");
        scrim.hidden = false;
        toggle.setAttribute("aria-expanded", "true");
        toggle.setAttribute("aria-label", gettext("Fermer la navigation"));
        document.querySelector("#admin-navigation a")?.focus();
      }
    });
    scrim.addEventListener("click", close);
    document.addEventListener("keydown", (event) => {
      if (
        event.key === "Escape" &&
        toggle.getAttribute("aria-expanded") === "true"
      ) {
        close();
        toggle.focus();
      }
    });
    document
      .getElementById("admin-navigation")
      .addEventListener("keydown", (event) => {
        if (
          event.key !== "Tab" ||
          toggle.getAttribute("aria-expanded") !== "true"
        )
          return;
        const nodes = [
          ...document.querySelectorAll(
            "#admin-navigation a,#admin-navigation button",
          ),
        ];
        if (event.shiftKey && document.activeElement === nodes[0]) {
          event.preventDefault();
          nodes.at(-1).focus();
        } else if (!event.shiftKey && document.activeElement === nodes.at(-1)) {
          event.preventDefault();
          nodes[0].focus();
        }
      });
  }
  // Petites commandes de présentation : elles écrivent dans un vrai champ, jamais une API.
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-step-input]");
    if (!button) return;
    const field = document.getElementById(button.dataset.stepInput);
    if (!field || field.disabled || field.readOnly) return;
    const value = Number(field.value || 0),
      step = Number(button.dataset.step || 1);
    if (!Number.isSafeInteger(value)) return;
    const min = field.min === "" ? 0 : Number(field.min),
      max = field.max === "" ? 1000000000 : Number(field.max);
    field.value = String(Math.min(max, Math.max(min, value + step)));
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
})();
(() => {
  const filters = document.getElementById("movement-filters");
  if (!filters) return;
  filters.addEventListener("click", (event) => {
    const button = event.target.closest("[data-movement-filter]");
    if (!button) return;
    const mode = button.dataset.movementFilter;
    filters
      .querySelectorAll("button")
      .forEach((item) =>
        item.setAttribute("aria-pressed", String(item === button)),
      );
    const rows = [...document.querySelectorAll("[data-movement-kind]")];
    rows.forEach((row) => {
      row.hidden =
        mode === "out"
          ? Number(row.dataset.physicalDelta) >= 0
          : mode === "adjustment"
            ? row.dataset.movementKind !== "MANUAL_ADJUSTMENT"
            : false;
    });
    document.getElementById("movement-empty").hidden = rows.some(
      (row) => !row.hidden,
    );
  });
})();
(() => {
  const button = document.getElementById("back-to-top");
  if (!button) return;
  const update = () => { button.hidden = window.scrollY < 500; };
  window.addEventListener("scroll", update, {passive:true});
  button.addEventListener("click", () => {
    window.scrollTo({top:0,behavior:matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
    document.getElementById("main").focus({preventScroll:true});
  });
  update();
})();
