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
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::menu::{AboutMetadata, Menu, MenuItem, PredefinedMenuItem, Submenu};
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

/// The page expects pywebview's bridge: window.pywebview.api.api_token()
/// returning a promise, and a `pywebviewready` event. This shim gives it
/// exactly that over Tauri's IPC and nothing more; it holds no secret, so
/// it is safe in every frame the window may ever show. The page's own code
/// (apiTok) retries until a string of 32+ chars comes back.
const BRIDGE_SHIM: &str = r#"(function(){
  if (window.pywebview) return;
  function invoke(){
    var t = window.__TAURI__ && window.__TAURI__.core && window.__TAURI__.core.invoke;
    return t ? t("api_token") : Promise.reject(new Error("no bridge"));
  }
  var api = Object.freeze({ api_token: function(){ return invoke(); } });
  Object.defineProperty(window, "pywebview", {
    value: Object.freeze({ platform: "tauri", api: api }), writable: false, configurable: false });
  var fire = function(){ try { window.dispatchEvent(new Event("pywebviewready")); } catch (e) {} };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", fire);
  else fire();
})();"#;

// ------------------------------------------------------------------ sidecar

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
    // with the blank page's load), in the Horizon night
    let Some(w) = app.get_webview_window("main") else { return };
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
         </style></head><body><main><h1>{}</h1><p>{}</p><pre>{}</pre></main></body></html>",
        esc(title),
        esc(why),
        esc(log)
    );
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

/// FIRST RUN ON A FRESH MAC (6b537): the Python app's launcher built the
/// venv on first run (launch.sh, written by build_macos_app.sh). The shell
/// carries that same script in its Resources (build_shell_dmg.sh extracts
/// it) and runs it with --bootstrap: it makes the venv, installs the
/// engine and exits 0, then the server starts as usual. A Mac that has the
/// Python app's venv already (an install updating into the shell) never
/// gets here. macOS only: the script is zsh and the venv path is a Mac's.
fn bootstrap_venv(app: &AppHandle, py: &Path) -> bool {
    if !cfg!(target_os = "macos") || std::env::var_os("CONCORDE_PYTHON").is_some() {
        return false;
    }
    let Ok(res) = app.path().resource_dir() else { return false };
    let script = res.join("launch.sh");
    if !script.exists() {
        return false;
    }
    show_error(
        app,
        "Setting up ConcordeAI",
        "First run: building the AI engine. This needs the internet and takes a few minutes; the window opens on its own when it is ready.",
        "",
    );
    let ok = Command::new("/bin/zsh")
        .arg(&script)
        .arg("--bootstrap")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false);
    ok && py.exists()
}

/// THE SIDECAR'S END IS THE SHELL'S (6b537). The updater (millenai.py's
/// _do_update) exits the server with 0 and leaves a helper waiting for
/// this process to be gone before it swaps the bundle and relaunches it
/// with `open -n`; a clean exit of the server therefore quits the shell,
/// the way the Python app's own exit closed its window. Any other end
/// (a crash, a signal) shows the log's tail instead of a dead page. Quit
/// takes the child out of the state first, so the watcher simply stops.
fn watch_sidecar(app: AppHandle, log_path: PathBuf) {
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
                Ok(None) => continue,
                Err(_) => return,
            }
        };
        if status.code() == Some(0) {
            app.exit(0);
        } else {
            let why = format!(
                "The server stopped: {} (exit {}).",
                exit_meaning(status.code()),
                status.code().map(|c| c.to_string()).unwrap_or_else(|| "signal".into())
            );
            show_error(&app, "ConcordeAI's server stopped", &why, &tail_of(&log_path, 30));
        }
        return;
    });
}

