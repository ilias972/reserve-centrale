"use strict";
(() => {
  const node=document.getElementById("request-watch");
  if(!node)return;
  window.ReserveLive.subscribe(async()=>{
    const data=await window.ReserveLive.read(`/api/v1/requests/${node.dataset.id}`);
    if(data.request.version!==Number(node.dataset.version))window.ReserveLive.reload();
  });
})();
