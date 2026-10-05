"""The one page, and the four routes behind it.

The page is public (a shell); everything with data needs the dashboard
key, the same X-Dash-Key the phone already keeps. An order tap also
needs the X-Reprice header (the CSRF guard every order-touching
endpoint has kept since 1.0)."""

from __future__ import annotations

import gzip
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from v3.web import _CSS, authed

# 3.0's pages: an old bookmark lands on the one page
OLD_PAGES = {"/graph", "/fills", "/watch", "/status", "/orders", "/pay", "/grades",
             "/bonds", "/focus", "/tiers", "/switch", "/log", "/plan", "/silver",
             "/map", "/lab", "/hunt", "/why", "/slate", "/unwind", "/v2", "/v3"}

PAGE_CSS = """
 .row{border-top:1px solid #2c3527;padding:8px 0;cursor:pointer}
 .row:first-child{border-top:0}
 .row .n{font-size:13px;color:#b6c1a8}
 .row .l{display:flex;justify-content:space-between;align-items:baseline}
 .row .a{font-size:18px;font-weight:700}
 .row .e{font-size:16px;font-weight:700;color:#9ec49a}
 .bk{display:flex;gap:10px}.bk>div{flex:1}
 .bk td{font-variant-numeric:tabular-nums}
 .bk tr.me td{color:#b9d98f;font-weight:700}
 .frm{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:8px 0}
 .frm input{width:84px}
 .frm button{margin:0}
 .seg button{background:#2c3527;color:#cfd8c2;margin:0 4px 0 0}
 .seg button.on{background:#4c7a2f;color:#fff}
 button.no{background:#7a3a2f}
 .note{margin:6px 0;font-size:13px}
"""

