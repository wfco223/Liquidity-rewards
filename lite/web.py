"""The one page, and the routes behind it.

The page is public (a shell); everything with data needs the dashboard
key, the same X-Dash-Key the phone already keeps. An order tap also
needs the X-Reprice header (the CSRF guard every order-touching
endpoint has kept since 1.0); so do a scan and a change to his tracked
list, which go through the same POST.

The look (owner, 2026-10-05: "make the aesthetic a little less hacker and
more sleek and mainstream"): the phone's own system font, light or dark as
the phone is set, white cards on a soft grey ground, one accent colour,
green for bids and red for asks."""

from __future__ import annotations

import gzip
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from v3.web import authed

# 3.0's pages: an old bookmark lands on the one page
OLD_PAGES = {"/graph", "/fills", "/watch", "/status", "/orders", "/pay", "/grades",
             "/bonds", "/focus", "/tiers", "/switch", "/log", "/plan", "/silver",
             "/map", "/lab", "/hunt", "/why", "/slate", "/unwind", "/v2", "/v3"}

PAGE_CSS = """
:root{--bg:#f2f2f7;--card:#fff;--text:#111114;--sub:#6e6e76;--faint:#a1a1a8;
 --line:rgba(60,60,67,.13);--fill:rgba(118,118,128,.12);--fill2:rgba(118,118,128,.2);
 --accent:#0a7cff;--pos:#12a150;--neg:#e5484d;--warn:#b86e00;--chart:#12a150;
 --shadow:0 1px 2px rgba(16,16,24,.04),0 6px 20px rgba(16,16,24,.05);--scrim:rgba(0,0,0,.32)}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#161618;--text:#f4f4f6;--sub:#9a9aa2;
 --faint:#66666d;--line:rgba(84,84,88,.42);--fill:rgba(118,118,128,.2);--fill2:rgba(118,118,128,.32);
 --accent:#3b9bff;--pos:#32d27a;--neg:#ff6b6b;--warn:#ffb340;--chart:#32d27a;
 --shadow:none;--scrim:rgba(0,0,0,.55)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
 font:15px/1.4 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 -webkit-font-smoothing:antialiased}
.wrap{max-width:640px;margin:0 auto;padding:6px 16px 48px}
.top{display:flex;align-items:baseline;justify-content:space-between;padding:14px 2px 2px}
.top h1{font-size:30px;font-weight:700;letter-spacing:-.025em;margin:0}
.num{font-variant-numeric:tabular-nums}
.card{background:var(--card);border-radius:20px;padding:18px;margin:14px 0;box-shadow:var(--shadow)}
.card.list{padding:4px 0}
.label{font-size:13px;color:var(--sub);font-weight:500}
.muted{color:var(--sub);font-size:13px}
.dim{color:var(--faint)}
.ok{color:var(--pos)}.bad{color:var(--neg)}.warn{color:var(--warn)}
.hero{font-size:46px;font-weight:700;letter-spacing:-.035em;line-height:1.05;margin:2px 0 4px;
 font-variant-numeric:tabular-nums}
.hero small{font-size:17px;font-weight:500;color:var(--sub);letter-spacing:0;margin-left:3px}
.graph{margin:10px -4px 6px}
.seg{display:inline-flex;background:var(--fill);border-radius:10px;padding:2px;gap:2px}
.seg button{border:0;background:transparent;color:var(--text);font-family:inherit;font-size:13px;line-height:1;font-weight:600;
 padding:7px 14px;border-radius:8px;cursor:pointer}
.seg button.on{background:var(--card);box-shadow:0 1px 3px rgba(0,0,0,.14)}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px;
 padding-top:14px;border-top:1px solid var(--line)}
.kpis .v{font-size:19px;font-weight:650;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.kpis .l{font-size:12px;color:var(--sub);margin-top:1px}
.note{margin:8px 0;font-size:13px}
.sec-h{display:flex;align-items:center;justify-content:space-between;margin:22px 2px 8px}
.sec-h h2{font-size:22px;font-weight:700;letter-spacing:-.02em;margin:0}
.chips{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding:2px 0;margin:6px 0}
.chips::-webkit-scrollbar{display:none}
.chip{flex:none;border:0;background:var(--fill);color:var(--text);font-family:inherit;font-size:13px;line-height:1;font-weight:600;
 padding:9px 13px;border-radius:999px;cursor:pointer;white-space:nowrap}
.chip.on{background:var(--text);color:var(--bg)}
.chip.tog.on{background:var(--accent);color:#fff}
.legend{font-size:12px;color:var(--sub);margin:2px 2px 0;display:flex;flex-wrap:wrap;
 justify-content:space-between;gap:2px 12px}
.legend>span{white-space:nowrap}
.btn{border:0;border-radius:12px;background:var(--accent);color:#fff;font-family:inherit;font-size:15px;line-height:1;font-weight:600;
 padding:12px 18px;cursor:pointer}
.btn.small{font-size:14px;padding:9px 14px;border-radius:999px}
.btn.gray{background:var(--fill);color:var(--text)}
.btn.red{background:rgba(229,72,77,.12);color:var(--neg)}
.btn:disabled{opacity:.45}
input{font-family:inherit;font-size:16px;line-height:1.2;color:var(--text);background:var(--fill);border:1px solid transparent;
 border-radius:12px;padding:11px 12px;outline:none;min-width:0}
input:focus{border-color:var(--accent);background:var(--card)}
.ghead{display:flex;align-items:center;gap:8px;width:100%;border:0;background:transparent;
 color:var(--text);font-family:inherit;font-size:17px;line-height:1.2;font-weight:700;padding:14px 16px;cursor:pointer;text-align:left}
.ghead .chev{flex:none;transition:transform .15s;color:var(--sub)}
.ghead .chev.open{transform:rotate(90deg)}
.ghead .gm{margin-left:auto;font-size:13px;font-weight:500;color:var(--sub)}
.subh{font-size:12px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.05em;
 padding:12px 16px 4px;border-top:1px solid var(--line)}
.secname{font-size:12px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.05em;
 margin:16px 4px 0}
.mrow{padding:12px 16px;border-top:1px solid var(--line);cursor:pointer}
.card.list>.mrow:first-child{border-top:0}
.subh+.mrow{border-top:0}
.mrow:active{background:var(--fill)}
.mtop{display:flex;align-items:flex-start;gap:8px}
.mn{font-size:15px;font-weight:600;line-height:1.3;flex:1;min-width:0}
.msub{font-size:12.5px;color:var(--sub);margin-top:2px}
.star{color:#f5a524;margin-left:5px;font-size:13px}
.sbtn{border:0;background:transparent;font-size:22px;line-height:1;color:var(--faint);padding:0 2px;cursor:pointer}
.sbtn.on{color:#f5a524}
.side{display:grid;grid-template-columns:34px 1fr auto 52px;gap:8px;align-items:center;
 margin-top:7px;font-size:13.5px;font-variant-numeric:tabular-nums}
.side .t{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.side .t.bid{color:var(--pos)}.side .t.ask{color:var(--neg)}
.side .so{display:flex;flex-wrap:wrap;gap:4px;min-width:0}
.side .d{text-align:right;font-weight:650}
.side .p{text-align:right;color:var(--sub)}
.pill{border:0;background:var(--fill);color:var(--text);border-radius:8px;padding:4px 8px;
 font-family:inherit;font-size:13px;line-height:1.2;font-weight:600;font-variant-numeric:tabular-nums;cursor:pointer}
.pill.ghost{background:transparent;color:var(--warn);padding:4px 0;font-weight:500}
.new{color:var(--faint);font-size:12.5px}
.bsheet{position:fixed;left:0;right:0;bottom:0;max-height:90vh;overflow:auto;z-index:20;
 background:var(--card);border-radius:22px 22px 0 0;padding:10px 18px 30px;
 box-shadow:0 -10px 40px rgba(0,0,0,.18);max-width:680px;margin:0 auto}
.bsheet .grab{width:38px;height:5px;border-radius:3px;background:var(--fill2);margin:2px auto 8px}
.bscrim{position:fixed;inset:0;background:var(--scrim);z-index:19;
 -webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px)}
.shead{display:flex;align-items:flex-start;gap:10px;margin:4px 0 2px}
.shead .tt{font-size:19px;font-weight:700;letter-spacing:-.01em;flex:1;line-height:1.25}
.close{border:0;background:var(--fill);color:var(--sub);border-radius:999px;width:30px;height:30px;
 font-family:inherit;font-size:15px;line-height:1;font-weight:600;cursor:pointer;flex:none}
.big{font-size:30px;font-weight:700;letter-spacing:-.02em;margin:6px 0 2px;font-variant-numeric:tabular-nums}
.stats{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0}
.stats .v{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums}
.stats .l{font-size:12px;color:var(--sub)}
.tbl{width:100%;border-collapse:collapse;font-size:13.5px;font-variant-numeric:tabular-nums}
.tbl td{padding:6px 2px;border-top:1px solid var(--line)}
.tbl tr:first-child td{border-top:0}
.tbl td.r{text-align:right}
.tbl tr.me td{font-weight:700}
.bk{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:10px 0 4px}
.bk .h{font-size:12px;font-weight:600;color:var(--sub);margin-bottom:2px}
.bk .px.bid{color:var(--pos)}.bk .px.ask{color:var(--neg)}
.bk tr.me td{background:var(--fill)}
.frm{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
.frm input{width:96px}
.frm .u{color:var(--sub);margin-left:-4px}
.panel{background:var(--fill);border-radius:14px;padding:10px 12px;margin:10px 0}
.nl{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:13.5px;
 font-variant-numeric:tabular-nums;padding:3px 0}
.chk{display:flex;align-items:center;gap:10px;padding:10px 2px;border-top:1px solid var(--line);cursor:pointer}
.chk:first-child{border-top:0}
.chk input{width:20px;height:20px;accent-color:var(--accent);flex:none}
.chk .cl{flex:1;font-size:14.5px;font-weight:500}
.chk .cm{font-size:12.5px;color:var(--sub);text-align:right;white-space:nowrap}
.bar{height:6px;border-radius:3px;background:var(--fill);overflow:hidden;margin:8px 0}
.bar>div{height:100%;background:var(--accent);border-radius:3px;transition:width .3s}
.link{border:0;background:transparent;color:var(--accent);font-family:inherit;font-size:13.5px;line-height:1;font-weight:600;padding:4px 0;cursor:pointer}
.banner{background:var(--card);border-radius:14px;padding:12px 16px;margin:12px 0;color:var(--sub);font-size:14px}
"""

