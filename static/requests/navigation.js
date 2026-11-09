"use strict";
// Compteurs de travail : lecture seule, même actualisation que les autres listes.
(() => {
  const counters = [
    {badge:"pending-request-count",description:"pending-request-description",url:"/api/v1/requests/pending-count",label:n=>window.ReserveI18n.plural("%(count)s demande à traiter","%(count)s demandes à traiter",n)},
    {badge:"pending-purchase-needs-count",description:"pending-purchase-needs-description",url:"/api/v1/recommendations/pending-count",label:n=>window.ReserveI18n.plural("%(count)s besoin d’achat à traiter","%(count)s besoins d’achat à traiter",n)}
  ];
  for(const config of counters){
    const badge=document.getElementById(config.badge);
    if(!badge)continue;
    const description=document.getElementById(config.description);
    window.ReserveLive.subscribe(async()=>{
      const data=await window.ReserveLive.read(config.url),count=data.pending_count;
      // Un échec de lecture conserve la dernière valeur et le message réseau commun.
      if(!Number.isSafeInteger(count) || count<0)throw new Error(gettext("Compteur indisponible"));
      badge.textContent=count ? String(count) : "";
      badge.hidden=count===0;
      const label=config.label(count);
      if(description.textContent!==label)description.textContent=label;
    });
  }
})();
