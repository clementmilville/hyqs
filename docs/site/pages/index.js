(function(){
  const cv=document.getElementById("flowArt"); if(!cv) return;
  const ctx=cv.getContext("2d"); const reduce=matchMedia("(prefers-reduced-motion: reduce)").matches;
  const W=1200,H=640; let dpr=1;
  const css=()=>{const s=getComputedStyle(document.documentElement);const g=n=>s.getPropertyValue(n).trim();return {ink:g("--ink"),muted:g("--muted"),line:g("--line2"),accent:g("--accent"),pass:g("--pass"),fail:g("--fail"),ochre:g("--ochre"),steel:g("--steel"),surface:g("--surface")};};
  let C=css(), tick=0;
  const size=()=>{dpr=Math.min(2,window.devicePixelRatio||1);cv.width=W*dpr;cv.height=H*dpr;ctx.setTransform(dpr,0,0,dpr,0,0);};
  size(); window.addEventListener("resize",size);
  // geometry: a main lane, five gates, a fix lane below, and a landing zone
  const laneY=250, laneH=150, fixY=470, gates=[{x:330,n:"lint",p:.04},{x:470,n:"test",p:.16},{x:610,n:"review",p:.12},{x:750,n:"security",p:.09},{x:890,n:"design",p:.03}];
  const mergeX=1010, liveX=1120, startX=40;
  const rnd=(a,b)=>a+Math.random()*(b-a);
  const parts=[]; const MAX=reduce?44:64;
  const spawn=(x)=>({x:x==null?startX-rnd(0,120):x,y:laneY+rnd(0,laneH),v:rnd(1.1,1.7),state:"run",gate:0,t:0,fix:0,r:rnd(4,6.5),flash:0,fc:null,ty:0});
  for(let i=0;i<MAX;i++){const p=spawn(rnd(startX,mergeX-40));p.gate=gates.findIndex(g=>g.x>p.x);if(p.gate<0)p.gate=gates.length;parts.push(p);}
  const drawStatic=()=>{
    ctx.clearRect(0,0,W,H);
    // lane band
    ctx.fillStyle=C.surface; ctx.globalAlpha=.5; roundRect(startX,laneY-24,liveX-startX+30,laneH+48,18); ctx.fill(); ctx.globalAlpha=1;
    // fix lane
    ctx.strokeStyle=C.ochre; ctx.setLineDash([6,6]); ctx.lineWidth=1.2; ctx.beginPath(); ctx.moveTo(gates[0].x-20,fixY); ctx.lineTo(gates[gates.length-1].x+20,fixY); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle=C.ochre; ctx.font="15px 'IBM Plex Mono', monospace"; ctx.textAlign="left"; ctx.fillText("fix · re-lint · re-test · re-review", gates[0].x-20, fixY+26);
    // gates
    gates.forEach(g=>{ctx.strokeStyle=C.steel; ctx.lineWidth=4; ctx.beginPath(); ctx.moveTo(g.x,laneY-30); ctx.lineTo(g.x,laneY+laneH+30); ctx.stroke(); ctx.fillStyle=C.muted; ctx.font="16px 'IBM Plex Mono', monospace"; ctx.textAlign="center"; ctx.fillText(g.n,g.x,laneY-46);});
    // merge + live
    ctx.strokeStyle=C.accent; ctx.lineWidth=4; ctx.beginPath(); ctx.moveTo(mergeX,laneY-30); ctx.lineTo(mergeX,laneY+laneH+30); ctx.stroke(); ctx.fillStyle=C.accent; ctx.font="16px 'IBM Plex Mono', monospace"; ctx.textAlign="center"; ctx.fillText("merge",mergeX,laneY-46);
    ctx.beginPath(); ctx.arc(liveX,laneY+laneH/2,30,0,Math.PI*2); ctx.strokeStyle=C.pass; ctx.lineWidth=4; ctx.stroke(); ctx.fillStyle=C.pass; ctx.fillText("live",liveX,laneY+laneH/2+66);
    ctx.fillStyle=C.muted; ctx.textAlign="left"; ctx.fillText("request",startX,laneY-46);
    ctx.font="14px 'IBM Plex Mono', monospace"; ctx.fillStyle=C.muted; ctx.textAlign="right"; ctx.fillText("deterministic gates · AI reviewers · budgets, not retries forever", liveX+28, H-28);
    // faint lane guides
    ctx.strokeStyle=C.line; ctx.lineWidth=1; ctx.globalAlpha=.5; for(let k=0;k<4;k++){const y=laneY+laneH*(k+.5)/4; ctx.beginPath(); ctx.moveTo(startX,y); ctx.lineTo(mergeX-12,y); ctx.stroke();} ctx.globalAlpha=1;
  };
  function roundRect(x,y,w,h,r){ctx.beginPath();ctx.moveTo(x+r,y);ctx.arcTo(x+w,y,x+w,y+h,r);ctx.arcTo(x+w,y+h,x,y+h,r);ctx.arcTo(x,y+h,x,y,r);ctx.arcTo(x,y,x+w,y,r);ctx.closePath();}
  const drawParts=()=>{
    parts.forEach(p=>{
      let col=C.ink; if(p.state==="fix")col=C.ochre; if(p.state==="done")col=C.pass;
      if(p.flash>0){col=p.fc;p.flash--;}
      ctx.globalAlpha=p.state==="done"?Math.max(0,1-p.t/60):1;
      ctx.fillStyle=col; ctx.beginPath(); ctx.arc(p.x,p.y,p.r,0,Math.PI*2); ctx.fill();
      ctx.globalAlpha=1;
    });
  };
  const step=()=>{
    parts.forEach((p,i)=>{
      if(p.state==="run"){
        p.x+=p.v;
        if(p.gate<gates.length&&p.x>=gates[p.gate].x){
          const g=gates[p.gate];
          if(Math.random()<g.p&&p.fix<2){p.state="fix";p.fix++;p.flash=18;p.fc=C.fail;p.ty=fixY+rnd(-12,12);}
          else{p.flash=12;p.fc=C.pass;p.gate++;}
        }
        if(p.x>=mergeX&&p.state==="run"){p.state="done";p.t=0;}
      }else if(p.state==="fix"){
        // curve down to the fix lane, travel left to before lint, then climb back into the lane
        if(p.y<p.ty){p.y+=3;p.x-=0.6;}
        else if(p.x>gates[0].x-40){p.x-=2.6;}
        else{p.state="climb";p.ty=laneY+rnd(0,laneH);}
      }else if(p.state==="climb"){
        if(p.y>p.ty){p.y-=3;p.x+=0.4;}else{p.state="run";p.gate=0;p.v=rnd(1.1,1.7);}
      }else if(p.state==="done"){
        p.x+=(liveX-p.x)*0.05; p.y+=(laneY+laneH/2-p.y)*0.05; p.t++;
        if(p.t>60)parts[i]=spawn();
      }
    });
  };
  const frame=()=>{if(tick++%30===0)C=css();drawStatic();drawParts();if(!reduce){step();requestAnimationFrame(frame);}};
  frame();
  document.getElementById("themeBtn")?.addEventListener("click",()=>{setTimeout(()=>{C=css();if(reduce){drawStatic();drawParts();}},50);});
})();
