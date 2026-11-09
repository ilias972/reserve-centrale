"use strict";
// Aucun gestionnaire fetch, aucun cache métier, aucune opération hors connexion.
self.addEventListener("push", event => {
  let data;
  try { data = event.data.json(); } catch { return; }
  if (!/^[a-f0-9-]{36}$/i.test(data.notification_id ?? "")) return;
  const title = typeof data.title === "string" && data.title.length <= 80 ? data.title : "Reserve Central";
  const body = typeof data.body === "string" && data.body.length <= 160 ? data.body : "You have an update in the app.";
  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon:"/static/ui/app-icon-192.png", badge:"/static/ui/app-icon-192.png",
    tag:`reserve-${data.notification_id}`, data:{id:data.notification_id}
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const id=event.notification.data?.id;
  if (!/^[a-f0-9-]{36}$/i.test(id ?? "")) return;
  const url=new URL(`/notifications/${id}/ouvrir`,self.location.origin).href;
  event.waitUntil(self.clients.matchAll({type:"window",includeUncontrolled:true}).then(async clients => {
    const existing=clients.find(client=>new URL(client.url).origin===self.location.origin);
    if(existing){await existing.navigate(url);return existing.focus();}
    return self.clients.openWindow(url);
  }));
});