/// Start the server and, once it has written its notes, point the window
/// at it. On its own thread: the window is already up and dark meanwhile.
fn start_sidecar(app: AppHandle) {
    std::thread::spawn(move || {
        let home = data_home();
        let py = python_path();
        let script = server_script(&app);
        if !py.exists() && !bootstrap_venv(&app, &py) {
            show_error(
                &app,
                "ConcordeAI can't find its Python engine",
                &format!(
                    "Expected {}. Open the Python app once (it builds its venv), or set CONCORDE_PYTHON.",
                    py.display()
                ),
                "",
            );
            return;
        }
        if !script.exists() {
            show_error(
                &app,
                "ConcordeAI can't find its server",
                &format!("Expected {}. Set CONCORDE_SERVER to a millenai.py.", script.display()),
                "",
            );
            return;
        }
        let run_dir = home.join("run");
        let note_path = run_dir.join("instance.json");
        let boot_path = run_dir.join("boot.json");
        let log_path = log_dir(&home).join("shell-server.log");

        let mut cmd = Command::new(&py);
        cmd.arg(&script)
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
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                show_error(&app, "ConcordeAI couldn't start its server", &e.to_string(), "");
                return;
            }
        };
        let pid = child.id();
        let t0 = Instant::now();
        let mut note: Option<InstanceNote> = None;
        let mut boot: Option<BootNote> = None;
        while t0.elapsed() < SERVER_START_TIMEOUT {
            if let Ok(Some(status)) = child.try_wait() {
                let why = format!(
                    "The server stopped before it was ready: {} (exit {}).",
                    exit_meaning(status.code()),
                    status.code().map(|c| c.to_string()).unwrap_or_else(|| "signal".into())
                );
                show_error(&app, "ConcordeAI couldn't start", &why, &tail_of(&log_path, 30));
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
        if let Some(w) = app.get_webview_window("main") {
            if let Ok(u) = Url::parse(&url) {
                let _ = w.navigate(u);
            }
        }
    });
}

/// Stop the server: SIGTERM first (its handler saves a streaming answer,
/// stops its engines and drops its note), SIGKILL if it hasn't gone in 8 s.
fn stop_sidecar(app: &AppHandle) {
    let Some(sc) = app.try_state::<Sidecar>() else { return };
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

fn build_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let about = AboutMetadata {
        name: Some("ConcordeAI".into()),
        version: Some(app.package_info().version.to_string()),
        comments: Some("Local-first AI. Models run on your own machine.".into()),
        ..Default::default()
    };
    let app_menu = Submenu::with_items(
        app,
        "ConcordeAI",
        true,
        &[
            &PredefinedMenuItem::about(app, Some("About ConcordeAI"), Some(about))?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::services(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::hide(app, None)?,
            &PredefinedMenuItem::hide_others(app, None)?,
            &PredefinedMenuItem::show_all(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::quit(app, Some("Quit ConcordeAI"))?,
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
    let reload = MenuItem::with_id(app, "reload", "Reload", true, Some("CmdOrCtrl+R"))?;
    let view = Submenu::with_items(app, "View", true, &[&reload])?;
    let window = Submenu::with_items(
        app,
        "Window",
        true,
        &[
            &PredefinedMenuItem::minimize(app, None)?,
            &PredefinedMenuItem::maximize(app, Some("Zoom"))?,
            &PredefinedMenuItem::fullscreen(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::close_window(app, None)?,
        ],
    )?;
    Menu::with_items(app, &[&app_menu, &edit, &view, &window])
}

fn open_window(app: &AppHandle) -> tauri::Result<()> {
    let nav_app = app.clone();
    let mut b = WebviewWindowBuilder::new(app, "main", WebviewUrl::External("about:blank".parse().unwrap()))
        .title("ConcordeAI")
        .inner_size(1320.0, 860.0)
        .min_inner_size(940.0, 620.0)
        .center()
        .background_color(NIGHT)
        .theme(Some(tauri::Theme::Dark))
        .initialization_script(BRIDGE_SHIM)
        // only the app's own origin and the shell's own page load in this
        // window; anything else (a link in an answer) goes to the browser
        .on_page_load(|_w, payload| {
            if cfg!(debug_assertions) {
                eprintln!("[shell] page {:?} {}", payload.event(), payload.url());
            }
        })
        .on_navigation(move |url: &Url| {
            if cfg!(debug_assertions) {
                eprintln!("[shell] navigate {}", url);
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
    let _w = b.build()?;
    let _ = nav_app;
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
    let ctx = tauri::generate_context!();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // a second launch brings the window forward, like
            // /api/window/focus did for a second Python launch (6b310)
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.unminimize();
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .manage(Bridge::default())
        .manage(Sidecar { child: Mutex::new(None) })
        .invoke_handler(tauri::generate_handler![api_token])
        .setup(|app| {
            let handle = app.handle().clone();
            let menu = build_menu(&handle)?;
            app.set_menu(menu)?;
            app.on_menu_event(|app, ev| {
                if ev.id() == "reload" {
                    if let Some(w) = app.get_webview_window("main") {
                        let _ = w.eval("location.reload()");
                    }
                }
            });
            open_window(&handle)?;
            start_sidecar(handle.clone());
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
    app.run(|app, event| match event {
        RunEvent::ExitRequested { .. } | RunEvent::Exit => stop_sidecar(app),
        _ => {}
    });
}
