(function(){
  const $=id=>document.getElementById(id);
  const f=(n,c)=>c+n.toLocaleString(undefined,{maximumFractionDigits:0});
  const calc=()=>{
    const vol=+$("cVol").value,cost=+$("cCost").value,rw=+$("cRework").value,eng=+$("cEng").value,min=+$("cMin").value;
    $("oVol").textContent=vol;$("oCost").textContent=cost.toFixed(2);$("oRework").textContent=rw;$("oEng").textContent=eng;$("oMin").textContent=min;
    const bill=vol*cost,rework=bill*rw/100,hours=vol*min/60,eq=hours*eng;
    $("rBill").textContent=f(bill,"$");$("rRework").textContent=f(rework,"$");$("rHours").textContent=hours.toLocaleString(undefined,{maximumFractionDigits:0})+" h";$("rEq").textContent=f(eq,"€");
    $("rRatio").textContent=(eq/bill).toFixed(1)+"×";
  };
  ["cVol","cCost","cRework","cEng","cMin"].forEach(id=>$(id).addEventListener("input",calc));calc();
})();
