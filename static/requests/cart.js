"use strict";
(() => {
  const config = JSON.parse(document.querySelector("#cart-data").textContent);
  const form = document.querySelector("#request-cart");
  const list = document.querySelector("#request-products");
  const review = document.querySelector("#request-review");
  const selected = new Map(); const known = new Map(config.rows.map(row=>[row.product_id,row]));
  let visible = config.rows.map(row=>row.product_id), nextOffset = config.next_offset, loadSequence=0;
  const el = (tag,text,className) => { const node=document.createElement(tag); if(text!==undefined) node.textContent=text; if(className) node.className=className; return node; };
  let appliedParams=new URLSearchParams(new FormData(document.getElementById("request-filter")));
  let operation;
  function locked() { return operation && (operation.pending || operation.busy); }
  function actions() {
    const invalid = Boolean(document.querySelector(".request-counter input:invalid")) || [...selected.values()].some(item=>item.quantity < 1 || item.quantity > (known.get(item.row.product_id)?.maximum_allowed || 0));
    document.querySelector("#cart-count").textContent=selected.size ? window.ReserveI18n.plural("%(count)s référence sélectionnée","%(count)s références sélectionnées",selected.size) : gettext("Aucun produit sélectionné");
    document.querySelector("#cart-review").disabled=!selected.size || invalid || Boolean(locked());
    document.querySelector("#cart-clear").disabled=!selected.size || Boolean(locked());
    document.querySelector("#request-send").disabled=!selected.size || invalid || Boolean(locked()) || !navigator.onLine;
    for(const control of document.querySelectorAll(".request-counter input,.request-counter button,.cart-remove")) control.disabled=Boolean(locked()) || control.dataset.unavailable === "true";
    document.querySelectorAll(".request-product").forEach(card=>card.classList.toggle("is-selected",selected.has(card.dataset.productId)));
    const basket=document.querySelector("#cart-lines"), focusedRemove=basket.contains(document.activeElement) ? document.activeElement.getAttribute("aria-label") : null; basket.replaceChildren();
    for(const item of selected.values()) {
      const li=el("li");
      if(item.row.photo_url){const photo=document.createElement("img");photo.src=item.row.photo_url+"?thumbnail=1";photo.alt="";photo.width=photo.height=40;photo.className="basket-thumb";li.append(photo);}
      const details=el("div",undefined,"basket-product");details.append(el("strong",item.row.name),el("small",item.row.unit));li.append(details,el("span",`×${item.quantity}`,"mono"));
      const remove=el("button",gettext("Retirer"),"secondary cart-remove"); remove.type="button"; remove.disabled=Boolean(locked());
      remove.setAttribute("aria-label",window.ReserveI18n.format(gettext("Retirer — %(name)s"),{name:item.row.name}));remove.replaceChildren(window.ReserveUI.icon("trash"));
      remove.addEventListener("click",()=>{selected.delete(item.row.product_id);review.hidden=true;render();});li.append(remove);basket.append(li);
      if(item.quantity > (known.get(item.row.product_id)?.maximum_allowed || 0)) li.append(el("p",gettext("Quantité à recontrôler : disponibilité ou droit modifié."),"notice"));
    }
    if(focusedRemove)[...basket.querySelectorAll("button")].find(node=>node.getAttribute("aria-label")===focusedRemove)?.focus({preventScroll:true});
  }
  function choose(row,quantity,redraw=true) {
    if(locked() || !Number.isSafeInteger(quantity) || quantity<0 || quantity>1000000000) return;
    if(!selected.has(row.product_id) && selected.size>=100 && quantity>0) {operation.message(gettext("Une demande peut contenir jusqu'à 100 références."),true);return;}
    if(quantity===0) selected.delete(row.product_id); else selected.set(row.product_id,{row,quantity});
    review.hidden=true;if(redraw)render();else actions();
  }
  function render() {
    list.replaceChildren();
    for(const id of visible) {
      const row=known.get(id), item=selected.get(id), quantity=item?.quantity || 0;
      const card=el("article",undefined,"card stock-row request-product"); card.dataset.productId=id;card.classList.toggle("is-selected",Boolean(item));
      const info=el("div",undefined,"stock-product");
      if(row.photo_url){const image=document.createElement("img");image.src=row.photo_url+"?thumbnail=1";image.alt=`${row.name} — ${row.unit}`;image.width=70;image.height=70;image.loading="lazy";info.append(image);}
      const text=el("div");text.append(el("h2",row.name),el("p",`${row.code} · ${row.unit}`,"hint"));
      text.append(el("p",row.available===null ? gettext("Non inventorié — quantité inconnue") : window.ReserveI18n.format(gettext("Disponible : %(quantity)s %(unit)s"),{quantity:row.available,unit:row.unit}),"availability"+(!row.demandable?" unavailable":"")));
      text.append(el("p",row.maximum ? window.ReserveI18n.format(gettext("Maximum par demande : %(quantity)s %(unit)s"),{quantity:row.maximum,unit:row.unit}) : gettext("Plafond non configuré.")));
      if(row.reason)text.append(el("p",row.reason,"hint")); info.append(text);card.append(info);
      const counter=el("div",undefined,"stock-counter request-counter");
      const minus=el("button",gettext("−"),"secondary"),plus=el("button",gettext("+"),"secondary");minus.type=plus.type="button";
      minus.setAttribute("aria-label",window.ReserveI18n.format(gettext("Diminuer — %(name)s"),{name:row.name}));plus.setAttribute("aria-label",window.ReserveI18n.format(gettext("Augmenter — %(name)s"),{name:row.name}));
      plus.dataset.unavailable=String(!row.demandable || quantity>=row.maximum_allowed);
      minus.dataset.unavailable=String(quantity===0);
      const input=document.createElement("input");input.type="text";input.inputMode="numeric";input.pattern="[0-9]+";input.value=String(quantity);input.autocomplete="off";
      input.setAttribute("aria-label",window.ReserveI18n.format(gettext("Quantité demandée — %(name)s"),{name:row.name}));input.dataset.unavailable=String(!row.demandable && !item);
      input.addEventListener("input",()=>{
        const valid=/^\d+$/.test(input.value) && Number.isSafeInteger(Number(input.value)) && Number(input.value)<=1000000000;
        input.setCustomValidity(valid ? "" : "Saisissez un entier positif ou nul.");
        if(!valid){review.hidden=true;document.querySelector("#cart-review").disabled=true;return;}
        const value=Number(input.value);plus.dataset.unavailable=String(!row.demandable || value>=row.maximum_allowed);minus.dataset.unavailable=String(value===0);choose(row,value,false);
      });
      minus.addEventListener("click",()=>choose(row,Math.max(0,(selected.get(id)?.quantity || 0)-1)));plus.addEventListener("click",()=>choose(row,Math.min(row.maximum_allowed,(selected.get(id)?.quantity || 0)+1)));
      counter.append(minus,input,plus);card.append(counter);list.append(card);
    }
    document.querySelector("#request-empty").hidden=visible.length>0;document.querySelector("#request-more").hidden=nextOffset===null;actions();
  }
  async function load(append=false) {
    const job=++loadSequence;
    const params=append ? new URLSearchParams(appliedParams) : new URLSearchParams(new FormData(document.querySelector("#request-filter")));
    if(!append)appliedParams=new URLSearchParams(params);
    if(append && nextOffset!==null)params.set("offset",String(nextOffset));
    const response=await fetch("/api/v1/request-catalog?"+params,{credentials:"same-origin",cache:"no-store"});
    if(!response.ok) throw new Error(gettext("Catalogue non disponible"));
    const data=await response.json();if(job!==loadSequence)return; const ids=[];
    for(const row of data.products){known.set(row.product_id,row);ids.push(row.product_id);}
    visible=append?[...new Set([...visible,...ids])]:ids;nextOffset=data.next_offset;render();
  }
  operation=new window.ReserveRequestOperation({actorId:config.actor_id,kind:"requests.create",csrf:form.querySelector("[name=csrfmiddlewaretoken]").value,onState:actions,onTerminal:async(body)=>{
    if(body.result==="CONFIRMED") {
      selected.clear();review.hidden=true;operation.message(window.ReserveI18n.format(gettext("Demande %(reference)s envoyée. Quantités réservées."),{reference:body.resource.reference}));
      const link=el("a",gettext("Suivre cette demande"));link.href=`/demandes/${body.resource.id}`;document.querySelector("#operation-result").append(link);
    } else {
      operation.message(body.error?.message || gettext("Aucune demande envoyée."),true); review.hidden=true;
      for(const issue of body.error?.lines || []) {
        const row=known.get(issue.product_id);if(row)known.set(issue.product_id,{...row,maximum_allowed:issue.maximum_allowed,demandable:issue.maximum_allowed>0});
        document.querySelector("#operation-result").append(el("p",window.ReserveI18n.format(gettext("%(product)s: %(message)s Maximum possible: %(maximum)s."),{product:selected.get(issue.product_id)?.row.name || gettext("Produit sélectionné"),message:issue.message,maximum:issue.maximum_allowed})));
      }
    }
    // Le résultat terminal reste visible même si l'actualisation échoue.
    // Refléter d'abord la sélection vidée/corrigée ; aucune réussite n'est effacée.
    render();
    try{await load();}catch{
      document.querySelector("#operation-result").append(el("p",gettext("Disponibilités non actualisées — données potentiellement obsolètes. Vérifiez la connexion avant une nouvelle demande."),"notice"));
    }
  }});
  document.querySelector("#cart-review").addEventListener("click",()=>{
    if(locked() || !selected.size)return;
    const rows=document.querySelector("#request-review-lines");rows.replaceChildren();
    for(const item of selected.values())rows.append(el("li",window.ReserveI18n.format(gettext("%(product)s: %(quantity)s %(unit)s"),{product:item.row.name,quantity:item.quantity,unit:item.row.unit})));
    review.hidden=false;review.focus();
  });
  document.querySelector("#request-edit").addEventListener("click",()=>{if(!locked())review.hidden=true;});
  document.querySelector("#cart-clear").addEventListener("click",()=>{if(!locked()){selected.clear();review.hidden=true;render();}});
  form.addEventListener("submit",event=>{event.preventDefault();if(review.hidden || locked() || !selected.size)return;const payload={lines:[...selected.values()].map(item=>({product_id:item.row.product_id,quantity:item.quantity}))};operation.send("/api/v1/requests",payload);});
  document.querySelector("#request-filter").addEventListener("submit",async event=>{event.preventDefault();try{await load();}catch{operation.message(gettext("Recherche indisponible. Sélection conservée en mémoire ; données potentiellement non actualisées."),true);}});
  document.querySelector("#request-more").addEventListener("click",async()=>{try{await load(true);}catch{operation.message(gettext("Chargement indisponible."),true);}});
  window.addEventListener("beforeunload",event=>{if(selected.size && !operation.pending){event.preventDefault();event.returnValue="";}});
  document.getElementById("request-category").addEventListener("change",()=>document.getElementById("request-filter").requestSubmit());
  window.ReserveLive.subscribe(async()=>{
    if(locked())return;
    const job=loadSequence;
    const params=new URLSearchParams(appliedParams);
    const data=await window.ReserveLive.products("/api/v1/request-catalog",params,visible.length);
    if(job!==loadSequence || locked())return;
    const queriedIds=[...selected.keys()];
    const selectedRows=queriedIds.length ? (await window.ReserveLive.read("/api/v1/request-catalog?"+new URLSearchParams({product_ids:queriedIds.join(",")}))).products : [];
    if(job!==loadSequence || locked() || queriedIds.join(",")!==[...selected.keys()].join(","))return;
    const currentSelected=[...selected.keys()].map(id=>selectedRows.find(row=>row.product_id===id) || {...known.get(id),demandable:false,maximum_allowed:0});
    const changed=JSON.stringify(data.products)!==JSON.stringify(visible.map(id=>known.get(id))) || JSON.stringify(currentSelected)!==JSON.stringify([...selected.keys()].map(id=>known.get(id)));
    const focused=list.contains(document.activeElement) ? document.activeElement : null;
    const invalid=[...list.querySelectorAll("input:invalid")].map(input=>[input.closest("[data-product-id]").dataset.productId,input.value,input.validationMessage]);
    const focusId=focused?.closest("[data-product-id]")?.dataset.productId;
    const start=focused?.selectionStart,end=focused?.selectionEnd;
    for(const row of data.products)known.set(row.product_id,row);
    for(const row of currentSelected)known.set(row.product_id,row);
    for(const [id,item] of selected)item.row=known.get(id) || item.row;
    if(changed){
      visible=data.products.map(row=>row.product_id);nextOffset=data.next_offset;render();
      for(const [id,value,message] of invalid){const input=[...list.querySelectorAll("input")].find(input=>input.closest("[data-product-id]").dataset.productId===id);if(input){input.value=value;input.setCustomValidity(message);}}
      if(focusId){const card=[...list.children].find(node=>node.dataset.productId===focusId);const target=card?.querySelector(focused.tagName==="INPUT"?"input":focused.textContent==="+"?"button:last-child":"button");target?.focus({preventScroll:true});if(target?.tagName==="INPUT" && start!==null)target.setSelectionRange(start,end);}
      actions();
    }
  });
  render();operation.start();
})();
