//! ConcordeAI's native window (6b532, per Patrick): step 1 of the move
//! from Python to Rust. A Tauri 2 shell replaces pywebview as the window.
//! The Python server (millenai.py) runs as a sidecar in MILLENAI_SHELL=tauri
//! mode; this process starts it, waits for its run/instance.json and
//! run/boot.json, loads /?boot=<code> (which trades the one-time code for
//! the launch-key cookie, 6b321) and hands the page its API token through
//! one IPC command that answers only the app's own origin, the way
//! pywebview's _WindowBridge did. Quit stops the sidecar; the sidecar also
//! exits on its own when this process dies, so nothing is orphaned.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::webview::DownloadEvent;
use tauri::{AppHandle, Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder};
use url::Url;

/// The night the Horizon design opens on: the window is this colour before
/// the page paints, so there is no white flash.
const NIGHT: tauri::window::Color = tauri::window::Color(0x03, 0x04, 0x0a, 0xff);

/// The traffic lights' inset on a Mac (the close button's left edge and
/// top, in points; per Patrick: they crowded the sidebar toggle, "too
/// tight"). They sit on the page's 72 px header row, their centre line at
/// y 36, the green button's right edge near x 75; the page (html.tl, set
/// when millenai.py runs under this shell on a Mac) reserves 96 px at a
/// header's left, so the toggle starts 16 px or more right of them.
#[cfg(target_os = "macos")]
const TL_X: f64 = 16.0;
#[cfg(target_os = "macos")]
const TL_Y: f64 = 38.0;

/// How long to give the server to write its note before giving up.
const SERVER_START_TIMEOUT: Duration = Duration::from_secs(180);

/// A sidecar that stops on its own (a crash, a signal, anything but the
/// updater's clean exit) is started again, at most this many times in a
/// row (shell-polish); a run that lasted STEADY counts as healthy and
/// clears the tally. After the last try the window says so plainly.
const MAX_RESTARTS: u32 = 3;
const STEADY: Duration = Duration::from_secs(60);

static RESTARTS: AtomicU32 = AtomicU32::new(0);
/// Set by Quit before the sidecar is stopped, so nothing restarts it.
static QUITTING: AtomicBool = AtomicBool::new(false);

/// CONCORDE_SHELL_TIMING=1 prints each launch milestone's time since the
/// process started to stderr (shell-polish; how the launch was measured).
static T0: OnceLock<Instant> = OnceLock::new();
fn mark(what: &str) {
    if std::env::var_os("CONCORDE_SHELL_TIMING").is_some() {
        let t = T0.get_or_init(Instant::now).elapsed();
        eprintln!("[shell-timing] {:>7.1} ms  {}", t.as_secs_f64() * 1000.0, what);
    }
}

/// NO BLANK WINDOW, THE PAGE FADES IN (shell-polish, per Patrick: "do
/// better on the Rust version"). The window and the webview are the night
/// from their first frame (NIGHT); on the app's own page this script, run
/// at document start in the main frame only, keeps the body out of sight
/// until the page's script has run and laid it out (DOMContentLoaded and
/// two frames, a timer beside each frame because a hidden window never
/// fires one), then fades it in over 240 ms, none under reduced motion.
/// The page is revealed after 2.5 s whatever happens, and the style is
/// removed once it is in (no class, no attribute), so the page is left as it was written.
/// It holds no secret and runs nowhere but http://127.0.0.1.
const FADE_IN: &str = r#"(function(){
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
})();"#;

/// The calm state while a stopped sidecar is started again: a veil over
/// whatever the window shows (the page, or the night), its words only.
/// The bare veil drags the window like the header it covers.
const RECONNECTING: &str = r#"(function(){
  if (!document.documentElement || document.getElementById("shell-reconnect")) return;
  var d = document.createElement("div");
  d.id = "shell-reconnect"; d.setAttribute("role", "status"); d.setAttribute("data-tauri-drag-region", "");
  d.style.cssText = "position:fixed;inset:0;z-index:2147483647;display:flex;align-items:center;justify-content:center;"
    + "background:rgba(3,4,10,.78);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);color:#c9d0e2;"
    + "font:15px/1.5 -apple-system,system-ui,sans-serif;letter-spacing:.01em;opacity:0;transition:opacity .3s ease-out;cursor:default";
  d.textContent = "Reconnecting…";
  document.documentElement.appendChild(d);
  setTimeout(function(){ d.style.opacity = "1"; }, 16);
})();"#;

// ------------------------------------------------------------------ bridge

/// What the api_token command needs: the sidecar's port and token, known
/// once run/instance.json is read. Nothing else is ever handed to a page.
#[derive(Default)]
struct Bridge {
    inner: Mutex<Option<(u16, String)>>,
}

/// The page's one way to the API token (mirrors _WindowBridge.api_token,
/// 6b321): answered only while the calling webview shows this app's own
/// origin, http://127.0.0.1:<the sidecar's port>. Another port, localhost,
/// https, a map tile site, about:blank: None. The capability in
/// capabilities/main.json already limits which origins may invoke at all;
/// this is the second gate, and the one that knows the port.
#[tauri::command]
fn api_token(webview: tauri::Webview, bridge: State<'_, Bridge>) -> Option<String> {
    let url = webview.url().ok()?;
    let guard = bridge.inner.lock().ok()?;
    let (port, token) = guard.as_ref()?;
    if url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(*port) {
        Some(token.clone())
    } else {
        None
    }
}

/// THE FOLDER PICKER (6b628, per Patrick: "it has to be usable"; the
/// walkthrough: the Code lane's Workspace wanted a typed path). The page asks
/// for a folder and gets the system's own panel: a folder's path, or None when
/// the person cancels. Answered only for the app's own origin, as api_token is
/// (the token's own check comes first). A sync command runs on the main
/// thread, where AppKit's panels must; off a Mac it is None and the page keeps
/// its typed path. The path goes nowhere from here: the page hands it to
/// /api/workspace/set, which checks it as it checks a typed one.
#[tauri::command]
fn pick_folder(webview: tauri::Webview, bridge: State<'_, Bridge>) -> Option<String> {
    api_token(webview, bridge)?;
    folder_panel()
}

#[cfg(target_os = "macos")]
fn folder_panel() -> Option<String> {
    use objc2::MainThreadMarker;
    use objc2_app_kit::{NSModalResponseOK, NSOpenPanel};
    use objc2_foundation::NSString;
    let mtm = MainThreadMarker::new()?;
    let panel = NSOpenPanel::openPanel(mtm);
    panel.setCanChooseDirectories(true);
    panel.setCanChooseFiles(false);
    panel.setAllowsMultipleSelection(false);
    panel.setCanCreateDirectories(true);
    panel.setPrompt(Some(&NSString::from_str("Choose")));
    if panel.runModal() != NSModalResponseOK {
        return None;
    }
    let path = panel.URL()?.path()?.to_string();
    if path.is_empty() { None } else { Some(path) }
}