PAGE_JS = r"""
var K='dashKey';
function LS(k,d){try{var v=localStorage.getItem(k);return v==null?d:v;}catch(e){return d;}}
function LSset(k,v){try{localStorage.setItem(k,v);}catch(e){}}
function hdrs(){var h=new Headers();h.set('X-Dash-Key',LS(K,''));return h;}
function saveKey(){LSset(K,document.getElementById('k').value);load();}
function usd(x){var v=x||0;return (v<0?'−$':'$')+Math.abs(v).toFixed(2);}
function usd0(x){var v=x||0;return '$'+(v>=100?Math.round(v).toLocaleString():v.toFixed(v%1?2:0));}
function ct(x){var v=Math.round((x||0)*1000)/10;return (v%1?v.toFixed(1):''+Math.round(v))+'¢';}
function pct(x){if(x==null)return '—';var v=x*100;return (v>=10?Math.round(v):v>=1?v.toFixed(1):v.toFixed(2))+'%';}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function sz(q){q=q||0;if(q>=1e6)return (q/1e6).toFixed(1)+'M';if(q>=1e4)return (q/1e3).toFixed(1)+'k';return (Math.round(q*100)/100).toLocaleString();}
function fmtT(ts){var d=new Date(ts*1000);return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}
function side(s){return s==='BUY'?'Bid':'Ask';}
function day(e){return e==null?'—':usd(e)+'/day';}
function ago(s){s=Math.round(s||0);return s<120?s+'s':Math.round(s/60)+' min';}
function stake(){var v=parseFloat(LS('stake','50'));return v>0?v:50;}
function get(url,cb){
 fetch(url,{headers:hdrs()}).then(function(r){
  if(r.status===401){document.getElementById('login').style.display='';return null;}
  return r.json();}).then(function(j){if(j)cb(j);})
  .catch(function(){cb({ok:false,note:'no answer'});});
}
function post(body,cb){
 var h=hdrs();h.set('X-Reprice','1');h.set('Content-Type','application/json');
 var ac=window.AbortController?new AbortController():null;if(ac)setTimeout(function(){ac.abort();},60000);
 fetch('/op',{method:'POST',headers:h,body:JSON.stringify(body),signal:ac?ac.signal:undefined})
  .then(function(r){return r.json();}).then(cb)
  .catch(function(){cb({ok:false,note:'No answer in time. It may still have gone through — check before tapping again.'});});
}

// -- the graph: 3.0's quick look, one band ------------------------------------
function graph(d,win){
 var now=Date.now()/1000;
 var dots=(d.dots||[]).filter(function(x){return x[0]>=now-win;});
 var m={};dots.forEach(function(x){m[Math.round(x[0]/20)*20]=x[1];});
 var ts=Object.keys(m).map(Number).sort(function(a,b){return a-b;});
 if(ts.length<2)return '<div class="muted" style="padding:30px 4px">Not enough samples yet</div>';
 var last=0,lt=-1e12;
 var v=ts.map(function(t){if(m[t]!=null){last=m[t];lt=t;}return (t-lt>180)?0:last;});
 var ymax=Math.max.apply(null,v)*1.08||1;
 var W=340,H=170,PL=34,PB=18,PT=6,PR=4,t0=ts[0],t1=ts[ts.length-1],span=Math.max(t1-t0,60);
 function X(t){return PL+(W-PL-PR)*(t-t0)/span;}
 function Y(y){return PT+(H-PT-PB)*(1-y/ymax);}
 var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto;display:block" role="img" aria-label="earning rate">'
  +'<defs><linearGradient id="gf" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--chart);stop-opacity:.32"/>'
  +'<stop offset="1" style="stop-color:var(--chart);stop-opacity:0"/></linearGradient></defs>';
 [0,0.5,1].forEach(function(f){var y=Y(ymax*f);
  s+='<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" style="stroke:var(--line)" stroke-dasharray="'+(f?'2 3':'')+'"/>'
   +'<text x="'+(PL-6)+'" y="'+(y+3)+'" text-anchor="end" font-size="9" style="fill:var(--faint)">$'+(ymax*f).toFixed(0)+'</text>';});
 var pts=ts.map(function(t,i){return X(t).toFixed(1)+','+Y(v[i]).toFixed(1);}).join(' ');
 var base=ts.map(function(t){return X(t).toFixed(1)+','+Y(0).toFixed(1);}).reverse().join(' ');
 s+='<polygon points="'+pts+' '+base+'" fill="url(#gf)"/>';
 s+='<polyline points="'+pts+'" fill="none" style="stroke:var(--chart)" stroke-width="1.8" stroke-linejoin="round"/>';
 [t0,(t0+t1)/2,t1].forEach(function(t,i){
  s+='<text x="'+X(t)+'" y="'+(H-4)+'" text-anchor="'+(i===0?'start':i===2?'end':'middle')+'" font-size="9" style="fill:var(--faint)">'+fmtT(t)+'</text>';});
 return s+'</svg>';
}
function win(sec){window._win=sec;if(window._d)draw(window._d);}

// -- the list of markets --------------------------------------------------------
var SORTS=[['type','Type'],['bid_day','Bid $'],['ask_day','Ask $'],['bid_pct','Bid %'],['ask_pct','Ask %']];
var KINDS=['House','Senate','Governor','Other'];
function sortKey(){var k=LS('sort','type');return SORTS.some(function(s){return s[0]===k;})?k:'type';}
function setSort(k){LSset('sort',k);window._revealSort=1;if(window._d)draw(window._d);}
function noFirst(){return LS('nofirst','0')==='1';}
function toggleNo(){LSset('nofirst',noFirst()?'0':'1');if(window._d)draw(window._d);}
function cols(){try{return JSON.parse(LS('col','{}'))||{};}catch(e){return {};}}
function tog(id){var c=cols();c[id]=!c[id];LSset('col',JSON.stringify(c));if(window._d)draw(window._d);}
function live(x){return (x.o||[]).filter(function(o){return !o.ghost;});}
function hasSide(x){return live(x).length>0;}
function byName(a,b){return a.name.localeCompare(b.name,undefined,{numeric:true});}
function cmp(k){var sd=k.slice(0,3),f=k.slice(4);
 return function(a,b){var x=a[sd],y=b[sd],ha=hasSide(x),hb=hasSide(y);
  // his orders first, by what they earn; then the sides he has no order
  // on, by what a new order would
  if(ha!==hb)return ha?-1:1;
  var va=ha?x[f]:(x.new||{})[f],vb=hb?y[f]:(y.new||{})[f];
  va=va==null?-1:va;vb=vb==null?-1:vb;
  return vb!==va?vb-va:byName(a,b);};}
function rowDay(r){return (hasSide(r.bid)?r.bid.day||0:0)+(hasSide(r.ask)?r.ask.day||0:0);}
function sideHtml(sd,x){
 var lv=live(x),gh=(x.o||[]).filter(function(o){return o.ghost;}),n=x.new||{};
 var h='<div class="side"><span class="t '+(sd==='BUY'?'bid':'ask')+'">'+side(sd)+'</span><span class="so">';
 lv.forEach(function(o){h+='<button class="pill" onclick="event.stopPropagation();openOrder(\''+esc(o.id)+'\')">'+ct(o.price)+' × '+sz(o.size)+'</button>';});
 gh.forEach(function(o){h+='<span class="pill ghost">'+ct(o.price)+' × '+sz(o.size)+' cancel sent</span>';});
 if(!lv.length)h+='<span class="new">'+(n.day>0?'new '+ct(n.px)+' × '+sz(n.qty):esc(n.why||'—'))+'</span>';
 h+='</span>';
 if(lv.length)h+='<span class="d">'+(x.day==null?'—':usd(x.day))+'</span><span class="p">'+pct(x.pct)+'</span>';
 else h+='<span class="d dim">'+(n.day>0?usd(n.day):'')+'</span><span class="p dim">'+(n.day>0?pct(n.pct):'')+'</span>';
 return h+'</div>';
}
function rowHtml(r){
 var sub=[];
 if(r.net)sub.push((r.net>0?'Yes ':'No ')+sz(Math.abs(r.net))+(r.value?' · '+usd(r.value):''));
 if(!r.has)sub.push(r.why==='event'?'in your event':r.why==='watched'?'tracked':'no order');
 return '<div class="mrow" onclick="openMarket(\''+esc(r.m)+'\')"><div class="mtop"><div class="mn">'+esc(r.name)
  +(r.w?'<span class="star">★</span>':'')+'</div></div>'
  +(sub.length?'<div class="msub">'+esc(sub.join(' · '))+'</div>':'')
  +sideHtml('BUY',r.bid)+sideHtml('SELL',r.ask)+'</div>';
}
function groupsHtml(pre,rows){
 var h='',c=cols();
 KINDS.forEach(function(kd){
  var g=rows.filter(function(r){return r.kind===kd;});if(!g.length)return;
  var id=pre+kd,open=!c[id],d=0,no=0;
  g.forEach(function(r){d+=rowDay(r);if(r.has)no++;});
  h+='<div class="card list"><button class="ghead" onclick="tog(\''+id+'\')"><svg class="chev'+(open?' open':'')+'" viewBox="0 0 10 16" width="9" height="14"><path d="M2.5 2l6 6-6 6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>'+kd
   +'<span class="gm">'+g.length+(no?' · '+no+' with orders':'')+(d?' · '+usd(d)+'/day':'')+'</span></button>';
  if(open){
   var by={};g.forEach(function(r){(by[r.st]=by[r.st]||[]).push(r);});
   Object.keys(by).sort(function(a,b){return a===''?-1:b===''?1:a.localeCompare(b);}).forEach(function(st){
    if(st||kd!=='Other')h+='<div class="subh">'+esc(st||'National')+'</div>';
    by[st].sort(byName).forEach(function(r){h+=rowHtml(r);});});
  }
  h+='</div>';});
 return h;
}
function listHtml(pre,rows,k){
 if(k==='type')return groupsHtml(pre,rows);
 rows.sort(cmp(k));
 return '<div class="card list">'+rows.map(rowHtml).join('')+'</div>';
}
function marketsHtml(d){
 var rows=(d.markets||[]).slice(),k=sortKey(),tot=0,nOrd=0;
 rows.forEach(function(r){tot+=rowDay(r);if(r.has)nOrd++;});
 var h='<div class="sec-h"><h2>Markets</h2><button class="btn small" onclick="openScan()">Scan</button></div>'
  +'<div class="chips" id="sorts">'+SORTS.map(function(s){return '<button class="chip'+(k===s[0]?' on':'')+'" onclick="setSort(\''+s[0]+'\')">'+s[1]+'</button>';}).join('')+'</div>'
  +'<div class="chips"><button class="chip tog'+(noFirst()?' on':'')+'" onclick="toggleNo()">No orders first</button></div>'
  +'<div class="legend"><span>'+rows.length+' markets · '+nOrd+' with orders · '+usd(tot)+'/day</span>'
  +'<span>$ a day · % per $ a day</span></div>'
  +(d.orders_age>60?'<div class="note warn">Orders read '+ago(d.orders_age)+' ago</div>':'')
  +(d.positions_age>120?'<div class="note warn">Holdings read '+ago(d.positions_age)+' ago</div>':'');
 if(noFirst()){
  var top=rows.filter(function(r){return !r.has;}),rest=rows.filter(function(r){return r.has;});
  if(top.length)h+='<div class="secname">No orders</div>'+listHtml('n',top,k);
  if(rest.length)h+='<div class="secname">With orders</div>'+listHtml('',rest,k);
 } else if(rows.length)h+=listHtml('',rows,k);
 if(!rows.length)h+='<div class="card muted">No orders or holdings yet. Tap Scan to find places to earn.</div>';
 if(d.small_n)h+='<div class="muted" style="margin:6px 4px">+'+d.small_n+' holdings under $1 ('+usd(d.small_v)+')</div>';
 return h;
}

// -- the page ----------------------------------------------------------------------
function rowKeep(id){var e=document.getElementById(id);return e?e.scrollLeft:null;}
function rowBack(id,left,reveal){var r=document.getElementById(id);if(!r)return;
 if(left!=null)r.scrollLeft=left;
 if(!reveal)return;
 var c=r.querySelector('.chip.on');if(!c)return;var cr=c.getBoundingClientRect(),rr=r.getBoundingClientRect();
 if(cr.left<rr.left)r.scrollLeft-=rr.left-cr.left+8;else if(cr.right>rr.right)r.scrollLeft+=cr.right-rr.right+8;}
function typing(){var a=document.activeElement;
 return !!(a&&a.tagName==='INPUT'&&a.type!=='checkbox'&&a.closest&&a.closest('#sheet'));}
function draw(d){
 window._d=d;
 if(window._win==null)window._win=21600;
 var b=function(sec,l){return '<button class="'+(window._win===sec?'on':'')+'" onclick="win('+sec+')">'+l+'</button>';};
 var h='';
 if(d.starting)h+='<div class="banner">Starting — reading your orders</div>';
 h+='<div class="card"><div class="label">Earning now</div><div class="hero">'+usd(d.rate)+'<small>/day</small></div>'
  +'<div class="graph">'+graph(d,window._win)+'</div>'
  +'<div class="seg">'+b(900,'15 min')+b(21600,'6 hours')+'</div>'
  +'<div class="kpis"><div><div class="v">'+usd(d.earned)+'</div><div class="l">Earned today</div></div>'
  +'<div><div class="v">'+(d.bp==null?'—':usd(d.bp))+'</div><div class="l">Buying power</div></div>'
  +'<div><div class="v">'+usd(d.holdings_value)+'</div><div class="l">Holdings</div></div></div>'
  +(d.unmeasured_min>5?'<div class="note muted">'+Math.round(d.unmeasured_min)+' min unmeasured today</div>':'')
  +(d.bp_age>60?'<div class="note warn">Buying power read '+ago(d.bp_age)+' ago</div>':'')
  +(d.state_note?'<div class="note bad">'+esc(d.state_note)+'</div>':'')
  +'</div>';
 h+=marketsHtml(d);
 var left=rowKeep('sorts');
 document.getElementById('view').innerHTML=h;
 rowBack('sorts',left,window._revealSort);window._revealSort=0;
}
function load(){
 get('/data.json?stake='+stake(),function(d){
  if(d.ok===false)return;
  document.getElementById('login').style.display='none';draw(d);});
}

// -- the card: one order, one market, or the scan --------------------------------
function bookHtml(b,mine){
 var at={};(b.ours||[]).forEach(function(o){at[o.side+o.price.toFixed(4)]=1;});
 function col(lv,sd){var r='<table class="tbl">';lv.forEach(function(x){
  var me=at[sd+x[0].toFixed(4)]||(mine&&mine.side===sd&&Math.abs(mine.price-x[0])<1e-9);
  r+='<tr class="'+(me?'me':'')+'"><td class="px '+(sd==='BUY'?'bid':'ask')+'">'+ct(x[0])+(me?' •':'')+'</td><td class="r">'+sz(x[1])+'</td></tr>';});
  return r+'</table>';}
 return '<div class="bk"><div><div class="h">Bids</div>'+col(b.bids||[],'BUY')
  +'</div><div><div class="h">Asks</div>'+col(b.asks||[],'SELL')+'</div></div>'
  +'<div class="muted">'+(b.age==null?'No book':'Book '+Math.round(b.age)+'s old · tick '+ct(b.tick))+'</div>';
}
function mathHtml(o,m){
 var h='<table class="tbl">';
 function r(k,v){h+='<tr><td>'+k+'</td><td class="r">'+v+'</td></tr>';}
 r('Pool, this side',m.side_pool==null?'—':usd(m.side_pool)+'/day');
 r('Target Size',m.target==null?'—':sz(m.target));
 if(m.side_total!=null)r('Side total',sz(m.side_total));
 if(m.ticks!=null)r('Ticks from best',m.ticks);
 if(m.df!=null)r('Discount a tick',m.df);
 if(m.score!=null)r('Your score',sz(m.score));
 if(m.denom!=null)r('Window score',sz(m.denom));
 if(m.share!=null)r('Share',(m.share*100).toFixed(1)+'%');
 r('Earns',m.est==null?'—':usd(m.est)+'/day');
 h+='</table>';
 if(m.why)h+='<div class="muted">'+esc(m.why)+'</div>';
 if(m.window&&m.window.length){
  h+='<div class="label" style="margin-top:10px">Window</div><table class="tbl">';
  m.window.forEach(function(w){h+='<tr class="'+(w[4]?'me':'')+'"><td>'+ct(w[0])+'</td><td class="r">'+sz(w[1])
   +'</td><td class="r">'+w[2]+'t</td><td class="r">'+sz(w[3])+'</td></tr>';});
  h+='</table>';
 }
 return h;
}
function head(t){return '<div class="grab"></div><div class="shead"><div class="tt">'+t+'</div><button class="close" onclick="closeSheet()" aria-label="close">✕</button></div>';}
function sheet(html){
 var s=document.getElementById('sheet');
 if(!s){s=document.createElement('div');s.id='sheet';s.className='bsheet';document.body.appendChild(s);
  var c=document.createElement('div');c.id='scrim';c.className='bscrim';c.onclick=closeSheet;document.body.appendChild(c);}
 var a=document.activeElement,keep=(a&&a.tagName==='INPUT'&&a.id&&s.contains(a))?[a.id,a.value]:null;
 var left=rowKeep('ssorts');
 s.innerHTML=html;
 rowBack('ssorts',left,window._revealSS);window._revealSS=0;
 if(keep){var e=document.getElementById(keep[0]);if(e){e.value=keep[1];}}
}
function closeSheet(){window._card=null;window._from=null;['sheet','scrim'].forEach(function(i){var e=document.getElementById(i);if(e)e.remove();});}
function say(j){window._said=j;refreshCard();load();}
function said(){var j=window._said;return '<div id="said">'+(j?'<div class="note '+(j.ok?'ok':'bad')+'">'+esc(j.note||'')+'</div>':'')+'</div>';}
function ohead(o){return '<div class="big"><span class="'+(o.side==='BUY'?'ok':'bad')+'">'+side(o.side)+'</span> '+ct(o.price)+' × '+sz(o.size)+'</div>';}
function openOrder(id){window._card={kind:'order',id:id};window._said=null;window._typed={};window._shown=null;
 // the order and its Cancel come up at once from the list, before any read
 var o=((window._d||{}).orders||[]).filter(function(x){return x.id===id;})[0];
 sheet(btns(o?head(esc(o.name))+ohead(o)
  +'<div class="muted">Reading…</div><div class="frm"><button class="btn red" onclick="doCancel()">Cancel order</button></div>'+said()
  :head('Order')+'<div class="muted">Reading…</div>'));
 refreshCard();}
function openMarket(m){if(!m)return;
 window._from=(window._card&&window._card.kind==='scan')?'scan':null;
 window._card={kind:'market',m:m};window._said=null;window._typed={};window._side=null;
 sheet(head('Market')+'<div class="muted">Reading…</div>');refreshCard();}
function openScan(){window._card={kind:'scan'};window._said=null;window._typed={};window._scanJ=null;
 sheet(head('Scan')+'<div class="muted">Reading…</div>');refreshCard();}
function typed(k){var e=document.getElementById(k);if(e)window._typed[k]=e.value;}
function val(k,d){var t=window._typed||{};return t[k]!=null?t[k]:d;}
function edited(){var t=window._typed||{};return t.px!=null||t.qty!=null;}
function refreshCard(force){
 var c=window._card;if(!c||window._busy)return;
 // an answer that lands while he types is dropped: the next read redraws
 function ok(){return window._card===c&&!window._busy&&!typing();}
 if(c.kind==='order')get('/math.json?id='+encodeURIComponent(c.id),function(j){if(ok())drawOrder(j);});
 else if(c.kind==='market')get('/book.json?m='+encodeURIComponent(c.m)+'&stake='+stake(),function(j){if(ok())drawMarket(j);});
 else if(c.kind==='scan'){
  // the scan sheet re-reads only while a scan runs: his ticks stay put
  var sj=window._scanJ;if(sj&&sj.state!=='running'&&!force)return;
  get('/scan.json?sort='+LS('ssort','pct'),function(j){if(ok())drawScan(j);});}
}
function btns(html){return window._busy?html.replace(/<button(?! class="close")/g,'<button disabled'):html;}
function drawOrder(j){
 var o=j.order||(((window._d||{}).orders||[]).filter(function(x){return x.id===(window._card||{}).id;})[0]);
 if(!j.ok){
  // the order is still his to cancel when its book cannot be read
  var c0=o?ohead(o)+'<div class="frm"><button class="btn red" onclick="doCancel()">Cancel order</button></div>':'';
  sheet(btns(head('Order')+'<div class="note bad">'+esc(j.note)+'</div>'+c0+said()));return;}
 // what the card showed when he started typing is what a change is
 // checked against: a fill meanwhile is refused, never resized over
 if(!edited()||!window._shown)window._shown={px:o.price,qty:o.size};
 var m=j.math||{},bs=j.basis||0;
 var h=head(esc(j.name))+ohead(o)
  +'<div class="stats"><div><div class="v">'+(m.est==null?'—':usd(m.est))+'</div><div class="l">a day</div></div>'
  +'<div><div class="v">'+(m.est==null||!bs?'—':pct(m.est/bs))+'</div><div class="l">per $ a day</div></div>'
  +'<div><div class="v">'+usd(bs)+'</div><div class="l">money behind it</div></div></div>'
  +(j.stale?'<div class="note warn">'+esc(j.stale)+'</div>':'')
  +'<div class="frm"><input id="px" inputmode="decimal" value="'+esc(val('px',Math.round(o.price*1000)/10))+'" oninput="typed(\'px\')"><span class="u">¢</span>'
  +'<input id="qty" inputmode="decimal" value="'+esc(val('qty',o.size))+'" oninput="typed(\'qty\')">'
  +'<button class="btn" onclick="doMove()">Change</button><button class="btn red" onclick="doCancel()">Cancel order</button></div>'
  +said()
  +'<div class="panel">'+mathHtml(o,m)+'</div>'+bookHtml(j,o);
 sheet(btns(h));
}
function newLine(sd,n){
 var t='<span class="'+(sd==='BUY'?'ok':'bad')+'">'+side(sd)+'</span> ';
 if(!(n&&n.day>0))return '<div class="nl"><span>'+t+'<span class="muted">'+esc((n&&n.why)||'—')+'</span></span></div>';
 return '<div class="nl"><span>'+t+ct(n.px)+' × '+sz(n.qty)+' <span class="muted">('+usd(n.cost)+')</span></span>'
  +'<span><b>'+usd(n.day)+'</b>/day · '+pct(n.pct)+' <button class="link" onclick="useNew(\''+sd+'\')">Use</button></span></div>';
}
function drawMarket(j){
 var back=window._from==='scan'?'<button class="link" onclick="openScan()">‹ Scan results</button>':'';
 if(!j.ok){sheet(head('Market')+back+'<div class="note bad">'+esc(j.note)+'</div>'+said());return;}
 var sd=window._side||'BUY';
 var h=head(esc(j.name))+back
  +'<div class="muted">'+(j.net?(j.net>0?'Yes ':'No ')+sz(Math.abs(j.net))+' · '+usd(j.value)+' · ':'')
  +(j.pool!=null?usd(j.pool)+'/day a side · Target '+sz(j.target)
    :(!j.prog?'No reward program read':(!j.live?'Program not live':'Event size unknown')))
  +(j.first_day?' · first day':'')+'</div>'
  +'<div class="frm" style="margin:8px 0"><button class="btn small '+(j.watched?'gray':'')+'" onclick="doWatch(\''+esc(j.market)+'\','+(!j.watched)+')">'
  +(j.watched?'★ Tracking':'☆ Track')+'</button></div>'
  +(j.stale?'<div class="note warn">'+esc(j.stale)+'</div>':'');
 (j.ours||[]).forEach(function(o){h+='<div class="nl" style="cursor:pointer" onclick="openOrder(\''+esc(o.id)+'\')"><span><span class="'+(o.side==='BUY'?'ok':'bad')+'">'+side(o.side)+'</span> '
  +ct(o.price)+' × '+sz(o.size)+'</span><span><b>'+day(o.est)+'</b> ›</span></div>';});
 h+='<div class="panel"><div class="label">New order at the best price, $'+esc(j.stake)+'</div>'
  +newLine('BUY',(j.new||{}).BUY)+newLine('SELL',(j.new||{}).SELL)+'</div>';
 h+=bookHtml(j,null)
  +'<div class="frm"><div class="seg"><button class="'+(sd==='BUY'?'on':'')+'" onclick="pick(\'BUY\')">Bid</button>'
  +'<button class="'+(sd==='SELL'?'on':'')+'" onclick="pick(\'SELL\')">Ask</button></div></div>'
  +'<div class="frm"><input id="px" inputmode="decimal" placeholder="price" value="'+esc(val('px',''))+'" oninput="typed(\'px\')"><span class="u">¢</span>'
  +'<input id="qty" inputmode="decimal" placeholder="size" value="'+esc(val('qty',''))+'" oninput="typed(\'qty\')">'
  +'<button class="btn" onclick="doPlace()">Place</button></div>'+said();
 window._mk=j;sheet(btns(h));
}
function useNew(sd){var n=((window._mk||{}).new||{})[sd];if(!n||n.px==null)return;
 window._side=sd;window._typed=window._typed||{};
 window._typed.px=''+Math.round(n.px*1000)/10;window._typed.qty=''+n.qty;drawMarket(window._mk);}
function pick(s){window._side=s;if(window._mk)drawMarket(window._mk);}
function nums(){var p=parseFloat(document.getElementById('px').value),q=parseFloat(document.getElementById('qty').value);
 if(!(p>0)||!(q>0)){alert('price and size');return null;}return [p,q];}
function busy(on,note){window._busy=on;
 if(on)window._said={ok:true,note:note};
 var s=document.getElementById('sheet');if(s)s.querySelectorAll('button:not(.close)').forEach(function(b){b.disabled=on;});
 if(on){var n=document.getElementById('said');var t='<div class="note ok">'+esc(note)+'</div>';
  if(n)n.innerHTML=t;else if(s)s.insertAdjacentHTML('beforeend',t);}}
function wait(){if(window._busy){alert('Your last tap has not answered yet — wait for it.');return true;}return false;}
function done(r,mine){var here=!!window._card&&window._card===mine;busy(false);window._typed={};window._shown=null;
 // a card he closed, or left for another, mid-tap still gets its answer
 if(!here){window._said=null;alert(r.note||'');load();return;}
 say(r);}
function doPlace(){if(wait())return;var n=nums();if(!n)return;var c=window._card,sd=window._side||'BUY',j=window._mk||{};
 // what he holds, less what his orders already offer, is what an ask
 // can sell or a bid can buy back without tying up money
 var free=sd==='BUY'?(j.free_short||0):(j.free_long||0);
 var cost=n[1]<=free+1e-9?0:(sd==='BUY'?n[0]/100*n[1]:(1-n[0]/100)*n[1]);
 if(!confirm(side(sd)+' '+n[0]+'¢ × '+n[1]+(cost?' — ties up '+usd(cost):' — from what you hold')+'?'))return;
 busy(true,'Placing…');
 post({op:'place',market:c.m,side:sd,px:n[0],qty:n[1]},function(r){done(r,c);});}
function doMove(){if(wait())return;var n=nums();if(!n)return;var c=window._card,w=window._shown||{},t=window._typed||{};
 // only what he changed is sent, with what the card showed: an order
 // that filled meanwhile is refused, never regrown
 var body={op:'move',order_id:c.id,was_price:Math.round(w.px*100000)/1000,was_size:w.qty};
 if(t.px!=null)body.px=n[0];
 if(t.qty!=null)body.qty=n[1];
 if(body.px==null&&body.qty==null){alert('change the price or the size first');return;}
 if(!confirm('Change to '+(body.px!=null?n[0]:Math.round(w.px*1000)/10)+'¢ × '+(body.qty!=null?n[1]:w.qty)+'?'))return;
 busy(true,'Changing… (up to 15 s)');
 post(body,function(r){
  // the card follows the order to its new id, only if he is still on it
  if(r.ok&&r.id&&window._card===c){c={kind:'order',id:r.id};window._card=c;}
  done(r,c);});}
function doCancel(){if(wait())return;var c=window._card;if(!confirm('Cancel this order?'))return;
 busy(true,'Cancelling…');
 post({op:'cancel',order_id:c.id},function(r){var here=!!window._card&&window._card===c;busy(false);
  if(r.ok||!here){if(here)closeSheet();else window._said=null;load();alert(r.note);}else say(r);});}
function doWatch(m,on){
 post({op:'watch',market:m,on:on},function(r){
  if(!r.ok){alert(r.note||'');return;}
  var sj=window._scanJ;if(sj)(sj.rows||[]).forEach(function(x){if(x.m===m)x.w=!!r.watched;});
  var c=window._card;if(c&&c.kind==='scan'&&sj)drawScan(sj);else refreshCard(true);
  load();});}

// -- the scan ------------------------------------------------------------------------
var SSORTS=[['pct','Best %'],['day','Best $'],['bid_pct','Bid %'],['ask_pct','Ask %'],['bid_day','Bid $'],['ask_day','Ask $']];
function pickG(k,on){window._picks=window._picks||{};window._picks[k]=on?1:0;}
function allG(on){var j=window._scanJ||{};window._picks={};(j.groups||[]).forEach(function(g){window._picks[g.key]=on?1:0;});drawScan(j);}
function setSS(k){LSset('ssort',k);window._revealSS=1;refreshCard(true);}
function scanRow(r){
 function ns(sd,n){var t='<span class="t '+(sd==='BUY'?'bid':'ask')+'">'+side(sd)+'</span>';
  if(!(n&&n.day>0))return '<div class="side">'+t+'<span class="so"><span class="new">'+esc((n&&n.why)||'—')+'</span></span><span></span><span></span></div>';
  return '<div class="side">'+t+'<span class="so"><span>'+ct(n.px)+' × '+sz(n.qty)+'</span></span><span class="d">'+usd(n.day)+'</span><span class="p">'+pct(n.pct)+'</span></div>';}
 return '<div class="mrow" onclick="openMarket(\''+esc(r.m)+'\')"><div class="mtop"><div class="mn">'+esc(r.name)+'</div>'
  +'<button class="sbtn'+(r.w?' on':'')+'" onclick="event.stopPropagation();doWatch(\''+esc(r.m)+'\','+(!r.w)+')" aria-label="track">'+(r.w?'★':'☆')+'</button></div>'
  +'<div class="msub">'+esc(r.kind+(r.st?' · '+r.st:''))+(r.mine?' · yours':'')+(r.pool!=null?' · '+usd(r.pool)+'/day a side':'')+'</div>'
  +ns('BUY',r.bid)+ns('SELL',r.ask)+'</div>';
}
function slugBox(){return '<div class="label" style="margin-top:16px">Or open a market by its slug</div>'
  +'<div class="frm"><input id="slug" style="flex:1" placeholder="market slug" value="'+esc(val('slug',''))+'" oninput="typed(\'slug\')">'
  +'<button class="btn gray" onclick="openMarket(document.getElementById(\'slug\').value)">Open</button></div>';}
function drawScan(j){
 var err='';
 if(j.ok===false){
  err=j.note||'no answer';
  var last=window._scanJ;
  if(!last||last.ok===false){
   window._scanJ=j;
   sheet(head('Scan')+'<div class="note bad">'+esc(err)+'</div>'
    +'<div class="frm"><button class="btn gray" onclick="refreshCard(true)">Try again</button></div>'+slugBox());return;}
  j=last;
 }
 window._scanJ=j;
 if(!window._picks){window._picks={};(j.keys||[]).forEach(function(k){window._picks[k]=1;});}
 var p=window._picks,run=j.state==='running';
 var h=head('Scan for places to earn')
  +'<div class="legend" style="margin:6px 0"><span>Politics programs</span><span><button class="link" onclick="allG(1)">All</button> · <button class="link" onclick="allG(0)">None</button></span></div>'
  +'<div class="panel" style="padding:2px 12px">';
 (j.groups||[]).forEach(function(g){
  h+='<label class="chk"><input type="checkbox" '+(p[g.key]?'checked ':'')+'onchange="pickG(\''+esc(g.key)+'\',this.checked)">'
   +'<span class="cl">'+esc(g.label)+'</span><span class="cm">'+g.n.toLocaleString()+' markets<br>'+usd0(g.pool)+'/day an event</span></label>';});
 if(!(j.groups||[]).length)h+='<div class="muted" style="padding:10px 0">The politics list is still being read. Try again in a few minutes.</div>';
 h+='</div><div class="frm"><span class="label">Stake a side</span> <span class="u" style="margin:0">$</span>'
  +'<input id="stake" inputmode="decimal" value="'+esc(val('stake',stake()))+'" oninput="typed(\'stake\')">'
  +'<button class="btn" onclick="doScan()"'+(run?' disabled':'')+'>'+(run?'Scanning…':'Scan')+'</button></div>';
 if(run){var f=j.total?Math.min(j.done/j.total,1):0;
  h+='<div class="muted">'+esc(j.step||'starting')+(j.total?' · '+(j.done||0).toLocaleString()+' of '+j.total.toLocaleString():'')+'</div>'
   +'<div class="bar"><div style="width:'+Math.round(f*100)+'%"></div></div>';
  // never while he types: a redraw would close the phone's keyboard
  clearTimeout(window._scanT);
  window._scanT=setTimeout(function tick(){
   if(!(window._card&&window._card.kind==='scan'))return;
   if(typing()){window._scanT=setTimeout(tick,2000);return;}
   refreshCard();},2000);}
 if(err)h+='<div class="note bad">'+esc(err)+(run?' — still trying':'')+'</div>';
 if(j.state==='error')h+='<div class="note bad">'+esc(j.note)+'</div>';
 if(j.state==='done'||(j.rows||[]).length){
  var c=j.counts||{},ss=LS('ssort','pct');
  h+='<div class="sec-h" style="margin-top:18px"><h2 style="font-size:19px">'+(run?'Last results':'Results')+'</h2><span class="muted">$'+esc(j.stake)+' a side</span></div>'
   +'<div class="chips" id="ssorts">'+SSORTS.map(function(s){return '<button class="chip'+(ss===s[0]?' on':'')+'" onclick="setSS(\''+s[0]+'\')">'+s[1]+'</button>';}).join('')+'</div>'
   +'<div class="muted">'+(c.markets||0).toLocaleString()+' read · '+(j.of||0).toLocaleString()+' would pay'
   +((j.rows||[]).length<(j.of||0)?' · best '+(j.rows||[]).length+' shown':'')+' · tap ☆ to track</div>'
   +(j.note?'<div class="note warn">'+esc(j.note)+'</div>':'')
   +'<div class="card list" style="margin:8px -6px">'+(j.rows||[]).map(scanRow).join('')+'</div>';
 }
 h+=slugBox();
 sheet(btns(h));
}
function doScan(){
 var p=window._picks||{},keys=Object.keys(p).filter(function(k){return p[k];});
 if(!keys.length){alert('Tick at least one');return;}
 var e=document.getElementById('stake'),s=parseFloat(e?e.value:stake());
 if(!(s>0)){alert('Stake must be a number of dollars');return;}
 LSset('stake',''+s);
 post({op:'scan',groups:keys,stake:s},function(r){
  if(!r.ok)alert(r.note||'');
  // poll until a read says how it stands — even when this answer was lost
  // or a scan was already running; a read that fails keeps polling
  var sj=window._scanJ;if(sj&&sj.ok!==false)sj.state='running';
  refreshCard(true);load();});
}

load();setInterval(load,20000);
setInterval(function(){if(window._card&&!typing())refreshCard();},5000);
"""