PAGE_JS = r"""
var K='dashKey';
function hdrs(){var h=new Headers();h.set('X-Dash-Key',localStorage.getItem(K)||'');return h;}
function saveKey(){localStorage.setItem(K,document.getElementById('k').value);load();}
function usd(x){var v=x||0;return (v<0?'−$':'$')+Math.abs(v).toFixed(2);}
function ct(x){var v=Math.round((x||0)*1000)/10;return (v%1?v.toFixed(1):''+Math.round(v))+'¢';}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function sz(q){q=q||0;if(q>=1e6)return (q/1e6).toFixed(1)+'M';if(q>=1e4)return (q/1e3).toFixed(1)+'k';return (Math.round(q*100)/100).toLocaleString();}
function fmtT(ts){var d=new Date(ts*1000);return ('0'+d.getHours()).slice(-2)+':'+('0'+d.getMinutes()).slice(-2);}
function side(s){return s==='BUY'?'Bid':'Ask';}
function day(e){return e==null?'—':usd(e)+'/day';}
function ago(s){s=Math.round(s||0);return s<120?s+'s':Math.round(s/60)+' min';}
function get(url,cb){
 fetch(url,{headers:hdrs()}).then(function(r){
  if(r.status===401){document.getElementById('login').style.display='';return null;}
  return r.json();}).then(function(j){if(j)cb(j);})
  .catch(function(){cb({ok:false,note:'no answer'});});
}
function post(body,cb){
 var h=hdrs();h.set('X-Reprice','1');h.set('Content-Type','application/json');
 fetch('/op',{method:'POST',headers:h,body:JSON.stringify(body)})
  .then(function(r){return r.json();}).then(cb)
  .catch(function(){cb({ok:false,note:'No answer in time. It may still have gone through — check before tapping again.'});});
}

// -- the graph: the same as 3.0's quick look, one band ------------------
function graph(d,win){
 var now=Date.now()/1000;
 var dots=(d.dots||[]).filter(function(x){return x[0]>=now-win;});
 var m={};dots.forEach(function(x){m[Math.round(x[0]/20)*20]=x[1];});
 var ts=Object.keys(m).map(Number).sort(function(a,b){return a-b;});
 if(ts.length<2)return '<div class="muted">not enough samples yet</div>';
 var last=0,lt=-1e12;
 var v=ts.map(function(t){if(m[t]!=null){last=m[t];lt=t;}return (t-lt>180)?0:last;});
 var ymax=Math.max.apply(null,v)*1.08||1;
 var W=340,H=190,PL=36,PB=18,PT=8,PR=6,t0=ts[0],t1=ts[ts.length-1],span=Math.max(t1-t0,60);
 function X(t){return PL+(W-PL-PR)*(t-t0)/span;}
 function Y(y){return PT+(H-PT-PB)*(1-y/ymax);}
 var s='<svg viewBox="0 0 '+W+' '+H+'" style="width:100%;height:auto" role="img" aria-label="earning rate">';
 [0,0.5,1].forEach(function(f){var y=Y(ymax*f);
  s+='<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" stroke="rgba(255,255,255,0.08)"/>'
   +'<text x="'+(PL-4)+'" y="'+(y+3)+'" text-anchor="end" font-size="8" fill="rgba(255,255,255,0.45)">$'+(ymax*f).toFixed(0)+'</text>';});
 var pts=ts.map(function(t,i){return X(t).toFixed(1)+','+Y(v[i]).toFixed(1);}).join(' ');
 var base=ts.map(function(t){return X(t).toFixed(1)+','+Y(0).toFixed(1);}).reverse().join(' ');
 s+='<polygon points="'+pts+' '+base+'" fill="#7fd77f" fill-opacity="0.5"/>';
 s+='<polyline points="'+pts+'" fill="none" stroke="#7fd77f" stroke-width="1.4"/>';
 [t0,(t0+t1)/2,t1].forEach(function(t,i){
  s+='<text x="'+X(t)+'" y="'+(H-4)+'" text-anchor="'+(i===0?'start':i===2?'end':'middle')+'" font-size="8" fill="rgba(255,255,255,0.45)">'+fmtT(t)+'</text>';});
 return s+'</svg>';
}
function win(sec){window._win=sec;if(window._d)draw(window._d);}

// -- the page --------------------------------------------------------------
function draw(d){
 window._d=d;
 if(window._win==null)window._win=21600;
 var b=function(sec,l){return '<button class="'+(window._win===sec?'on':'')+'" onclick="win('+sec+')">'+l+'</button>';};
 var h='';
 if(d.starting)h+='<div class="card muted">starting — reading the open list</div>';
 h+='<div class="card"><div class="hero">'+usd(d.rate)+'<span class="u">/day</span></div>'
  +'<div class="tabs">'+b(900,'15 min')+b(21600,'6 hours')+'</div>'+graph(d,window._win)
  +'<div class="kpi"><div><div class="v">'+usd(d.earned)+'</div><div class="l">earned today</div></div>'
  +'<div><div class="v">'+(d.bp==null?'—':usd(d.bp))+'</div><div class="l">buying power</div></div>'
  +'<div><div class="v">'+usd(d.holdings_value)+'</div><div class="l">holdings</div></div></div>'
  +(d.unmeasured_min>5?'<div class="muted">'+Math.round(d.unmeasured_min)+' min unmeasured today</div>':'')
  +(d.bp_age>60?'<div class="warn">buying power read '+ago(d.bp_age)+' ago</div>':'')
  +(d.state_note?'<div class="bad">'+esc(d.state_note)+'</div>':'')
  +'</div>';
 var tot=0;(d.orders||[]).forEach(function(o){tot+=(o.est||0);});
 h+='<div class="card"><b>Orders</b> <span class="muted">'+(d.orders||[]).length+' · '+usd(tot)+'/day</span>'
  +(d.orders_age>60?' <span class="warn">list read '+ago(d.orders_age)+' ago</span>':'');
 (d.orders||[]).forEach(function(o){
  h+='<div class="row" onclick="openOrder(\''+esc(o.id)+'\')"><div class="n">'+esc(o.name)+'</div>'
   +'<div class="l"><span class="a">'+side(o.side)+' '+ct(o.price)+' × '+sz(o.size)+'</span>'
   +'<span class="e">'+day(o.est)+'</span></div></div>';});
 if(!(d.orders||[]).length)h+='<div class="muted">none</div>';
 h+='</div><div class="card"><b>Holdings</b>'+(d.positions_age>120?' <span class="warn">read '+ago(d.positions_age)+' ago</span>':'');
 (d.holdings||[]).forEach(function(p){
  h+='<div class="row" onclick="openMarket(\''+esc(p.market)+'\')"><div class="n">'+esc(p.name)+'</div>'
   +'<div class="l"><span class="a">'+(p.net>0?'Yes ':'No ')+sz(Math.abs(p.net))+'</span>'
   +'<span class="e">'+usd(p.value)+'</span></div></div>';});
 if(d.small_n)h+='<div class="muted">+'+d.small_n+' under $1 ('+usd(d.small_v)+')</div>';
 h+='</div>';
 document.getElementById('view').innerHTML=h;
}
function load(){
 get('/data.json',function(d){
  if(d.ok===false)return;
  document.getElementById('login').style.display='none';draw(d);});
}

// -- the card: one order, or one market -----------------------------------
function bookHtml(b,mine){
 var at={};(b.ours||[]).forEach(function(o){at[o.side+o.price.toFixed(4)]=1;});
 function col(lv,sd){var r='<table>';lv.forEach(function(x){
  var me=at[sd+x[0].toFixed(4)]||(mine&&mine.side===sd&&Math.abs(mine.price-x[0])<1e-9);
  r+='<tr class="'+(me?'me':'')+'"><td>'+ct(x[0])+'</td><td class="r">'+sz(x[1])+'</td></tr>';});
  return r+'</table>';}
 return '<div class="bk"><div><div class="muted">bids</div>'+col(b.bids||[],'BUY')
  +'</div><div><div class="muted">asks</div>'+col(b.asks||[],'SELL')+'</div></div>'
  +'<div class="muted">'+(b.age==null?'no book':'book '+Math.round(b.age)+'s old · tick '+ct(b.tick))+'</div>';
}
function mathHtml(o,m){
 var h='<table>';
 function r(k,v){h+='<tr><td>'+k+'</td><td class="r">'+v+'</td></tr>';}
 r('pool, this side',m.side_pool==null?'—':usd(m.side_pool)+'/day');
 r('Target Size',m.target==null?'—':sz(m.target));
 if(m.side_total!=null)r('side total',sz(m.side_total));
 if(m.ticks!=null)r('ticks from best',m.ticks);
 if(m.df!=null)r('discount a tick',m.df);
 if(m.score!=null)r('your score',sz(m.score));
 if(m.denom!=null)r('window score',sz(m.denom));
 if(m.share!=null)r('share',(m.share*100).toFixed(1)+'%');
 r('earns',m.est==null?'—':usd(m.est)+'/day');
 h+='</table>';
 if(m.why)h+='<div class="muted">'+esc(m.why)+'</div>';
 if(m.window&&m.window.length){
  h+='<div class="muted" style="margin-top:6px">window</div><table>';
  m.window.forEach(function(w){h+='<tr class="'+(w[4]?'me':'')+'"><td>'+ct(w[0])+'</td><td class="r">'+sz(w[1])
   +'</td><td class="r">'+w[2]+'t</td><td class="r">'+sz(w[3])+'</td></tr>';});
  h+='</table>';
 }
 return h;
}
function sheet(html){
 var s=document.getElementById('sheet');
 if(!s){s=document.createElement('div');s.id='sheet';s.className='bsheet';document.body.appendChild(s);
  var c=document.createElement('div');c.id='scrim';c.className='bscrim';c.onclick=closeSheet;document.body.appendChild(c);}
 var keep={};['px','qty'].forEach(function(k){var e=document.getElementById(k);if(e&&document.activeElement===e)keep[k]=e.value;});
 s.innerHTML='<button class="close" onclick="closeSheet()">close</button>'+html;
 for(var k in keep){var e=document.getElementById(k);if(e){e.value=keep[k];}}
}
function closeSheet(){window._card=null;['sheet','scrim'].forEach(function(i){var e=document.getElementById(i);if(e)e.remove();});}
function say(j){window._said=j;refreshCard();load();}
function said(){var j=window._said;if(!j)return '';return '<div class="note '+(j.ok?'ok':'bad')+'">'+esc(j.note||'')+'</div>';}
function openOrder(id){window._card={kind:'order',id:id};window._said=null;window._typed={};window._shown=null;
 // the order and its Cancel come up at once from the list, before any read
 var o=((window._d||{}).orders||[]).filter(function(x){return x.id===id;})[0];
 sheet(o?'<div><b>'+esc(o.name)+'</b></div><div class="hero" style="font-size:26px">'+side(o.side)+' '+ct(o.price)+' × '+sz(o.size)+'</div>'
  +'<div class="muted">reading…</div><div class="frm"><button class="no" onclick="doCancel()">Cancel order</button></div>'
  :'<div class="muted">reading…</div>');
 refreshCard();}
function openMarket(m){if(!m)return;window._card={kind:'market',m:m};window._said=null;window._typed={};window._side=null;sheet('<div class="muted">reading…</div>');refreshCard();}
function typed(k){var e=document.getElementById(k);if(e)window._typed[k]=e.value;}
function val(k,d){var t=window._typed||{};return t[k]!=null?t[k]:d;}
function edited(){var t=window._typed||{};return t.px!=null||t.qty!=null;}
function refreshCard(){
 var c=window._card;if(!c||window._busy)return;
 if(c.kind==='order')get('/math.json?id='+encodeURIComponent(c.id),function(j){if(window._card===c&&!window._busy)drawOrder(j);});
 else get('/book.json?m='+encodeURIComponent(c.m),function(j){if(window._card===c&&!window._busy)drawMarket(j);});
}
function btns(html){return window._busy?html.replace(/<button(?! class="close")/g,'<button disabled'):html;}
function drawOrder(j){
 var o=j.order||(((window._d||{}).orders||[]).filter(function(x){return x.id===(window._card||{}).id;})[0]);
 if(!j.ok){
  // the order is still his to cancel when its book cannot be read
  var c0=o?'<div class="hero" style="font-size:26px">'+side(o.side)+' '+ct(o.price)+' × '+sz(o.size)+'</div>'
   +'<div class="frm"><button class="no" onclick="doCancel()">Cancel order</button></div>':'';
  sheet(btns(said()+'<div class="bad">'+esc(j.note)+'</div>'+c0));return;}
 // what the card showed when he started typing is what a change is
 // checked against: a fill meanwhile is refused, never resized over
 if(!edited()||!window._shown)window._shown={px:o.price,qty:o.size};
 var h='<div><b>'+esc(j.name)+'</b></div><div class="hero" style="font-size:26px">'+side(o.side)+' '+ct(o.price)+' × '+sz(o.size)+'</div>'
  +said()+(j.stale?'<div class="warn">'+esc(j.stale)+'</div>':'')+mathHtml(o,j.math)+bookHtml(j,o)
  +'<div class="frm"><input id="px" inputmode="decimal" value="'+esc(val('px',Math.round(o.price*1000)/10))+'" oninput="typed(\'px\')">¢'
  +'<input id="qty" inputmode="decimal" value="'+esc(val('qty',o.size))+'" oninput="typed(\'qty\')">'
  +'<button onclick="doMove()">Change</button><button class="no" onclick="doCancel()">Cancel order</button></div>';
 sheet(btns(h));
}
function drawMarket(j){
 if(!j.ok){sheet(said()+'<div class="bad">'+esc(j.note)+'</div>');return;}
 var sd=window._side||'BUY';
 var h='<div><b>'+esc(j.name)+'</b></div>'
  +'<div class="muted">'+(j.net?(j.net>0?'Yes ':'No ')+sz(Math.abs(j.net))+' · '+usd(j.value)+' · ':'')
  +(j.pool!=null?usd(j.pool)+'/day a side, Target '+sz(j.target)
    :(!j.prog?'no reward program read':(!j.live?'program not live':'event size unknown')))
  +(j.first_day?' · first day':'')+'</div>'
  +(j.stale?'<div class="warn">'+esc(j.stale)+'</div>':'')
  +said();
 (j.ours||[]).forEach(function(o){h+='<div class="row" onclick="openOrder(\''+esc(o.id)+'\')"><div class="l"><span class="a">'
  +side(o.side)+' '+ct(o.price)+' × '+sz(o.size)+'</span><span class="e">'+day(o.est)+'</span></div></div>';});
 h+=bookHtml(j,null)
  +'<div class="frm seg"><button class="'+(sd==='BUY'?'on':'')+'" onclick="pick(\'BUY\')">Bid</button>'
  +'<button class="'+(sd==='SELL'?'on':'')+'" onclick="pick(\'SELL\')">Ask</button></div>'
  +'<div class="frm"><input id="px" inputmode="decimal" placeholder="¢" value="'+esc(val('px',''))+'" oninput="typed(\'px\')">¢'
  +'<input id="qty" inputmode="decimal" placeholder="size" value="'+esc(val('qty',''))+'" oninput="typed(\'qty\')">'
  +'<button onclick="doPlace()">Place</button></div>';
 window._mk=j;sheet(btns(h));
}
function pick(s){window._side=s;if(window._mk)drawMarket(window._mk);}
function nums(){var p=parseFloat(document.getElementById('px').value),q=parseFloat(document.getElementById('qty').value);
 if(!(p>0)||!(q>0)){alert('price and size');return null;}return [p,q];}
function busy(on,note){window._busy=on;if(on)window._said={ok:true,note:note};
 var s=document.getElementById('sheet');if(s)s.querySelectorAll('button:not(.close)').forEach(function(b){b.disabled=on;});
 if(on){var n=s&&s.querySelector('.note');if(n)n.textContent=note;else if(s)s.insertAdjacentHTML('beforeend','<div class="note ok">'+esc(note)+'</div>');}}
function done(r){busy(false);window._typed={};window._shown=null;say(r);}
function doPlace(){var n=nums();if(!n)return;var c=window._card,sd=window._side||'BUY',j=window._mk||{};
 // what he holds, less what his orders already offer, is what an ask
 // can sell or a bid can buy back without tying up money
 var free=sd==='BUY'?(j.free_short||0):(j.free_long||0);
 var cost=n[1]<=free+1e-9?0:(sd==='BUY'?n[0]/100*n[1]:(1-n[0]/100)*n[1]);
 if(!confirm(side(sd)+' '+n[0]+'¢ × '+n[1]+(cost?' — ties up '+usd(cost):' — from what you hold')+'?'))return;
 busy(true,'placing…');
 post({op:'place',market:c.m,side:sd,px:n[0],qty:n[1]},done);}
function doMove(){var n=nums();if(!n)return;var c=window._card,w=window._shown||{},t=window._typed||{};
 // only what he changed is sent, with what the card showed: an order
 // that filled meanwhile is refused, never regrown
 var body={op:'move',order_id:c.id,was_price:Math.round(w.px*100000)/1000,was_size:w.qty};
 if(t.px!=null)body.px=n[0];
 if(t.qty!=null)body.qty=n[1];
 if(body.px==null&&body.qty==null){alert('change the price or the size first');return;}
 if(!confirm('Change to '+(body.px!=null?n[0]:Math.round(w.px*1000)/10)+'¢ × '+(body.qty!=null?n[1]:w.qty)+'?'))return;
 busy(true,'changing… (up to 15 s)');
 post(body,function(r){if(r.ok&&r.id)window._card={kind:'order',id:r.id};done(r);});}
function doCancel(){var c=window._card;if(!confirm('Cancel this order?'))return;
 busy(true,'cancelling…');
 post({op:'cancel',order_id:c.id},function(r){busy(false);if(r.ok){closeSheet();load();alert(r.note);}else say(r);});}

load();setInterval(load,20000);
setInterval(function(){var a=document.activeElement;
 if(window._card&&!(a&&a.tagName==='INPUT'&&a.closest&&a.closest('#sheet')))refreshCard();},5000);
"""

PAGE = f"""<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store">
<title>Rewards</title><style>{_CSS}{PAGE_CSS}</style></head><body>
<div id="login" style="display:none" class="card">
 <div class="sub">key</div>
 <input id="k" type="password" placeholder="key"><button onclick="saveKey()">Open</button>
</div>
<div id="view" class="muted">loading&hellip;</div>
<div class="card"><div class="frm"><input id="slug" placeholder="market slug" style="width:60%">
<button onclick="openMarket(document.getElementById('slug').value)">Open</button></div></div>
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
        if path in ("/data.json", "/book.json", "/math.json"):
            if not self._authed(u.query):
                return self._json({"ok": False, "note": "key"}, 401)
            try:
                if path == "/data.json":
                    return self._json(self.app.data())
                if path == "/book.json":
                    return self._json(self.app.book_view((q.get("m") or [""])[0]))
                return self._json(self.app.order_math((q.get("id") or [""])[0]))
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
    port = int(port or os.environ.get("PORT") or 8080)
    handler = type("LiteHandler", (Handler,), {"app": app,
                                               "password": os.environ.get("DASH_PASSWORD", "")})
    srv = ThreadingHTTPServer((bind, port), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    return srv
