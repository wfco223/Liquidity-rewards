"""3.0's pages — a few small ones, each answering one question, in 1.0's
voice: summary before detail, plain English on every number, never a bare
slug where a name will fit.

    /          am I earning, is the data fresh, what is armed?
    /orders    every resting order: its name, its verdict, move/cancel
    /plan      what would 3.0 do next, and why?
    /switch    the switches (master + per family) and the risk line
    /log       what happened recently, in words

Served on localhost; 1.0's monitor is the container's front door and
forwards /v3/* here, so the browser's stored dashKey just works. Pages
are public SHELLS holding no data; data.json underneath demands the key.
The only mutating route is /op — auth plus the X-Reprice CSRF header.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8092
DEFAULT_BIND = "127.0.0.1"

# The live card (owner, 2026-08-25): an open card streams its market's
# book read fresh from the exchange every tick. One thread per open
# stream; the cap bounds both threads and API traffic. After LIVE_MAX_S
# the stream ends and the page quietly reconnects, so a forgotten tab
# never holds a line forever.
LIVE_TICK_S = 1.0
LIVE_MAX_S = 900.0
LIVE_MAX_STREAMS = 3
BONDS_MAX_STREAMS = 2       # bonds pages open at once with a live line


def authed(get_header, query_string: str, password: str) -> bool:
    """Same three ways in as 1.0: X-Dash-Key header (localStorage),
    ?key= (widgets/Shortcuts), legacy Basic. No password set = locked."""
    if not password:
        return False
    if get_header("X-Dash-Key") == password:
        return True
    if (parse_qs(query_string).get("key") or [""])[0] == password:
        return True
    header = get_header("Authorization") or ""
    if header.startswith("Basic "):
        try:
            return base64.b64decode(header[6:]).decode().split(":", 1)[1] == password
        except Exception:  # noqa: BLE001
            return False
    return False


# Owner, 2026-08-31: plan and model run in the background — their
# routes still answer for a bookmark, but they are off the bar. Fills
# and watch became sub-pages of quick look.
NAV = (("quick look", "."), ("status", "status"), ("orders", "orders"),
       ("pay", "pay"), ("bonds", "bonds"), ("survey", "survey"),
       ("log", "log"), ("switch", "switch"))
SUBNAV = {"quick": (("meter", "."), ("fills", "fills"), ("watch", "watch")),
          "orders": (("orders", "orders"),)}

_CSS = """
 body{background:#151b12;color:#e8ecdf;font:16px/1.45 -apple-system,system-ui,sans-serif;
      margin:0;padding:14px;max-width:680px;margin:auto}
 h1{font-size:19px;margin:4px 0 6px} .muted{color:#93a08a;font-size:13px}
 .big{font-size:34px;font-weight:700;margin:2px 0}
 .ok{color:#7fd77f}.bad{color:#ff8a7a}.warn{color:#ffd06b}
 table{border-collapse:collapse;width:100%;font-size:13px;margin:6px 0}
 td,th{padding:4px 6px;border-bottom:1px solid #2c3527;text-align:left;
       vertical-align:top}
 td.r,th.r{text-align:right} code{color:#b9d98f;font-size:12px;word-break:break-all}
 .card{background:#1f2818;border-radius:10px;padding:10px 12px;margin:10px 0;
       overflow-x:auto}
 input,button{font-size:16px;padding:8px;border-radius:8px}
 input{background:#12180d;color:#e8ecdf;border:1px solid #3c4a2f;width:60%}
 button{background:#4c7a2f;color:#fff;border:0;margin-left:6px}
 button.off{background:#7a3a2f}
 button.small{font-size:13px;padding:4px 9px;margin:0 0 0 6px}
 .nav{margin:2px 0 6px}
 .subnav{margin:0 0 10px;padding-bottom:6px;border-bottom:1px solid #2c3527}
 .subnav a,.subnav span{margin-right:14px;font-size:13.5px}
 .subnav a{color:#93a08a;text-decoration:none}
 .subnav .here{color:#b9d98f;font-weight:700}
 .tabs{display:flex;gap:6px;margin:8px 0 6px;flex-wrap:wrap}
 .tabs button{font-size:13px;padding:5px 12px;margin:0;background:#2c3527;color:#cfd8c2}
 .tabs button.on{background:#4c7a2f;color:#fff;font-weight:700}
 .hero{font-size:40px;font-weight:700;line-height:1.05;margin:2px 0}
 .hero .u{font-size:15px;font-weight:400;color:#93a08a}
 .kpi{display:flex;gap:18px;flex-wrap:wrap;margin:6px 0 2px}
 .kpi .v{font-size:26px;font-weight:700;line-height:1.1}
 .kpi .l{color:#93a08a;font-size:11px;text-transform:uppercase;letter-spacing:.4px}
 .orow{border-top:1px solid #2c3527;padding:9px 0 7px}
 .orow .px{font-size:22px;font-weight:700}
 .orow .rt{font-size:18px;font-weight:700;color:#9ec49a}
 .nav a,.nav span{margin-right:12px;font-size:15px}
 .nav a{color:#b9d98f;text-decoration:none}
 .nav .here{color:#e8ecdf;font-weight:700}
 .pill{display:inline-block;background:#2c3527;border-radius:6px;padding:1px 7px;
       font-size:12px;margin:1px 3px 1px 0;color:#cfd8c2}
 .pill.on{background:#4c7a2f;color:#fff}
 .stats{display:flex;gap:20px;flex-wrap:wrap;margin:8px 0 2px}
 .stat .lab{color:#93a08a;font-size:12px}
 .stat .val{font-size:22px;font-weight:700}
 .stat .val .u{font-size:13px;font-weight:400;color:#93a08a}
 .sub{color:#b6c1a8;font-size:14px;margin:3px 0}
 .hint{color:#93a08a;font-size:12.5px;margin:5px 0 2px;line-height:1.5}
 details.how{margin:6px 0 0;font-size:12.5px;color:#93a08a;line-height:1.5}
 details.how summary{color:#79856d;font-size:12px;cursor:pointer}
 summary{cursor:pointer}
 .vrd{color:#93a08a;font-size:12px;margin:1px 0 4px}
 .name{font-size:13.5px}
 .mtrack{height:10px;background:#2c3d20;border-radius:5px;margin:8px 0 2px;
         position:relative;overflow:hidden}
 .tri{display:flex;gap:8px;margin:8px 0 2px}
 .tri-col{flex:1;min-width:0}
 .tri-h{font-size:11px;color:#79856d;margin:0 0 4px;text-transform:uppercase;
        letter-spacing:.4px}
 .tchip{border-radius:8px;padding:5px 8px;margin:4px 0;font-size:12px;
        background:#1a2214;border-left:3px solid #55482a;overflow:hidden}
 .tchip.win{border-left-color:#4c7a2f;background:#1c2a16}
 .tchip .tn{color:#e8ecdf;font-size:12px}
 .tchip .tm{color:#93a08a;font-size:11px}
 @keyframes triL{from{transform:translateX(70%);opacity:0}
                 to{transform:translateX(0);opacity:1}}
 @keyframes triR{from{transform:translateX(-70%);opacity:0}
                 to{transform:translateX(0);opacity:1}}
 .tchip.new-l{animation:triL .7s ease-out}
 .tchip.new-r{animation:triR .7s ease-out}
 .mfill{position:absolute;left:0;top:0;bottom:0;background:#4c7a2f;
        border-radius:5px}
 @keyframes lvprog{0%{left:0}50%{left:75%}100%{left:0}}
 .lvp{width:25%;animation:lvprog 1.4s ease-in-out infinite}
 .lvlive{position:absolute;top:7px;right:12px;font-size:11px;
         font-weight:700;color:#7fd77f;z-index:2}
 .lvrow{padding:6px;border-radius:6px}
 .lvrow.sel{background:#2c3d20;outline:1px solid #4c7a2f}
 .lvdot{color:#9ec49a;font-weight:700}
"""

_PLUMBING = """
function hdrs(){const h=new Headers();h.set('X-Dash-Key',localStorage.getItem('dashKey')||'');return h;}
function saveKey(){localStorage.setItem('dashKey',document.getElementById('k').value);load();}
function usd(x){var v=x||0;return (v<0?'\\u2212$':'$')+Math.abs(v).toFixed(2);}
function pc(x){var v=Math.round((x||0)*1000)/10;return (v%1?v.toFixed(1):''+Math.round(v))+'\\u00A2';}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function when(t){return new Date(t*1000).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});}
function row(c){return '<tr>'+c.map(function(x,i){return '<td class="'+(i?'r':'')+'">'+x+'</td>';}).join('')+'</tr>';}
function nm(d,s){return esc((d.labels&&d.labels[s])||s);}
function fams(d){var out=[];for(var k in (d.summaries||{})){out.push([k,d.summaries[k]]);}return out;}
function post(body,cb){
 var h=hdrs();h.set('X-Reprice','1');h.set('Content-Type','application/json');
 fetch('/op',{method:'POST',headers:h,body:JSON.stringify(body)})
  .then(function(r){return r.json();}).then(function(j){if(cb)cb(j);load();})
  .catch(function(){alert('unreachable');});
}
function bootCard(d){
 var b=(d.boot||{});
 return '<div class="card"><b>starting up</b>'
  +'<div class="sub">'+esc(b.stage||'reading the board')+'</div>'
  +'<div class="mtrack"><div class="mfill" style="width:'+(b.pct||5)+'%"></div></div>'
  +'<div class="muted">The first pass after a restart reads the board before the pages fill in. This refreshes itself.</div></div>';
}
function fmtsz(q){if(q>=1e6)return (q/1e6).toFixed(1)+'M';if(q>=1e3)return (q/1e3).toFixed(1)+'k';return ''+Math.round(q);}
function lvKey(){return encodeURIComponent(localStorage.getItem('dashKey')||'');}
function lvBox(m,box,onclose){
 // one live line at a time, shared by the purchase cards and the
 // orders/plan book folds; reuses a lingering connection to the same
 // market so coming back within 10s is instant
 if(window._lvLingerT){clearTimeout(window._lvLingerT);window._lvLingerT=null;}
 if(window._lv&&window._lv.m===m&&window._lvB){
  window._lv.box=box;
  if(onclose)window._lv.onclose=onclose;
  window._liveOpen=true;window._lvSel=null;
  box.innerHTML='<div class="lvlivepart"></div><div class="lvladder"></div>';
  lvDrawBook(window._lvB);
  return;
 }
 lvShut();
 window._liveOpen=true;window._lvSel=null;window._lvB=null;
 box.innerHTML='<div class="lvlivepart"><div class="muted">opening the live line\\u2026</div><div class="mtrack"><div class="mfill lvp"></div></div></div><div class="lvladder"></div>';
 var es=new EventSource('/live?m='+encodeURIComponent(m)+'&key='+lvKey());
 window._lv={es:es,box:box,m:m,onclose:onclose};
 es.onmessage=function(ev){
  var b;try{b=JSON.parse(ev.data);}catch(e){return;}
  window._lvB=b;
  if(!window._lvLingerT)lvDrawBook(b);
 };
 es.onerror=function(){
  if(!window._lvB&&window._liveOpen&&window._lv){
   var t=window._lv.box.querySelector('.lvlivepart');
   if(t)t.innerHTML='<div class="muted">line dropped \\u2014 reconnecting\\u2026</div><div class="mtrack"><div class="mfill lvp"></div></div>';
  }
 };
}
function lvShut(linger){
 if(linger&&window._lv){
  if(window._lvLingerT)clearTimeout(window._lvLingerT);
  window._lvLingerT=setTimeout(function(){lvShut();},10000);
  return;
 }
 if(window._lvLingerT){clearTimeout(window._lvLingerT);window._lvLingerT=null;}
 if(window._lv){
  try{window._lv.es.close();}catch(e){}
  try{if(window._lv.onclose)window._lv.onclose();}catch(e){}
  window._lv=null;
 }
 window._liveOpen=false;window._lvSel=null;window._lvB=null;window._lvAfterOp=null;
}
function lvDrawBook(b){
 if(!window._lv||!window._liveOpen)return;
 var box=window._lv.box;
 var t=box.querySelector('.lvlivepart')||box;
 if(!b.ok){t.innerHTML='<div class="bad">'+esc(b.note||'no book')+'</div>';return;}
 var h='<div style="font-size:15px"><b>'+esc(b.name||b.market)+'</b> <span class="ok" style="font-size:12px">\\u25CF LIVE</span></div>'
  +'<div class="muted" style="font-size:12px">read straight from the exchange, updating every second</div>';
 if(b.pool_day!=null)h+='<div class="muted" style="font-size:12px">each side here competes for <b>'+usd(b.pool_day)+'/day</b> of rewards</div>';
 else if(b.prog_note)h+='<div class="muted" style="font-size:12px">'+esc(b.prog_note)+'</div>';
 var bid=(b.bids&&b.bids[0])?b.bids[0][0]:null;
 if(b.position&&b.position.qty>0.005){
  var av=(b.position.cost/b.position.qty)*100;
  h+='<div class="sub" style="margin:5px 0 2px">Position: <b>'+b.position.qty+' shares</b> at '+av.toFixed(1)+'c average</div>';
  if(bid!=null){
   h+='<div style="margin:4px 0"><button class="small off" onclick="event.stopPropagation();lvCloseOut()">Close out \\u2014 sell at the bid ('+pc(bid)+')</button></div>';
  }
 }
 var oursAt={};(b.ours||[]).forEach(function(o){oursAt[o.side+(o.price*100).toFixed(1)]=1;});
 h+='<div><table><tr><th class="r">bid size</th><th class="r">bid</th><th>ask</th><th>ask size</th></tr>';
 var nr=Math.max((b.bids||[]).length,(b.asks||[]).length);
 for(var i=0;i<nr;i++){
  var bd=(b.bids||[])[i],ak=(b.asks||[])[i];
  var bm=bd&&oursAt['BUY'+(bd[0]*100).toFixed(1)]?' <span class="lvdot">\\u25CF</span>':'';
  var am=ak&&oursAt['SELL'+(ak[0]*100).toFixed(1)]?' <span class="lvdot">\\u25CF</span>':'';
  h+='<tr><td class="r">'+(bd?fmtsz(bd[1]):'')+'</td><td class="r">'+(bd?pc(bd[0])+bm:'')+'</td>'
    +'<td>'+(ak?pc(ak[0])+am:'')+'</td><td>'+(ak?fmtsz(ak[1]):'')+'</td></tr>';
 }
 h+='</table></div>';
 var sel=window._lvSel;
 if(sel&&!(b.ours||[]).some(function(o){return o.id===sel;}))sel=window._lvSel=null;
 if((b.ours||[]).length){
  var tot=0;(b.ours||[]).forEach(function(o){tot+=(o.est||0);});
  h+='<div class="muted" style="font-size:12px;margin-top:4px"><b>Your orders here</b>'
   +(b.pool_day!=null?' \\u2014 earning ~'+usd(tot)+'/day together':'')
   +' \\u2014 tap one to move or cancel it</div>';
  (b.ours||[]).forEach(function(o){
   var on=o.id===sel;
   var math='';
   if(o.qualifies===false){math='its side is under Target Size \\u2014 the whole side pays nobody \\u2192 $0.00/day';}
   else if(o.share!=null&&b.pool_day!=null){math=(o.share*100).toFixed(1)+'% of its side\\u2019s score \\u00d7 '+usd(b.pool_day)+'/day pool = <b>'+usd(o.est||0)+'/day</b>';}
   else if(o.share!=null){math=(o.share*100).toFixed(1)+'% of its side\\u2019s score \\u2014 no dollar figure until the pool share is confirmed';}
   h+='<div class="lvrow'+(on?' sel':'')+'" onclick="event.stopPropagation();lvSel(\\''+esc(o.id)+'\\')">'
    +'<span class="lvdot">\\u25CF</span> '+(o.side==='BUY'?'bid':'ask')+' '+o.qty+' @ '+pc(o.price)
    +' <span class="pill">'+esc(o.purpose)+'</span>'
    +(o.pinned?' <span class="pill on">hand-set</span>':'')
    +(math?'<div class="muted" style="font-size:12px;margin:1px 0 0 18px">'+math+'</div>':'')
    +'</div>';
   if(on){
    var tk=b.tick||0.01;
    var dn=Math.round((o.price-tk)*1000)/1000,up=Math.round((o.price+tk)*1000)/1000;
    h+='<div style="margin:2px 0 6px" onclick="event.stopPropagation()">';
    if(dn>0.0005)h+='<button class="small" onclick="lvMove(\\''+esc(o.id)+'\\','+dn+')">move to '+pc(dn)+'</button>';
    if(up<0.9995)h+='<button class="small" onclick="lvMove(\\''+esc(o.id)+'\\','+up+')">move to '+pc(up)+'</button>';
    h+='<button class="small" onclick="lvType(\\''+esc(o.id)+'\\','+o.price+')">type a price</button>'
     +'<button class="small" onclick="lvSize(\\''+esc(o.id)+'\\','+o.qty+','+o.price+',\\''+o.side+'\\')">change size</button>'
     +'<button class="small off" onclick="lvCancelOrder(\\''+esc(o.id)+'\\')">cancel</button></div>';
   }
  });
 }else{h+='<div class="muted" style="font-size:12px;margin-top:4px">none of your orders rest here right now</div>';}
 h+='<div class="hint">The math is the exchange\\u2019s own: your share of the side\\u2019s score \\u00d7 the side\\u2019s daily pool, refigured every second as the book moves. Claims tend to run high \\u2014 the meter page\\u2019s audited rate is the ground truth for what actually pays. A move you make here is HAND-SET: the engine leaves it alone until the book turns against it (its earning rate falls under half of what it was when you set it) \\u2014 the quick-guard process still watches it.</div>';
 t.innerHTML=h;
}
function lvSel(id){window._lvSel=(window._lvSel===id?null:id);if(window._lvB)lvDrawBook(window._lvB);}
function lvMove(id,px){
 if(!confirm('Move this order to '+pc(px)+'? Your price then holds until the book turns against it.'))return;
 post({op:'move',order_id:id,price:px,pin:1},function(j){if(!j.ok)alert(j.note||'refused');window._lvSel=null;if(window._lvAfterOp)window._lvAfterOp();});
}
function lvType(id,cur){
 var v=prompt('New price in cents (e.g. 3.4):',(cur*100).toFixed(1));
 if(v==null)return;var p=parseFloat(v)/100;
 if(!(p>0&&p<1)){alert('price must be between 0.1c and 99.9c');return;}
 lvMove(id,Math.round(p*1000)/1000);
}
function lvSize(id,cur,px,side){
 var v=prompt('New size in shares (now '+cur+'):',''+cur);
 if(v==null)return;var q=parseFloat(v);
 if(!(q>0)){alert('need a positive number of shares');return;}
 q=Math.round(q*100)/100;
 if(Math.abs(q-cur)<0.005)return;
 var msg='Resize this order from '+cur+' to '+q+' shares at '+pc(px)+'?';
 var d=Math.round((q-cur)*100)/100;
 if(d>0)msg+=side==='BUY'
  ? ' The extra '+d+' shares put about '+usd(d*px)+' more at risk.'
  : ' That offers '+d+' more of your shares for sale.';
 if(!confirm(msg))return;
 post({op:'move',order_id:id,price:px,qty:q,pin:1},function(j){if(!j.ok)alert(j.note||'refused');window._lvSel=null;if(window._lvAfterOp)window._lvAfterOp();});
}
function lvCancelOrder(id){
 if(!confirm('Cancel this order?'))return;
 post({op:'cancel',order_id:id},function(j){if(!j.ok)alert(j.note||'refused');window._lvSel=null;if(window._lvAfterOp)window._lvAfterOp();});
}
function lvCloseOut(){
 var b=window._lvB;if(!b||!window._lv)return;
 var bid=(b.bids&&b.bids[0])?b.bids[0][0]:null;
 var q=b.position?b.position.qty:0;
 if(bid==null||!(q>0))return;
 if(!confirm('Sell up to '+q+' shares at '+pc(bid)+' \\u2014 about '+usd(q*bid)+'. The resting exit is cancelled first; whatever the bid cannot take rests at '+pc(bid)+' as your own ask. Sure?'))return;
 post({op:'close_position',market:window._lv.m},function(j){alert(j.note||(j.ok?'done':'refused'));if(window._lvAfterOp)window._lvAfterOp();});
}
function showbook(slug,el){
 var box=document.getElementById(el);
 if(!box)return;
 if(box.innerHTML){box.innerHTML='';lvShut(true);return;}
 window._lvAfterOp=null;
 lvBox(slug,box,function(){try{box.innerHTML='';}catch(e){}});
 fetch('/book.json?m='+encodeURIComponent(slug),{headers:hdrs(),cache:'no-store'})
  .then(function(r){return r.json();}).then(function(b){
   var ladbox=box.querySelector('.lvladder');
   if(!ladbox)return;
   if(!b.ok){ladbox.innerHTML='<div class="muted">'+esc(b.note||'no book')+'</div>';return;}
   var g=b.fair!=null?'model '+(b.fair*100).toFixed(1)+'c':(b.band&&b.band.med!=null?'no model \u2014 evidence '+b.band.lo.toFixed(0)+'\u2013'+b.band.hi.toFixed(0)+'c, confidence '+Math.round((b.conf||0)*100)+'%':'NO GROUNDING \u2014 no model, no evidence');
   var h='<div class="muted" style="margin:6px 0 2px">'+g+' \u00b7 the planner\u2019s ladder below is the engine\u2019s last read, not live</div>';
   var lad=b.ladder||{};
   if(lad.ok&&lad.sides){
    if(lad.note)h+='<div class="muted">'+esc(lad.note)+'</div>';
    ['BUY','SELL'].forEach(function(side){
     var s=lad.sides[side]||{};var rows=s.rows||[];
     if(!rows.length)return;
     h+='<div class="muted" style="margin-top:8px"><b>'+(side==='BUY'?'bid':'ask')+' ladder</b> \u2014 what resting at each price would do</div>';
     h+='<table><tr><th class="r">price</th><th class="r">size</th><th class="r">share</th><th class="r">$/day</th><th class="r">fill odds</th><th class="r">fill cost</th><th class="r">EV/day</th></tr>';
     rows.forEach(function(r){
      var st=r.picked?' style="font-weight:bold"':(r.clears_bar?'':' class="muted"');
      h+='<tr'+st+'><td class="r">'+pc(r.px)+(r.picked?' \u25C0':'')+'</td><td class="r">'+r.qty+'</td><td class="r">'+Math.round(r.share*100)+'%</td>'
        +'<td class="r">'+usd(r.est)+'</td><td class="r">'+Math.round(r.p_fill*100)+'%</td>'
        +'<td class="r">'+(r.fill_cost*100).toFixed(1)+'c</td><td class="r">'+usd(r.ev)+'</td></tr>';
     });
     h+='</table>';
    });
    h+='<div class="hint">\u25C0 is the planner\u2019s pick; dim rows pay under the '+((lad.bar||0.75)*100).toFixed(0)+'c bar. Fill odds are per day; fill cost is per share.</div>';
   }else if(lad.note){h+='<div class="muted">ladder: '+esc(lad.note)+'</div>';}
   ladbox.innerHTML=h;
  }).catch(function(){});
}
function load(){
 fetch('/data.json',{headers:hdrs(),cache:'no-store'}).then(function(r){
  if(r.status===401){document.getElementById('login').style.display='block';
    document.getElementById('view').innerHTML='';return null;}
  return r.json();
 }).then(function(d){
  if(!d)return;
  window._d=d;
  document.getElementById('login').style.display='none';
  // reading protection (owner, 2026-08-22): while you are scrolled into
  // the page, refreshes HOLD — the data keeps arriving, but the page
  // only redraws when you are back near the top, so lists stay put and
  // your place is never lost
  // a live card holds the page still the same way scrolling does —
  // redrawing the list would tear down its open stream mid-look
  if(window._loaded&&window._liveOpen){window._held=true;return;}
  if(window._loaded&&(window.scrollY||0)>120){
   window._held=true;
   var hb=document.getElementById('heldnote');
   if(!hb){hb=document.createElement('div');hb.id='heldnote';
    hb.style.cssText='position:fixed;bottom:10px;right:10px;background:rgba(0,0,0,0.55);color:#cfe3cf;padding:4px 10px;border-radius:8px;font-size:11px;z-index:9';
    hb.textContent='refresh held while you read \\u2014 scroll up to update';
    document.body.appendChild(hb);}
   return;
  }
  var hb2=document.getElementById('heldnote');if(hb2)hb2.remove();
  window._held=false;
  var y=window.scrollY||0;
  document.getElementById('view').innerHTML=render(d);
  if(y>0)window.scrollTo(0,y);
  window._loaded=true;
 }).catch(function(){if(!window._held)document.getElementById('view').innerHTML='<div class="card bad">unreachable</div>';});
}
load();setInterval(load,30000);
if(window.addEventListener)window.addEventListener('scroll',function(){
 if(window._held&&!window._liveOpen&&(window.scrollY||0)<=120&&window._d){
  window._held=false;
  var hb=document.getElementById('heldnote');if(hb)hb.remove();
  document.getElementById('view').innerHTML=render(window._d);
 }
});
"""


def _shell(title: str, here: str, render_js: str, sub: str = "") -> str:
    nav = "".join(
        (f'<span class="here">{label}</span>' if label == here
         else f'<a href="{href}">{label}</a>')
        for label, href in NAV)
    subs = SUBNAV.get(sub or "", ())
    subrow = ""
    if subs:
        subrow = '<div class="subnav">' + "".join(
            (f'<span class="here">{label}</span>' if label == here
             else f'<a href="{href}">{label}</a>')
            for label, href in subs) + "</div>"
    return f"""<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store">
<title>{title}</title><style>{_CSS}</style></head><body>
<h1>{title}</h1><div class="nav">{nav}</div>{subrow}
<div id="login" style="display:none" class="card">
 <div class="sub">This page needs the dashboard key.</div>
 <input id="k" type="password" placeholder="key"><button onclick="saveKey()">Open</button>
</div>
<div id="view" class="muted">loading&hellip;</div>
<script>{render_js}{_PLUMBING}</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

STATUS_JS = """
function bar(pct,col){return '<div class="mtrack"><div class="mfill" style="width:'+Math.min(100,Math.max(0,pct))+'%'+(col?';background:'+col:'')+'"></div></div>';}
function render(d){
 if(d.starting)return bootCard(d);
 var out='';
 var age=Math.max(0,Math.round(Date.now()/1000-(d.saved_at||0)));
 var bt=(d.boot||{});var btage=(Date.now()/1000)-(bt.ts||0);
 if(age>=180&&bt.pct!=null&&bt.pct<100&&btage<900){
  out+='<div class="card"><span class="warn">starting up \u2014 '+esc(bt.stage||'')+'</span>'+bar(bt.pct)+'</div>';
 }else{
  out+='<div class="card">'+(age<180
   ?'<span class="ok">\u2705 fresh</span> <span class="muted">'+age+'s</span>'
   :'<span class="bad">\u274C stale</span> <span class="muted">'+Math.round(age/60)+' min</span>');
  var sv=(d.switch_view||{});var m=(sv.master||{});
  out+='<div style="margin-top:6px"><span class="pill'+(m.on?' on':'')+'">master '+(m.on?'ON':'off')+'</span>';
  for(var k in sv){if(k==='master')continue;
   out+='<span class="pill'+(sv[k].on?' on':'')+'">'+esc(k)+' '+(sv[k].on?'ON':'off')+'</span>';}
  out+='</div>';
  var fz=(d.flatten||{});
  if(fz.active&&fz.phase!=='rebuild'){out+='<div class="warn">FLATTEN \u2014 '+(fz.cancelled_total||0)+' cancelled, '+(fz.remaining||0)+' to go</div>';}
  out+='</div>';
 }
 fams(d).forEach(function(kv){
  var k=kv[0],s2=kv[1];
  if(s2.error){out+='<div class="card"><b>'+esc(s2.name||k)+'</b><div class="bad">'+esc(s2.error)+'</div></div>';return;}
  var cap=s2.capital_usd||0, spent=s2.spent||0;
  var w=s2.worth||{};
  var tri=s2.triage||{};
  out+='<div class="card"><b>'+esc(s2.name||k)+'</b> <span class="pill">'+esc(s2.mode)+'</span>'
   +'<div class="kpi">'
   +'<div><div class="v">'+usd(s2.earned_today||0)+'</div><div class="l">earned today</div></div>'
   +'<div><div class="v">'+usd(s2.est_day||0)+'</div><div class="l">rate $/day</div></div>'
   +'<div><div class="v">'+(s2.orders||[]).length+'</div><div class="l">orders</div></div>'
   +'</div>'
   +'<div class="l" style="margin-top:6px">budget '+usd(spent)+' / '+usd(cap)+'</div>'
   +bar(cap?100*spent/cap:0)
   +'<div class="l" style="margin-top:8px">worth the budget \u2014 '+(w.pct||0)+'% of '+(w.scored||0)+' scored'
   +(w.cycle_n?' \u00b7 this cycle '+(w.cycle_pct||0)+'% of '+w.cycle_n:'')+'</div>'
   +bar(w.pct||0,'#6fa8dc')
   +'<div class="l" style="margin-top:8px">board covered '+(tri.done||0)+' / '+(tri.total||0)+'</div>'
   +bar(tri.total?100*(tri.done||0)/tri.total:0,'#8a7a2f')
   +'</div>';
 });
 return out;
}
"""

ORDERS_JS = """
function fold(title,sub,body,open){
 return '<details'+(open?' open':'')+'><summary><b>'+title+'</b> <span class="muted">'+sub+'</span></summary>'+body+'</details>';
}
function odrop(o){
 var cur=(o.live_est!=null?o.live_est:o.est_day)||0;
 var pk=o.est_peak8||0;
 if(pk<0.02)return null;
 return Math.max(0,(pk-cur)/pk);
}
function estAge(d){var a=(Date.now()/1000)-(d.now||0);return (a>0&&a<86400)?a:null;}
function perday(x){
 // a repricing's benefit is usually small — cents read better than
 // $0.03 (owner, 2026-08-31: "increased earning from exits by x cents")
 var v=x||0, s=v<0?'−':'+';
 return Math.abs(v)<1?s+(Math.abs(v)*100).toFixed(1)+'¢/day'
                     :s+'$'+Math.abs(v).toFixed(2)+'/day';
}
function dur(sec){
 if(sec==null||!(sec>0))return null;
 if(sec<3600)return Math.round(sec/60)+'m';
 if(sec<86400)return (sec/3600).toFixed(sec<7200?1:0)+'h';
 return (sec/86400).toFixed(sec<172800?1:0)+'d';
}
function oTab(t){window._oTab=t;if(window._d)document.getElementById('view').innerHTML=render(window._d);}
function orow(d,o){
 var e=(o.live_est!=null?o.live_est:o.est_day);
 var now=(d.now||0), age=o.placed_ts?now-o.placed_ts:null;
 var pf=o.live_pf, exp=null;
 if(pf!=null&&pf>0&&pf<1)exp=86400/(-Math.log(1-pf)); else if(pf!=null&&pf>=1)exp=3600;
 var beaten=(exp!=null&&age!=null&&age>exp);
 var facts=[];
 if(age!=null)facts.push('rested '+dur(age));
 if(exp!=null)facts.push('fill ~'+dur(exp)+(beaten?' \u00b7 outliving':''));
 var dp=odrop(o);
 return '<div class="orow">'
  +'<div style="display:flex;gap:16px;align-items:baseline;margin:3px 0">'
  +'<span class="px">'+(o.side==='BUY'?'bid':'ask')+' '+pc(o.price)+'</span>'
  +'<span class="rt">'+(e==null?'\u2014':usd(e)+'/d')+'</span>'
  +'<span class="muted">'+(o.qty||0)+' sh</span>'
  +'<span class="pill">'+esc(o.purpose)+'</span></div>'
  +(facts.length?'<div class="vrd'+(beaten?' warn':'')+'">'+facts.join(' \u00b7 ')+'</div>':'')
  +(dp!=null&&dp>0.05?'<div class="vrd'+(dp>=0.5?' warn':'')+'">\u25BC '+Math.round(dp*100)+'% off peak</div>':'')
  +'<div><button class="small" onclick="mv(\\''+esc(o.id)+'\\','+o.price+')">Move</button>'
  +'<button class="small off" onclick="cx(\\''+esc(o.id)+'\\')">Cancel</button></div>'
  +'</div>';
}
function oest(o){return (o.live_est!=null?o.live_est:o.est_day)||0;}
function mgroups(os){
 // one row per MARKET, both sides added together (owner, 2026-08-31:
 // "the orders daily estimate is only considering one side of the book
 // and not adding them together"). A market's bid and ask each earn
 // from their own side's pool, so the market's total is their sum.
 var by={},seq=[];
 os.forEach(function(o){
  var m=o.market;
  if(!by[m]){by[m]={m:m,os:[],est:0,peak:0,qty:0};seq.push(m);}
  var g=by[m];g.os.push(o);g.est+=oest(o);g.peak+=(o.est_peak8||0);
  g.qty+=(o.qty||0);
 });
 return seq.map(function(m){return by[m];});
}
function gdrop(g){
 if(!(g.peak>0.02))return null;
 return Math.max(0,(g.peak-g.est)/g.peak);
}
function oqual(o){
 // a QUALIFIER is a wall order: it exists only to lift its side over
 // Target Size and rests far from the touch, so it earns nothing and
 // is not a decision. The button's own orders say so in their why; a
 // wall built by hand is a deep ask up at 98-99c.
 if(String(o.why||'').indexOf('qualify-ask wall')>=0)return true;
 return o.side==='SELL'&&(o.price||0)>=0.98;
}
function oshow(o){
 // owner, 2026-08-31: "Hide the qualifiers so long as they are not
 // earning." One that IS earning is a real order again, so it shows.
 return !(oqual(o)&&oest(o)<0.005);
}
function mrow(d,g){
 var bid='bk_'+esc(g.m);
 var dp=gdrop(g);
 var sides={};g.os.forEach(function(o){sides[o.side==='BUY'?'bid':'ask']=1;});
 var sl=[];for(var s in sides)sl.push(s);
 var shown=g.os.filter(oshow);
 var hid=g.os.length-shown.length;
 var body='';shown.forEach(function(o){body+=orow(d,o);});
 if(!shown.length)body+='<div class="muted" style="font-size:12px">nothing here but wall orders</div>';
 if(hid)body+='<div class="muted" style="font-size:12px">'+hid+' qualifying order'
  +(hid===1?'':'s')+' holding the side over Target Size, earning nothing \u2014 hidden</div>';
 body+='<div><button class="small" onclick="showbook(\\''+esc(g.m)+'\\',\\''+bid+'\\')">Book</button></div>'
  +'<div id="'+bid+'"></div>';
 return '<details class="orow"><summary>'
  +'<span class="name" style="font-size:15px">'+nm(d,g.m)+'</span>'
  +'<div style="display:flex;gap:16px;align-items:baseline;margin:3px 0">'
  +'<span class="rt" style="font-size:20px">'+usd(g.est)+'/d</span>'
  +'<span class="muted">'+shown.length+' order'+(shown.length===1?'':'s')
  +(hid?' +'+hid+' wall':'')+' \u00b7 '+sl.join('+')+'</span></div>'
  +(dp!=null&&dp>0.05?'<div class="vrd'+(dp>=0.5?' warn':'')+'">\u25BC '+Math.round(dp*100)+'% off peak</div>':'')
  +'</summary>'
  +'<div style="margin-left:10px;border-left:2px solid rgba(255,255,255,0.08);padding-left:8px">'+body+'</div>'
  +'</details>';
}
function cancelBanner(d){
 // a pending scheduled cancel is money-affecting and time-boxed, so it
 // is stated at the top of the orders page with a way to call it off
 var cj=d.cancel_jobs||[];
 var out='';
 if(!cj.length){
  return '<div class="card"><button class="small off" onclick="setCancel()">'
   +'Schedule a cancel</button>'
   +'<div class="muted" style="font-size:12px">Cancels every resting order in the markets you name, once, at the hour you set. Nothing else is touched.</div></div>';
 }
 cj.forEach(function(j){
  var left=((j.at||0)-(Date.now()/1000))/3600;
  out+='<div class="card"><b>Scheduled cancel</b>'
   +'<div class="vrd warn">every resting order matching <code>'+esc(j.match)+'</code>'
   +' will be cancelled '+(left>0?('in '+left.toFixed(1)+'h'):'on the next cycle')+'</div>'
   +(j.note?'<div class="muted">'+esc(j.note)+'</div>':'')
   +'<div><button class="small off" onclick="clrCancel(\\''+esc(j.match)+'\\')">Call it off</button></div></div>';
 });
 return out;
}
function setCancel(){
 var m=prompt('Cancel orders in which markets? Any part of the slug, e.g. mov-ma-dem');
 if(!m)return;
 var h=prompt('In how many hours?','6');
 if(h==null)return; var hrs=parseFloat(h);
 if(!(hrs>0&&hrs<72)){alert('give a number of hours between 0 and 72');return;}
 var when=new Date(Date.now()+hrs*3600*1000);
 if(!confirm('Cancel EVERY resting order matching "'+m+'" at '
   +when.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})+'?'))return;
 post({op:'schedule_cancel',match:m,at:Math.round(Date.now()/1000+hrs*3600),
       note:'set from the phone'},function(j){alert(j.note||'done');});
}
function clrCancel(m){
 if(!confirm('Call off the scheduled cancel for '+m+'? Your orders stay resting.'))return;
 post({op:'clear_cancel',match:m},function(j){alert(j.note||'done');});
}
function ordersTab(d){
 var srt=window._ordSort||'est';var out=cancelBanner(d);
 out+='<div class="tabs" style="margin-top:0">'
  +'<button class="'+(srt==='est'?'on':'')+'" onclick="oSort(\\'est\\')">$/day</button>'
  +'<button class="'+(srt==='drop'?'on':'')+'" onclick="oSort(\\'drop\\')">off peak</button></div>';
 var ag=estAge(d);
 if(ag!=null&&ag>60)out+='<div class="card vrd'+(ag>300?' warn':'')+'">rates as of '+dur(ag)+' ago</div>';
 var any=false;
 fams(d).forEach(function(kv){
  var k=kv[0],s=kv[1];var os=(s.orders||[]);if(!os.length)return;any=true;
  var gs=mgroups(os);
  var byest=function(a,b){return b.est-a.est;};
  var bydrop=function(a,b){var x=gdrop(a),y=gdrop(b);return (y==null?-1:y)-(x==null?-1:x);};
  gs.sort(srt==='drop'?bydrop:byest);
  var tot=0;gs.forEach(function(g){tot+=g.est;});
  out+='<div class="card"><b>'+esc(s.name||k)+'</b>'
   +'<div class="kpi">'
   +'<div><div class="v">'+usd(s.earned_today||0)+'</div><div class="l">earned today</div></div>'
   +'<div><div class="v">'+usd(tot)+'</div><div class="l">rate $/day</div></div>'
   +'<div><div class="v">'+gs.length+'</div><div class="l">markets</div></div>'
   +'<div><div class="v">'+os.length+'</div><div class="l">orders</div></div>'
   +'</div>';
  gs.forEach(function(g){out+=mrow(d,g);});
  out+='</div>';
 });
 if(!any)out+='<div class="card muted">No resting orders.</div>';
 return out;
}
function wallsTab(d){
 var out='';
 var wl=[];fams(d).forEach(function(kv){(kv[1].watched||[]).forEach(function(w){wl.push(w);});});
 if(!wl.length)return '<div class="card muted">No watched races.</div>';
 wl.forEach(function(w,i){
  var bid='wq_'+i, goal=w.goal||w.target||0;
  var pctv=(w.target?100*(w.ask_total||0)/w.target:0);
  out+='<div class="card"><div class="name" style="cursor:pointer;font-size:15px" onclick="showbook(\\''+esc(w.market)+'\\',\\''+bid+'\\')">'+nm(d,w.market)+'</div><div id="'+bid+'"></div>'
   +'<div class="kpi"><div><div class="v'+(w.has_room?' ok':(w.qualifies?' warn':' bad'))+'">'+Math.round(w.ask_total||0).toLocaleString()+'</div><div class="l">of '+Math.round(w.target||0).toLocaleString()+' needed \u00b7 '+Math.round(pctv)+'%</div></div></div>'
   +'<div class="mtrack"><div class="mfill" style="width:'+Math.min(100,(goal?100*(w.ask_total||0)/goal:0))+'%'+(w.has_room?'':';background:#8a5a2f')+'"></div></div>';
  if(w.has_room===false){out+='<div style="margin-top:6px"><button class="small" onclick="qax(\\''+esc(w.market)+'\\','+Math.ceil(goal-(w.ask_total||0))+',\\'qo_'+i+'\\')">'+(w.qualifies?'Top up to 125%':'Qualify ask')+'</button></div><div id="qo_'+i+'"></div>';}
  out+='</div>';
 });
 return out;
}
function posTab(d){
 var pos=[];fams(d).forEach(function(kv){(kv[1].positions||[]).forEach(function(p){p.fam=kv[0];pos.push(p);});});
 if(!pos.length)return '<div class="card muted">No positions.</div>';
 pos.sort(function(a,b){return (a.per_dollar-b.per_dollar)||(b.liq-a.liq);});
 var idle=pos.filter(function(p){return p.earn<0.005;}).length;
 var out='<div class="card"><div class="kpi">'
  +'<div><div class="v">'+pos.length+'</div><div class="l">positions</div></div>'
  +'<div><div class="v">'+usd(pos.reduce(function(a,p){return a+p.liq;},0))+'</div><div class="l">if closed now</div></div>'
  +'<div><div class="v'+(idle?' warn':'')+'">'+idle+'</div><div class="l">earning nothing</div></div>'
  +'</div></div>';
 pos.forEach(function(p,i){
  var bid='pv_'+i;
  out+='<div class="orow"><div class="name" style="cursor:pointer" onclick="showbook(\\''+esc(p.market)+'\\',\\''+bid+'\\')">'+nm(d,p.market)+'</div><div id="'+bid+'"></div>'
   +'<div style="display:flex;gap:16px;align-items:baseline;margin:3px 0">'
   +'<span class="px">'+(p.qty>0?p.qty+' sh':(-p.qty)+' short')+'</span>'
   +'<span class="rt">'+usd(p.liq)+'</span>'
   +'<span class="'+(p.earn<0.005?'warn':'muted')+'">'+(p.earn<0.005?'idle':usd(p.earn)+'/d')+'</span></div></div>';
 });
 return out;
}
function soldTab(d){
 var wd={day_n:0,day_usd:0,week_n:0,week_usd:0,flat_day:0,
         moves_n:0,moves_usd:0,moves_4h_n:0,moves_4h_markets:0,
         moves_4h_gain:0,by:{},recent:[]};
 fams(d).forEach(function(kv){var w=kv[1].wind_down;if(!w)return;
  wd.day_n+=w.day_n||0;wd.day_usd+=w.day_usd||0;wd.week_n+=w.week_n||0;wd.week_usd+=w.week_usd||0;
  wd.flat_day+=w.flat_day||0;
  wd.moves_n+=w.moves_n||0;wd.moves_usd+=w.moves_usd||0;
  wd.moves_4h_n+=w.moves_4h_n||0;wd.moves_4h_markets+=w.moves_4h_markets||0;
  wd.moves_4h_gain+=w.moves_4h_gain||0;
  for(var k in (w.by_kind||{})){var b=w.by_kind[k];wd.by[k]=wd.by[k]||{n:0,usd:0};wd.by[k].n+=b.n;wd.by[k].usd+=b.usd;}
  (w.recent||[]).forEach(function(r){wd.recent.push(r);});});
 if(!wd.week_n&&!wd.moves_n)return '<div class="card muted">Nothing sold yet.</div>';
 var out='<div class="card"><div class="kpi">'
  +'<div><div class="v">'+wd.day_n+'</div><div class="l">sold 24h</div></div>'
  +'<div><div class="v">'+usd(wd.day_usd)+'</div><div class="l">proceeds 24h</div></div>'
  +'<div><div class="v">'+wd.flat_day+'</div><div class="l">went flat</div></div>'
  +'<div><div class="v">'+usd(wd.week_usd)+'</div><div class="l">week</div></div>'
  +'</div>';
 // the repricings collapse to ONE line \u2014 how many markets moved a
 // price in the last four hours and what the model says it added per
 // day \u2014 and stay out of the list until asked for (owner, 2026-08-31:
 // "Most of the time I only want to see sales")
 if(wd.moves_4h_n){
  out+='<div class="vrd">'+wd.moves_4h_markets+' market'
   +(wd.moves_4h_markets===1?'':'s')+' moved a price in the last 4h '
   +'\u2014 exits earning '+perday(wd.moves_4h_gain)+' more'
   +' <button class="small" onclick="oMoves()">'
   +(window._showMoves?'hide':'show')+'</button></div>';
 }
 for(var k in wd.by){out+='<div class="vrd muted">'+esc(k)+': '+wd.by[k].n+' \u00b7 '+usd(wd.by[k].usd)+'</div>';}
 out+='</div>';
 wd.recent.sort(function(a,b){return (b.ts||0)-(a.ts||0);});
 var list=wd.recent.filter(function(r){
  return r.sale!==false||window._showMoves;});
 if(!list.length)out+='<div class="card muted">No sales in the last 24h.</div>';
 list.slice(0,12).forEach(function(r){
  var sale=r.sale!==false;
  out+='<div class="orow"><div class="name">'+nm(d,r.market)+'</div>'
   +'<div style="display:flex;gap:16px;align-items:baseline">'
   +(sale?'<span class="px">'+usd(r.usd)+'</span>'
         :'<span class="muted">'+perday(r.gain)+'</span>')
   +'<span class="muted">'+esc(r.kind)+' '+r.qty+' '
   +(sale?'@ '+pc(r.px)
         :(r.from_px!=null?pc(r.from_px)+'\u2192':'@ ')+pc(r.px))+'</span>'
   +(sale?(r.flat?'<span class="ok">flat</span>':''):'')
   +'</div></div>';});
 return out;
}
function oMoves(){window._showMoves=!window._showMoves;
 if(window._d)document.getElementById('view').innerHTML=render(window._d);}
function render(d){
 if(d.starting)return bootCard(d);
 window._d=d;
 var t=window._oTab||'orders';
 var tabs=[['orders','Orders'],['walls','Walls'],['pos','Positions'],['sold','Sold']];
 var out='<div class="tabs">'+tabs.map(function(x){
  return '<button class="'+(t===x[0]?'on':'')+'" onclick="oTab(\\''+x[0]+'\\')">'+x[1]+'</button>';}).join('')+'</div>';
 if(t==='walls')return out+wallsTab(d);
 if(t==='pos')return out+posTab(d);
 if(t==='sold')return out+soldTab(d);
 return out+ordersTab(d);
}
function oSort(which){window._ordSort=which;
 if(window._d)document.getElementById('view').innerHTML=render(window._d);}
function mv(id,px){
 var v=prompt('New price in cents:',(px*100).toFixed(1));
 if(v==null)return; var p=parseFloat(v)/100;
 if(!(p>0&&p<1)){alert('0.1c to 99.9c');return;}
 post({op:'move',order_id:id,price:p},function(j){if(!j.ok)alert(j.note||'refused');});
}
function cx(id){
 if(!confirm('Cancel this order?'))return;
 post({op:'cancel',order_id:id},function(j){if(!j.ok)alert(j.note||'refused');});
}
function qax(m,gap,outid){
 if(!confirm('Build the ask wall to 125% of Target Size? '+gap.toLocaleString()+' shares to go at 99c (~$'+Math.ceil(gap*0.01)+').'))return;
 document.getElementById(outid).innerHTML='<div class="muted">starting\u2026</div>';
 post({op:'qualify_ask',market:m},function(j){
  document.getElementById(outid).innerHTML='<div class="'+(j.ok?'ok':'bad')+'">'+esc(j.note||'')+'</div>';});
}
"""

PLAN_JS = """
function render(d){
 var out='';
 fams(d).forEach(function(kv){
  var k=kv[0],s=kv[1];
  out+='<div class="card"><b>'+esc(s.name||k)+'</b> <span class="pill">'+esc(s.mode)+'</span>';
  if(s.mode==='observing'){out+='<div class="hint">Switch is off \\u2014 nothing below will be placed. This is exactly what I would do if you armed it, so it can be judged first.</div>';}
  var best=s.best_idle||[];
  if(!best.length){out+='<div class="muted">Nothing worth entering right now \\u2014 every scored market either pays under the bar, is louder than the courtesy share, resolves too soon, or has a dead side I don\\u2019t revive.</div></div>';return;}
  out+='<div class="sub">Best candidates, best first:</div>';
  best.forEach(function(b){
   var pid='pb_'+esc(b.market).replace(/[^a-z0-9]/g,'');
   out+='<div style="margin:8px 0 0;border-top:1px solid #2c3527;padding-top:6px">'
    +'<div class="name" style="cursor:pointer" onclick="showbook(\\''+esc(b.market)+'\\',\\''+pid+'\\')">'+esc(b.name||b.market)+' <span class="muted">\u25be book</span></div>'
    +'<div id="'+pid+'"></div>'
    +'<div class="muted"><code>'+esc(b.market)+'</code> \\u2014 worth ~'+usd(b.est)+'/day</div>';
   (b.plans||[]).forEach(function(p){
    out+='<div class="vrd">'+(p.side==='BUY'?'bid':'ask')+' '+p.qty+' @ '+pc(p.px)
     +' ('+usd(p.cost)+' at risk) \\u2014 '+esc(p.why||'')+'</div>';
   });
   out+='</div>';
  });
  out+='</div>';
 });
 return out;
}
"""

SWITCH_JS = """
function render(d){
 var sv=(d.switch_view||{});var out='';
 var ph=d.place_health||{};
 if(ph.blocked){var t=new Date((ph.since||0)*1000);out+='<div class="card"><div class="warn"><b>The exchange is refusing this server\\'s orders</b> \\u2014 "your connection looks like a VPN", since '+('0'+t.getHours()).slice(-2)+':'+('0'+t.getMinutes()).slice(-2)+', '+(ph.refused||0)+' refused.</div><div class="muted">Nothing is cancelled that could not come back: moves, step-ups and re-prices are paused; one placement a minute probes for recovery. Cancels that only reduce risk still run. The fix is a new outbound address: tap Deploy on DigitalOcean.</div></div>';}
 var order=['master'];for(var k in sv){if(k!=='master')order.push(k);}
 order.forEach(function(k){
  var s=sv[k]||{};var label=(k==='master'?'Master switch \\u2014 all of 3.0':k+' switch');
  var sm=(d.summaries||{})[k];
  out+='<div class="card"><b>'+esc(label)+'</b> ';
  out+=s.on?'<span class="pill on">ON</span>':(s.armed?'<span class="pill">armed</span>':'<span class="pill">off</span>');
  if(sm){out+='<div class="muted">'+usd(sm.spent)+' of '+usd(sm.capital_usd)+' at risk'+(sm.holdings_usd?(sm.holdings_counted?' (incl. holdings worth '+usd(sm.holdings_usd)+' at liquidation)':' \u00b7 plus holdings worth '+usd(sm.holdings_usd)+' at liquidation, not counted'):'')+'; resting earns ~'+usd(Math.min(sm.est_day||0,(sm.est_rate!=null?sm.est_rate:1e9)))+'/day.</div>';}
  if(k==='master'){out+='<div class="hint">Master gates every family, and it moves the whole operation: ON asks 1.0 and 2.0 to halt their automation first \\u2014 3.0 touches nothing until both confirm \\u2014 then 3.0 adopts every resting order in its families and runs the book alone. OFF hands the floor straight back. One tap here stops all of 3.0.</div>';
   var fl=(window._d&&window._d.floor)||{};
   if(s.on){out+=fl.acked?'<div class="ok">1.0 and 2.0 have stood down \\u2014 3.0 has the floor.</div>':'<div class="warn">Waiting for 1.0/2.0 to stand down\\u2026</div>';}}
  if(s.on){out+='<div><button class="off" onclick="tap(\\'off\\',\\''+k+'\\')">Turn OFF</button></div>';}
  else if(s.armed){out+='<div class="sub warn">Armed \\u2014 confirm within '+(s.arm_expires_in||0)+'s to turn on.</div>'
   +'<div><button onclick="tap(\\'confirm\\',\\''+k+'\\')">Confirm ON</button>'
   +'<button class="off" onclick="tap(\\'off\\',\\''+k+'\\')">Never mind</button></div>';}
  else{out+='<div><button onclick="tap(\\'arm\\',\\''+k+'\\')">Arm&hellip;</button></div>'
   +'<div class="hint">Turning on takes two taps (arm, then confirm). Turning off takes one. Every flip is logged and pushed to the phone.</div>';}
  if(s.has_window){
   var au=s.active_until||0;var live=au>Date.now()/1000;
   out+='<div class="sub">Game window: '+(s.resting_now?'resting hours now':(live?'<b>kept active until '+when(au)+' by you</b>':'<span class="warn">orders pulled now \\u2014 game window</span>'))+(live?' <button class="off" onclick="auClear(\\''+k+'\\')">Clear</button>':'')+'</div>';
   out+='<div>Stay active until <input id="au-'+k+'" type="time" style="font-size:16px;padding:6px"> ET <select id="aud-'+k+'" style="font-size:16px;padding:6px"><option value="">today</option><option value="tomorrow">tomorrow</option></select> <button onclick="auSet(\\''+k+'\\')">Set</button></div>';
   out+='<div class="hint">Until that time the family rests its orders as in resting hours, whatever the window says. It clears itself when the time passes.</div>';
  }
  var lg=(s.log||[]);
  if(lg.length){out+='<details class="how"><summary>last flips</summary>';
   lg.slice(-6).reverse().forEach(function(r){out+='<div class="muted">'+when(r.ts)+' \\u2014 '+esc(r.action)+'</div>';});
   out+='</details>';}
  out+='</div>';
 });
 return out;
}
function tap(op,which){post({op:'switch_'+op,which:which});}
function auSet(k){var e=document.getElementById('au-'+k);var v=e?e.value:'';if(!v){alert('pick a time first');return;}var d=document.getElementById('aud-'+k);if(d&&d.value)v+=' '+d.value;post({op:'family_active_until',which:k,value:v});}
function auClear(k){post({op:'family_active_until',which:k,value:''});}
"""

BONDS_JS = r"""
function bPct(x){return x==null?'—':(x*100).toFixed(x>=0.1?0:1)+'%';}
function bOdds(r){return r.bond==='NO'?'NO '+bPct(1-(r.odds||0)):'YES '+bPct(r.odds);}
function bPill(s){return '<span class="pill">'+s+' bond</span>';}
// an order's YES price in the bond's own terms (a NO bond's exit is a YES bid: 100 − it)
function bTerms(r,px){return r.bond==='NO'?Math.round((1-px)*10000)/10000:px;}
function bBtn(label,onclick,cls){return '<button class="'+(cls||'')+'" style="padding:10px 16px;font-size:16px;margin:4px 8px 4px 0" onclick="'+onclick+'">'+label+'</button>';}
function bOp(op,m,v){var body={op:op,market:m};if(v!=null)body.value=v;window._bNote='';post(body,function(j){
 bSay('<div class="'+(j.ok?'ok':'bad')+'">'+esc(j.note||'')+'</div>');});}
// typed fields instead of prompt() (owner, 2026-09-03: "Clicking set
// budget does nothing"). A field keeps its text across a redraw, and
// holds the redraw while it has focus (the live-card hold in load()).
function bKeep(id){var e=document.getElementById(id);return e&&e.value?e.value:'';}
function bField(id,val,w){return '<input id="'+id+'" type="number" inputmode="decimal" step="0.01" style="width:'+w+';font-size:16px;padding:8px" value="'+esc(val)+'" onfocus="window._liveOpen=true" onblur="window._liveOpen=false">';}
function bSay(h){window._bNote=h;var el=document.getElementById('bmsg');if(el)el.innerHTML=h;}
function bBudget(){var x=parseFloat(bKeep('bbud'));if(!(x>=0)){bSay('<div class="bad">type the budget in dollars first</div>');return;}var f=document.getElementById('bbud');if(f)f.value='';bOp('bonds_budget','-',x);}
function bMoreSet(m){var x=parseFloat(bKeep('bmore-'+m));if(!(x>=0)){bSay('<div class="bad">dollars, please</div>');return;}bOp('bonds_more_cap',m,x);}
function bExitSet(m){var x=parseFloat(bKeep('bexit-'+m));if(!(x>0)){bSay('<div class="bad">type the exit price in cents first</div>');return;}bOp('bonds_exit_at',m,x);}
function bBuy(m){var q=parseFloat(bKeep('bbq-'+m));var p=parseFloat(bKeep('bbp-'+m));if(!(q>=1)||!(p>0)){bSay('<div class="bad">how many shares, and the price in cents?</div>');return;}if(confirm('Rest a buy for '+q+' shares at '+p+'¢?'))bOp('bonds_buy',m,{qty:q,px:p});}
function bSellInto(m,px,qty,pr){if(confirm('Sell '+qty+' shares into the bids out to '+pc(px)+' for about '+usd(pr)+'? The commission comes off the proceeds. Our own exit comes off first.'))bOp('bonds_sell_into',m,{px:px,qty:qty});}
function bEnter(m,px,qty,cost,money){if(confirm('Buy '+qty+' shares out to '+pc(px)+' for about '+usd(cost)+'? Money: '+usd(money)+'.'))bOp('bonds_enter',m,px);}
// the live line (owner, 2026-09-03): one stream carries the held
// markets' rows as their books move; the page redraws only while you
// are at the top and no field has focus.
function bLiveOpen(){
 if(window._bEs)return;
 var es=new EventSource('/bonds_live?key='+lvKey());
 window._bEs=es;
 es.onmessage=function(ev){var j;try{j=JSON.parse(ev.data);}catch(e){return;}
  if(!j||!j.rows)return;window._bLive=j.rows;window._bLiveAt=Date.now();bLiveApply();};
}
function bLiveShut(){if(window._bEs){try{window._bEs.close();}catch(e){}window._bEs=null;}window._bLive=null;}
function bLiveApply(){
 var d=window._d;if(!d||!d.bonds||!window._bLive)return;
 var rows=d.bonds.rows||[];
 rows.forEach(function(r,i){var l=window._bLive[r.market];if(l)rows[i]=l;});
 if((window.scrollY||0)>120||window._liveOpen)return;
 document.getElementById('view').innerHTML=render(d);
}
// which markets you have opened for details, kept across redraws
window._bOpen=window._bOpen||{};
function bTog(el,m){window._bOpen[m]=!!el.open;}
function bExit(r){return (r.calc&&r.calc.orders||[]).filter(function(o){return !o.decoy;});}
function bTop(r,L,held){
 var mk=r.mark;var black=!!(mk&&mk.black);
 var h='<div class="name">'+esc(L[r.market]||r.market)+' '+bPill(r.bond)+(black?' <span class="pill on">in the black</span>':'')+(r.odds_changed?' <span class="pill" style="border-color:#c9a227;color:#e8c547">odds changed · Silver '+bOdds(r)+'</span>':'')+(r.stale?' <span class="warn">stale</span>':'')+'</div>';
 if(held){
  if(r.odds_changed)h+='<div class="sub warn">No longer in the 99% band: the exit keeps working, nothing new is bought here. It leaves the page once you are out, and comes back if the odds return.</div>';
  if(r.unconfirmed)h+='<div class="sub warn">The exchange shows '+r.unconfirmed.exch+' of '+r.unconfirmed.ledger+' here but the transaction record shows no sale (it puts you at '+r.unconfirmed.record+'). Kept until the record explains it.</div>';
  if(mk)h+='<div class="sub">bid <b class="'+(black?'ok':'warn')+'">'+pc(mk.bid)+'</b> vs your cost '+pc(mk.cost)+' ('+(mk.edge>=0?'+':'−')+(Math.abs(mk.edge)*100).toFixed(1)+'¢ a share)'+(r.cost_src&&r.cost_src!=='record'?' <span class="muted">· cost from the '+(r.cost_src==='exchange'?'exchange’s own figure':r.cost_src==='record+exchange'?'record and the exchange’s figure':'ledger')+'</span>':'')+'</div>';
  var ex=bExit(r);
  var e=ex.length?('exit '+ex.map(function(o){return o.qty+' @ '+pc(bTerms(r,o.price));}).join(' + ')+(r.pin?' <span class="pill on">your price</span>':'')+' → <b>'+usd(ex.reduce(function(s,o){return s+(o.est||0);},0))+'/day</b>'):'<span class="warn">no exit resting</span>';
  var mos=(r.more&&r.more.orders&&r.more.orders.length)?r.more.orders:((r.more&&r.more.order)?[r.more.order]:[]);
  var mo=mos.length?(' · buying more '+mos.map(function(o){return o.qty+' @ '+pc(o.price);}).join(' + ')):'';
  h+='<div class="sub"><b>'+r.qty+' held @ '+pc(r.cost_px)+'</b> · '+e+mo+(r.rewards?' · earned '+usd(r.rewards):'')+'</div>';
 } else {
  h+='<div class="sub">Silver '+bOdds(r)+(r.cost!=null?' · take '+pc(r.cost)+', '+r.size+' avail'+(r.days!=null?' · '+bPct(r['yield'])+' in '+r.days+'d ≈ '+bPct(r.annual)+'/yr':''):' · nothing to take')+'</div>';
 }
 return h;
}
function bBook(r){
 var bk=r.book;if(!bk)return '<div class="muted">no book yet</div>';
 var bids=bk.bids||[],asks=bk.asks||[];var n=Math.max(bids.length,asks.length);if(!n)return '<div class="muted">empty book</div>';
 var h='<div class="muted">'+(bk.terms==='NO'?'NO prices (100 − YES)':'YES prices')+' · green = ours</div><table><tr><th class="r">bid size</th><th class="r">bid</th><th>ask</th><th>ask size</th></tr>';
 for(var i=0;i<n;i++){var b=bids[i],a=asks[i];
  h+='<tr><td class="r">'+(b?(b[1]+(b[2]?' <span class="ok">('+b[2]+')</span>':'')):'')+'</td><td class="r">'+(b?pc(b[0]):'')+'</td><td>'+(a?pc(a[0]):'')+'</td><td>'+(a?(a[1]+(a[2]?' <span class="ok">('+a[2]+')</span>':'')):'')+'</td></tr>';}
 return h+'</table>';
}
function bCalc(r){
 var c=r.calc;if(!c)return '';
 var sideWord=(c.side==='SELL'?'ask':'bid');var pool=c.side_pool;var h='';
 h+='<div class="muted">'+sideWord+' side: '+c.side_size+' shares, Target Size '+c.target+' · side pool '+(pool==null?'unconfirmed':usd(pool)+'/day = '+usd(c.pool_day)+' ÷ '+(c.event_n||'?')+' ÷ 2')+'</div>';
 (c.orders||[]).forEach(function(o){
  h+='<div>'+(o.decoy?'decoy ':'exit ')+o.qty+' @ '+pc(bTerms(r,o.price))+(o.ticks?', '+o.ticks+' tick'+(o.ticks>1?'s':'')+' behind':', at the touch')+': '+(o.qualifies?bPct(o.share)+' of the side × '+(pool==null?'?':usd(pool))+' = <b>'+usd(o.est)+'/day</b>':'<span class="warn">earning nothing</span>')+'</div>';
 });
 if(c.touch)h+='<div class="muted">the whole lot at the touch ('+pc(bTerms(r,c.touch.price))+') would take '+bPct(c.touch.share)+' = '+usd(c.touch.est)+'/day</div>';
 var s=r.slot;
 if(s&&s.split&&s.levels&&s.single)h+='<div class="muted">split: the '+s.levels[0][1]+' up front carry '+Math.round((s.keep||0)*100)+'% of the best reward; expected to sell ~'+s.exposure+' shares a day, against ~'+s.single.exposure+' with the whole lot at '+pc(bTerms(r,s.single.px))+'</div>';
 return h;
}
function bSniper(r,b,sw){
 var main=bExit(r);
 if(!main.length)return '<div class="sub">Sniper: no exit resting'+(sw.on?'':' (bonds switch off)')+'</div>';
 if(!r.front){
  if(r.dance&&r.decoy&&r.decoy.length){var cs=r.dance.clear_since;var mins=cs?Math.floor((Date.now()/1000-cs)/60):0;return '<div class="sub">Sniper: decoy holding @ '+pc(r.decoy[0].price)+' · nothing foreign in front for '+mins+' min (comes off after '+Math.round((b.decoy_linger_s||300)/60)+')</div>';}
  return '<div class="sub">Sniper: nothing in front of our exit</div>';
 }
 var h='Sniper: '+r.front.qty+' @ '+pc(r.front.price)+' in front';
 if(!r.minnow)return '<div class="sub">'+h+' — too big to lead (over '+(b.minnow_max||25)+')</div>';
 var dq=(r.decoy&&r.decoy.length)?r.decoy[0]:null;
 if(dq){var snap=(r.dance?(r.dance.since||0):0)+(b.dance_wait_s||7200);h+=' · decoy '+dq.qty+' @ '+pc(dq.price)+(r.dance?', move '+r.dance.moves+' of 3':'')+((r.front.qty>=1&&r.dance)?' · bought at '+when(snap)+' if it stays put':' · dust: nothing to buy, the decoy holds');}
 else if(r.dance&&r.dance.idle)h+=' · dust at the far touch, nowhere for it to move: no decoy; the exit steps up on the cooldown';
 else if(r.dance&&r.dance.note)h+=' · <span class="warn">no decoy resting: '+esc(r.dance.note)+'</span>';
 else h+=(sw.on?' · <span class="warn">no decoy resting yet</span>':' · bonds switch off');
 return '<div class="sub">'+h+'</div>';
}
function bMore(r,b,sw){
 var mo=r.more;if(!mo)return '';
 var m=esc(r.market);var pct=Math.round((b.more_share||0.3)*100);
 var h='Buy more: up to $'+bField('bmore-'+m,bKeep('bmore-'+m)||(mo.cap_usd||0).toFixed(2),'6em')+' '+bBtn('Set','bMoreSet(\''+m+'\')')+(mo.cap_px!=null?'<br>at '+pc(mo.cap_px)+' or better (your first price here)':'');
 if(mo.paused)h+='<br><span class="warn">'+esc(mo.paused)+'</span>';
 else if(mo.order){var ol=(mo.orders&&mo.orders.length>1)?mo.orders:[mo.order];h+='<br>resting '+ol.map(function(o){return o.qty+' @ '+pc(o.price);}).join(' + ')+': '+bPct(mo.order.share)+' of its side'+(ol.length>1?' together':'')+' = <b>'+usd(mo.order.est)+'/day</b>';}
 else if(mo.retry_at)h+='<br><span class="muted">'+(mo.note?esc(mo.note)+' · ':'')+'tries again '+when(mo.retry_at)+'</span>';
 else if(mo.slot){var sl=(mo.slot.levels&&mo.slot.levels.length>1)?mo.slot.levels.map(function(l){return l[1]+' @ '+pc(l[0]);}).join(' + '):(mo.slot.qty+' @ '+pc(mo.slot.price));h+='<br>would rest '+sl+' ('+bPct(mo.slot.share)+')'+(sw.on?'':' — bonds switch off');}
 else h+='<br><span class="muted">not resting: '+(mo.note?esc(mo.note):'no price at or under your first price captures '+pct+'% of its side')+'</span>';
 return '<div class="sub">'+h+'</div>';
}
function bExitAt(r){
 var m=esc(r.market);var pin=r.pin;
 var h='<b>Exit at</b> — rest the whole lot at a price of your own, above your cost ('+pc(r.cost_px)+' with fees):<br>';
 if(pin)h+='pinned at '+pc(pin.bond_px)+' since '+when(pin.since)+' · it only moves if the other side comes up to meet it '+bBtn('Clear','bOp(\'bonds_exit_clear\',\''+m+'\')','off');
 else h+=bField('bexit-'+m,bKeep('bexit-'+m),'5em')+'¢ '+bBtn('Set exit','bExitSet(\''+m+'\')');
 return '<div class="sub">'+h+'</div>';
}
function bBuyAt(r){
 var m=esc(r.market);
 var h='<b>Buy</b> — rest an order of your own for some shares at your price (it fills when someone sells to it):<br>';
 (r.buys||[]).forEach(function(o){h+='resting '+o.qty+' @ '+pc(o.price)+' '+bBtn('Pull','bOp(\'bonds_pull_buy\',\''+m+'\',\''+esc(o.id)+'\')','off')+'<br>';});
 h+=bField('bbq-'+m,bKeep('bbq-'+m),'5em')+' shares at '+bField('bbp-'+m,bKeep('bbp-'+m),'5em')+'¢ '+bBtn('Buy','bBuy(\''+m+'\')');
 return '<div class="sub">'+h+'</div>';
}
function bSellLadder(r){
 var bk=r.book;if(!bk||!(r.qty>0.005))return '';
 var bids=bk.bids||[];if(!bids.length)return '';
 var m=esc(r.market);var cost=r.cost_px||0;var cum=0;var rows=0;
 var h='<div class="sub"><b>Sell into the bids</b> — take the buyers resting there, best first, out to a price (our exit comes off first):</div><table><tr><th class="r">bid</th><th class="r">avail</th><th class="r">you sell</th><th class="r">proceeds</th><th class="r">vs cost</th><th></th></tr>';
 bids.forEach(function(b){var px=b[0],q=b[1]-(b[2]||0);if(q<=0)return;cum+=q;var sell=Math.min(Math.floor(cum),Math.floor(r.qty));if(sell<1)return;var pr=sell*px;var pnl=(px-cost)*sell;var ok=px>cost+1e-9;rows++;
  h+='<tr><td class="r">'+pc(px)+'</td><td class="r">'+q.toFixed(1)+'</td><td class="r">'+sell+'</td><td class="r">'+usd(pr)+'</td><td class="r '+(pnl>=0?'ok':'warn')+'">'+(pnl>=0?'+':'−')+usd(Math.abs(pnl))+'</td><td>'+(ok?bBtn('Sell','bSellInto(\''+m+'\','+px+','+sell+','+pr.toFixed(2)+')'):'<span class="muted">under cost</span>')+'</td></tr>';});
 return rows?h+'</table>':'';
}
function bBait(r){
 var bt=r.bait||{};var m=esc(r.market);
 var h='<b>Bait</b> — one share a tick inside their best on the buy side, to pull their offers up:<br>';
 if(bt.resting)h+='resting @ '+pc(bt.px)+' since '+when(bt.since)+' · they have followed '+(bt.followed||0)+' time'+((bt.followed||0)===1?'':'s')+' '+bBtn('Pull bait','bOp(\'bonds_pull_bait\',\''+m+'\')','off');
 else h+=(bt.note?esc(bt.note)+' · ':'')+bBtn('Bait','bOp(\'bonds_bait\',\''+m+'\')');
 return '<div class="sub">'+h+'</div>';
}
function bLadder(r,b){
 var lad=r.ladder||[];if(!lad.length)return '';
 var m=esc(r.market);
 var h='<div class="sub"><b>Enter</b> — buy everything others have out to a price:</div><table><tr><th class="r">price</th><th class="r">avail</th><th class="r">total</th><th class="r">cost</th><th></th></tr>';
 lad.forEach(function(l){h+='<tr><td class="r">'+pc(l.px)+(l.cost!==l.px?' <span class="muted">('+pc(l.cost)+')</span>':'')+'</td><td class="r">'+l.qty+'</td><td class="r">'+l.cum_qty+'</td><td class="r">'+usd(l.cum_usd)+'</td><td>'+bBtn('Enter','bEnter(\''+m+'\','+l.px+','+l.cum_qty+','+l.cum_usd+','+(b.money||0)+')')+'</td></tr>';});
 return h+'</table>';
}
function bRow(r,d,b,sw,held){
 var L=d.labels||{};var m=esc(r.market);var open=window._bOpen[r.market]?' open':'';
 var black=!!(r.mark&&r.mark.black);
 var h='<div style="margin:8px 0;border-top:1px solid #2c3527;padding:6px 0 0 8px;border-left:4px solid '+(held?(black?'#7fd77f':'#2c3527'):'transparent')+'">'+bTop(r,L,held);
 h+='<details'+open+' ontoggle="bTog(this,\''+m+'\')"><summary style="font-size:16px;padding:8px 0;cursor:pointer">Details</summary>';
 h+=bBook(r);
 if(held)h+=bCalc(r)+bSniper(r,b,sw)+bExitAt(r)+bSellLadder(r)+bMore(r,b,sw)+bBait(r);
 h+=bLadder(r,b);
 if(!r.odds_changed)h+=bBuyAt(r);
 h+='<div>'+bBtn('Remove from list','if(confirm(\'Remove from the bond list?\'))bOp(\'bonds_remove\',\''+m+'\')','off')+'</div>';
 return h+'</details></div>';
}
function render(d){
 if(d.starting)return bootCard(d);
 var b=d.bonds||{};var sw=((d.switch_view||{}).bonds)||{};var L=d.labels||{};
 var out='';
 var rows=b.rows||[];
 var held=rows.filter(function(r){return r.qty>0.005;});
 var rest=rows.filter(function(r){return !(r.qty>0.005);});
 if(held.length)bLiveOpen();else bLiveShut();
 var live=window._bLiveAt&&(Date.now()-window._bLiveAt)<15000;
 var eh=b.earned||{};
 out+='<div class="card"><div class="hero">'+usd(eh.invested||0)+'<span class="u"> in bonds</span></div>'
  +'<div class="sub"><b>'+(eh.return_pct==null?'—':bPct(eh.return_pct))+' return to date</b> = '+usd(eh.total||0)+' earned on '+usd(eh.deployed||0)+' put in'+(eh.days>=1?' over '+Math.round(eh.days)+' day'+(Math.round(eh.days)===1?'':'s'):'')+(eh.annual_pct!=null?' ≈ '+bPct(eh.annual_pct)+' a year':'')+'</div></div>';
 var nb=held.filter(function(r){return r.mark&&r.mark.black;}).length;
 out+='<div class="card"><b>Your bonds</b> '+(held.length?(live?'<span class="ok" style="font-size:12px">● LIVE</span>':'<span class="muted" style="font-size:12px">live line opening…</span>'):'')+' <span class="pill'+(sw.on?' on':'')+'">'+(sw.on?'switch ON':(sw.armed?'armed':'switch off'))+'</span>'+(held.length?' <span class="muted">'+nb+' of '+held.length+' in the black</span>':'');
 if(!held.length)out+='<div class="muted">None yet. Open a market below and tap Enter.</div>';
 held.forEach(function(r){out+=bRow(r,d,b,sw,true);});
 out+='</div>';
 var tx=b.tax||{};var tax=b.budget_mode==='tax';var e=b.earned||{};
 out+='<div class="card"><b>Money</b>';
 out+='<div class="sub"><b>'+usd(b.money||0)+'</b> to deploy = budget '+usd(b.budget||0)+' + proceeds '+usd(b.cash||0)+' · held at cost '+usd(b.held_cost||0)+'</div>';
 out+='<div class="sub">Earned <b>'+usd(e.total||0)+'</b> = '+usd(e.sales||0)+' on sales + '+usd(e.rewards||0)+' rewards'+(e.today?' ('+usd(e.today)+' today)':'')+'</div>';
 out+='<div id="bmsg">'+(window._bNote||'')+'</div>';
 if(b.error)out+='<div class="bad">'+esc(b.error)+'</div>';
 out+='<details ontoggle="bTog(this,\'-money\')"'+(window._bOpen['-money']?' open':'')+'><summary style="font-size:16px;padding:8px 0;cursor:pointer">Details</summary>';
 out+='<div class="sub">Budget '+(tax?'= taxes owed '+usd(tx.owed||0)+' ('+Math.round((tx.rate||0.22)*100)+'% of '+usd(tx.gross||0)+' paid)':'fixed by you')+(b.spent?' − '+usd(b.spent)+' spent':'')+' · Silver checked '+(b.scan_day?esc(b.scan_day):'never')+'</div>';
 out+='<div>'+bField('bbud',bKeep('bbud'),'7em')+' '+bBtn('Set budget','bBudget()')+(tax?'':bBtn('Follow taxes owed','bOp(\'bonds_budget_tax\',\'-\')'))+bBtn('Check Silver now','bOp(\'bonds_scan\',\'-\')')+'</div>';
 out+='<div class="muted" style="margin-top:6px">A YES bond: Silver has YES at '+bPct(b.high||0.99)+'+. A NO bond: YES at '+bPct(b.low||0.01)+' or under, bought as NO. You buy in with Enter; the exit rests where it keeps '+Math.round((b.keep||0.6)*100)+'% of the best reward with only the shares that need to be out, never under what you paid; a second order buys more up to an amount you set, never over your first price; the sniper leads a small order in front of the exit down and buys it; bait pulls the other side\'s offers up. From your first purchase until you hold nothing, the engine stays out of the market. Rewards shown are what the bond orders measured while resting.</div>';
 out+='</details></div>';
 var pr=b.proposed||[];
 if(pr.length){out+='<div class="card"><b>New from Silver</b>';
  pr.forEach(function(p){out+='<div class="sub">'+esc(L[p.market]||p.market)+' '+bPill(p.bond)+' · Silver '+bOdds(p)+'<br>'+bBtn('Add','bOp(\'bonds_approve\',\''+esc(p.market)+'\')')+bBtn('Ignore','bOp(\'bonds_ignore\',\''+esc(p.market)+'\')','off')+'</div>';});
  out+='</div>';}
 out+='<div class="card"><b>Bond list</b> <span class="muted">cheapest first</span>';
 if(!rest.length)out+='<div class="muted">Empty.</div>';
 rest.forEach(function(r){out+=bRow(r,d,b,sw,false);});
 out+='</div>';
 var dr=b.dropped||[];
 if(dr.length){out+='<details class="how"><summary>dropped ('+dr.length+')</summary>';
  dr.forEach(function(x){out+='<div class="muted"><code>'+esc(x.market)+'</code> · Silver '+bPct(x.odds)+' · '+(x.by==='owner'?'removed by you':'left the band')+(x.held>0.005?' · <b>still holding '+x.held+'</b>':'')+'</div>';});
  out+='</details>';}
 var ig=b.ignored||[];
 if(ig.length){out+='<details class="how"><summary>ignored ('+ig.length+')</summary>';ig.forEach(function(m){out+='<div class="muted"><code>'+esc(m)+'</code> <button onclick="bOp(\'bonds_unignore\',\''+esc(m)+'\')">un-ignore</button></div>';});out+='</details>';}
 var lg=b.log||[];
 if(lg.length){out+='<details class="how"><summary>recent bond actions</summary>';lg.slice().reverse().forEach(function(r){out+='<div class="muted">'+when(r.ts)+' — '+esc(r.event)+(r.market?' '+esc(r.market):'')+(r.price!=null?' @ '+pc(r.price):'')+(r.qty!=null?' x'+r.qty:'')+(r.note?' — '+esc(r.note):'')+'</div>';});out+='</details>';}
 return out;
}
"""

LOG_JS = """
function render(d){
 var rows=[];
 fams(d).forEach(function(kv){
  var k=kv[0],s=kv[1];
  ((d['fam_log_'+k])||[]).forEach(function(r){rows.push([r.ts||0,esc(s.name||k),r]);});
 });
 (d.audit||[]).forEach(function(r){rows.push([r.ts||0,'rails',r]);});
 (d.alerts_log||[]).forEach(function(r){rows.push([r.ts||0,'alert',r]);});
 rows.sort(function(a,b){return b[0]-a[0];});
 var out='<div class="card">';
 if(!rows.length)out+='<div class="muted">Nothing yet.</div>';
 rows.slice(0,80).forEach(function(t){
  var r=t[2];var line='';
  if(t[1]==='alert'){line=(r.sent?'pushed':'held')+': '+esc(r.title)+(r.why?' ('+esc(r.why)+')':'');}
  else if(r.event){line=esc(r.event)+(r.market?' \\u2014 '+nm(window._d,r.market):'')
    +(r.why?' \\u2014 '+esc(r.why):'')+(r.note?' \\u2014 '+esc(r.note):'')
    +(r.error?' \\u2014 '+esc(r.error):'');}
  else if(r.op){line=esc(r.op)+(r.market?' \\u2014 '+nm(window._d,r.market):'')
    +(r.refused?' \\u2014 refused: '+esc(r.refused):'')
    +(r.initiator?' ('+esc(r.initiator)+')':'');}
  else{line=esc(JSON.stringify(r)).slice(0,140);}
  out+='<div class="muted" style="margin:4px 0"><span class="pill">'+t[1]+'</span> '
    +when(t[0])+' \\u2014 '+line+'</div>';
 });
 return out+'</div>';
}
"""

WATCH_JS = """
function wFmtC(v){return Math.round(v*100)+'c';}
function wCurve(rows,X,Y,color){
 if(!rows.length)return '';
 var pts=rows.map(function(r){return [X(r.px),Y(r.ev)];});
 var d='M'+pts[0][0].toFixed(1)+' '+pts[0][1].toFixed(1);
 for(var i=1;i<pts.length;i++){d+=' L'+pts[i][0].toFixed(1)+' '+pts[i][1].toFixed(1);}
 var s='<path d="'+d+'" fill="none" stroke="'+color+'" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" opacity="0.9"/>';
 rows.forEach(function(r){
  s+='<circle cx="'+X(r.px).toFixed(1)+'" cy="'+Y(r.ev).toFixed(1)+'" r="'+(r.picked?5:2.2)+'" fill="'+color+'"'+(r.picked?' stroke="#fff" stroke-width="1.5"':' opacity="0.75"')+'/>';
 });
 return s;
}
function wSnapT(tri,oursAt){
 var pickAt={};(tri.picks||[]).forEach(function(r){pickAt[r.s+(r.px*100).toFixed(1)]=r;});
 function wRows(arr,side,desc){
  var out=arr.filter(function(x){
   var k=side+(x[0]*100).toFixed(1);
   return x[1]>=0.5||pickAt[k]||oursAt[k];   // dust hidden unless marked
  });
  (tri.picks||[]).forEach(function(r){
   if(r.s!==side)return;
   if(out.some(function(x){return Math.abs(x[0]-r.px)<0.0001;}))return;
   out.push([r.px,r.q]);                     // decision rows in price order
  });
  out.sort(function(a,b){return desc?b[0]-a[0]:a[0]-b[0];});
  return out;
 }
 var sb=wRows(tri.book.b||[],'BUY',true),sa=wRows(tri.book.a||[],'SELL',false);
 var age=Math.max(0,Math.round((Date.now()/1000-tri.ts)/60));
 var bt='<div class="muted" style="font-size:12px">the book as the engine saw it \u2014 '+(age<1?'moments':age+' min')+' ago \u00b7 \u25c9 marks the decision</div>';
 bt+='<table><tr><th class="r">bid size</th><th class="r">bid</th><th>ask</th><th>ask size</th></tr>';
 var nrows=Math.min(Math.max(sb.length,sa.length),7);
 for(var i=0;i<nrows;i++){
  var bd=sb[i],ak=sa[i];
  var bm=bd?((pickAt['BUY'+(bd[0]*100).toFixed(1)]?' \u25c9':'')+(oursAt['BUY'+(bd[0]*100).toFixed(1)]?' \u25CF':'')):'';
  var am=ak?((pickAt['SELL'+(ak[0]*100).toFixed(1)]?' \u25c9':'')+(oursAt['SELL'+(ak[0]*100).toFixed(1)]?' \u25CF':'')):'';
  bt+='<tr><td class="r">'+(bd?fmtsz(bd[1]):'')+'</td><td class="r">'+(bd?pc(bd[0])+bm:'')+'</td>'
    +'<td>'+(ak?pc(ak[0])+am:'')+'</td><td>'+(ak?fmtsz(ak[1]):'')+'</td></tr>';
 }
 return bt+'</table>';
}
function wCard(name,slug,b,tri){
 var lad=(b&&b.ladder)||{};var sides=lad.sides||{};
 var bids=(sides.BUY||{}).rows||[],asks=(sides.SELL||{}).rows||[];
 var all=bids.concat(asks);
 var g=b&&b.fair!=null?'model '+(b.fair*100).toFixed(1)+'c':(b&&b.band&&b.band.med!=null?'evidence '+b.band.lo.toFixed(0)+'\u2013'+b.band.hi.toFixed(0)+'c \u00b7 confidence '+Math.round((b.conf||0)*100)+'%':'no grounding');
 var head='<div style="font-size:22px;font-weight:700;line-height:1.2;margin:2px 0">'+esc(name)+'</div>'
  +'<div class="muted">'+esc(g)+(lad.pool_day!=null?' \u00b7 pool $'+lad.pool_day+'/day per side':'')+(lad.note?' \u00b7 '+esc(lad.note):'')+'</div>';
 if(tri&&tri.why)head+='<div class="muted" style="margin:2px 0">'+(tri['in']?'\u2705 worth budget':'\u25cb passed on')+' \u2014 '+esc(tri.why)+'</div>';
 if(!all.length)return head+((tri&&tri.book)?wSnapT(tri,{}):'')+'<div class="muted" style="padding:14px 0">no priced ladder \u2014 '+esc(lad.note||'nothing clears here')+'</div>';
 var W=340,H=210,PL=36,PB=26,PT=14,PR=10;
 var pxs=all.map(function(r){return r.px;});
 var x0=Math.max(Math.min.apply(null,pxs)-0.02,0),x1=Math.min(Math.max.apply(null,pxs)+0.02,1);
 var evs=all.map(function(r){return r.ev;});
 var y1=Math.max(Math.max.apply(null,evs)*1.12,(lad.bar||0.5)*1.4),y0=Math.min(0,Math.min.apply(null,evs));
 function X(p){return PL+(W-PL-PR)*(p-x0)/(x1-x0);}
 function Y(v){return PT+(H-PT-PB)*(1-(v-y0)/(y1-y0));}
 var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="EV curve">';
 if(b&&b.band&&b.band.lo!=null&&b.fair==null){
  var bl=Math.max(b.band.lo/100,x0),bh=Math.min(b.band.hi/100,x1);
  if(bh>bl)s+='<rect x="'+X(bl).toFixed(1)+'" y="'+PT+'" width="'+(X(bh)-X(bl)).toFixed(1)+'" height="'+(H-PT-PB)+'" fill="rgba(158,196,154,0.07)"/>';
 }
 [0.25,0.5,0.75,1].forEach(function(f){
  var v=y0+(y1-y0)*f,y=Y(v);
  s+='<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" stroke="rgba(255,255,255,0.06)"/>';
  s+='<text x="'+(PL-4)+'" y="'+(y+3)+'" text-anchor="end" font-size="8" fill="rgba(255,255,255,0.4)">$'+v.toFixed(1)+'</text>';
 });
 var yb=Y(lad.bar||0.5);
 s+='<line x1="'+PL+'" y1="'+yb+'" x2="'+(W-PR)+'" y2="'+yb+'" stroke="rgba(255,208,107,0.5)" stroke-dasharray="4 3"/>';
 s+='<text x="'+(W-PR)+'" y="'+(yb-3)+'" text-anchor="end" font-size="8" fill="rgba(255,208,107,0.8)">the '+((lad.bar||0.5)*100).toFixed(0)+'c bar</text>';
 if(Y(0)<H-PB)s+='<line x1="'+PL+'" y1="'+Y(0)+'" x2="'+(W-PR)+'" y2="'+Y(0)+'" stroke="rgba(255,255,255,0.15)"/>';
 if(b&&b.fair!=null&&b.fair>=x0&&b.fair<=x1){
  s+='<line x1="'+X(b.fair).toFixed(1)+'" y1="'+PT+'" x2="'+X(b.fair).toFixed(1)+'" y2="'+(H-PB)+'" stroke="rgba(255,255,255,0.3)" stroke-dasharray="2 3"/>';
  s+='<text x="'+X(b.fair).toFixed(1)+'" y="'+(PT-3)+'" text-anchor="middle" font-size="8" fill="rgba(255,255,255,0.6)">model '+wFmtC(b.fair)+'</text>';
 }
 [x0,(x0+x1)/2,x1].forEach(function(p,i){
  var anch=i===0?'start':(i===2?'end':'middle');
  s+='<text x="'+X(p).toFixed(1)+'" y="'+(H-8)+'" text-anchor="'+anch+'" font-size="8" fill="rgba(255,255,255,0.4)">'+wFmtC(p)+'</text>';
 });
 s+=wCurve(bids,X,Y,'#9ec49a');
 s+=wCurve(asks,X,Y,'#d9b36a');
 s+='</svg>';
 var legend='<div class="muted" style="font-size:12px"><span style="color:#9ec49a">\u25cf</span> bids &nbsp;<span style="color:#d9b36a">\u25cf</span> asks &nbsp;\u00b7 big dot = the pick &nbsp;\u00b7 EV/day at each resting price</div>';
 var notes='';
 ['BUY','SELL'].forEach(function(sd){
  var e=(lad.sides||{})[sd]||{};
  if(e.note)notes+='<div class="muted" style="font-size:12px">'+(sd==='BUY'?'bids: ':'asks: ')+esc(e.note)+'</div>';
 });
 legend+=notes;
 var pkRows=all.filter(function(r){return r.picked;});
 var pk;
 if(tri&&(tri.picks||[]).length){
  pk='<div style="font-size:16px;margin:6px 0"><b>Decision: '+tri.picks.map(function(r){
   return (r.s==='BUY'?'bid':'ask')+' '+r.q+' @ '+wFmtC(r.px)+' \u2192 $'+r.ev.toFixed(2)+'/day';
  }).join(' \u00b7 ')+'</b><div class="muted" style="font-size:12px;font-weight:400">'+esc(tri.why||'')
   +'<br>this is what the engine decided WHEN IT LOOKED. The "best by EV/day" line below re-prices the same ladder against the book as it is right now, so the two differ whenever the book has moved since.'
   +'</div></div>';
 }else if(pkRows.length){
  // NOT a decision: the engine recorded no pick for this market, so
  // this is the ladder's best spot priced RIGHT NOW, against a book
  // the scan may not have seen yet. Labelling it "Decision" told the
  // owner the engine had acted when it had not (2026-08-23).
  pk='<div style="font-size:16px;margin:6px 0"><b>Not taken \u2014 best spot if it were priced now: '+pkRows.map(function(r){
   return (bids.indexOf(r)>=0?'bid':'ask')+' '+r.qty+' @ '+wFmtC(r.px)+' \u2192 $'+r.ev.toFixed(2)+'/day, '+Math.round(r.p_fill*100)+'% fill odds';
  }).join(' \u00b7 ')+'</b><div class="muted" style="font-size:12px;font-weight:400">the engine placed nothing here \u2014 its own last look at this market is what the verdict above reports</div>'+pkRows.map(function(r){return r.why?'<div class="muted" style="font-size:12px;font-weight:400">'+esc(r.why)+'</div>':'';}).join('')+'</div>';
 }else{
  pk='<div class="muted" style="margin:6px 0"><b>Decision:</b> nothing here clears the bar</div>';
 }
 var evs='';
 [['bids',bids],['asks',asks]].forEach(function(pr){
  var rs=pr[1].slice().sort(function(x,y){return y.ev-x.ev;}).slice(0,3);
  if(rs.length)evs+='<div class="muted" style="font-size:12px">best '+pr[0]+' by EV/day: '+rs.map(function(r2){return pc(r2.px)+' \u2192 $'+r2.ev.toFixed(2);}).join(' \u00b7 ')+'</div>';
 });
 var oursAt={};(b.ours||[]).forEach(function(o){oursAt[o.side+(o.price*100).toFixed(1)]=o;});
 var bt;
 if(tri&&tri.book&&(tri.book.b||tri.book.a)){
  bt=wSnapT(tri,oursAt);
 }else{
  bt='<table><tr><th class="r">bid size</th><th class="r">bid</th><th>ask</th><th>ask size</th></tr>';
  var nrows=Math.min(Math.max((b.bids||[]).length,(b.asks||[]).length),6);
  for(var i=0;i<nrows;i++){
   var bd=(b.bids||[])[i],ak=(b.asks||[])[i];
   var bm=bd&&oursAt['BUY'+(bd[0]*100).toFixed(1)]?' \u25CF':'';
   var am=ak&&oursAt['SELL'+(ak[0]*100).toFixed(1)]?' \u25CF':'';
   bt+='<tr><td class="r">'+(bd?fmtsz(bd[1]):'')+'</td><td class="r">'+(bd?pc(bd[0])+bm:'')+'</td>'
     +'<td>'+(ak?pc(ak[0])+am:'')+'</td><td>'+(ak?fmtsz(ak[1]):'')+'</td></tr>';
  }
  bt+='</table>';
 }
 var mine='';
 if((b.ours||[]).length){
  mine='<div style="margin:4px 0"><b>Where I am:</b> '+b.ours.map(function(o){
   var tag=o.purpose==='sell'?'exit':o.purpose;
   var earn=(o.est&&o.est>=0.005)?' \u2014 earning ~$'+o.est.toFixed(2)+'/day':(o.verdict?' \u2014 '+esc(o.verdict):' \u2014 earning $0');
   return (o.side==='BUY'?'bid':'ask')+' '+o.qty+' @ '+wFmtC(o.price)+' ['+tag+']'+earn;
  }).join(' \u00b7 ')+'</div>';
 }else{mine='<div class="muted" style="margin:4px 0">no orders resting here yet</div>';}
 if(b.position&&b.position.qty){
  var pq=b.position.qty,pc2=b.position.cost;
  mine+='<div><b>Position:</b> '+pq+' shares'+(pq>0?' at '+((pc2/pq)*100).toFixed(1)+'c average':' (short)')+'</div>';
 }
 return head+s+legend+pk+evs+bt+'<div class="hint">\u25CF marks our order</div>'+mine;
}
function wShow(t){
 fetch('/book.json?m='+encodeURIComponent(t.market),{headers:hdrs(),cache:'no-store'})
  .then(function(r){return r.json();}).then(function(b){
   var el=document.getElementById('spot');
   if(!el)return;
   el.style.opacity=0;
   setTimeout(function(){
    window._watchCurHTML=wCard(nm(window._watchD,t.market),t.market,b,t);
    el.innerHTML=window._watchCurHTML;
    el.style.opacity=1;
    wCount();
   },200);
  }).catch(function(){});
}
function wCount(){
 var el=document.getElementById('wn');
 if(el)el.innerHTML=(window._watchBuf||[]).length
  ? 'next market \u25b8 ('+window._watchBuf.length+' saved)'
  : 'caught up \u2014 the sweep is scoring more';
}
function wNext(){
 var q=window._watchBuf||[];
 if(!q.length){wCount();return;}
 var t=q.shift();
 window._watchSeen=window._watchSeen||{};
 window._watchSeen[t.market]=t.ts;
 wShow(t);
}
function render(d){
 window._watchD=d;
 window._watchBuf=window._watchBuf||[];
 window._watchSeen=window._watchSeen||{};
 var buf=window._watchBuf;
 ['politics'].forEach(function(k){
  var s=(d.summaries||{})[k]||{};
  (s.triage_feed||[]).forEach(function(t){
   if((window._watchSeen[t.market]||0)>=t.ts)return;
   for(var i=0;i<buf.length;i++){
    if(buf[i].market===t.market){if(t.ts>buf[i].ts)buf[i]=t;return;}
   }
   if(buf.length<25)buf.push(t);
  });
 });
 buf.sort(function(a,b){return a.ts-b.ts;});
 if(!window._watchCurHTML&&buf.length)setTimeout(wNext,300);
 return '<div class="card"><div class="muted">One politics market per tap \u2014 what the engine saw as it considered. Up to 25 verdicts wait; the queue refills as the sweep scores.</div>'
  +'<div style="margin:8px 0"><button onclick="wNext()" id="wn" style="font-size:16px;padding:10px 16px;width:100%">next market \u25b8'+(buf.length?' ('+buf.length+' saved)':'')+'</button></div>'
  +'<div id="spot" style="transition:opacity 0.2s ease;min-height:280px">'+(window._watchCurHTML||'')+'</div></div>';
}
"""


GRAPH_JS = """
function fmtT(ts){var d=new Date(ts*1000);return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}
var FAMS=[['Politics','est_politics','#7fd77f'],['Bonds','est_bonds','#4a90e2'],
          ['College football','est_cfb','#e0b83a'],
          ['NFL','est_nfl','#6fa8dc'],['NBA','est_nba','#c08fd0'],
          ['Game day','est_gameday','#f08a5d']];
function stacked(d,win){
 var now=Date.now()/1000, series=[], names=[];
 FAMS.forEach(function(f){
  var dots=((d[f[1]]||{}).dots)||[];
  if(win)dots=dots.filter(function(x){return x[0]>=now-win;});
  if(dots.length)  {series.push(dots); names.push(f);}
 });
 if(!series.length)return '<div class="card muted">no samples yet</div>';
 var stamps={};
 series.forEach(function(ds){ds.forEach(function(x){stamps[Math.round(x[0]/20)*20]=1;});});
 var ts=Object.keys(stamps).map(Number).sort(function(a,b){return a-b;});
 if(ts.length<2)return '<div class="card muted">not enough samples yet</div>';
 var vals=series.map(function(ds){
  var m={};ds.forEach(function(x){m[Math.round(x[0]/20)*20]=x[1];});
  var last=0;return ts.map(function(t){if(m[t]!=null)last=m[t];return last;});
 });
 // bonds are politics orders too (owner, 2026-09-03: "separate out
 // earnings from bonds, in blue, vs politics"): their rate comes out
 // of the politics band so the stack still totals the same
 var ip=-1,ib=-1;names.forEach(function(f,i){if(f[1]==='est_politics')ip=i;if(f[1]==='est_bonds')ib=i;});
 if(ip>=0&&ib>=0)vals[ip]=vals[ip].map(function(v,i){return Math.max(v-vals[ib][i],0);});
 var tot=ts.map(function(_,i){var v=0;vals.forEach(function(col){v+=col[i];});return v;});
 var ymax=Math.max.apply(null,tot)*1.08||1;
 var W=340,H=190,PL=36,PB=18,PT=8,PR=6,t0=ts[0],t1=ts[ts.length-1],span=Math.max(t1-t0,60);
 function X(t){return PL+(W-PL-PR)*(t-t0)/span;}
 function Y(v){return PT+(H-PT-PB)*(1-v/ymax);}
 var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="earning rate by family, stacked">';
 [0,0.5,1].forEach(function(f){var y=Y(ymax*f);
  s+='<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" stroke="rgba(255,255,255,0.08)"/>'
   +'<text x="'+(PL-4)+'" y="'+(y+3)+'" text-anchor="end" font-size="8" fill="rgba(255,255,255,0.45)">$'+(ymax*f).toFixed(0)+'</text>';});
 var base=ts.map(function(){return 0;});
 for(var k=vals.length-1;k>=0;k--){
  var top=base.map(function(b,i){return b+vals[k][i];});
  var pts=ts.map(function(t,i){return X(t).toFixed(1)+','+Y(top[i]).toFixed(1);}).join(' ');
  var back=ts.map(function(t,i){return X(t).toFixed(1)+','+Y(base[i]).toFixed(1);}).reverse().join(' ');
  s+='<polygon points="'+pts+' '+back+'" fill="'+names[k][2]+'" fill-opacity="0.5"/>';
  s+='<polyline points="'+pts+'" fill="none" stroke="'+names[k][2]+'" stroke-width="1.4"/>';
  base=top;
 }
 [t0,(t0+t1)/2,t1].forEach(function(t,i){
  s+='<text x="'+X(t)+'" y="'+(H-4)+'" text-anchor="'+(i===0?'start':i===2?'end':'middle')+'" font-size="8" fill="rgba(255,255,255,0.45)">'+fmtT(t)+'</text>';});
 s+='</svg>';
 var leg=names.map(function(f,i){
  var v=vals[i][vals[i].length-1];
  return '<span class="pill" style="border-left:4px solid '+f[2]+'">'+esc(f[0])+' '+usd(v)+'</span>';}).join(' ');
 return '<div class="card"><div class="hero">'+usd(tot[tot.length-1])+'<span class="u">/day</span></div>'
  +'<div style="margin:2px 0 6px">'+leg+'</div>'+s+'</div>';
}
function mWin(sec){window._meterWin=sec;
 if(window._meterD){var el=document.getElementById('view');if(el)el.innerHTML=render(window._meterD);}}
function render(d){
 window._meterD=d;
 if(d.starting)return bootCard(d);
 var w=window._meterWin||0;
 var b=function(sec,label){return '<button class="'+((window._meterWin||0)===sec?'on':'')+'" onclick="mWin('+sec+')">'+label+'</button>';};
 var earned=0;
 fams(d).forEach(function(kv){earned+=(kv[1].earned_today||0);});
 var out='<div class="tabs">'+b(900,'15 min')+b(0,'today')+'</div>';
 out+=stacked(d,w);
 out+='<div class="card"><div class="kpi">'
  +'<div><div class="v">'+usd(earned)+'</div><div class="l">earned today</div></div>';
 fams(d).forEach(function(kv){
  out+='<div><div class="v">'+usd(kv[1].earned_today||0)+'</div><div class="l">'+esc(kv[0])+'</div></div>';});
 out+='</div></div>';
 return out;
}
"""

PAY_JS = """
function taxLine(gross){
 var res=gross*0.22;
 return '<div class="card"><div class="kpi">'
  +'<div><div class="v">'+usd(gross)+'</div><div class="l">gross paid</div></div>'
  +'<div><div class="v warn">'+usd(res)+'</div><div class="l">set aside \u2014 tax at 22%</div></div>'
  +'<div><div class="v">'+usd(gross-res)+'</div><div class="l">yours after</div></div>'
  +'</div></div>';
}
function render(d){
 if(d.starting)return bootCard(d);
 var rows=(d.grades||[]);
 var pt=d.paid_total;
 if(!pt){var tot=0,nd=0;rows.forEach(function(r){if(r.actual!=null){tot+=r.actual;nd++;}});
  pt=nd?{usd:tot,days:nd,since:''}:{usd:0,days:0,since:''};}
 var out=taxLine(pt.usd||0);
 out+='<div class="card"><div class="tabs"><button onclick="ckrw()">Check for new payouts</button></div>'+rwcard()+'</div>';
 var mx=1;rows.forEach(function(r){mx=Math.max(mx,r.est||0,r.actual||0);});
 var body='';
 rows.slice().reverse().forEach(function(r){
  body+='<div style="margin:12px 0 0"><b>'+esc(r.day)+'</b>'
   +'<div class="kpi" style="margin:2px 0 4px">'
   +'<div><div class="v">'+(r.actual==null?'\u2014':usd(r.actual))+'</div><div class="l">paid</div></div>'
   +'<div><div class="v muted">'+(r.est==null?'\u2014':usd(r.est))+'</div><div class="l">estimate</div></div>'
   +(r.actual!=null&&r.est?'<div><div class="v">'+(r.actual/r.est).toFixed(2)+'x</div><div class="l">paid/est</div></div>':'')
   +'</div>';
  if(r.est!=null){body+='<div class="mtrack"><div class="mfill" style="width:'+(100*(r.est||0)/mx)+'%"></div></div>';}
  if(r.actual!=null){body+='<div class="mtrack"><div class="mfill" style="width:'+(100*(r.actual||0)/mx)+'%;background:#8a7a2f"></div></div>';}
  body+='</div>';
 });
 out+='<div class="card"><b>'+usd(pt.usd)+'</b> <span class="muted">over '+pt.days+' posted days</span>'+body+'</div>';
 return out;
}
function rwcard(){
 if(window._rwbusy)return '<div class="muted">checking\u2026</div>';
 var j=window._rw;                       // ONLY after the button (owner)
 if(!j)return '<div class="muted">Press to check.</div>';
 if(!j.ok)return '<div class="bad">'+esc(j.note||'failed')+'</div>';
 var h='';
 var nr=(j.new_rows||[]);
 h+='<div class="kpi"><div><div class="v">'+(j.new_count||0)+'</div><div class="l">new rows</div></div></div>';
 (j.progress||[]).forEach(function(p){
  var pct=(p.pct==null?0:p.pct);
  h+='<div class="sub"><b>'+esc((p.day||'').slice(5))+'</b>: '+p.appeared+' of '+p.expected+' markets we estimated have appeared'+(p.expected?' ('+pct+'%)':'')+(p.pending?' \\u00b7 '+p.pending+' pending':'')+(p.paid?' \\u00b7 '+p.paid+' paid':'')+(p.extra?' \\u00b7 '+p.extra+' posted that we did not estimate':'')+'</div>';
  h+='<div class="mtrack"><div class="mfill" style="width:'+pct+'%"></div></div>';
 });
 var dk=Object.keys(j.days||{}).sort().reverse().slice(0,4);
 if(dk.length){h+='<div class="sub">'+dk.map(function(x){return x.slice(5)+' '+usd(j.days[x]);}).join(' \u00b7 ')+'</div>';}
 nr.slice().reverse().forEach(function(r){
  h+='<div class="sub">'+esc(r.day)+' \u00b7 <b>'+usd(r.usd)+'</b> \u00b7 '+esc(r.name)+'</div>';});
 if(!nr.length)h+='<div class="muted">Nothing new.</div>';
 return h;
}
function ckrw(){
 window._rwbusy=true;
 if(window._d)document.getElementById('view').innerHTML=render(window._d);
 post({op:'refresh_rewards'},function(j){window._rwbusy=false;window._rw=j;
  if(window._d)document.getElementById('view').innerHTML=render(window._d);});
}
"""

SILVER_JS = """
function render(d){
 var sv=d.silver||{};var out='';
 out+='<div class="card"><b>The Silver model, as this system sees it</b>';
 out+='<div class="sub">'+(sv.senate_races||0)+' senate and '+(sv.gov_races||0)+' governor races. Tables checked '+(sv.tables_age_min==null?'?':sv.tables_age_min)+' min ago.'+(sv.official_age_h!=null?' Seat simulations from '+sv.official_age_h+' hours ago ('+esc(sv.official_source||'')+').':'')+'</div>';
 out+='<div class="sub">Model coverage in your scope: <b>'+(sv.priced||0)+'</b> markets priced, '+(sv.unpriced||0)+' without a model number (margins, primaries, the 2028 slate) \\u2014 those run on evidence alone and every card says so.</div>';
 out+='<div class="hint">The feed carries the model\\u2019s odds, not the polls behind them. So this page shows every MOVE in the odds and when this system saw it. The tables update when Silver posts new polling \\u2014 about daily in season, checked every 6 hours. The simulations update only when he reruns the model; past 5 days old, the system widens its bands instead of trusting them alone.</div>';
 out+='</div>';
 var log=(d.silver_log||[]).slice().reverse();
 out+='<div class="card"><b>Model moves seen</b>';
 if(!log.length)out+='<div class="muted">None yet \\u2014 the log starts now and fills as the odds move.</div>';
 var day='';
 log.slice(0,60).forEach(function(r){
  var dt=new Date((r.ts||0)*1000);var dl=dt.toLocaleDateString([], {month:'short',day:'numeric'});
  if(dl!==day){day=dl;out+='<div class="tri-h" style="margin-top:8px">'+esc(dl)+'</div>';}
  var dd=(r.new-r.old);
  out+='<div class="sub">'+esc(r.name)+' ('+esc(r.chamber)+'): R '+r.old+'% \\u2192 '+r.new+'% <span class="'+(Math.abs(dd)>=2?'warn':'muted')+'">('+(dd>0?'+':'')+dd.toFixed(1)+')</span> <span class="muted">'+when(r.ts)+'</span></div>';
 });
 out+='</div>';
 return out;
}
"""

FILLS_JS = """
var _MO=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function fWhen(ts){var d=new Date(ts*1000);return _MO[d.getMonth()]+' '+d.getDate()+', '+('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}
function fUsd(v){return (v<-0.005?'\\u2212$':'+$')+Math.abs(v).toFixed(2);}
function fRest(h){if(h==null)return '';return h<1?Math.round(h*60)+' min':(h<48?h.toFixed(1)+' h':(h/24).toFixed(1)+' days');}
function fParts(f){
 var claimed=(f.est_day&&f.rested_h!=null)?f.est_day*f.rested_h/24:0;
 // posted-grounded rewards (owner, 2026-08-27): once the exchange
 // posts a rested day, the card's rewards use the REAL pay share;
 // only days not yet posted keep the claim
 var earned=(f.posted_usd!=null)?(f.posted_usd+(f.claim_unposted||0)):claimed;
 var oq=f.open_qty!=null?f.open_qty:f.qty;
 var flat=f.pos_now!=null&&Math.abs(f.pos_now)<0.005;
 var open=!f.stray_close&&oq>0.005&&!flat;
 var reconciled=!f.stray_close&&oq>0.005&&flat;
 var mk=0;
 if(open){
  if(f.side==='BUY'&&f.now_bid!=null)mk=(f.now_bid-f.px)*oq;
  if(f.side==='SELL'&&f.now_ask!=null)mk=(f.px-f.now_ask)*oq;
 }
 var net=(f.realized||0)+earned+(open?mk+(f.exit_earned||0):0);
 return {open:open,oq:oq,mark:mk,earned:earned,net:net,
         reconciled:reconciled,
         rate:open?(f.exit_rate||0):0};
}
function fTint(net){
 if(net>=1)return 'rgba(96,170,96,0.32)';
 if(net>=0.05)return 'rgba(96,170,96,0.16)';
 if(net<=-1)return 'rgba(200,84,84,0.30)';
 if(net<=-0.05)return 'rgba(200,84,84,0.15)';
 return 'rgba(255,255,255,0.04)';
}
function fFlip(el){
 var faces=[el.querySelector('.ffront'),el.querySelector('.fback'),el.querySelector('.flive')];
 var cur=parseInt(el.getAttribute('data-face')||'0',10);
 var n=faces[2]?3:2;
 var next=(cur+1)%n;
 el.style.transition='transform 0.14s ease';
 el.style.transform='rotateY(90deg)';
 setTimeout(function(){
  for(var i=0;i<faces.length;i++){if(faces[i])faces[i].style.display=(i===next?'':'none');}
  el.setAttribute('data-face',''+next);
  if(next===2)lvOpen(el);else if(cur===2)lvShut(true);
  el.style.transform='rotateY(0deg)';
 },140);
}
function lvOpen(el){
 // the card's live face rides the shared live line (lvBox); the card
 // adds its corner light, and hand ops refresh the WHOLE card
 var box=el.querySelector('.flive');
 if(!box)return;
 var lt=el.querySelector('.lvlive');if(lt)lt.style.display='';
 window._lvCard=el;
 lvBox(el.getAttribute('data-m'),box,function(){
  try{var l2=el.querySelector('.lvlive');if(l2)l2.style.display='none';}catch(e){}
  window._lvCard=null;
 });
 window._lvAfterOp=lvRefresh;
}
function lvClose(){lvShut();}
function lvRefresh(){
 // an order changed by hand: bring the WHOLE card up to date, not
 // just the live face - front and story redrawn from a fresh journal
 var el=window._lvCard;
 if(!el)return;
 fetch('/fills.json',{headers:hdrs(),cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
  window._fillsJ=j;
  if(window._lvCard!==el)return;
  var m=el.getAttribute('data-m'),ts=el.getAttribute('data-ts');
  var f=null;
  (j.fills||[]).forEach(function(x){if(x.market===m&&''+x.ts===ts)f=x;});
  if(!f)return;
  var p=fParts(f);
  var a=el.querySelector('.ffront'),bk=el.querySelector('.fback');
  if(a)a.innerHTML=fFront(f,p);
  if(bk)bk.innerHTML=fBack(f,p);
  el.style.background=fTint(p.net);
  var dt=(Date.now()/1000)-(window._fillT0||0);
  var els=el.querySelectorAll('.fnet');
  for(var i=0;i<els.length;i++){
   var r=parseFloat(els[i].getAttribute('data-rate')||'0');
   var b0=parseFloat(els[i].getAttribute('data-base')||'0');
   if(r>0.005)els[i].setAttribute('data-base',(b0-r*dt/86400).toFixed(4));
  }
 }).catch(function(){});
}
function fFront(f,p){
 var st=f.stray_close?'CLOSED OUT':(p.open?'OPEN \\u00b7 so far':'CLOSED');
 var tick=p.open&&p.rate>0.005?' <span class="muted" style="font-size:12px">+'+p.rate.toFixed(2)+'/day ticking</span>':'';
 return '<div style="font-size:17px;line-height:1.25"><b>'+esc(f.name||f.market)+'</b></div>'
  +'<div style="font-size:21px;margin:3px 0"><b>'+(f.side==='BUY'?'bought':'sold')+' '+f.qty+' @ '+pc(f.px)+'</b></div>'
  +'<div style="font-size:26px;margin:2px 0"><b class="fnet" data-base="'+p.net.toFixed(4)+'" data-rate="'+p.rate.toFixed(4)+'">'+fUsd(p.net)+'</b> <span class="muted" style="font-size:13px">'+st+'</span>'+tick+'</div>'
  +'<div class="muted" style="font-size:12px">'+esc(f.family||'')+' \\u00b7 '+fWhen(f.ts)+' \\u00b7 tap for the story</div>';
}
function fBack(f,p){
 var out='<div><b>'+esc(f.name||f.market)+'</b> <span class="muted" style="font-size:12px">'+esc(f.family||'')+'</span></div>';
 out+='<div style="font-size:15px;margin:2px 0"><b>'+(f.side==='BUY'?'bought':'sold')+' '+f.qty+' @ '+pc(f.px)+'</b> \\u00b7 '+fWhen(f.ts)+(f.stray_close?' \\u00b7 an exit':'')+'</div>';
 var plan='The order: '+esc(f.why||'(no note)');
 if(f.est_day)plan+=' \\u2014 estimated ~$'+f.est_day.toFixed(2)+'/day while resting';
 if(f.rested_h!=null)plan+=' \\u00b7 rested '+fRest(f.rested_h)+' before filling';
 out+='<div class="muted" style="margin:2px 0">'+plan+'</div>';
 var v='';
 if(f.fair!=null)v='Model said '+pc(f.fair);
 else if(f.band)v='No model \\u2014 evidence put value between '+f.band[0].toFixed(0)+'c and '+f.band[1].toFixed(0)+'c';
 else v='No independent sense of value at the time';
 if(f.touch_bid!=null||f.touch_ask!=null)v+=' \\u00b7 book was '+(f.touch_bid!=null?pc(f.touch_bid):'\\u2014')+'/'+(f.touch_ask!=null?pc(f.touch_ask):'\\u2014');
 out+='<div style="margin:2px 0">'+v+'</div>';
 var lot=(f.conc!=null)?-f.conc*f.qty:null;
 var cl;
 if(f.conc==null)cl='Value unknown then \\u2014 no concession math';
 else if(f.conc>0.0005)cl='Paid '+(f.conc*100).toFixed(1)+'c past value \\u2192 '+fUsd(lot)+' on the lot';
 else if(f.conc<-0.0005)cl='Filled '+(-f.conc*100).toFixed(1)+'c inside value \\u2192 '+fUsd(lot)+' on the lot';
 else cl='Filled right at value';
 if(f.posted_usd!=null){
  cl+=' \\u00b7 rewards POSTED by the exchange: $'+f.posted_usd.toFixed(2);
  if((f.claim_graded||0)>f.posted_usd+0.05)cl+=' <span class="warn">(was claimed $'+f.claim_graded.toFixed(2)+')</span>';
  if((f.claim_unposted||0)>0.005)cl+=' \\u00b7 ~$'+f.claim_unposted.toFixed(2)+' more claimed for days not posted yet';
 }else if(p.earned){cl+=' \\u00b7 claimed ~$'+p.earned.toFixed(2)+' in rewards while it rested (nothing posted yet)';}
 out+='<div style="margin:2px 0">'+cl+'</div>';
 if((f.closes||[]).length){
  out+='<div style="margin:2px 0">'+f.closes.map(function(c){
   return '\\u21b3 '+(f.side==='BUY'?'sold':'bought back')+' '+c.qty+' @ '+pc(c.px)+' \\u00b7 '+fWhen(c.ts)+' \\u2192 '+fUsd(c.pl)+(c.kind==='hand'?' \\u00b7 your own trade':'');
  }).join('<br>')+'</div>';
 }
 if(f.stray_close&&f.purpose==='hand'){
  out+='<div class="muted" style="margin:2px 0">Your own trade, from the exchange\\u2019s record \\u2014 no matching purchase in the journal, so no round-trip math.</div>';
 }else if(f.stray_close){
  out+='<div class="muted" style="margin:2px 0">This closed stock bought before the journal began \\u2014 no matching purchase on record, so no round-trip math.</div>';
 }else if(p.reconciled){
  out+='<div style="margin:2px 0"><b>Closed by reconciliation</b> \\u2014 the exchange shows this market flat, so the remaining '+p.oq+' closed outside the journal (a correction or an untracked fill; no price recorded). Realized covers only the recorded closes: '+fUsd(f.realized||0)+'.</div>';
 }else if(!p.open){
  out+='<div style="margin:2px 0"><b>Round trip closed \\u2014 realized '+fUsd(f.realized||0)+(p.earned?' \\u00b7 plus ~$'+p.earned.toFixed(2)+' rewards':'')+'</b></div>';
 }else{
  var nw='Now: book '+(f.now_bid!=null?pc(f.now_bid):'\\u2014')+'/'+(f.now_ask!=null?pc(f.now_ask):'\\u2014')+' \\u00b7 position '+(f.pos_now!=null?f.pos_now:'?');
  if(p.oq<f.qty)nw+='<br>Still open: '+p.oq+' of '+f.qty+' \\u00b7 realized so far '+fUsd(f.realized||0);
  nw+='<br>The open part marks '+fUsd(p.mark)+' today';
  if(f.exit_resting)nw+='<br>Exit resting \\u2014 earning ~$'+(f.exit_rate||0).toFixed(2)+'/day, ~$'+(f.exit_earned||0).toFixed(2)+' since it rested';
  else nw+='<br>No exit resting yet \\u2014 the open part earns $0.00/day until one rests';
  out+='<div style="margin:2px 0">'+nw+'</div>';
 }
 out+='<div class="muted" style="font-size:12px;margin-top:4px">'+(p.open?'tap again for the LIVE book \\u2014 move orders, close out':'tap to flip back')+'</div>';
 return out;
}
function fCard(f){
 var p=fParts(f);
 var live=p.open&&f.market;
 return '<div class="card" data-m="'+esc(f.market)+'" data-ts="'+f.ts+'" data-face="0" onclick="fFlip(this)" style="cursor:pointer;position:relative;background:'+fTint(p.net)+'">'
  +(live?'<span class="lvlive" style="display:none">\\u25CF LIVE</span>':'')
  +'<div class="ffront">'+fFront(f,p)+'</div>'
  +'<div class="fback" style="display:none">'+fBack(f,p)+'</div>'
  +(live?'<div class="flive" style="display:none"></div>':'')
  +'</div>';
}
function fTick(){
 if(!document.querySelectorAll)return;
 var dt=(Date.now()/1000)-(window._fillT0||0);
 var els=document.querySelectorAll('.fnet');
 for(var i=0;i<els.length;i++){
  var r=parseFloat(els[i].getAttribute('data-rate')||'0');
  if(r>0.005){
   var b=parseFloat(els[i].getAttribute('data-base')||'0');
   els[i].textContent=fUsd(b+r*dt/86400);
  }
 }
}
function fDraw(){
 if(window._liveOpen)lvClose();   // a redraw tears the cards down —
                                  // never leave a stream running blind
 var el=document.getElementById('fl');
 var j=window._fillsJ;
 if(!el||!j)return;
 var pend=(j.pending||[]);
 if(!j.ok||(!(j.fills||[]).length&&!pend.length)){el.innerHTML='<div class="card muted">No purchases on record yet \\u2014 the journal starts with the next fill.</div>';return;}
 var open=[],closed=[];
 (j.fills||[]).forEach(function(f){(fParts(f).open?open:closed).push(f);});
 var tab=(window._fillTab!=null?window._fillTab:1);
 if(tab===1&&!open.length&&(closed.length||pend.length))tab=0;
 var btn=function(t,label,n){
  var on=tab===t;
  return '<button onclick="fTabSet('+t+')" style="font-size:15px;padding:8px 18px;margin-right:8px'+(on?';font-weight:bold;text-decoration:underline':'')+'">'+label+' <span style="opacity:0.7">'+n+'</span></button>';
 };
 var greens=j.open_hidden||0;
 // counts are the TRUE totals, not the number that fit in the list
 var nOpen=(j.open_total!=null?j.open_total:open.length);
 var nClosed=(j.closed_total!=null?j.closed_total:closed.length)+pend.length;
 var out='<div style="margin:2px 0 8px 0">'+btn(1,'open',nOpen)+(greens?'<span style="color:#9ec49a;font-size:13px;margin-right:8px">+'+greens+' in profit</span>':'')+btn(0,'closed',nClosed)+'</div>';
 var hr=j.hidden_reconciled||0;
 if(hr)out+='<div class="muted" style="margin:-4px 0 8px 0">'+hr+' more closed without a recorded sale \\u2014 hidden; the exchange\\u2019s record of them is in data/trades.csv</div>';
 var list=tab===1?open:closed;
 if(tab===0&&pend.length){
  out+=pend.map(function(p){
   return '<div class="card" style="opacity:0.55;border-left:3px solid #8a8a8a"><b>'+esc(p.name||p.market)+'</b> <span class="muted">'+esc(p.family||'')+'</span>'
    +'<div class="muted">'+(p.side==='BUY'?'bought':'sold')+' '+p.qty+' @ '+pc(p.px)+' \\u00b7 '+when(p.ts)+'</div>'
    +'<div class="muted"><b>waiting for the position feed to close out</b> \\u2014 the order left the book; the trade history or the feed confirms it within a few minutes</div></div>';
  }).join('');
 }
 if(!list.length&&!(tab===0&&pend.length))out+='<div class="card muted">nothing '+(tab===1?'open':'closed')+' right now</div>';
 else out+=list.map(fCard).join('');
 window._fillT0=Date.now()/1000;
 el.innerHTML=out;
}
function fTabSet(t){window._fillTab=t;fDraw();}
function fbackfill(){
 var b=document.getElementById('thout');
 if(b)b.innerHTML='<div class="muted">comparing the exchange record with the journal\\u2026</div>';
 post({op:'backfill',days:3,dry_run:true},function(j){
  if(!b)return;
  if(!j||!j.ok){b.innerHTML='<div class="bad">'+esc((j&&j.note)||'failed')+'</div>';return;}
  if(!j.added){b.innerHTML='<div class="muted">nothing missing \\u2014 the journal already matches the exchange for the last '+j.days+' days</div>';return;}
  var lines=(j.sample||[]).map(function(x){return '<div class="muted">'+esc(x)+'</div>';}).join('');
  b.innerHTML='<div><b>'+j.added+' fills</b> ('+j.shares+' shares) are in the exchange record but not the cards:</div>'+lines
   +'<div style="margin:6px 0"><button onclick="fbackapply()">Add them to the cards</button></div>';
 });
}
function fbackapply(){
 var b=document.getElementById('thout');
 if(b)b.innerHTML='<div class="muted">writing\\u2026</div>';
 post({op:'backfill',days:3,dry_run:false},function(j){
  if(!b)return;
  b.innerHTML=(j&&j.ok)?'<div class="muted">added '+j.added+' fills ('+j.shares+' shares) to the cards</div>'
   :'<div class="bad">'+esc((j&&j.note)||'failed')+'</div>';
 });
}
function ftrades(){
 var b=document.getElementById('thout');
 if(b)b.innerHTML='<div class="muted">asking the exchange\\u2026</div>';
 post({op:'fetch_trades'},function(j){
  if(!b)return;
  b.innerHTML=j&&j.ok
   ? '<div class="muted">'+j.activities+' activities read, '+j.parsed+' ours, +'+j.added+' new rows written to data/trades.csv</div>'
   : '<div class="bad">'+esc((j&&j.note)||'failed')+'</div>';
 });
}
function render(d){
 fetch('/fills.json',{headers:hdrs(),cache:'no-store'}).then(function(r){return r.json();}).then(function(j){
  window._fillsJ=j;
  fDraw();
  if(!window._fillTick)window._fillTick=setInterval(fTick,1000);
 }).catch(function(){});
 return '<div class="card"><div class="muted">One card per purchase \\u2014 open lots tick as their exits earn; the color grades how it went. Tap a card for the story. Closed cards stay 3 days; open ones stay until they turn profitable.</div>'
  +'<div style="margin:8px 0 0"><button onclick="ftrades()">Refresh transaction history</button> <button onclick="fbackfill()">Recover missing fills</button> <span class="muted">\\u2014 the exchange\\u2019s own record, into data/trades.csv and the cards</span></div><div id="thout"></div></div>'
  +'<div id="fl"><div class="card muted">loading\\u2026</div></div>';
}
"""

# (title, nav highlight, page JS, sub-nav group). Plan and model keep
# their routes for a bookmark but are off the bar (owner, 2026-08-31).
SURVEY_JS = r"""
// A continuously cycling leaderboard of market prefixes (owner,
// 2026-08-31). It samples a few markets every cycle, at random within
// each prefix, and ranks on the MEDIAN share of a side per dollar at
// risk — cfb holds 13% of a side for 41 cents, about 32 on this scale.
// Never on the max: one lucky thin book would crown a prefix.
function svBest(i){window._svOpen=(window._svOpen===i?null:i);
 if(window._d)document.getElementById('view').innerHTML=render(window._d);}
function svNum(x,d){return (x||0).toFixed(d==null?2:d);}
function render(d){
 if(d.starting)return bootCard(d);
 var s=d.survey;
 if(!s)return '<div class="card muted">The survey has not run yet.</div>';
 var sm=s.sampler||{}, full=(s.frame||'').indexOf('NOT a full frame')<0;
 var out='<div class="card"><b>Prefix leaderboard</b>'
  +'<div class="muted" style="font-size:12px">Samples a few markets every cycle, at random within each prefix. Reads books and terms; it places nothing.</div>'
  +'<div class="kpi" style="margin-top:8px">'
  +'<div><div class="v">'+(sm.population||0).toLocaleString()+'</div><div class="l">markets in frame</div></div>'
  +'<div><div class="v">'+(sm.prefixes||0)+'</div><div class="l">prefixes</div></div>'
  +'<div><div class="v">'+(s.ranked||[]).length+'</div><div class="l">ranked</div></div>'
  +'<div><div class="v">'+(s.sampling||[]).length+'</div><div class="l">still sampling</div></div>'
  +'</div>'
  +'<div class="vrd'+(full?'':' warn')+'">'+esc(s.frame||'frame not loaded yet')+'</div>'
  +'<div class="muted" style="font-size:12px">seed '+esc(String(sm.seed))
  +' \u00b7 '+(sm.left_this_pass||0).toLocaleString()+' left in this pass'
  +' \u00b7 '+(sm.passes||0)+' passes'
  +(s.at?' \u00b7 last sampled '+when(s.at):'')+'</div></div>';
 var r=s.ranked||[];
 if(r.length){
  out+='<div class="card"><table><tr><th>market kind</th><th class="r">n</th>'
   +'<th class="r">$/day per $1</th><th class="r">side per $1</th>'
   +'<th class="r">share</th><th class="r">touch</th></tr>';
  r.forEach(function(k,i){
   var good=k.median_ypd>=0.16;
   out+='<tr'+((k.best||[]).length?' style="cursor:pointer" onclick="svBest('+i+')"':'')+'>'
    +'<td>'+esc(k.prefix)+((k.best||[]).length?' <span class="muted">\u25be</span>':'')+'</td>'
    +'<td class="r">'+k.n+'</td>'
    +'<td class="r'+(good?' ok':'')+'"><b>'+svNum(k.median_ypd,3)+'</b></td>'
    +'<td class="r muted">'+svNum(k.median_spd,3)+'</td>'
    +'<td class="r">'+svNum(k.median_share_pct,3)+'%</td>'
    +'<td class="r">'+(k.median_touch||0).toLocaleString()+'</td></tr>';
   if(window._svOpen===i){
    (k.best||[]).forEach(function(b){
     out+='<tr><td colspan="6" class="muted" style="font-size:12px;padding-left:14px">'
      +'<code>'+esc(b.market)+'</code> '+(b.side==='BUY'?'bid':'ask')+' '+pc(b.px)
      +' \u00b7 '+svNum(b.ypd,3)+' $/day per $1'
      +' \u00b7 '+svNum(b.share_pct,2)+'% of side'
      +' \u00b7 touch '+(b.touch||0).toLocaleString()
      +' \u00b7 '+usd(b.est_day)+'/day</td></tr>';});
   }});
  out+='</table><div class="hint">Ranked on <b>$/day per $1 at risk</b> \u2014 what a dollar resting here earns in a day. College football, the one that works, runs a median of <b>0.16</b>; politics 0.05. Marked green at 0.16 or better. Tap a row for the actual markets behind it. "side per $1" is how much of a side that dollar buys: high on its own means the side is cheap to own but may pay nothing, which is why it is not the ranking.</div></div>';
 }else{
  out+='<div class="card muted">Nothing ranked yet. A prefix needs '
   +(s.min_samples||12)+' scored sides before its median means anything.</div>';
 }
 var y=s.sampling||[];
 if(y.length){
  var body='';
  y.slice(0,40).forEach(function(k){
   body+='<div class="vrd muted">'+esc(k.prefix)+' \u2014 '+k.n+' of '
    +(s.min_samples||12)+' sides'
    +(k.live_skipped?' \u00b7 '+k.live_skipped+' skipped, event live':'')
    +'</div>';});
  out+='<div class="card"><details><summary><b>Still sampling</b> '
   +'<span class="muted">\u2014 '+y.length+' prefixes</span></summary>'
   +body+'</details></div>';
 }
 return out;
}
"""

PAGES = {
    "/": ("Quick look", "meter", GRAPH_JS, "quick"),
    "/graph": ("Quick look", "meter", GRAPH_JS, "quick"),
    "/fills": ("Fills", "fills", FILLS_JS, "quick"),
    "/watch": ("Watch", "watch", WATCH_JS, "quick"),
    "/status": ("Status", "status", STATUS_JS, ""),
    "/orders": ("Orders", "orders", ORDERS_JS, ""),
    "/pay": ("Pay", "pay", PAY_JS, ""),
    "/grades": ("Pay", "pay", PAY_JS, ""),
    "/survey": ("Survey", "survey", SURVEY_JS, ""),
    "/bonds": ("Bonds", "bonds", BONDS_JS, ""),
    "/switch": ("Switches", "switch", SWITCH_JS, ""),
    "/log": ("Log", "log", LOG_JS, ""),
    "/plan": ("Plan", "", PLAN_JS, ""),
    "/silver": ("Model", "", SILVER_JS, ""),
}


class WebServer:
    def __init__(self, monitor, port: int | None = None, bind: str = DEFAULT_BIND):
        self.monitor = monitor
        if port is None and os.environ.get("V1_ENABLED", "0") == "0":
            # 1.0 retired: 3.0 IS the front door on the public port
            self.port = int(os.environ.get("PORT", "8080"))
            self.bind = "0.0.0.0"
        else:
            self.port = (port if port is not None
                         else int(os.environ.get("V3_PORT", DEFAULT_PORT)))
            self.bind = bind
        self.password = os.environ.get("DASH_PASSWORD", "")
        self._httpd: ThreadingHTTPServer | None = None
        self._live_lock = threading.Lock()
        self._live_count = 0

    def stream_live(self, slug: str, wfile) -> None:
        """One live card: hold the connection open and push the market's
        real book down it about once a second — each tick read straight
        from the exchange (monitor.live_view), never the stored copy.
        Ends when the phone closes the card (the write breaks) or after
        LIVE_MAX_S; the page reconnects by itself if it is still open.
        A hard cap on simultaneous streams bounds the API traffic."""
        with self._live_lock:
            if self._live_count >= LIVE_MAX_STREAMS:
                try:
                    wfile.write(b'data: {"ok": false, "note": "too many '
                                b'live views open at once - close one '
                                b'first"}\n\n')
                    wfile.flush()
                except OSError:
                    pass
                return
            self._live_count += 1
        try:
            t0 = time.time()
            last = None
            while time.time() - t0 < LIVE_MAX_S:
                try:
                    body = json.dumps(self.monitor.live_view(slug))
                except Exception as e:  # noqa: BLE001 — tell the phone, keep going
                    body = json.dumps(
                        {"ok": False,
                         "note": f"{type(e).__name__}: {e}"})
                if body != last:
                    wfile.write(b"data: " + body.encode() + b"\n\n")
                    last = body
                else:
                    wfile.write(b": hb\n\n")   # heartbeat — detects a
                                               # closed phone within a tick
                wfile.flush()
                time.sleep(LIVE_TICK_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                               # the card was closed
        finally:
            with self._live_lock:
                self._live_count -= 1

    def stream_bonds(self, wfile) -> None:
        """The bonds page's live line (owner, 2026-09-03): the rows of
        the markets he is in, pushed whenever they change. The books
        come from the cache the exchange's stream feeds — those markets
        hold seats on it — so a tick costs no REST read."""
        with self._live_lock:
            if getattr(self, "_bonds_count", 0) >= BONDS_MAX_STREAMS:
                try:
                    wfile.write(b'data: {"ok": false, "note": "too many '
                                b'bonds pages open at once"}\n\n')
                    wfile.flush()
                except OSError:
                    pass
                return
            self._bonds_count = getattr(self, "_bonds_count", 0) + 1
        try:
            t0 = time.time()
            last = None
            while time.time() - t0 < LIVE_MAX_S:
                try:
                    body = json.dumps({"ok": True, "rows": self.monitor.bonds_live()})
                except Exception as e:  # noqa: BLE001 — tell the phone, keep going
                    body = json.dumps({"ok": False,
                                       "note": f"{type(e).__name__}: {e}"})
                if body != last:
                    wfile.write(b"data: " + body.encode() + b"\n\n")
                    last = body
                else:
                    wfile.write(b": hb\n\n")
                wfile.flush()
                time.sleep(LIVE_TICK_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self._live_lock:
                self._bonds_count -= 1

    def data_payload(self) -> dict:
        # boot-time fallback only: once the first cycle completes, the
        # handler serves monitor.payload_json — bytes frozen on the
        # cycle thread, under the cycle lock, so the web thread never
        # serializes live dicts (the 2026-08-22 "unreachable" race)
        return self.monitor.build_phone_payload()

    def handle_op(self, body: dict) -> dict:
        op = str(body.get("op") or "")
        if op.startswith("switch_"):
            return {"ok": True,
                    "state": self.monitor.switch_tap(op[len("switch_"):],
                                                     str(body.get("which") or "master"))}
        if op == "family_active_until":
            return self.monitor.set_active_until(str(body.get("which") or ""),
                                                 body.get("value"))
        if op == "refresh_rewards":
            return self.monitor.refresh_rewards()
        if op == "schedule_cancel":
            return self.monitor.schedule_cancel(
                str(body.get("match") or ""), float(body.get("at") or 0),
                str(body.get("note") or ""))
        if op == "clear_cancel":
            return self.monitor.clear_cancel(str(body.get("match") or ""))
        if op == "place":
            return self.monitor.owner_place(
                str(body.get("market") or ""), str(body.get("side") or ""),
                float(body.get("price") or 0), float(body.get("qty") or 0))
        if op in ("cancel", "move"):
            price = body.get("price")
            qty = body.get("qty")
            return self.monitor.order_op(op, str(body.get("order_id") or ""),
                                         float(price) if price is not None else None,
                                         pin=bool(body.get("pin")),
                                         qty=float(qty) if qty is not None else None)
        if op == "close_position":
            return self.monitor.close_position(str(body.get("market") or ""))
        if op == "qualify_ask":
            return self.monitor.qualify_ask(str(body.get("market") or ""))
        if op.startswith("bonds_"):
            return self.monitor.bonds_op(op, str(body.get("market") or ""),
                                         body.get("value"))
        if op == "backfill":
            return self.monitor.backfill_journal(
                days=float(body.get("days") or 3.0),
                dry_run=bool(body.get("dry_run", True)))
        if op == "fetch_trades":
            import time as _t
            return self.monitor.publish_trades(_t.time(), deep=True)
        if op == "set_fair":
            f = body.get("fair")
            return self.monitor.set_owner_fair(
                str(body.get("market") or ""),
                float(f) if f not in (None, "") else None)
        return {"ok": False, "note": f"unknown op {op}"}

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: A003 — quiet
                pass

            def _send(self, code: int, ctype: str, body: bytes) -> None:
                # gzip when the client accepts it: the data payload is
                # hundreds of KB of JSON, and the owner reads it over a
                # phone connection — 10x smaller on the wire
                enc = ""
                if (len(body) > 2048 and "gzip" in
                        (self.headers.get("Accept-Encoding") or "")):
                    body = gzip.compress(body, 5)
                    enc = "gzip"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                if enc:
                    self.send_header("Content-Encoding", enc)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                path = u.path
                if path == "/v3" or path.startswith("/v3/"):
                    path = path[len("/v3"):] or "/"   # old bookmarks
                if path.startswith(("/map", "/lab", "/hunt", "/why",
                                    "/slate", "/unwind", "/v2")):
                    self.send_response(302)           # the old pages retired
                    self.send_header("Location", "/")
                    self.end_headers()
                    return
                route = path.rstrip("/") or "/"
                if route in PAGES:
                    title, here, js, sub = PAGES[route]
                    self._send(200, "text/html; charset=utf-8",
                               _shell(title, here, js, sub).encode())
                    return
                if route == "/live":
                    # the live card's open line. EventSource cannot set
                    # headers, so the key rides the query string — the
                    # same ?key= door authed() has always accepted.
                    if not authed(self.headers.get, u.query, server.password):
                        self._send(401, "application/json", b'{"error":"key required"}')
                        return
                    slug = (parse_qs(u.query).get("m") or [""])[0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    server.stream_live(slug, self.wfile)
                    return
                if route == "/bonds_live":
                    if not authed(self.headers.get, u.query, server.password):
                        self._send(401, "application/json", b'{"error":"key required"}')
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    server.stream_bonds(self.wfile)
                    return
                if route == "/book.json":
                    if not authed(self.headers.get, u.query, server.password):
                        self._send(401, "application/json", b'{"error":"key required"}')
                        return
                    slug = (parse_qs(u.query).get("m") or [""])[0]
                    self._send(200, "application/json",
                               json.dumps(server.monitor.book_view(slug)).encode())
                    return
                if route == "/fills.json":
                    if not authed(self.headers.get, u.query, server.password):
                        self._send(401, "application/json", b'{"error":"key required"}')
                        return
                    self._send(200, "application/json",
                               json.dumps(server.monitor.fills_view()).encode())
                    return
                if route == "/data.json":
                    if not authed(self.headers.get, u.query, server.password):
                        self._send(401, "application/json", b'{"error":"key required"}')
                        return
                    body = getattr(server.monitor, "payload_json", None)
                    if not body:
                        # before the first cycle freezes one: a safe
                        # boot snapshot, never a live-dict rebuild.
                        # Never let this drop the socket — a bare page
                        # beats "unreachable".
                        try:
                            body = server.monitor.boot_payload()
                        except Exception:  # noqa: BLE001
                            body = (b'{"starting": true, "summaries": {},'
                                    b' "labels": {}}')

                    self._send(200, "application/json", body)
                    return
                self._send(404, "text/plain", b"not found")

            def do_POST(self):  # noqa: N802
                u = urlparse(self.path)
                p = u.path
                if p == "/v3" or p.startswith("/v3/"):
                    p = p[len("/v3"):] or "/"
                if p.rstrip("/") != "/op":
                    self._send(404, "text/plain", b"not found")
                    return
                if not authed(self.headers.get, u.query, server.password):
                    self._send(401, "application/json", b'{"error":"key required"}')
                    return
                if self.headers.get("X-Reprice") != "1":
                    self._send(403, "text/plain", b"missing X-Reprice header")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length)) if length else {}
                except Exception:  # noqa: BLE001
                    self._send(400, "application/json", b'{"ok":false,"note":"bad request"}')
                    return
                try:
                    out = server.handle_op(body)
                except Exception as e:  # noqa: BLE001
                    out = {"ok": False, "note": f"{type(e).__name__}: {e}"}
                self._send(200, "application/json", json.dumps(out).encode())

        self._httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        t = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        t.start()
