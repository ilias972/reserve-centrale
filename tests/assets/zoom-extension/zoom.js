// Extension de test chargée uniquement dans un profil Chromium temporaire.
globalThis.setRecetteZoom = async (factor) => {
  const tabs = await chrome.tabs.query({});
  const tab = tabs.find(
    (item) =>
      item.url?.startsWith("http://127.0.0.1:") ||
      item.url?.startsWith("http://localhost:"),
  );
  if (!tab) throw new Error("Onglet local de recette absent");
  await chrome.tabs.setZoom(tab.id, factor);
  return chrome.tabs.getZoom(tab.id);
};
