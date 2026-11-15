"use strict";
(() => {
  let stale=false;
  const config=JSON.parse(document.querySelector("#cancel-data").textContent);
  const form=document.querySelector("#cancel-request");
  const open=document.querySelector("#cancel-open");
  const operation=new window.ReserveRequestOperation({actorId:config.actor_id,kind:"requests.cancel",resourceId:config.request_id,csrf:document.querySelector("[name=csrfmiddlewaretoken]").value,onState:()=>{
    for(const id of ["cancel-open","cancel-confirm","cancel-back"]){const node=document.getElementById(id);if(node)node.disabled=stale || operation.busy || Boolean(operation.pending) || !navigator.onLine;}
  },onTerminal:async body=>{
    if(body.result==="CONFIRMED") window.ReserveLive.reload(true);
    else {operation.message(body.error?.message || gettext("Annulation non confirmée."),true);const link=document.createElement("a");link.href=location.pathname;link.textContent=gettext("Actualiser la demande");document.querySelector("#operation-result").append(link);}
  }});
  // The server-rendered control starts disabled and is enabled by operation.start()
  // only after its click handler and the cancellation form handler are attached.
  if(open)open.addEventListener("click",()=>{form.hidden=false;open.hidden=true;});
  document.querySelector("#cancel-back")?.addEventListener("click",()=>{form.hidden=true;open.hidden=false;});
  form?.addEventListener("submit",event=>{event.preventDefault();if(stale)return;operation.send(`/api/v1/requests/${config.request_id}/cancel`,{expected_version:config.expected_version});});
  window.ReserveLive.subscribe(async()=>{
    if(operation.busy || operation.pending || stale)return;
    const data=await window.ReserveLive.read(`/api/v1/requests/${config.request_id}`);
    if(data.request.version===config.expected_version)return;
    if(operation.busy || operation.pending)return;
    if(!document.querySelector("#cancel-request:not([hidden])")){window.ReserveLive.reload();return;}
    stale=true;
    operation.message(gettext("Cette demande a changé. Votre saisie est conservée, mais actualisez la demande avant toute confirmation."),true);
    for(const id of ["claim-request","delivery-review","delivery-confirm","cancel-confirm"]) {const button=document.getElementById(id);if(button)button.disabled=true;}
    const link=document.createElement("a");link.href=location.pathname;link.textContent=gettext("Actualiser la demande");document.getElementById("operation-result").append(link);
  });
  operation.start();
})();
