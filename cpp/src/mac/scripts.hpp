// The window's page scripts (CPP_PLAN.md step 1). FADE_IN and RECONNECTING
// are the Tauri shell's, byte for byte (main.rs, 6b548). BRIDGE_SHIM is its
// shim over WebKit's own reply channel instead of Tauri's IPC. CHROME is
// what Tauri's injected runtime did for the page: the header's
// data-tauri-drag-region drags the window (a double click zooms), and the
// page's paint times reach the launch marks. None holds a secret.
#pragma once

namespace concorde::mac {

// The page expects pywebview's bridge: window.pywebview.api.api_token()
// returning a promise, and a `pywebviewready` event (6b321). This gives it
// exactly that over one message handler, `concorde`, whose reply is the
// token only while the asking frame is this window's main frame on
// http://127.0.0.1:<the front's port> (the handler checks; the shim knows
// nothing). Safe in every frame the window may ever show. voice() is the
// native dictation's (voice.mm, 6b642), answered under the same check.
inline const char* const BRIDGE_SHIM = R"JS((function(){
  if (window.pywebview) return;
  function invoke(m){
    var h = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.concorde;
    return h ? h.postMessage(m) : Promise.reject(new Error("no bridge"));
  }
  // beta-4's second method (6b628): the Workspace's "Choose folder…", the system's folder panel
  function pick(){
    return invoke({op: "pick_folder"}).catch(function(){ return null; });
  }
  // voice (6b642): dictation in the shell itself; cmd is {act, lang, u},
  // copied field by field so nothing else rides along and op can't be set
  function voice(cmd){
    cmd = cmd || {};
    var m = {op: "voice", act: String(cmd.act || "")};
    if (typeof cmd.lang === "string") m.lang = cmd.lang;
    if (typeof cmd.u === "number") m.u = cmd.u;
    return invoke(m);
  }
  var api = Object.freeze({ api_token: function(){ return invoke({op: "api_token"}); },
                            pick_folder: pick, voice: voice });
  Object.defineProperty(window, "pywebview", {
    value: Object.freeze({ platform: "concorde-cpp", api: api }), writable: false, configurable: false });
  var fire = function(){ try { window.dispatchEvent(new Event("pywebviewready")); } catch (e) {} };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fire);
  else fire();
})();)JS";

inline const char* const FADE_IN = R"JS((function(){
  if (location.protocol !== "http:" || location.hostname !== "127.0.0.1") return;
  var calm = false;
  try { calm = matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) {}
  var st = document.createElement("style");
  var bg = "html{background-color:#03040a}";
  st.textContent = bg + "html>body{opacity:0}";
  (document.head || document.documentElement).appendChild(st);
  var done = false;
  function reveal(){
    if (done) return; done = true;
    st.textContent = calm ? "" : bg + "html>body{transition:opacity .24s ease-out}";
    setTimeout(function(){ st.remove(); }, calm ? 0 : 400);
  }
  function frame(f){ var g = false; var h = function(){ if (!g) { g = true; f(); } };
    try { requestAnimationFrame(h); } catch (e) {} setTimeout(h, 50); }
  var ready = function(){ frame(function(){ frame(reveal); }); };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", ready);
  else ready();
  setTimeout(reveal, 2500);
})();)JS";

inline const char* const RECONNECTING = R"JS((function(){
  if (!document.documentElement || document.getElementById("shell-reconnect")) return;
  var d = document.createElement("div");
  d.id = "shell-reconnect"; d.setAttribute("role", "status"); d.setAttribute("data-tauri-drag-region", "");
  d.style.cssText = "position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;"
    + "background:rgba(3,4,10,.78);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);color:#c9d0e2;"
    + "font:15px/1.5 -apple-system,system-ui,sans-serif;letter-spacing:.01em;opacity:0;transition:opacity .3s ease-out;cursor:default";
  d.textContent = "Reconnecting…";
  document.documentElement.appendChild(d);
  setTimeout(function(){ d.style.opacity = "1"; }, 16);
})();)JS";

// Tauri's drag rule: a primary-button press on an element that itself
// carries data-tauri-drag-region (not its children) drags the window; the
// second press of a double click zooms it. The native side starts the
// drag only while the button is still down.
inline const char* const CHROME = R"JS((function(){
  // the app's page, and the shell's own pages (the setup page, an error)
  if (!(location.protocol === "http:" && location.hostname === "127.0.0.1") && location.protocol !== "about:") return;
  function post(m){ try { window.webkit.messageHandlers.concorde.postMessage(m).catch(function(){}); } catch (e) {} }
  document.addEventListener("mousedown", function(e){
    var t = e.target;
    if (e.button !== 0 || !t || !t.getAttribute) return;
    var a = t.getAttribute("data-tauri-drag-region");
    if (a === null || a === "false") return;
    e.preventDefault(); e.stopImmediatePropagation();
    post({op: e.detail === 2 ? "zoom" : "drag"});
  });
  try {
    new PerformanceObserver(function(list){
      list.getEntries().forEach(function(en){
        post({op: "paint", name: en.name, at: performance.timeOrigin + en.startTime});
      });
    }).observe({type: "paint", buffered: true});
  } catch (e) {}
})();)JS";

