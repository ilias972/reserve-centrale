"use strict";
(() => {
  let stale=false, edited=false;
  const config=JSON.parse(document.getElementById("refusal-data").textContent);
  const form=document.getElementById("refuse-request");
  const operation=new window.ReserveRequestOperation({
    actorId:config.actor_id,kind:"requests.refuse",resourceId:config.request_id,
    csrf:document.querySelector("[name=csrfmiddlewaretoken]").value,
    onState:()=>{
      form?.querySelectorAll("button,textarea").forEach(node=>{
        node.disabled=stale || operation.busy || Boolean(operation.pending) || !navigator.onLine;
      });
    },
    onTerminal:async body=>{
      if(body.result==="CONFIRMED") location.replace(`/demandes/${config.request_id}`);
      else {
        operation.message(body.error?.message || gettext("Refus non confirmé."),true);
        const link=document.createElement("a");link.href=location.pathname;link.textContent=gettext("Actualiser la demande");
        document.getElementById("operation-result").append(link);
      }
    }
  });
  form?.addEventListener("input",()=>{edited=true;});
  form?.addEventListener("submit",event=>{
    event.preventDefault();if(stale)return;
    operation.send(`/api/v1/requests/${config.request_id}/refuse`,{
      expected_version:config.expected_version,message:document.getElementById("refusal-message").value
    });
  });
  window.ReserveLive.subscribe(async()=>{
    if(stale || operation.busy || operation.pending)return;
    const data=await window.ReserveLive.read(`/api/v1/requests/${config.request_id}`);
    if(data.request.version===config.expected_version || operation.busy || operation.pending)return;
    if(!edited){window.ReserveLive.reload();return;}
    stale=true;operation.state();
    operation.message(gettext("Cette demande a changé. Votre justification est conservée ; actualisez la demande avant de confirmer."),true);
    const link=document.createElement("a");link.href=location.pathname;link.textContent=gettext("Actualiser la demande");
    document.getElementById("operation-result").append(link);
  });
  operation.start();
})();
