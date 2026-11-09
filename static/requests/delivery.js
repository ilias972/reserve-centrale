"use strict";
(() => {
  let stale=false, edited=false;
  const config=JSON.parse(document.getElementById("delivery-data").textContent);
  const recap=document.getElementById("delivery-recap");
  const form=document.getElementById("delivery-editor");
  const shortageAck=document.getElementById("shortage-ack");
  const confirm=document.getElementById("delivery-confirm");
  let payload=null;
  const ready=()=>!(payload?.mode==="SHORTAGE" && !shortageAck.checked);
  const options={actorId:config.actor_id,resourceId:config.request_id,csrf:document.querySelector("[name=csrfmiddlewaretoken]").value,onState:()=>{
    document.querySelectorAll("#claim-request,#delivery-confirm,#delivery-review,#delivery-back,#shortage-ack,[data-step-input],.handed-quantity,[name=delivery-mode]").forEach(node=>node.disabled=stale || operation.busy || Boolean(operation.pending) || !navigator.onLine);
    if(confirm && !ready())confirm.disabled=true;
    const refusalLink=document.getElementById("refuse-link");
    if(refusalLink)refusalLink.hidden=stale || operation.busy || Boolean(operation.pending);
  },onTerminal:async body=>{
    if(body.result==="CONFIRMED" || (operation.kind==="requests.claim" && config.kind!=="requests.claim")) window.ReserveLive.reload(true);
    else {operation.message(body.error?.message || gettext("Remise non confirmée."),true);const link=document.createElement("a");link.href=location.pathname;link.textContent=gettext("Actualiser la demande");document.getElementById("operation-result").append(link);}
  }};
  // Une prise en charge confirmée à réponse perdue reste à résoudre après reload.
  let operation=new window.ReserveRequestOperation({...options,kind:"requests.claim"});
  if(!operation.pending && config.kind!=="requests.claim") operation=new window.ReserveRequestOperation({...options,kind:config.kind});
  document.getElementById("claim-request")?.addEventListener("click",()=>operation.send(`/api/v1/requests/${config.request_id}/claim`,{expected_version:config.expected_version}));
  form?.addEventListener("submit",event=>{
    event.preventDefault();if(stale)return;const mode=document.querySelector("[name=delivery-mode]:checked").value;
    const summary=document.getElementById("delivery-summary");summary.replaceChildren();let valid=true,total=0,missing=0;
    const lines=config.lines.map(line=>{
      const raw=document.querySelector(`[data-line="${line.id}"]`).value;
      const y=Number(raw);const m=mode==="SHORTAGE"?line.reserved-y:0;
      if(!raw || !Number.isSafeInteger(y) || y<0 || y>line.reserved || (mode==="COMPLETE" && y!==line.reserved)) valid=false;
      total+=y;missing+=m;
      const p=document.createElement("p");p.textContent=window.ReserveI18n.format(gettext("%(name)s : %(delivered)s %(unit)s remis ; %(missing)s manquants clôturés ; %(remaining)s restant réservés."),{name:line.name,delivered:y,unit:line.unit,missing:m,remaining:line.reserved-y-m});summary.append(p);
      return {request_line_id:line.id,delivered_quantity:y,missing_quantity:m};
    });
    if(!valid || (!total && !missing) || (mode==="SHORTAGE" && !missing)){operation.message(gettext("Vérifiez les quantités entières et le choix de traitement du reste. Aucune remise enregistrée."),true);return;}
    payload={expected_version:config.expected_version,mode,lines};
    shortageAck.checked=false;
    document.getElementById("shortage-acknowledgment").hidden=mode!=="SHORTAGE";
    document.getElementById("delivery-consequence").textContent=mode==="SHORTAGE"
      ? window.ReserveI18n.format(gettext("Le stock en réserve diminuera de %(delivered)s unités remises et de %(missing)s unités introuvables. Les réservations correspondantes seront libérées et les reliquats clôturés. La responsable recevra un signalement. Ne choisissez pas ce traitement pour un simple refus."),{delivered:total,missing})
      : window.ReserveI18n.format(gettext("Le stock en réserve diminuera seulement des %(delivered)s unités réellement remises. Le reste demeure réservé, sans correction pour manque. Aucune seconde déduction dans Stocks."),{delivered:total});
    recap.hidden=false;form.hidden=true;options.onState();
  });
  document.querySelectorAll(".handed-quantity,[name=delivery-mode]").forEach(node=>node.addEventListener("input",()=>{payload=null;if(recap)recap.hidden=true;if(form)form.hidden=false;}));
  shortageAck?.addEventListener("change",()=>options.onState());
  document.getElementById("delivery-back")?.addEventListener("click",()=>{recap.hidden=true;form.hidden=false;payload=null;});
  document.getElementById("delivery-confirm")?.addEventListener("click",()=>{if(payload && !stale && ready())operation.send(`/api/v1/requests/${config.request_id}/deliveries`,payload);});
  document.querySelectorAll(".handed-quantity,[name=delivery-mode]").forEach(node=>node.addEventListener("input",()=>{edited=true;}));
  window.ReserveLive.subscribe(async()=>{
    if(operation.busy || operation.pending || stale)return;
    const data=await window.ReserveLive.read(`/api/v1/requests/${config.request_id}`);
    if(data.request.version===config.expected_version)return;
    if(operation.busy || operation.pending)return;
    if(!edited){window.ReserveLive.reload();return;}
    stale=true;
    operation.message(gettext("Cette demande a changé. Votre saisie est conservée, mais actualisez la demande avant toute confirmation."),true);
    for(const id of ["claim-request","delivery-review","delivery-confirm","cancel-confirm"]) {const button=document.getElementById(id);if(button)button.disabled=true;}
    const link=document.createElement("a");link.href=location.pathname;link.textContent=gettext("Actualiser la demande");document.getElementById("operation-result").append(link);
  });
  operation.start();
})();
