// ConcordeAI's native window on Windows (CPP_PLAN.md step 1): a STUB.
//
// TODO(cpp-win): not built or run yet; nothing on the Mac that wrote this
// can compile WebView2. What is here compiles against the Windows SDK
// alone: the same Host (front + Python sidecar) as the Mac app, and the
// page opened in the default browser so the plumbing can be exercised.
// The real window is WebView2 (Microsoft.Web.WebView2 NuGet, fetched by
// CMake on Windows):
//   * CreateCoreWebView2EnvironmentWithOptions(user data folder in the
//     data home's cache\webview2, so a dev copy's storage is its own)
//   * AddScriptToExecuteOnDocumentCreated(BRIDGE_SHIM, FADE_IN, CHROME)
//     with the shim over window.chrome.webview.postMessage and a reply by
//     PostWebMessageAsJson; the token is answered only when the sender's
//     Source is http://127.0.0.1:<front port> and it is the main frame
//   * DefaultBackgroundColor = #03040a (no white flash)
//   * NavigationStarting: own origin, about:, data: only; others to the
//     browser (ShellExecuteW); NewWindowRequested likewise
//   * the drag region: WM_NCLBUTTONDOWN/HTCAPTION from the "drag" message
//   * DownloadStarting: the Downloads folder, never over a file
//   * single instance: a named mutex keyed on the data folder's hash, the
//     first window found by a registered message
// Windows ARM64 and x64 are both MSVC targets (cmake -A ARM64 / -A x64).
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <shellapi.h>

#include <atomic>
#include <string>
#include <thread>

#include "../core/env.hpp"
#include "../core/host.hpp"

namespace concorde {
extern std::string g_resource_dir;
}

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int) {
  using namespace concorde;
  mark("process start");
  std::string why = preview_guard();
  if (!why.empty()) {
    MessageBoxA(nullptr, why.c_str(), "ConcordeAI (C++ preview)", MB_OK | MB_ICONINFORMATION);
    return 2;
  }
  Host host(/*windowed=*/true);
  std::string err;
  if (!host.bind_front(&err)) {
    MessageBoxA(nullptr, err.c_str(), "ConcordeAI couldn't start", MB_OK | MB_ICONERROR);
    return 1;
  }
  std::atomic<bool> done{false};
  HostCallbacks cb;
  cb.ready = [](const std::string& url) {
    // TODO(cpp-win): the WebView2 window loads this; the stub hands it to
    // the browser, which can't answer the token (no bridge), by design
    ShellExecuteA(nullptr, "open", url.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
  };
  cb.error = [](const std::string& t, const std::string& w, const std::string& log) {
    std::string m = w + "\n\n" + log;
    MessageBoxA(nullptr, m.c_str(), t.c_str(), MB_OK | MB_ICONERROR);
  };
  cb.exited = [&done](int) { done = true; };
  host.start(cb);
  MSG msg;
  while (!done.load()) {
    while (PeekMessage(&msg, nullptr, 0, 0, PM_REMOVE)) {
      TranslateMessage(&msg);
      DispatchMessage(&msg);
    }
    Sleep(50);
  }
  host.stop();
  return 0;
}
