"use strict";
(() => {
 const config=JSON.parse(document.getElementById("case-config").textContent);
 const form=document.getElementById("case-form"), submit=document.getElementById("case-submit"), confirm=document.getElementById("case-confirm"), preview=document.getElementById("case-preview");
 const csrf=form.querySelector("[name=csrfmiddlewaretoken]").value;
 let checked=null, checking=false, completed=false;
 const message=()=>document.getElementById("case-message").value;
 const verified=()=>document.getElementById("case-verification").checked;
 const quantities=()=>Array.from(form.querySelectorAll("[data-reopen-line]"),n=>({request_line_id:n.dataset.reopenLine,quantity:Number(n.value)}));
 function body(){
   const common={verification:verified(),message:message()};
   if(config.mode==="review")return {...common,expected_version:config.expected_version};
   if(config.mode==="resolve")return {...common,expected_version:config.expected_version,product_counts:config.product_counts.map(row=>({...row,counted_quantity:Number(form.querySelector(`[data-count-product="${row.product_id}"]`).value)}))};
   if(config.mode==="open")return {...common,kind:"DELIVERY_ERROR",source_id:config.source_id};
   if(config.mode==="correction"){
     const disposition=config.closed?document.getElementById("case-disposition").value:"KEEP_OPEN";
     return {...common,kind:"DELIVERY",source_id:config.source_id,case_id:config.case_id,expected_versions:config.expected_versions,corrected_lines:Array.from(form.querySelectorAll("[data-correct-line]"),n=>({delivery_line_id:n.dataset.correctLine,quantity:Number(n.value)})),disposition,reopen_quantities:disposition==="REOPEN"?quantities():[]};
   }
   if(config.mode==="reconciliation")return {...common,case_id:config.case_id,expected_case_version:config.expected_case_version,product_counts:config.product_counts.map(row=>({...row,counted_quantity:Number(form.querySelector(`[data-count-product="${row.product_id}"]`).value)})),commitments:config.commitments.map(row=>({...row,release_quantity:Number(form.querySelector(`[data-release-line="${row.request_line_id}"]`).value)})).filter(row=>row.release_quantity>0)};
   if(config.mode==="reassign")return {...common,expected_version:config.expected_version,new_operator_id:document.getElementById("case-operator").value};
   return {verification:verified(),reason:message(),expected_version:config.expected_version,...(config.mode==="reopen"?{quantities:quantities()}:{})};
 }
 function states(){
   const blocked=operation.busy || Boolean(operation.pending) || checking || completed;
   for(const input of form.querySelectorAll("input,select,textarea"))input.disabled=blocked;
   submit.disabled=blocked || !navigator.onLine;
   if(confirm){confirm.disabled=blocked || !navigator.onLine || !checked;confirm.hidden=!checked;}
 }
 const operation=new window.ReserveRequestOperation({actorId:config.actor_id,kind:config.kind,resourceId:config.resource_id,csrf,onState:states,onTerminal:async result=>{
   if(result.result==="CONFIRMED"){
     completed=true;checked=null;operation.message(gettext("Décision enregistrée et historisée. Les données affichées ci-dessus décrivent la prévisualisation confirmée."));
     form.hidden=true;
     if(config.auto_return){location.assign(config.success_url);return;}
     const link=document.createElement("a");link.href=config.mode==="open"?`/dossiers/${result.resource.id}`:config.success_url;link.textContent=gettext("Consulter le dossier à jour");document.getElementById("operation-result").append(link);
   } else {
     checked=null;if(preview)preview.hidden=true;
     operation.message(result.error?.message || gettext("Décision non enregistrée. Rechargez les données avant une nouvelle vérification."),true);
     if(config.success_url){const reload=document.createElement("a");reload.href=config.success_url;reload.textContent=gettext("Recharger la situation actuelle du dossier");document.getElementById("operation-result").append(reload);}
     if(result.case_url){const a=document.createElement("a");a.href=result.case_url;a.textContent=gettext("Consulter le conflit de stock créé");document.getElementById("operation-result").append(a);}
   }
   states();
 }});
 function paragraph(parent,text){const p=document.createElement("p");p.textContent=text;parent.append(p);}
 function show(plan){
   const target=document.getElementById("case-preview-content");target.replaceChildren();
   if(plan.lines){
     for(const c of (plan.corrections || []))paragraph(target,window.ReserveI18n.format(gettext("Remise initiale : %(initial)s ; quantité effective actuelle : %(before)s ; quantité réelle vérifiée : %(after)s."),{initial:c.initial,before:c.effective_before,after:c.effective_after}));
     for(const row of plan.lines){const a=row.before,b=row.after;paragraph(target,window.ReserveI18n.format(gettext("%(name)s (%(unit)s) — en réserve : %(p_before)s → %(p_after)s ; réservé : %(r_before)s → %(r_after)s ; disponible : %(d_before)s → %(d_after)s. Demandé : %(requested)s ; remis : %(issued_before)s → %(issued_after)s ; clôturé : %(closed_before)s → %(closed_after)s ; reste réservé : %(open_before)s → %(open_after)s."),{name:row.name,unit:row.unit,p_before:a.P,p_after:b.P,r_before:a.R,r_after:b.R,d_before:a.D,d_after:b.D,requested:row.Q,issued_before:a.L,issued_after:b.L,closed_before:a.C,closed_after:b.C,open_before:a.U,open_after:b.U}));}
     const names={NEW:gettext("Nouvelle"),PREPARING:gettext("En préparation"),PARTIAL_OPEN:gettext("Partiellement livrée — reste à livrer"),PARTIAL_CLOSED:gettext("Clôturée — quantités non remises"),DELIVERED:gettext("Livrée")};
     paragraph(target,window.ReserveI18n.format(gettext("État après confirmation : %(status)s"),{status:names[plan.status_after]}));
     if(!plan.action)paragraph(target,plan.disposition==="KEEP_CLOSED"?gettext("La demande restera close."):plan.disposition==="REOPEN"?gettext("Réouverture explicitement choisie."):gettext("Le reliquat restera réservé."));
   } else {
     for(const row of plan.products)paragraph(target,window.ReserveI18n.format(gettext("%(name)s (%(unit)s) — en réserve : %(p_before)s → %(p_after)s ; réservé : %(r_before)s → %(r_after)s ; disponible : %(d_before)s → %(d_after)s."),{name:row.name,unit:row.unit,p_before:row.P_before,p_after:row.P_after,r_before:row.R_before,r_after:row.R_after,d_before:row.D_before,d_after:row.D_after}));
     for(const row of plan.commitments)paragraph(target,window.ReserveI18n.format(gettext("%(reference)s · %(unit)s · %(requester)s : %(released)s libérés, %(reserved)s restant réservés."),{reference:row.request_reference,unit:row.floor,requester:row.assistant,released:row.release_quantity,reserved:row.reserved_after}));
   }
   if(plan.explanation)paragraph(target,plan.explanation);
   if(plan.next_operator_name)paragraph(target,window.ReserveI18n.format(gettext("Nouvel opérateur : %(name)s."),{name:plan.next_operator_name}));
   paragraph(target,gettext("Les personnes concernées retrouveront la décision dans le suivi de leur demande. Aucune notification externe n’est envoyée."));
   preview.hidden=false;
 }
 form.addEventListener("input",()=>{checked=null;if(preview)preview.hidden=true;states();});
 const disposition=document.getElementById("case-disposition");
 disposition?.addEventListener("change",()=>{document.getElementById("reopen-fields").hidden=disposition.value!=="REOPEN";});
 form.addEventListener("submit",async event=>{
   event.preventDefault();if(checking || operation.busy || operation.pending || completed || !navigator.onLine || !form.reportValidity())return;
   const payload=body();
   if(config.mode==="resolve"){
     const mismatch=payload.product_counts.some(row=>row.counted_quantity!==config.current_stocks.find(s=>s.id===row.product_id).physical);
     if(mismatch){checked=null;preview.hidden=true;operation.message(gettext("Le comptage diffère du stock enregistré. Faites d’abord vérifier et régulariser cet écart dans Stocks. Aucun mouvement ni résolution enregistré."),true);return;}
     const target=document.getElementById("case-preview-content");target.replaceChildren();
     for(const row of config.current_stocks)paragraph(target,window.ReserveI18n.format(gettext("%(name)s (%(unit)s) — comptage vérifié : %(count)s ; en réserve : %(p_before)s → %(p_after)s ; réservé : %(r_before)s → %(r_after)s ; disponible : %(d_before)s → %(d_after)s."),{name:row.name,unit:row.unit,count:row.physical,p_before:row.physical,p_after:row.physical,r_before:row.reserved,r_after:row.reserved,d_before:row.available,d_after:row.available}));
     paragraph(target,gettext("Le dossier sera résolu. La demande, ses quantités et son état restent inchangés. Le serveur revérifiera les versions et l’absence de conflit à la confirmation."));
     checked=payload;preview.hidden=false;states();return;
   }
   if(!config.preview_url){operation.send(config.url,payload);return;}
   checking=true;states();
   try{
     const response=await fetch(config.preview_url,{method:"POST",credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json","X-CSRFToken":csrf},body:JSON.stringify(payload)});
     const result=await response.json();
     if(!response.ok){checked=null;if(preview)preview.hidden=true;operation.message(result.error?.message || gettext("Prévisualisation impossible. Aucun mouvement enregistré."),true);return;}
     checked={...payload,preview_hash:result.preview.preview_hash};show(result.preview);
   }catch{checked=null;if(preview)preview.hidden=true;operation.message(gettext("Connexion indisponible. Aucune correction envoyée ; votre saisie est conservée."),true);}
   finally{checking=false;states();}
 });
 confirm?.addEventListener("click",()=>{if(checked && form.reportValidity())operation.send(config.url,checked);});
 operation.start();
})();