#[cfg(not(target_os = "macos"))]
fn folder_panel() -> Option<String> {
    None
}

/// The page expects pywebview's bridge: window.pywebview.api.api_token()
/// returning a promise, and a `pywebviewready` event. This shim gives it
/// exactly that over Tauri's IPC, and the folder picker (6b628), and nothing
/// more; it holds no secret, so
/// it is safe in every frame the window may ever show. The page's own code
/// (apiTok) retries until a string of 32+ chars comes back.
const BRIDGE_SHIM: &str = r#"(function(){
  if (window.pywebview) return;
  function invoke(){
    var t = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    return t ? t("api_token") : Promise.reject(new Error("no bridge"));
  }
  function pick(){
    var t = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    return t ? t("pick_folder") : Promise.resolve(null);
  }
  var api = Object.freeze({ api_token: function(){ return invoke(); }, pick_folder: function(){ return pick(); } });
  Object.defineProperty(window, "pywebview", {
    value: Object.freeze({ platform: "tauri", api: api }), writable: false, configurable: false });
  var fire = function(){ try { window.dispatchEvent(new Event("pywebviewready")); } catch (e) {} };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fire);
  else fire();
})();"#;

// ------------------------------------------------------------------ sidecar

/// THE SERVER'S BYTECODE, CACHED (shell-polish). `python3 millenai.py`
/// compiles the whole 3.4 MB file at every launch, because a script run as
/// __main__ never uses a .pyc: 0.29 s of the sidecar's ~0.4 s boot on an M4
/// Pro, measured. This runs the same file the same way (as __main__, its
/// path as argv[0] and __file__, its folder first on sys.path, so its own
/// code and anything that relaunches it cannot tell), but takes the code
/// object through the import system's loader, which keeps it under
/// CONCORDE_PYCACHE (outside the signed bundle) and recompiles whenever the
/// source's mtime or size changes, or the Python does. Anything wrong with
/// the cache falls back to compiling, as before; the server's own errors
/// are never caught here. The path stays on the command line, so every
/// `pgrep -f millenai.py` still finds the server.
const BOOT_PY: &str = r#"import os as _cb_os, sys as _cb_sys
_cb_p = _cb_sys.argv[1]
_cb_sys.argv = _cb_sys.argv[1:]
_cb_sys.path[0] = _cb_os.path.dirname(_cb_p)
_cb_pre = _cb_os.environ.pop("CONCORDE_PYCACHE", "")
_cb_code = _cb_ld = None
try:
    import importlib.machinery as _cb_m
    _cb_ld = _cb_m.SourceFileLoader("__main__", _cb_p)
    if _cb_pre:
        _cb_old = (_cb_sys.pycache_prefix, _cb_sys.dont_write_bytecode)
        _cb_sys.pycache_prefix, _cb_sys.dont_write_bytecode = _cb_pre, False
        try:
            _cb_code = _cb_ld.get_code("__main__")
        finally:
            _cb_sys.pycache_prefix, _cb_sys.dont_write_bytecode = _cb_old
except Exception:
    _cb_code = None
if _cb_code is None:
    with open(_cb_p, "rb") as _cb_f:
        _cb_code = compile(_cb_f.read(), _cb_p, "exec")
_cb_g = _cb_sys.modules["__main__"].__dict__
_cb_g.update(__file__=_cb_p, __cached__=None)
if _cb_ld is not None:
    _cb_g["__loader__"] = _cb_ld
exec(_cb_code, _cb_g)
"#;

/// Where the cached bytecode lives: the user's Caches folder for the app
/// on a Mac, a dev copy's own folder for a dev copy (6b319), the data
/// folder's cache/ elsewhere.
fn pycache_dir(home: &Path) -> PathBuf {
    if std::env::var("MILLENAI_DEV").ok().as_deref() != Some("1") && cfg!(target_os = "macos") {
        return PathBuf::from(expand_tilde("~/Library/Caches/MillenAI/pyc"));
    }
    home.join("cache").join("pyc")
}

struct Sidecar {
    child: Mutex<Option<Child>>,
}

#[derive(serde::Deserialize)]
struct InstanceNote {
    port: u16,
    #[allow(dead_code)]
    key: String,
    token: String,
    pid: u32,
}

#[derive(serde::Deserialize)]
struct BootNote {
    code: String,
    pid: u32,
}

/// The data folder, by the same rules as millenai.py (_real_app_dir and
/// _dev_home): MILLENAI_DEV=1 with MILLENAI_HOME names a dev copy's own
/// folder; otherwise the real one. The server refuses anything half-set
/// (exit 2), so this only has to agree with it when it starts.
fn data_home() -> PathBuf {
    if std::env::var("MILLENAI_DEV").ok().as_deref() == Some("1") {
        if let Some(h) = std::env::var_os("MILLENAI_HOME") {
            let h = h.to_string_lossy().trim().to_string();
            if !h.is_empty() {
                return PathBuf::from(expand_tilde(&h));
            }
        }
    }
    real_app_dir()
}

fn real_app_dir() -> PathBuf {
    #[cfg(windows)]
    {
        let base = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("USERPROFILE").map(PathBuf::from))
            .unwrap_or_else(|| PathBuf::from("."));
        return base.join("MillenAI");
    }
    #[cfg(not(windows))]
    {
        PathBuf::from(expand_tilde("~/Library/Application Support/MillenAI"))
    }
}

fn expand_tilde(p: &str) -> String {
    if let Some(rest) = p.strip_prefix("~") {
        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            return format!("{}{}", home.to_string_lossy(), rest);
        }
    }
    p.to_string()
}

/// The Python that runs the server: CONCORDE_PYTHON when set (a dev's own
/// interpreter), else the app's venv, the same one the .app launcher built.
fn python_path() -> PathBuf {
    if let Some(p) = std::env::var_os("CONCORDE_PYTHON") {
        return PathBuf::from(p);
    }
    let venv = real_app_dir().join("venv");
    #[cfg(windows)]
    {
        return venv.join("Scripts").join("python.exe");
    }
    #[cfg(not(windows))]
    {
        venv.join("bin").join("python3")
    }
}