// The first-run setup page: beta-4's shell's SETUP_PAGE (main.rs, 6b630),
// byte for byte. Its "Try again" and "Copy details" navigate to
// concorde-shell://retry and concorde-shell://copy, which the window catches.
inline const char* const SETUP_PAGE = R"HTML(<!doctype html><html><head><meta charset="utf-8"><style>
:root{--ink:#eceef3;--soft:#9aa1b2;--faint:#5d6476;--line:rgba(255,255,255,.09)}
html,body{margin:0;height:100%;background:#03040a;color:var(--ink);
  font:14px/1.5 Inter,-apple-system,system-ui,sans-serif;-webkit-font-smoothing:antialiased;cursor:default;
  -webkit-user-select:none;user-select:none}
main{position:absolute;left:50%;top:38%;transform:translate(-50%,-50%);width:360px;max-width:calc(100% - 48px)}
.mark{display:flex;align-items:center;gap:10px;margin:0 0 40px}
.mark svg{width:22px;height:19px}
.mark b{font-weight:500;font-size:15px;letter-spacing:.02em}
h1{font-size:17px;font-weight:600;margin:0 0 6px;letter-spacing:-.005em}
p{margin:0;color:var(--soft)}
.bar{height:2px;background:var(--line);border-radius:1px;margin:28px 0 12px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:#e9ebf1;border-radius:1px;transition:width .4s cubic-bezier(.2,.7,.2,1)}
.row{display:flex;justify-content:space-between;gap:16px;font-size:13px;color:var(--soft);font-variant-numeric:tabular-nums}
.row span:last-child{color:var(--faint)}
.item{font-size:12px;color:var(--faint);margin-top:4px;min-height:18px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acts{display:flex;align-items:center;gap:20px;margin-top:28px}
button{font:inherit;font-size:13px;font-weight:500;height:32px;padding:0 16px;border-radius:8px;border:0;
  background:#eceef3;color:#0b0c10;cursor:pointer}
button:focus-visible,a:focus-visible{outline:1px solid #e0344b;outline-offset:2px}
a{color:var(--soft);font-size:13px;text-decoration:none;cursor:pointer}
a:hover{color:var(--ink)}
#fail{display:none}
body.failed #run{display:none}
body.failed #fail{display:block}
</style></head><body data-tauri-drag-region>
<main>
<div class="mark"><svg viewBox="2 2.3 19.6 16.4" aria-hidden="true"><g stroke="#d9dce4" stroke-width="2.4" stroke-linecap="round"><line x1="3.2" y1="17.5" x2="20.4" y2="3.5"/><line x1="7.5" y1="17.5" x2="20.4" y2="7"/><line x1="11.8" y1="17.5" x2="20.4" y2="10.5"/><line x1="16.1" y1="17.5" x2="20.4" y2="14"/><line x1="19.3" y1="17.5" x2="20.4" y2="16.6"/></g></svg><b>ConcordeAI</b></div>
<section id="run" role="status" aria-live="polite">
<h1>Setting up ConcordeAI</h1>
<p>This takes a few minutes the first time.</p>
<div class="bar"><i id="fill"></i></div>
<div class="row"><span id="what">Starting</span><span id="step"></span></div>
<div class="item" id="item"></div>
</section>
<section id="fail" role="alert">
<h1 id="ftitle">Setup didn’t finish</h1>
<p id="fwhy">Check your internet connection, then try again.</p>
<div class="acts"><button id="retry" type="button">Try again</button><a id="copy" href="concorde-shell://copy">Copy details</a></div>
</section>
</main>
<script>
var NAMES={python:"Downloading",venv:"Preparing",base:"Installing",engine:"Installing the AI engine"};
// the bar is the whole setup: each step its share, the file downloading now
// filling most of that share; it never goes back (a new file starts at 0)
var far=0;
window.__boot=function(i,n,name,pct,item){
  document.body.classList.remove("failed");
  var p=Math.max(0,Math.min(100,pct|0));
  if(!n){far=0;}else far=Math.max(far,((i-1)+0.9*p/100)/n);
  document.getElementById("fill").style.width=(Math.min(1,far)*100).toFixed(1)+"%";
  document.getElementById("what").textContent=NAMES[name]||"Starting";
  document.getElementById("step").textContent=n?"Step "+i+" of "+n:"";
  document.getElementById("item").textContent=(item||"")+(item&&p>0&&p<100?" · "+p+"%":"");
};
window.__failed=function(t,w){
  if(t)document.getElementById("ftitle").textContent=t;
  if(w)document.getElementById("fwhy").textContent=w;
  document.body.classList.add("failed");
  document.getElementById("copy").textContent="Copy details";
};
window.__copied=function(){document.getElementById("copy").textContent="Copied";};
document.getElementById("retry").addEventListener("click",function(){
  window.__boot(0,0,"",0,"");location.href="concorde-shell://retry";});
</script></body></html>)HTML";

}  // namespace concorde::mac