PAGE = f"""<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta http-equiv="Cache-Control" content="no-store">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#f2f2f7" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)">
<title>Rewards</title><style>{PAGE_CSS}</style></head><body>
<div class="wrap">
<div class="top"><h1>Rewards</h1></div>
<div id="login" style="display:none" class="card">
 <div class="label">Dashboard key</div>
 <div class="frm"><input id="k" type="password" placeholder="key" style="flex:1"><button class="btn" onclick="saveKey()">Open</button></div>
</div>
<div id="view"><div class="banner">Loading&hellip;</div></div>
</div>
<script>{PAGE_JS}</script>
</body></html>"""


def handle_op(app, body: dict) -> dict:
    op = str(body.get("op") or "")
    if op == "place":
        return app.place(str(body.get("market") or ""), body.get("side"),
                         body.get("px"), body.get("qty"))
    if op == "move":
        return app.move(str(body.get("order_id") or ""), body.get("px"), body.get("qty"),
                        was_price=body.get("was_price"), was_size=body.get("was_size"))
    if op == "cancel":
        return app.cancel(str(body.get("order_id") or ""))
    if op == "watch":
        return app.set_watch(str(body.get("market") or ""), bool(body.get("on")))
    if op == "scan":
        if (g := app._gate()):
            return {"ok": False, "note": g}
        groups = body.get("groups")
        return app.scanner.start(groups if isinstance(groups, list) else [], body.get("stake"))
    return {"ok": False, "note": f"unknown op {op!r}"}