/// millenai.py: CONCORDE_SERVER when set; in a debug build the repo's copy
/// (two folders up from src-tauri); in a bundle the one in Resources.
fn server_script(app: &AppHandle) -> PathBuf {
    if let Some(p) = std::env::var_os("CONCORDE_SERVER") {
        return PathBuf::from(p);
    }
    if cfg!(debug_assertions) {
        let dev = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../millenai.py");
        if dev.exists() {
            return dev.canonicalize().unwrap_or(dev);
        }
    }
    app.path()
        .resource_dir()
        .map(|r| r.join("millenai.py"))
        .unwrap_or_else(|_| PathBuf::from("millenai.py"))
}

/// Where the sidecar's stdout and stderr go: the log folder the Python app
/// already uses on a Mac, the data folder's logs/ elsewhere. A dev build
/// inherits the terminal instead so `cargo tauri dev` shows the server.
/// A dev copy (MILLENAI_DEV=1) keeps its log in its own folder, like
/// everything else of its (6b319); the real app logs where the Python
/// app's launcher did on a Mac.
fn log_dir(home: &Path) -> PathBuf {
    if std::env::var("MILLENAI_DEV").ok().as_deref() == Some("1") {
        return home.join("logs");
    }
    #[cfg(target_os = "macos")]
    {
        return PathBuf::from(expand_tilde("~/Library/Logs/MillenAI"));
    }
    #[cfg(not(target_os = "macos"))]
    {
        home.join("logs")
    }
}

fn log_file(home: &Path) -> Option<std::fs::File> {
    if cfg!(debug_assertions) {
        return None;
    }
    let dir = log_dir(home);
    let _ = std::fs::create_dir_all(&dir);
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("shell-server.log"))
        .ok()
}

fn read_json<T: serde::de::DeserializeOwned>(p: &Path) -> Option<T> {
    let s = std::fs::read_to_string(p).ok()?;
    serde_json::from_str(&s).ok()
}

fn tail_of(p: &Path, n: usize) -> String {
    std::fs::read_to_string(p)
        .map(|s| {
            let lines: Vec<&str> = s.lines().collect();
            let from = lines.len().saturating_sub(n);
            lines[from..].join("\n")
        })
        .unwrap_or_default()
}

/// What each of the server's exit codes means (millenai.py's __main__).
fn exit_meaning(code: Option<i32>) -> &'static str {
    match code {
        Some(1) => "no free port",
        Some(2) => "a refused setting (MILLENAI_DEV, MILLENAI_HOME, MILLENAI_SHELL)",
        Some(3) => "another copy is using this data folder",
        Some(4) => "it wanted a window of its own (not running in shell mode)",
        Some(0) => "it stopped on its own",
        _ => "it crashed",
    }
}

fn show_error(app: &AppHandle, title: &str, why: &str, log: &str) {
    // the window shows about:blank until the server is up; the message
    // is a whole document of its own, loaded as a data: URL (no race
    // with the blank page's load), in the Horizon night. The sidecar now
    // starts before the window is built, so wait a moment for it.
    let esc = |s: &str| s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;");
    let html = format!(
        "<!doctype html><html><head><meta charset=utf-8><style>\
         html,body{{margin:0;height:100%;background:#03040a;color:#c9d0e2;\
         font:14px/1.5 -apple-system,system-ui,sans-serif}}\
         main{{max-width:640px;margin:18vh auto 0;padding:0 24px;white-space:pre-wrap}}\
         h1{{font-weight:500;font-size:19px;color:#f1f3fa;margin:0 0 12px}}\
         pre{{font:12px/1.5 ui-monospace,Menlo,monospace;color:#9aa6c6;\
         background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09);\
         border-radius:12px;padding:14px;overflow:auto;max-height:40vh}}\
         pre:empty{{display:none}}\
         </style></head><body><main><h1>{}</h1><p>{}</p><pre>{}</pre></main></body></html>",
        esc(title),
        esc(why),
        esc(log)
    );
    show_page(app, &html);
}

/// A document of the shell's own, loaded as a data: URL (no race with the
/// blank page's load, no origin that could reach the IPC).
fn show_page(app: &AppHandle, html: &str) {
    let Some(w) = main_window(app) else { return };
    let mut enc = String::with_capacity(html.len() * 3);
    for b in html.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => enc.push(b as char),
            _ => enc.push_str(&format!("%{:02X}", b)),
        }
    }
    if let Ok(u) = Url::parse(&format!("data:text/html;charset=utf-8,{}", enc)) {
        let _ = w.navigate(u);
    }
}

/// THE FIRST-RUN SCREEN (6b630, per Patrick: a fresh Mac sat on static
/// text and an empty log box, told the window would appear by itself while it
/// was open, and a failure asked for Terminal commands). The app's own
/// look: the wordmark on the night, Inter (the system face until the font
/// file is approved), one hairline bar for the whole setup, the step and
/// its percent under it, live from launch.sh's "@@" lines. If it fails:
/// a calm sentence, "Try again" and "Copy details" (the log's tail goes to
/// the clipboard, never onto the screen). Both are plain navigations to
/// concorde-shell://, which on_navigation answers and cancels; the page
/// holds no secret and can reach nothing else.
const SETUP_PAGE: &str = r##"<!doctype html><html><head><meta charset="utf-8"><style>
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
</script></body></html>"##;

/// What the first-run screen's two links ask of the shell (6b630): set by
/// on_navigation, read by the thread that waits on the failure screen.
static RETRY: AtomicBool = AtomicBool::new(false);
/// The words "Copy details" puts on the clipboard: the log's tail and the
/// machine, never shown on the screen.
static DETAILS: Mutex<String> = Mutex::new(String::new());
/// The bootstrap's process group while it runs, so Quit stops pip too.
static BOOT_PGID: AtomicU32 = AtomicU32::new(0);

fn set_details(app: &AppHandle, what: &str, log: &Path) {
    let os = Command::new("/usr/bin/sw_vers")
        .arg("-productVersion")
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_default();
    let text = format!(
        "ConcordeAI {} setup\n{}\nmacOS {} on {}\n\n{}\n",
        app.package_info().version,
        what,
        os,
        std::env::consts::ARCH,
        tail_of(log, 60)
    );
    if let Ok(mut d) = DETAILS.lock() {
        *d = text;
    }
}

/// The details onto the clipboard (pbcopy is part of macOS).
fn copy_details(app: &AppHandle) {
    let text = DETAILS.lock().map(|d| d.clone()).unwrap_or_default();
    #[cfg(target_os = "macos")]
    {
        use std::io::Write;
        if let Ok(mut c) = Command::new("/usr/bin/pbcopy").stdin(Stdio::piped()).spawn() {
            if let Some(mut i) = c.stdin.take() {
                let _ = i.write_all(text.as_bytes());
            }
            let _ = c.wait();
        }
    }
    let _ = text;
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.eval("window.__copied&&__copied()");
    }
}

