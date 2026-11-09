"use strict";
(() => {
  const cfg=JSON.parse(document.getElementById("purchase-config").textContent);
  const form=document.getElementById("purchase-form"),submit=document.getElementById("purchase-submit"),review=document.getElementById("purchase-review"),confirm=document.getElementById("purchase-confirm");
  const warning=document.getElementById("purchase-duplicates"),distinct=document.getElementById("purchase-distinct"),distinctCheck=document.getElementById("purchase-distinct-check"),picker=document.getElementById("purchase-picker");
  const csrf=form.querySelector("[name=csrfmiddlewaretoken]").value;
  const selected=new Map();let checked=null,ack=null,complete=false,loading=false;
  function states(){
    const busy=operation.busy || Boolean(operation.pending) || complete;
    for(const field of document.querySelectorAll("#purchase-form input,#purchase-form select,#purchase-form button,#purchase-picker input,#purchase-picker button"))field.disabled=busy || loading;
    submit.disabled=busy || loading || !navigator.onLine;
    confirm.disabled=busy || loading || !navigator.onLine || !checked;
    distinct.disabled=busy || loading || !navigator.onLine || !ack || !distinctCheck.checked;
    distinctCheck.disabled=busy;
    if(picker && !navigator.onLine)document.getElementById("purchase-add").disabled=true;
  }
  function paragraph(parent,text){const p=document.createElement("p");p.textContent=text;parent.append(p);}
  function changed(){checked=null;ack=null;review.hidden=true;warning.hidden=true;distinctCheck.checked=false;states();}
  const operation=new window.ReserveRequestOperation({actorId:cfg.actor_id,kind:cfg.kind,resourceId:cfg.resource_id,csrf,onState:states,onTerminal:async result=>{
    if(result.result==="CONFIRMED"){
      complete=true;form.hidden=true;if(picker)picker.hidden=true;warning.hidden=true;
      operation.message(cfg.mode==="purchase"?gettext("Achat enregistré et historisé. Événement comptable enregistré ; aucun e-mail externe envoyé dans ce prototype."):gettext("Décision enregistrée et historisée. Les stocks n’ont pas été modifiés."));
      const a=document.createElement("a");a.href=cfg.mode==="purchase" || cfg.mode==="cancel"?`/achats/${result.resource.id}`:`/achats/besoins/${result.resource.id}`;a.textContent=gettext("Consulter les données à jour");document.getElementById("operation-result").append(a);
    }else{
      checked=null;review.hidden=true;ack=null;
      operation.message(result.error?.message || gettext("Enregistrement refusé. Votre saisie est conservée."),true);
      if(result.error?.candidates){
        const candidates=result.error.candidates;
        ack={candidate_ids:candidates.map(c=>c.id),candidate_fingerprint:result.error.candidate_fingerprint,verified_distinct:true};
        const target=document.getElementById("purchase-candidates");target.replaceChildren();
        for(const c of candidates){const card=document.createElement("article"),a=document.createElement("a");a.href=`/achats/${encodeURIComponent(c.id)}`;a.target="_blank";a.rel="noopener noreferrer";a.textContent=`${c.reference} · ${c.supplier_name} · ${window.ReserveUI.date(c.purchased_on)}`;card.append(a);for(const line of c.lines)paragraph(card,`${line.name} · ${line.quantity} ${line.unit}`);if(c.external_reference)paragraph(card,window.ReserveI18n.format(gettext("Référence fournisseur : %(reference)s"),{reference:c.external_reference}));target.append(card);}
        warning.hidden=false;distinctCheck.checked=false;
      }
    }
    states();
  }});
  function add(product){
    const status=document.getElementById("purchase-picker-status");
    if(selected.has(product.id)){status.textContent=gettext("Ce produit est déjà présent. Modifiez sa quantité dans la déclaration.");return;}
    if(selected.size>=100){status.textContent=gettext("Une déclaration contient au maximum 100 produits.");return;}
    const box=document.createElement("fieldset");box.className="card";
    const legend=document.createElement("legend");legend.textContent=`${product.name} · ${product.unit}`;box.append(legend);
    if(product.photo_url){const photo=document.createElement("img");photo.src=product.photo_url;photo.alt=product.name;photo.width=64;photo.height=64;photo.className="purchase-line-photo";box.append(photo);}
    const explanation=document.createElement("p");explanation.className="hint";
    explanation.textContent=product.physical===null?gettext("Stock non inventorié : saisissez la quantité réellement achetée."):window.ReserveI18n.format(gettext("Prérempli à partir de %(target)s souhaités − %(physical)s en réserve. Vérifiez et modifiez cette quantité pour qu’elle corresponde à l’achat réellement passé."),{target:product.target,physical:product.physical});box.append(explanation);
    const label=document.createElement("label");label.textContent=gettext("Quantité réellement achetée");
    const input=document.createElement("input");input.type="number";input.min="1";input.max="1000000000";input.step="1";input.required=true;input.inputMode="numeric";input.value=product.suggested_actual_quantity===null || product.suggested_actual_quantity===undefined?"":String(product.suggested_actual_quantity);input.dataset.purchaseProduct=product.id;label.append(input);box.append(label);
    const remove=document.createElement("button");remove.type="button";remove.className="secondary";remove.textContent=gettext("Retirer ce produit");remove.addEventListener("click",()=>{selected.delete(product.id);box.remove();document.getElementById("purchase-empty").hidden=selected.size>0;changed();});box.append(remove);
    if(product.recommended!==undefined){const advice=document.createElement("p");advice.className="hint";advice.textContent=window.ReserveI18n.format(gettext("Quantité conseillée : %(quantity)s %(unit)s. Cette recommandation tient aussi compte des réservations et des commandes attendues ; elle est distincte du préremplissage."),{quantity:product.recommended,unit:product.unit});box.prepend(advice);}
    selected.set(product.id,{product,input});document.getElementById("purchase-lines").append(box);document.getElementById("purchase-empty").hidden=true;
    status.textContent=window.ReserveI18n.format(gettext("%(name)s ajouté à la déclaration non enregistrée."),{name:product.name});changed();
  }
  function payload(){
    if(cfg.mode==="purchase"){
      const amount=document.getElementById("purchase-amount").value.trim();
      return {supplier_id:document.getElementById("purchase-supplier").value,purchased_on:document.getElementById("purchase-date").value,external_reference:document.getElementById("purchase-reference").value,amount:amount?amount.replace(",","."):null,currency:amount?document.getElementById("purchase-currency").value.trim().toUpperCase():"",purchase_confirmed:document.getElementById("purchase-attestation").checked,lines:[...selected].map(([id,row])=>({product_id:id,quantity:Number(row.input.value),expected_product_version:row.product.version}))};
    }
    if(cfg.mode==="cancel")return {expected_version:cfg.expected_version,external_verification:document.getElementById("purchase-verification").checked,lines:[...form.querySelectorAll("[data-order-line]")].map(input=>({order_line_id:input.dataset.orderLine,quantity:Number(input.value)})).filter(row=>row.quantity>0)};
    return {expected_version:cfg.expected_version,state_fingerprint:cfg.state_fingerprint};
  }
  form.addEventListener("input",changed);
  form.addEventListener("submit",event=>{
    event.preventDefault();if(loading || operation.busy || operation.pending || complete || !navigator.onLine || !form.reportValidity())return;
    const body=payload();
    if(["defer","reactivate"].includes(cfg.mode)){operation.send(cfg.url,body);return;}
    if(!body.lines.length){operation.message(gettext("Sélectionnez au moins un produit et une quantité positive."),true);return;}
    checked=body;const target=document.getElementById("purchase-review-lines");target.replaceChildren();
    if(cfg.mode==="purchase"){
      paragraph(target,`${document.getElementById("purchase-supplier").selectedOptions[0].textContent} · Achat du ${body.purchased_on}`);
      for(const row of selected.values())paragraph(target,window.ReserveI18n.format(gettext("%(name)s : %(quantity)s %(unit)s réellement achetés."),{name:row.product.name,quantity:row.input.value,unit:row.product.unit}));
      if(body.amount)paragraph(target,window.ReserveI18n.format(gettext("Montant déclaré : %(amount)s %(currency)s"),{amount:body.amount,currency:body.currency}));
    }else for(const row of body.lines){const line=cfg.lines.find(l=>l.id===row.order_line_id);paragraph(target,`${line.name} : annuler ${row.quantity} ${line.unit} ; ${line.waiting-row.quantity} resteront attendus.`);}
    review.hidden=false;states();
  });
  confirm.addEventListener("click",()=>{if(checked && form.reportValidity())operation.send(cfg.url,checked);});
  distinctCheck.addEventListener("change",states);
  distinct.addEventListener("click",()=>{if(ack && distinctCheck.checked && form.reportValidity())operation.send(cfg.url,{...payload(),distinct_purchase_ack:ack});});
  picker?.addEventListener("input",changed);
  picker?.addEventListener("submit",async event=>{
    event.preventDefault();if(loading || operation.busy || operation.pending || complete || !navigator.onLine)return;
    const q=document.getElementById("purchase-product-search").value.trim();if(!q)return;
    loading=true;states();const status=document.getElementById("purchase-picker-status");status.textContent=gettext("Recherche du produit…");
    const controller=new AbortController(),deadline=setTimeout(()=>controller.abort(),8000);
    try{
      const response=await fetch("/api/v1/purchase-products?"+new URLSearchParams({code:q}),{credentials:"same-origin",cache:"no-store",signal:controller.signal});
      const data=await response.json();
      if(!response.ok){status.textContent=data.error?.message || gettext("Recherche indisponible. La déclaration reste non enregistrée.");return;}
      const product=data.product;
      if(!product){status.textContent=gettext("Choisissez une référence exacte dans les suggestions, puis ajoutez le produit.");return;}
      add(product);document.getElementById("purchase-product-search").value="";
    }catch{status.textContent=gettext("Recherche indisponible. La déclaration reste non enregistrée.");}
    finally{clearTimeout(deadline);loading=false;states();}
  });
  operation.start();if(cfg.initial_product)add(cfg.initial_product);
})();