class Handler(BaseHTTPRequestHandler):
    app = None
    password = ""

    def log_message(self, fmt, *args):  # noqa: D401 — quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        gz = len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or "")
        if gz:
            body = gzip.compress(body, 5)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if gz:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _authed(self, qs: str) -> bool:
        return authed(self.headers.get, qs, self.password)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path.startswith("/v3/"):
            path = path[3:]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, b"ok", "text/plain")
        if path in OLD_PAGES:
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return None
        q = parse_qs(u.query)

        def arg(k: str, d: str = "") -> str:
            return (q.get(k) or [d])[0]
        if path in ("/data.json", "/book.json", "/math.json", "/scan.json"):
            if not self._authed(u.query):
                return self._json({"ok": False, "note": "key"}, 401)
            try:
                if path == "/data.json":
                    return self._json(self.app.data(stake=arg("stake") or None))
                if path == "/book.json":
                    return self._json(self.app.book_view(arg("m"), stake=arg("stake") or None))
                if path == "/scan.json":
                    return self._json(self.app.scanner.view(sort=arg("sort", "pct")))
                return self._json(self.app.order_math(arg("id")))
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "note": str(e)[:200]}, 500)
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/")
        if path.startswith("/v3/"):
            path = path[3:]
        if path != "/op":
            return self._send(404, b"not found", "text/plain")
        if not self._authed(u.query):
            return self._json({"ok": False, "note": "key"}, 401)
        if self.headers.get("X-Reprice") != "1":
            return self._json({"ok": False, "note": "missing X-Reprice header"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"ok": False, "note": "bad body"}, 400)
        try:
            return self._json(handle_op(self.app, body))
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "note": str(e)[:200]}, 500)


def serve(app, port: int | None = None, bind: str = "0.0.0.0"):
    port = int(port if port is not None else (os.environ.get("PORT") or 8080))
    handler = type("LiteHandler", (Handler,), {"app": app,
                                               "password": os.environ.get("DASH_PASSWORD", "")})
    srv = ThreadingHTTPServer((bind, port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    return srv
