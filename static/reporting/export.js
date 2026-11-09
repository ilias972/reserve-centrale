"use strict";
(() => {
  const form=document.querySelector("form[data-file-download]");
  if(!form)return;
  const button=form.querySelector("button[type=submit]");
  const status=document.getElementById("export-status");
  let pending=false;
  form.addEventListener("submit",async event=>{
    event.preventDefault();
    if(pending)return;
    if(!navigator.onLine){status.textContent=gettext("Connexion indisponible. Reconnectez-vous avant de demander le fichier.");return;}
    pending=true;button.disabled=true;form.setAttribute("aria-busy","true");
    status.textContent=gettext("Génération du fichier Excel en cours…");
    try{
      const response=await fetch(form.action,{method:"POST",body:new FormData(form),credentials:"same-origin"});
      if(!response.ok){
        const html=new DOMParser().parseFromString(await response.text(),"text/html");
        const message=html.querySelector("main p");
        throw new Error(message?.textContent?.trim() || gettext("Le fichier n’a pas pu être généré. Réduisez les filtres ou reconnectez-vous."));
      }
      if(!response.headers.get("Content-Type")?.includes("spreadsheetml"))throw new Error(gettext("L’accès a changé. Reconnectez-vous avant de relancer l’export."));
      const blob=await response.blob();
      const url=URL.createObjectURL(blob);
      const link=document.createElement("a");
      link.href=url;link.download=response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] || "reserve.xlsx";
      document.body.append(link);link.click();link.remove();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
      status.textContent=gettext("Fichier Excel généré et transmis au navigateur. Vous pouvez le retrouver dans vos téléchargements.");
    }catch(error){
      status.textContent=error instanceof TypeError ? gettext("Réponse réseau interrompue : aucun téléchargement confirmé. Reconnectez-vous puis relancez l’export ; les quantités ne sont pas modifiées.") : error.message;
    }finally{pending=false;button.disabled=false;form.removeAttribute("aria-busy");}
  });
})();
