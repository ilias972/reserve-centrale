"use strict";
// Les mêmes lignes/liens sont conservés : aucun contrôle mobile dupliqué.
(() => {
  const main=document.querySelector("main");if(!main)return;
  let nextId=0,scheduled=false;
  function enhance(){
    scheduled=false;
    for(const table of main.querySelectorAll("table")){
      const wrapper=table.closest(".table-scroll,.table-wrap");if(!wrapper)continue;
      if(!table.dataset.scrollHint){
        const hint=document.createElement("p");hint.className="hint table-scroll-hint";hint.id=`table-scroll-hint-${++nextId}`;
        hint.textContent=gettext("Faites défiler le tableau horizontalement pour consulter les autres colonnes.");
        wrapper.before(hint);table.dataset.scrollHint=hint.id;
        wrapper.dataset.uiTableDescription=wrapper.getAttribute("aria-describedby") || "";
        wrapper.dataset.uiTableTab=wrapper.getAttribute("tabindex") ?? "";
        wrapper.setAttribute("aria-describedby",[wrapper.dataset.uiTableDescription,hint.id].filter(Boolean).join(" "));
      }
      if(table.hasAttribute("data-action-table") && !table.dataset.tableEnhanced){
        const headers=[...table.querySelectorAll("thead th")].map(n=>n.textContent.trim());
        table.setAttribute("role","table");
        for(const row of table.querySelectorAll("tr")){
          row.setAttribute("role","row");
          for(const [index,cell] of [...row.children].entries()){
            if(cell.tagName==="TH")cell.setAttribute("role","columnheader");
            else {cell.setAttribute("role","cell");if(cell.colSpan===1)cell.dataset.label=headers[index] || "";}
          }
        }
        table.dataset.tableEnhanced="true";
      }
      // Même un dépassement d'un seul pixel est défilable : garder la région
      // au clavier évite qu'un arrondi CSS retire son arrêt de tabulation.
      const overflow=wrapper.scrollWidth>wrapper.clientWidth;
      const hint=document.getElementById(table.dataset.scrollHint);if(hint)hint.hidden=!overflow;
      wrapper.tabIndex=overflow?0:-1;
    }
  }
  function schedule(){if(!scheduled){scheduled=true;requestAnimationFrame(enhance);}}
  window.ReserveUI.tables = {
    comparableHTML(region) {
      const copy=region.cloneNode(true);
      for(const hint of copy.querySelectorAll(".table-scroll-hint"))hint.remove();
      for(const table of copy.querySelectorAll("table[data-scroll-hint]")){
        table.removeAttribute("data-scroll-hint");
        if(table.hasAttribute("data-table-enhanced")){
          table.removeAttribute("data-table-enhanced");table.removeAttribute("role");
          for(const cell of table.querySelectorAll("tr,th,td")){cell.removeAttribute("role");cell.removeAttribute("data-label");}
        }
      }
      for(const wrapper of copy.querySelectorAll("[data-ui-table-tab]")){
        const description=wrapper.dataset.uiTableDescription,tab=wrapper.dataset.uiTableTab;
        if(description)wrapper.setAttribute("aria-describedby",description);else wrapper.removeAttribute("aria-describedby");
        if(tab!=="")wrapper.setAttribute("tabindex",tab);else wrapper.removeAttribute("tabindex");
        wrapper.removeAttribute("data-ui-table-description");wrapper.removeAttribute("data-ui-table-tab");
      }
      return copy.innerHTML;
    }
  };
  // Les régions de liste peuvent être remplacées par l’actualisation M4/M7.
  new MutationObserver(schedule).observe(main,{childList:true,subtree:true});
  new ResizeObserver(schedule).observe(main);
  window.addEventListener("resize",schedule);
  enhance();
})();
