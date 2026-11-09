"use strict";
const i18nCatalog = window.django;
if (!i18nCatalog || typeof i18nCatalog.gettext !== "function") {
  throw new Error("Django JavaScript translations are unavailable");
}
window.gettext = (message) => i18nCatalog.gettext(message);
window.ngettext = (singular, plural, count) => i18nCatalog.ngettext(singular, plural, count);
window.interpolate = (message, values, useBrackets) => i18nCatalog.interpolate(message, values, useBrackets);
window.ReserveI18n = Object.freeze({
  format(message, values = {}) {
    return window.interpolate(message, values, true);
  },
  plural(singular, plural, count, values = {}) {
    return window.interpolate(window.ngettext(singular, plural, count), { ...values, count }, true);
  },
});