/// THE VENV'S PYTHON, FROM ITS pyvenv.cfg (6b630; never by running it):
/// older than 3.11 is the system's, from a first run before the app had a
/// Python of its own. pip stops at mlx-lm 0.29.1 there (no Gemma 4, no
/// Qwen 3.5), so launch.sh makes it again from the app's own Python.
fn venv_too_old(py: &Path) -> bool {
    let Some(venv) = py.parent().and_then(|b| b.parent()) else { return false };
    let Ok(cfg) = std::fs::read_to_string(venv.join("pyvenv.cfg")) else { return false };
    cfg.lines()
        .filter_map(|l| l.split_once('='))
        .find(|(k, _)| k.trim() == "version" || k.trim() == "version_info")
        .and_then(|(_, v)| {
            let mut it = v.trim().split('.');
            let major: u32 = it.next()?.parse().ok()?;
            let minor: u32 = it.next()?.parse().ok()?;
            Some(major == 3 && minor < 11)
        })
        .unwrap_or(false)
}

/// The venv is there, new enough and finished: launch.sh leaves
/// .setup-unfinished in it until a setup completes, so one stopped half way
/// (Quit, a lost connection) is finished at the next launch (6b630).
fn venv_ready(py: &Path) -> bool {
    let unfinished = py
        .parent()
        .and_then(|b| b.parent())
        .map(|v| v.join(".setup-unfinished").exists())
        .unwrap_or(false);
    py.exists() && !venv_too_old(py) && !unfinished
}

/// FIRST RUN ON A FRESH MAC (6b537): the Python app's launcher built the
/// venv on first run (launch.sh, written by build_macos_app.sh). The shell
/// carries that same script in its Resources (build_shell_dmg.sh extracts
/// it) and runs it with --bootstrap --shell: it downloads the app's own
/// Python (pinned and hash-checked, 6b630), makes the venv, installs the
/// engine and exits 0, then the server starts as usual. A Mac that has the
/// Python app's venv already (an install updating into the shell) never
/// gets here unless that venv is too old (venv_too_old). macOS only: the
/// script is zsh and the venv path is a Mac's. None: nothing to run here
/// (another platform, a dev's CONCORDE_PYTHON, no launch.sh).
fn bootstrap_venv(app: &AppHandle, py: &Path) -> Option<bool> {
    if !cfg!(target_os = "macos") || std::env::var_os("CONCORDE_PYTHON").is_some() {
        return None;
    }
    let res = app.path().resource_dir().ok()?;
    let script = res.join("launch.sh");
    if !script.exists() {
        return None;
    }
    let log = expand_tilde("~/Library/Logs/MillenAI/bootstrap.log");
    show_page(app, SETUP_PAGE);
    mark("bootstrap started");
    let mut cmd = Command::new("/bin/zsh");
    cmd.arg(&script)
        .arg("--bootstrap")
        .arg("--shell")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            set_details(app, &format!("launch.sh didn't start: {}", e), Path::new(&log));
            return Some(false);
        }
    };
    BOOT_PGID.store(child.id(), Ordering::SeqCst);
    // the "@@" lines drive the screen, at most every 120 ms (pip's raw
    // progress is a line per chunk) and at once when the step changes
    let (mut i, mut n, mut name, mut pct, mut item) = (0u32, 0u32, String::new(), 0u32, String::new());
    let mut sent = Instant::now() - Duration::from_secs(1);
    if let Some(out) = child.stdout.take() {
        use std::io::BufRead;
        for line in std::io::BufReader::new(out).lines().map_while(Result::ok) {
            if QUITTING.load(Ordering::SeqCst) {
                break;
            }
            let mut w = line.splitn(3, ' ');
            let (tag, a, b) = (w.next().unwrap_or(""), w.next().unwrap_or(""), w.next().unwrap_or(""));
            let mut now = false;
            match tag {
                "@@steps" => n = a.parse().unwrap_or(n),
                "@@step" => {
                    i = a.parse().unwrap_or(i);
                    name = b.to_string();
                    pct = 0;
                    item.clear();
                    now = true;
                    mark(&format!("bootstrap step {} {}", i, name));
                }
                "@@pct" => pct = a.parse().unwrap_or(pct),
                "@@item" => item = a.to_string(),
                _ => continue,
            }
            if now || sent.elapsed() > Duration::from_millis(120) {
                sent = Instant::now();
                if let Some(w) = app.get_webview_window("main") {
                    let js = format!(
                        "window.__boot&&__boot({},{},{},{},{})",
                        i,
                        n,
                        serde_json::to_string(&name).unwrap_or_default(),
                        pct,
                        serde_json::to_string(&item).unwrap_or_default()
                    );
                    let _ = w.eval(&js);
                }
            }
        }
    }
    let ok = child.wait().map(|s| s.success()).unwrap_or(false);
    BOOT_PGID.store(0, Ordering::SeqCst);
    mark(&format!("bootstrap ended ({})", if ok { "ok" } else { "failed" }));
    if ok && py.exists() {
        return Some(true);
    }
    set_details(app, "The first-run setup stopped.", Path::new(&log));
    Some(false)
}

