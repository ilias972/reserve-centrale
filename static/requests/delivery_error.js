"use strict";
(() => {
 const config=JSON.parse(document.getElementById("report-data").textContent);
 const operation=new window.ReserveRequestOperation({actorId:config.actor_id,kind:"deliveries.report_error",resourceId:config.delivery_id,csrf:document.querySelector("[name=csrfmiddlewaretoken]").value,onState:()=>{const button=document.getElementById("delivery-error-submit");if(button)button.disabled=operation.busy || Boolean(operation.pending) || !navigator.onLine;},onTerminal:async body=>{if(body.result==="CONFIRMED")window.ReserveLive.reload(true);else operation.message(body.error?.message || gettext("Signalement non confirmé."),true);}});
 document.getElementById("delivery-error-form")?.addEventListener("submit",event=>{event.preventDefault();operation.send(`/api/v1/deliveries/${config.delivery_id}/report-error`,{message:document.getElementById("delivery-message").value});});
 operation.start();
})();
