"use strict";
// Une sélection lance seulement le formulaire GET de recherche, jamais une écriture.
(() => {
  document.querySelectorAll("input[data-search-scope]").forEach((input, index) => {
    const form=input.form;
    if(!form || form.method.toLowerCase()!=="get")return;
    const box=document.createElement("div"),list=document.createElement("ul"),status=document.createElement("span");
    box.className="suggestion-field";list.className="search-suggestions";status.className="search-suggestion-status sr-only";
    const icon=input.parentElement.querySelector(":scope > svg.icon");
    list.id=`search-options-${index}`;list.setAttribute("role","listbox");list.setAttribute("aria-label",gettext("Suggestions de recherche"));list.hidden=true;
    status.setAttribute("role","status");status.hidden=true;
    input.before(box);box.append(input,list,status);if(icon)box.prepend(icon);
    input.setAttribute("role","combobox");input.setAttribute("aria-autocomplete","list");input.setAttribute("aria-controls",list.id);input.setAttribute("aria-expanded","false");input.setAttribute("autocomplete","off");
    let timer,controller,sequence=0,active=-1,items=[];
    function close(keepStatus=false){clearTimeout(timer);controller?.abort();sequence++;list.hidden=true;if(!keepStatus)status.hidden=true;input.setAttribute("aria-expanded","false");input.removeAttribute("aria-activedescendant");active=-1;items=[];list.replaceChildren();}
    function highlight(index){active=index;[...list.children].forEach((node,i)=>node.setAttribute("aria-selected",String(i===active)));if(active>=0){input.setAttribute("aria-activedescendant",list.children[active].id);list.children[active].scrollIntoView({block:"nearest"});}}
    function choose(index){const item=items[index];if(!item)return;input.value=item.value;close();input.dispatchEvent(new Event("change",{bubbles:true}));form.requestSubmit();}
    async function suggest(){
      close();const text=input.value.trim();if(!text)return;
      const job=sequence;controller=new AbortController();const deadline=setTimeout(()=>{if(job===sequence){controller.abort();status.classList.remove("sr-only");status.textContent=gettext("Suggestions indisponibles. Vous pouvez lancer la recherche.");status.hidden=false;}},5000);
      const params=new URLSearchParams({scope:input.dataset.searchScope,q:text});
      for(const name of ["category","status"]){const value=form.elements.namedItem(name)?.value;if(value)params.set(name,value);}
      if(input.dataset.searchMode)params.set("mode",input.dataset.searchMode);
      status.classList.add("sr-only");status.textContent=gettext("Recherche de suggestions…");status.hidden=false;
      try{
        const response=await fetch("/api/v1/search-suggestions?"+params,{credentials:"same-origin",cache:"no-store",headers:{"X-Reserve-Background":"1"},signal:controller.signal});
        if(!response.ok)throw new Error(gettext("Suggestions indisponibles"));
        const data=await response.json();if(job!==sequence || text!==input.value.trim() || document.activeElement!==input)return;
        items=data.suggestions;status.classList.toggle("sr-only",Boolean(items.length));status.textContent=items.length?`${window.ReserveI18n.plural("%(count)s suggestion","%(count)s suggestions",items.length)} — ${gettext("flèches puis Entrée, ou toucher un résultat.")}`:gettext("Aucune suggestion. Vous pouvez lancer la recherche.");
        items.forEach((item,i)=>{const node=document.createElement("li");node.id=`${list.id}-${i}`;node.setAttribute("role","option");node.setAttribute("aria-selected","false");node.dataset.index=String(i);
          if(input.dataset.searchPhotos==="true"){
            const photo=document.createElement(item.photo_url?"img":"span");photo.className="suggestion-photo";
            if(item.photo_url){const url=new URL(item.photo_url,location.origin);if(url.origin===location.origin){photo.src=url.href;photo.alt="";photo.width=44;photo.height=44;photo.loading="lazy";}}
            else{photo.textContent=gettext("Sans photo");photo.setAttribute("aria-hidden","true");}
            node.append(photo);
          }
          const label=document.createElement("span");label.textContent=item.label;node.append(label);list.append(node);});
        list.hidden=!items.length;input.setAttribute("aria-expanded",String(Boolean(items.length)));
      }catch(error){if(error.name!=="AbortError" && job===sequence){status.classList.remove("sr-only");status.textContent=gettext("Suggestions indisponibles. Vous pouvez lancer la recherche.");status.hidden=false;}}finally{clearTimeout(deadline);}
    }
    input.addEventListener("input",()=>{close();timer=setTimeout(suggest,250);});
    input.addEventListener("focus",()=>{if(input.value.trim())timer=setTimeout(suggest,250);});
    input.addEventListener("keydown",event=>{
      if(event.key==="Escape" && (!list.hidden || !status.hidden)){event.preventDefault();event.stopPropagation();close();return;}
      if(event.key==="Tab"){close();return;}
      if(!items.length)return;
      if(event.key==="ArrowDown" || event.key==="ArrowUp"){event.preventDefault();highlight(active<0 ? (event.key==="ArrowDown" ? 0 : items.length-1) : (active+(event.key==="ArrowDown"?1:items.length-1))%items.length);}
      else if(event.key==="Enter" && active>=0){event.preventDefault();choose(active);}
    });
    list.addEventListener("mousedown",event=>event.preventDefault());
    list.addEventListener("click",event=>{const option=event.target.closest("[role=option]");if(option)choose(Number(option.dataset.index));});
    document.addEventListener("click",event=>{if(!box.contains(event.target))close(true);});
    document.addEventListener("focusin",event=>{if(!box.contains(event.target))close(true);});
    document.addEventListener("visibilitychange",()=>{if(document.hidden)close();});
    form.addEventListener("submit",()=>close());window.addEventListener("pagehide",()=>close());
  });
})();