/// The failure screen's wait: true when "Try again" is pressed, false on
/// Quit.
fn wait_retry() -> bool {
    RETRY.store(false, Ordering::SeqCst);
    loop {
        if QUITTING.load(Ordering::SeqCst) {
            return false;
        }
        if RETRY.swap(false, Ordering::SeqCst) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// The server's Python, there and new enough, or the first-run setup until
/// it is (6b630): a failure waits on its screen for "Try again". false
/// only on Quit.
fn ensure_python(app: &AppHandle, py: &Path) -> bool {
    loop {
        if venv_ready(py) {
            return true;
        }
        let (title, why) = match bootstrap_venv(app, py) {
            Some(true) => return true,
            // an old venv and nothing to rebuild it with: it still runs
            None if py.exists() => return true,
            None => {
                // a dev build's way round it is in the log, never on the screen
                eprintln!("[shell] no Python at {}; a dev build names one with CONCORDE_PYTHON", py.display());
                set_details(app, &format!("No Python at {}.", py.display()), Path::new(""));
                show_page(app, SETUP_PAGE);
                ("ConcordeAI isn’t set up yet", "Something it needs is missing. Download ConcordeAI again and replace this copy.")
            }
            Some(false) => ("Setup didn’t finish", "Check your internet connection, then try again."),
        };
        if let Some(w) = main_window(app) {
            // the page may still be loading: say it once it can hear
            for _ in 0..20 {
                std::thread::sleep(Duration::from_millis(50));
                let _ = w.eval(&format!(
                    "window.__failed&&__failed({},{})",
                    serde_json::to_string(title).unwrap_or_default(),
                    serde_json::to_string(why).unwrap_or_default()
                ));
            }
        }
        if !wait_retry() {
            return false;
        }
    }
}

/// The window, waiting up to 3 s for it: the sidecar starts before the
/// window is built (shell-polish), so an early message may race it.
fn main_window(app: &AppHandle) -> Option<tauri::WebviewWindow> {
    let t0 = Instant::now();
    loop {
        if let Some(w) = app.get_webview_window("main") {
            return Some(w);
        }
        if t0.elapsed() > Duration::from_secs(3) {
            return None;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Whether a stop is worth a restart: not the updater's clean exit (0,
/// which quits the shell), not a setting the server refuses (2), not
/// another copy holding this data folder (3: the shell only restarts once
/// the old process is gone and its lock with it, so a 3 is a real second
/// copy) and not a launch that wanted a window of its own (4): those stop
/// the same way every time. A crash, a signal or a busy port (1) may well
/// start the next time.
fn restartable(code: Option<i32>) -> bool {
    !matches!(code, Some(0) | Some(2) | Some(3) | Some(4))
}

/// A dev copy (MILLENAI_DEV=1), the server's own rule (6b319).
fn dev_copy() -> bool {
    std::env::var("MILLENAI_DEV").ok().as_deref() == Some("1")
}

fn exit_label(code: Option<i32>) -> String {
    code.map(|c| c.to_string()).unwrap_or_else(|| "signal".into())
}

/// A stop the shell will try to recover from: the veil goes up over the
/// page and a new sidecar starts after a short pause (1 s, 2 s, 4 s), or,
/// after MAX_RESTARTS tries in a row, the plain error page says so.
fn recover(app: AppHandle, code: Option<i32>, log_path: &Path) {
    if QUITTING.load(Ordering::SeqCst) {
        return;
    }
    let n = RESTARTS.fetch_add(1, Ordering::SeqCst) + 1;
    if !restartable(code) || n > MAX_RESTARTS {
        let why = if restartable(code) {
            format!(
                "It kept stopping, even after {} restarts: {} (exit {}). Quit ConcordeAI and open it again; the log below says why.",
                MAX_RESTARTS,
                exit_meaning(code),
                exit_label(code)
            )
        } else {
            format!("The server stopped: {} (exit {}).", exit_meaning(code), exit_label(code))
        };
        show_error(&app, "ConcordeAI's server stopped", &why, &tail_of(log_path, 30));
        return;
    }
    mark(&format!("sidecar stopped (exit {}), restart {} of {}", exit_label(code), n, MAX_RESTARTS));
    if let Some(w) = main_window(&app) {
        let _ = w.eval(RECONNECTING);
    }
    std::thread::sleep(Duration::from_secs(1 << (n - 1)));
    start_sidecar(app);
}

/// THE SIDECAR'S END IS THE SHELL'S (6b537). The updater (millenai.py's
/// _do_update) exits the server with 0 and leaves a helper waiting for
/// this process to be gone before it swaps the bundle and relaunches it
/// with `open -n`; a clean exit of the server therefore quits the shell,
/// the way the Python app's own exit closed its window. Any other end
/// (a crash, a signal) starts it again behind a "Reconnecting…" veil, up
/// to MAX_RESTARTS times in a row, then shows the log's tail instead of a
/// dead page (shell-polish). Quit takes the child out of the state first,
/// so the watcher simply stops.
fn watch_sidecar(app: AppHandle, log_path: PathBuf) {
    let since = Instant::now();
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_millis(200));
        let Some(sc) = app.try_state::<Sidecar>() else { return };
        let status = {
            let Ok(mut g) = sc.child.lock() else { return };
            let Some(child) = g.as_mut() else { return };
            match child.try_wait() {
                Ok(Some(s)) => {
                    g.take();
                    s
                }
                Ok(None) => {
                    // a run that held up this long was healthy: the next
                    // stop starts a fresh tally of tries
                    if since.elapsed() > STEADY {
                        RESTARTS.store(0, Ordering::SeqCst);
                    }
                    continue;
                }
                Err(_) => return,
            }
        };
        if status.code() == Some(0) {
            app.exit(0);
        } else {
            recover(app, status.code(), &log_path);
        }
        return;
    });
}

/// Start the server and, once it has written its notes, point the window
/// at it. On its own thread: the window is up and dark meanwhile (it is
/// built while Python starts, shell-polish).
fn start_sidecar(app: AppHandle) {
    std::thread::spawn(move || {
        let home = data_home();
        let py = python_path();
        let script = server_script(&app);
        // the first run's setup, or a venv too old for the engine
        // (6b630): its own screen, live, and "Try again" if it fails;
        // nothing on it is a command or a setting to type
        if !ensure_python(&app, &py) {
            return;
        }
        if !script.exists() {
            // a dev's CONCORDE_SERVER is in the log, not on the screen
            eprintln!("[shell] no server at {}", script.display());
            show_error(
                &app,
                "ConcordeAI is missing a file",
                "Download ConcordeAI again and replace this copy.",
                "",
            );
            return;
        }
        let run_dir = home.join("run");
        let note_path = run_dir.join("instance.json");
        let boot_path = run_dir.join("boot.json");
        let log_path = log_dir(&home).join("shell-server.log");

        let mut cmd = Command::new(&py);
        cmd.arg("-c")
            .arg(BOOT_PY)
            .arg(&script)
            .env("CONCORDE_PYCACHE", pycache_dir(&home))
            .current_dir(script.parent().unwrap_or(Path::new(".")))
            .env("MILLENAI_SHELL", "tauri")
            .env("PYTHONUNBUFFERED", "1");
        // MILLENAI_* (DEV, HOME, PORT, TEST_HOOKS…) pass through untouched:
        // the dev-copy rules are the server's, and it refuses what it must
        match log_file(&home) {
            Some(f) => {
                let f2 = f.try_clone().ok();
                cmd.stdout(Stdio::from(f));
                if let Some(f2) = f2 {
                    cmd.stderr(Stdio::from(f2));
                }
            }
            None => {
                cmd.stdout(Stdio::inherit()).stderr(Stdio::inherit());
            }
        }
        cmd.stdin(Stdio::null());
        if QUITTING.load(Ordering::SeqCst) {
            return;
        }
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                show_error(&app, "ConcordeAI couldn't start its server", &e.to_string(), "");
                return;
            }
        };
        mark("sidecar spawned");
        let pid = child.id();
        let t0 = Instant::now();
        let mut note: Option<InstanceNote> = None;
        let mut boot: Option<BootNote> = None;
        while t0.elapsed() < SERVER_START_TIMEOUT {
            if QUITTING.load(Ordering::SeqCst) {
                let _ = child.kill();
                let _ = child.wait();
                return;
            }
            if let Ok(Some(status)) = child.try_wait() {
                // a stop before it was ready is tried again like any other
                // (a port still held by the copy that just died); one that
                // will stop the same way is said at once
                if !restartable(status.code()) || RESTARTS.load(Ordering::SeqCst) >= MAX_RESTARTS {
                    let why = format!(
                        "The server stopped before it was ready: {} (exit {}).",
                        exit_meaning(status.code()),
                        exit_label(status.code())
                    );
                    show_error(&app, "ConcordeAI couldn't start", &why, &tail_of(&log_path, 30));
                } else {
                    recover(app, status.code(), &log_path);
                }
                return;
            }
            if note.is_none() {
                note = read_json::<InstanceNote>(&note_path).filter(|n| n.pid == pid);
            }
            if note.is_some() && boot.is_none() {
                boot = read_json::<BootNote>(&boot_path).filter(|b| b.pid == pid);
            }
            if note.is_some() && boot.is_some() {
                break;
            }
            std::thread::sleep(Duration::from_millis(15));
        }
        let (Some(note), Some(boot)) = (note, boot) else {
            show_error(
                &app,
                "ConcordeAI's server didn't answer",
                "It started but never wrote run/instance.json and run/boot.json.",
                &tail_of(&log_path, 30),
            );
            let _ = child.kill();
            return;
        };
        mark("sidecar ready (notes read)");
        // the boot code is spent by the first load; it is never kept here
        let _ = std::fs::remove_file(&boot_path);
        if let Some(sc) = app.try_state::<Sidecar>() {
            *sc.child.lock().unwrap() = Some(child);
        }
        watch_sidecar(app.clone(), log_path.clone());
        if let Some(b) = app.try_state::<Bridge>() {
            *b.inner.lock().unwrap() = Some((note.port, note.token.clone()));
        }
        let url = format!("http://127.0.0.1:{}/?boot={}", note.port, boot.code);
        if let Some(w) = main_window(&app) {
            if let Ok(u) = Url::parse(&url) {
                let _ = w.navigate(u);
            }
        }
    });
}

/// Stop the server: SIGTERM first (its handler saves a streaming answer,
/// stops its engines and drops its note), SIGKILL if it hasn't gone in 8 s.
/// QUITTING first, so neither the watcher nor a start under way brings it
/// back.
fn stop_sidecar(app: &AppHandle) {
    QUITTING.store(true, Ordering::SeqCst);
    let Some(sc) = app.try_state::<Sidecar>() else { return };
    // a first-run setup under way stops with the app, pip and all (6b630):
    // its process group, so no download is left running unseen
    #[cfg(unix)]
    {
        let pg = BOOT_PGID.swap(0, Ordering::SeqCst);
        if pg > 0 {
            unsafe {
                libc::kill(-(pg as libc::pid_t), libc::SIGTERM);
            }
        }
    }
    let child = sc.child.lock().ok().and_then(|mut g| g.take());
    let Some(mut child) = child else { return };
    #[cfg(unix)]
    unsafe {
        libc::kill(child.id() as libc::pid_t, libc::SIGTERM);
    }
    #[cfg(not(unix))]
    {
        let _ = child.kill();
    }
    let t0 = Instant::now();
    while t0.elapsed() < Duration::from_secs(8) {
        if let Ok(Some(_)) = child.try_wait() {
            return;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    let _ = child.kill();
    let _ = child.wait();
}

// ------------------------------------------------------------------ window

/// THE MENU BAR, CURATED (per Patrick: "basically looked like one perfect
/// window"): the app menu, Edit, View and Window, nothing else. No File or
/// Help, no Services, no developer items in a release. Every item works:
/// Edit's are the native responders (without them copy and paste stop in
/// the webview); Settings, Import Chats (per Patrick: bring your chats
/// integrated, not a main feature), Check for Updates and Toggle Sidebar
/// call the page's own functions (menu_action). The app's name comes from the bundle's
/// product name, so the Nightly's menu says "ConcordeAI Nightly"; the
/// Nightly's updater is off, so its Check for Updates is greyed.
fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let name = app.package_info().name.clone();
    let nightly = app.config().identifier.ends_with(".nightly");
    // About opens the page's own About (Settings opens there): no native panel, so no
    // standard title bar anywhere (per Patrick: one window, its title bar hidden)
    let about = MenuItem::with_id(app, "about", format!("About {}", name), true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", "Settings\u{2026}", true, Some("CmdOrCtrl+,"))?;
    let import = MenuItem::with_id(app, "import", "Import Chats\u{2026}", true, None::<&str>)?;
    let updates = MenuItem::with_id(app, "updates", "Check for Updates\u{2026}", !nightly, None::<&str>)?;
    let app_menu = Submenu::with_items(
        app,
        &name,
        true,
        &[
            &about,
            &PredefinedMenuItem::separator(app)?,
            &settings,
            &import,
            &updates,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::hide(app, Some(&format!("Hide {}", name)))?,
            &PredefinedMenuItem::hide_others(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::quit(app, Some(&format!("Quit {}", name)))?,
        ],
    )?;
    let edit = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;
    let sidebar = MenuItem::with_id(app, "sidebar", "Toggle Sidebar", true, Some("CmdOrCtrl+Backslash"))?;
    let fullscreen = PredefinedMenuItem::fullscreen(app, Some("Enter Full Screen"))?;
    #[cfg(debug_assertions)]
    let reload = MenuItem::with_id(app, "reload", "Reload", true, Some("CmdOrCtrl+R"))?;
    #[cfg(debug_assertions)]
    let view = Submenu::with_items(app, "View", true, &[&sidebar, &fullscreen, &reload])?;
    #[cfg(not(debug_assertions))]
    let view = Submenu::with_items(app, "View", true, &[&sidebar, &fullscreen])?;
    let window = Submenu::with_items(
        app,
        "Window",
        true,
        &[
            &PredefinedMenuItem::minimize(app, None)?,
            &PredefinedMenuItem::maximize(app, Some("Zoom"))?,
        ],
    )?;
    Menu::with_items(app, &[&app_menu, &edit, &view, &window])
}

/// What a menu item of the page's asks of it: the same functions the
/// page's own buttons call (Settings opens on About, the update check is
/// About's button, the sidebar folds the one way the header's button does).
fn menu_action(id: &str) -> Option<&'static str> {
    match id {
        "settings" | "about" => Some("typeof openAbout==='function'&&openAbout()"),
        "updates" => Some(
            "typeof openAbout==='function'&&(openAbout(),document.getElementById('about-check')&&document.getElementById('about-check').click())",
        ),
        "import" => Some(
            "typeof impOpen==='function'&&(document.getElementById('about-veil').hidden=true,impOpen())",
        ),
        "sidebar" => Some("typeof hzSidebarToggle==='function'&&hzSidebarToggle()"),
        "reload" => Some("location.reload()"),
        _ => None,
    }
}

/// Full screen hides the traffic lights, so the page drops their zone
/// (html.tl-fs); the class follows every resize and every page load.
fn mark_fullscreen(w: &tauri::WebviewWindow) {
    if let Ok(fs) = w.is_fullscreen() {
        let _ = w.eval(&format!(
            "document.documentElement.classList.toggle('tl-fs',{})",
            fs
        ));
    }
}

// ------------------------------------------------------------ window state

/// WHERE THE WINDOW WAS (shell-polish): its outer position and inner size
/// in points, and whether it was zoomed, kept in the data folder's
/// shell-window.json (a dev copy's in its own folder) and put back at the
/// next launch when it still lands on a screen. Full screen and a
/// minimised window are never recorded; a zoomed one keeps the size it
/// unzooms to.
#[derive(serde::Serialize, serde::Deserialize, Clone, Copy, Default)]
struct WinState {
    x: f64,
    y: f64,
    w: f64,
    h: f64,
    #[serde(default)]
    zoomed: bool,
}

#[derive(Default)]
struct WinMemo {
    last: Mutex<Option<WinState>>,
}

fn win_state_path() -> PathBuf {
    data_home().join("shell-window.json")
}

fn note_window(w: &tauri::WebviewWindow) {
    if w.is_fullscreen().unwrap_or(false) || w.is_minimized().unwrap_or(false) || !w.is_visible().unwrap_or(true) {
        return;
    }
    let Some(memo) = w.app_handle().try_state::<WinMemo>() else { return };
    let Ok(mut g) = memo.last.lock() else { return };
    if w.is_maximized().unwrap_or(false) {
        if let Some(s) = g.as_mut() {
            s.zoomed = true;
        }
        return;
    }
    let (Ok(sf), Ok(pos), Ok(size)) = (w.scale_factor(), w.outer_position(), w.inner_size()) else { return };
    *g = Some(WinState {
        x: pos.x as f64 / sf,
        y: pos.y as f64 / sf,
        w: size.width as f64 / sf,
        h: size.height as f64 / sf,
        zoomed: false,
    });
}

fn save_window_state(app: &AppHandle) {
    let Some(memo) = app.try_state::<WinMemo>() else { return };
    let Some(s) = memo.last.lock().ok().and_then(|g| *g) else { return };
    if let Ok(js) = serde_json::to_string(&s) {
        let p = win_state_path();
        let tmp = p.with_extension("json.tmp");
        if std::fs::write(&tmp, js).is_ok() {
            let _ = std::fs::rename(&tmp, &p);
        }
    }
}

/// The saved place, if it is sane and at least 160 × 120 points of it
/// fall on a screen that is there now (a display unplugged since).
fn restored_window(app: &AppHandle) -> Option<WinState> {
    let s: WinState = read_json(&win_state_path())?;
    if !(s.x.is_finite() && s.y.is_finite() && s.w.is_finite() && s.h.is_finite()) {
        return None;
    }
    let s = WinState { w: s.w.clamp(940.0, 10000.0), h: s.h.clamp(620.0, 10000.0), ..s };
    let on_screen = app.available_monitors().ok()?.iter().any(|m| {
        let sf = m.scale_factor();
        let (mx, my) = (m.position().x as f64 / sf, m.position().y as f64 / sf);
        let (mw, mh) = (m.size().width as f64 / sf, m.size().height as f64 / sf);
        let ow = (s.x + s.w).min(mx + mw) - s.x.max(mx);
        let oh = (s.y + s.h).min(my + mh) - s.y.max(my);
        ow >= 160.0 && oh >= 120.0
    });
    on_screen.then_some(s)
}

/// Bring the window back: a Dock click, a second launch, or a closed
/// window being reopened.
fn show_main(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

fn open_window(app: &AppHandle) -> tauri::Result<()> {
    let nav_app = app.clone();
    let restored = restored_window(app);
    let b = WebviewWindowBuilder::new(app, "main", WebviewUrl::External("about:blank".parse().unwrap()))
        .title("ConcordeAI")
        .inner_size(1320.0, 860.0)
        .min_inner_size(940.0, 620.0);
    let b = match restored {
        Some(s) => b.inner_size(s.w, s.h).position(s.x, s.y),
        None => b.center(),
    };
    let mut b = b
        .background_color(NIGHT)
        .theme(Some(tauri::Theme::Dark))
        .initialization_script(BRIDGE_SHIM)
        .initialization_script(FADE_IN)
        // only the app's own origin and the shell's own page load in this
        // window; anything else (a link in an answer) goes to the browser
        .on_page_load(|w, payload| {
            if cfg!(debug_assertions) {
                eprintln!("[shell] page {:?} {}", payload.event(), payload.url());
            }
            if matches!(payload.event(), tauri::webview::PageLoadEvent::Finished) {
                mark_fullscreen(&w);
                if payload.url().host_str() == Some("127.0.0.1") {
                    mark("page loaded");
                }
            }
        })
        .on_navigation(move |url: &Url| {
            if cfg!(debug_assertions) {
                eprintln!("[shell] navigate {}", url);
            }
            // the first-run screen's two links (6b630): answered here,
            // never loaded
            if url.scheme() == "concorde-shell" {
                match url.host_str() {
                    Some("retry") => RETRY.store(true, Ordering::SeqCst),
                    Some("copy") => {
                        let a = nav_app.clone();
                        std::thread::spawn(move || copy_details(&a));
                    }
                    _ => {}
                }
                return false;
            }
            let own = (url.scheme() == "http" && url.host_str() == Some("127.0.0.1"))
                || url.scheme() == "tauri"
                || url.host_str() == Some("tauri.localhost")
                || url.scheme() == "about"
                || url.scheme() == "data";
            if !own && (url.scheme() == "http" || url.scheme() == "https" || url.scheme() == "mailto") {
                let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
            }
            own
        })
        // exports download straight to the Downloads folder (pywebview's
        // ALLOW_DOWNLOADS, 6b295)
        .on_download(|webview, event| {
            if let DownloadEvent::Requested { url, destination } = event {
                let name = url
                    .path_segments()
                    .and_then(|s| s.last().map(|x| x.to_string()))
                    .filter(|n| !n.is_empty())
                    .unwrap_or_else(|| "download".into());
                let dir = webview
                    .path()
                    .download_dir()
                    .unwrap_or_else(|_| std::env::temp_dir());
                let mut p = dir.join(&name);
                let mut n = 1;
                while p.exists() {
                    p = dir.join(format!("{} ({})", name, n));
                    n += 1;
                }
                *destination = p;
            }
            true
        });
    #[cfg(target_os = "macos")]
    {
        // the page IS the bar: the content runs under a transparent title
        // bar (Overlay), the title hidden, the traffic lights seated on the
        // header row (TL_X, TL_Y). The page keeps their zone clear and its
        // bare header drags the window (data-tauri-drag-region, granted
        // start-dragging in capabilities/main.json).
        b = b
            .title_bar_style(tauri::TitleBarStyle::Overlay)
            .hidden_title(true)
            .traffic_light_position(tauri::LogicalPosition::new(TL_X, TL_Y));
    }
    let _ = &mut b;
    let w = b.build()?;
    mark("window built");
    if restored.map(|s| s.zoomed).unwrap_or(false) {
        let _ = w.maximize();
    }
    note_window(&w);
    let w2 = w.clone();
    w.on_window_event(move |ev| match ev {
        tauri::WindowEvent::Resized(_) => {
            mark_fullscreen(&w2);
            note_window(&w2);
        }
        tauri::WindowEvent::Moved(_) => note_window(&w2),
        // CLOSING HIDES IT ON A MAC (shell-polish): the app stays in the
        // Dock with its server and the page as they were (an answer still
        // streaming goes on), and a Dock click or a second launch brings
        // the window back (show_main). Quit is Cmd+Q, as in any Mac app.
        // Full screen is left first, so no empty Space is left behind.
        #[cfg(target_os = "macos")]
        tauri::WindowEvent::CloseRequested { api, .. } => {
            api.prevent_close();
            save_window_state(&w2.app_handle());
            if w2.is_fullscreen().unwrap_or(false) {
                let _ = w2.set_fullscreen(false);
                let w3 = w2.clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(800));
                    let _ = w3.hide();
                });
            } else {
                let _ = w2.hide();
            }
        }
        _ => {}
    });
    Ok(())
}

/// A debug build's measuring hook (compiled out of a release): with
/// CONCORDE_SHELL_PROBE=<dir>, each `cmd-<n>.js` dropped there is evaluated
/// in the window's page (the real WKWebView) and its value, as JSON, lands
/// in `out-<n>.json`. Scripts outside the app measure the window's corner
/// and drive the sidebar this way; nothing here exists in a shipped app.
#[cfg(debug_assertions)]
fn probe_hook(app: AppHandle) {
    let Some(dir) = std::env::var_os("CONCORDE_SHELL_PROBE").map(PathBuf::from) else { return };
    std::thread::spawn(move || {
        let mut n: u64 = 1;
        loop {
            let cmd = dir.join(format!("cmd-{}.js", n));
            if let Ok(js) = std::fs::read_to_string(&cmd) {
                let _ = std::fs::remove_file(&cmd);
                let out = dir.join(format!("out-{}.json", n));
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.eval_with_callback(js, move |r| {
                        let _ = std::fs::write(&out, r);
                    });
                }
                n += 1;
            } else {
                std::thread::sleep(Duration::from_millis(40));
            }
        }
    });
}

fn main() {
    mark("process start");
    let mut ctx = tauri::generate_context!();
    // ONE WINDOW PER DATA FOLDER, NOT PER BUNDLE ID (shell-polish). The
    // single-instance plugin keys its socket on the identifier alone
    // (/tmp/com_millen_millenai_si.sock), so any shell with that id, a dev
    // copy or a test build of another folder, would take a launch meant for
    // another: the new process hands off to it and exits 0, the updater's
    // `open -n` relaunch included. A dev copy is kept apart from everything
    // else (6b319), so its identifier, as this process knows it, carries
    // its folder: a second launch on the same folder still brings that
    // window forward, and nothing else ever answers it. The identifier
    // feeds only Tauri's own app-data paths, which the shell never uses;
    // the bundle's (Info.plist, WebKit's storage) is unchanged.
    if dev_copy() {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        data_home().hash(&mut h);
        let id = format!("{}.dev{:016x}", ctx.config().identifier, h.finish());
        ctx.config_mut().identifier = id;
    }
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // a second launch brings the window forward, like
            // /api/window/focus did for a second Python launch (6b310),
            // and back from a close (it is only hidden)
            show_main(app);
        }))
        .plugin(tauri_plugin_opener::init())
        .manage(Bridge::default())
        .manage(Sidecar { child: Mutex::new(None) })
        .manage(WinMemo::default())
        .invoke_handler(tauri::generate_handler![api_token, pick_folder])
        .setup(|app| {
            mark("setup");
            let handle = app.handle().clone();
            let menu = build_menu(&handle)?;
            app.set_menu(menu)?;
            app.on_menu_event(|app, ev| {
                if let Some(js) = menu_action(ev.id().as_ref()) {
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.eval(js);
                    }
                }
            });
            // Python first, the window while it boots (shell-polish): the
            // sidecar's start is most of a launch, so it starts the moment
            // the app is set up and the window is built beside it
            start_sidecar(handle.clone());
            open_window(&handle)?;
            // a debug build's test hook: CONCORDE_SHELL_QUIT_AFTER=<secs>
            // quits through the same path as Cmd+Q (ExitRequested), so a
            // script can prove the sidecar stops with the shell
            if cfg!(debug_assertions) {
                if let Some(s) = std::env::var("CONCORDE_SHELL_QUIT_AFTER")
                    .ok()
                    .and_then(|v| v.parse::<f64>().ok())
                {
                    std::thread::spawn(move || {
                        std::thread::sleep(Duration::from_secs_f64(s));
                        handle.exit(0);
                    });
                }
            }
            #[cfg(debug_assertions)]
            probe_hook(app.handle().clone());
            Ok(())
        })
        .build(ctx)
        .expect("ConcordeAI's window couldn't be built");
    app.run(|app, event| {
        // a Quit Apple event (Cmd+Q, the Dock's Quit) may end the loop with
        // Exit alone, so the place is written at either
        if matches!(event, RunEvent::ExitRequested { .. } | RunEvent::Exit) {
            save_window_state(app);
        }
        match event {
            RunEvent::ExitRequested { .. } | RunEvent::Exit => stop_sidecar(app),
            // a Dock click with the window closed opens it again
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => show_main(app),
            _ => {}
        }
    });
}
