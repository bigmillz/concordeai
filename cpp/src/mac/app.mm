// ConcordeAI's native window in C++ (CPP_PLAN.md step 1): AppKit and
// WKWebView through Objective-C++, no framework. It does what the Tauri
// shell does (shell/src-tauri/src/main.rs, 6b532..6b548), in the same
// order and with the same rules:
//   * the server starts first, the window is built while it boots;
//   * the window starts on about:blank (6b532's lesson: a first load from
//     another document made WebKit withhold the SameSite=Strict cookie) and
//     loads http://127.0.0.1:<front>/?boot=<code> once the notes are read;
//   * the page gets its token through window.pywebview.api.api_token(),
//     answered only for this window's main frame on its own origin;
//   * the title bar hidden, the content under it, the traffic lights at
//     (16, 38) as tao seats them; the curated menu; close hides; the place
//     remembered in shell-window.json (the Tauri shell's file and format);
//   * a crashed server comes back behind "Reconnecting…", three times.
// What is new: the page talks to a C++ front server (src/core/front.cpp)
// that proxies every route to the Python server on a private port.
#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>
#include <objc/message.h>

#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <functional>
#include <string>
#include <thread>

#include "../core/env.hpp"
#include "../core/host.hpp"
#include "scripts.hpp"
#import "engine_panel.h"
#import "voice.h"

namespace concorde {
extern std::string g_resource_dir;
}

using namespace concorde;

// The night the Horizon design opens on (#03040a), before any paint.
static NSColor* night() { return [NSColor colorWithSRGBRed:0x03 / 255.0 green:0x04 / 255.0 blue:0x0a / 255.0 alpha:1]; }

// The traffic lights' inset (main.rs TL_X/TL_Y, 6b545: centre line y 36 on
// the page's 72 px header row; the page reserves 96 px at a header's left).
static const CGFloat TL_X = 16.0;
static const CGFloat TL_Y = 38.0;

static NSString* S(const std::string& s) { return [NSString stringWithUTF8String:s.c_str()]; }

// a JS string literal (JSON), for the setup page's calls
static NSString* jsq(const std::string& s) {
  NSData* d = [NSJSONSerialization dataWithJSONObject:@[ S(s) ?: @"" ] options:0 error:nil];
  NSString* a = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding];
  return [a substringWithRange:NSMakeRange(1, a.length - 2)];
}

// ------------------------------------------------------------------ the place

struct WinState {
  double x = 0, y = 0, w = 0, h = 0;
  bool zoomed = false;
  bool valid = false;
};

static std::string win_state_path() { return join(data_home(), "shell-window.json"); }

// The Tauri shell's coordinates: the outer frame's top-left in points,
// measured down from the top of the primary screen, and the inner size.
static CGFloat primary_height() { return NSScreen.screens.count ? NSScreen.screens[0].frame.size.height : 0; }

static WinState read_win_state() {
  WinState s;
  NSData* d = [NSData dataWithContentsOfFile:S(win_state_path())];
  if (!d) return s;
  NSDictionary* j = [NSJSONSerialization JSONObjectWithData:d options:0 error:nil];
  if (![j isKindOfClass:NSDictionary.class]) return s;
  for (NSString* k in @[ @"x", @"y", @"w", @"h" ])
    if (![j[k] isKindOfClass:NSNumber.class]) return s;
  s.x = [j[@"x"] doubleValue];
  s.y = [j[@"y"] doubleValue];
  s.w = [j[@"w"] doubleValue];
  s.h = [j[@"h"] doubleValue];
  s.zoomed = [j[@"zoomed"] boolValue];
  if (!(std::isfinite(s.x) && std::isfinite(s.y) && std::isfinite(s.w) && std::isfinite(s.h))) return s;
  s.w = std::min(std::max(s.w, 940.0), 10000.0);
  s.h = std::min(std::max(s.h, 620.0), 10000.0);
  // at least 160 x 120 points of it on a screen that is there now
  NSRect r = NSMakeRect(s.x, primary_height() - s.y - s.h, s.w, s.h);
  for (NSScreen* sc in NSScreen.screens) {
    NSRect i = NSIntersectionRect(r, sc.frame);
    if (i.size.width >= 160 && i.size.height >= 120) {
      s.valid = true;
      break;
    }
  }
  return s;
}

// ------------------------------------------------------------------ frame rate

// THE PAGE AT THE SCREEN'S OWN RATE (6b617, per Patrick: "it just feels
// tight"). WebKit caps a page's rendering updates near 60 fps by default
// (its "Prefer Page Rendering Updates near 60fps" feature, ON in every
// WKWebView: Safari's Feature Flags menu is where people turn it off), so on
// a 120 Hz ProMotion screen requestAnimationFrame, CSS animations and the
// page's own scrolling effects ran at half the display's rate in both
// shells. The feature is switched off through WKPreferences' feature SPI
// (+_features, -_setEnabled:forFeature:), looked up by key and guarded, so
// a WebKit without it is simply left as it is. CONCORDE_RENDER_60=1 keeps
// the cap (for measuring the difference).
static void allow_full_frame_rate(WKPreferences* prefs) {
  if (env("CONCORDE_RENDER_60")) return;
  SEL list = NSSelectorFromString(@"_features");
  SEL set = NSSelectorFromString(@"_setEnabled:forFeature:");
  if (![WKPreferences respondsToSelector:list] || ![prefs respondsToSelector:set]) return;
  NSArray* feats = ((NSArray * (*)(id, SEL))objc_msgSend)(WKPreferences.class, list);
  for (id f in feats) {
    id key = [f respondsToSelector:NSSelectorFromString(@"key")] ? [f valueForKey:@"key"] : nil;
    if ([key isKindOfClass:NSString.class] && [key isEqualToString:@"PreferPageRenderingUpdatesNear60FPSEnabled"]) {
      ((void (*)(id, SEL, BOOL, id))objc_msgSend)(prefs, set, NO, f);
      mark("page rendering at the screen's full rate");
      return;
    }
  }
}

// ------------------------------------------------------------------ views

@interface CCContainer : NSView
@end
@implementation CCContainer
// AppKit puts the buttons back on a resize, a title change or leaving
// full screen; tao seats them again from its view's drawRect:, this from
// layout, the same moments.
- (void)layout {
  [super layout];
  [self seatLights];
}
- (void)seatLights {
  NSWindow* w = self.window;
  if (!w || (w.styleMask & NSWindowStyleMaskFullScreen)) return;
  NSButton* close = [w standardWindowButton:NSWindowCloseButton];
  NSButton* mini = [w standardWindowButton:NSWindowMiniaturizeButton];
  NSButton* zoom = [w standardWindowButton:NSWindowZoomButton];
  if (!close || !mini || !zoom) return;
  // tao's inset_traffic_lights, step for step (tao 0.37 view.rs)
  NSView* bar = close.superview.superview;
  if (!bar) return;
  NSRect cr = close.frame;
  CGFloat h = cr.size.height + TL_Y;
  NSRect br = bar.frame;
  br.size.height = h;
  br.origin.y = w.frame.size.height - h;
  // only what moved is set (6b617): this runs on every layout pass, a live
  // resize included, and a frame set to what it already is still asks
  // AppKit for another pass
  if (!NSEqualRects(br, bar.frame)) bar.frame = br;
  CGFloat gap = mini.frame.origin.x - cr.origin.x;
  NSArray<NSButton*>* bs = @[ close, mini, zoom ];
  for (NSUInteger i = 0; i < bs.count; ++i) {
    NSRect r = bs[i].frame;
    CGFloat x = TL_X + i * gap;
    if (r.origin.x != x) [bs[i] setFrameOrigin:NSMakePoint(x, r.origin.y)];
  }
}
@end

// ------------------------------------------------------------------ the app

@interface CCApp : NSObject <NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
                             WKScriptMessageHandlerWithReply, WKDownloadDelegate>
@property(strong) NSWindow* window;
@property(strong) WKWebView* web;
@property(strong) CCContainer* container;
@property(strong) NSEvent* lastDown;
@property(strong) id downMonitor;
@property BOOL pageLoadedMarked;
@property BOOL paintMarked;
@property BOOL quitting;
@property(strong) CCEnginePanel* enginePanel;
@property(strong) CCVoice* voice;  // dictation, native (6b642)
@end

@implementation CCApp {
  Host* _host;
  WinState _last;
  int _lockFd;
}

- (instancetype)init {
  if ((self = [super init])) {
    _host = new Host(/*windowed=*/true);
    _lockFd = -1;
  }
  return self;
}

// -------------------------------------------------------------- single instance

// ONE WINDOW PER DATA FOLDER (6b548's rule): a lock in the folder's run/,
// and a second launch on the same folder asks the first, by a distributed
// notification named for the folder, to bring its window forward.
static NSString* focus_name() {
  std::string h = data_home();
  unsigned long long x = 1469598103934665603ULL;  // FNV-1a
  for (unsigned char c : h) {
    x ^= c;
    x *= 1099511628211ULL;
  }
  return [NSString stringWithFormat:@"com.millen.concordeai.cpp.focus.%016llx", x];
}

- (BOOL)claimFolder {
  std::string run = join(data_home(), "run");
  make_dirs(run, 0700);
  std::string p = join(run, "cpp-shell.lock");
  _lockFd = ::open(p.c_str(), O_RDWR | O_CREAT, 0600);
  if (_lockFd < 0) return YES;  // can't lock, can't police: don't block launch
  if (::flock(_lockFd, LOCK_EX | LOCK_NB) == 0) return YES;
  [[NSDistributedNotificationCenter defaultCenter] postNotificationName:focus_name()
                                                                 object:nil
                                                               userInfo:nil
                                                     deliverImmediately:YES];
  return NO;
}

// -------------------------------------------------------------- menu

- (NSMenu*)buildMenu {
  NSString* name = [NSBundle.mainBundle objectForInfoDictionaryKey:@"CFBundleName"] ?: @"ConcordeAI";
  BOOL nightly = [NSBundle.mainBundle.bundleIdentifier hasSuffix:@".nightly"];
#ifdef CONCORDE_PREVIEW
  nightly = YES;  // the preview's updater is off, like the Nightly's (6b614)
#endif
  NSMenu* bar = [NSMenu new];
  auto item = [](NSString* t, SEL a, NSString* k, id target) {
    NSMenuItem* i = [[NSMenuItem alloc] initWithTitle:t action:a keyEquivalent:k ?: @""];
    i.target = target;
    return i;
  };
  // the app menu: About opens the page's own About (no native panel, so
  // no standard title bar anywhere, 6b547)
  NSMenu* app = [[NSMenu alloc] initWithTitle:name];
  NSMenuItem* about = item([NSString stringWithFormat:@"About %@", name], @selector(menuAbout:), nil, self);
  [app addItem:about];
  [app addItem:NSMenuItem.separatorItem];
  [app addItem:item(@"Settings…", @selector(menuSettings:), @",", self)];
  [app addItem:item(@"Import Chats…", @selector(menuImport:), nil, self)];
  if ([CCEnginePanel available]) {
    // the C++ engine, visible (6b614): Llama 3.2 1B from C++ through MLX
    NSMenuItem* eng = item(@"Ask the C++ Engine…", @selector(menuEngine:), @"e", self);
    eng.keyEquivalentModifierMask = NSEventModifierFlagCommand | NSEventModifierFlagShift;
    [app addItem:eng];
  }
  NSMenuItem* upd = item(@"Check for Updates…", nightly ? nil : @selector(menuUpdates:), nil, self);
  upd.enabled = !nightly;
  [app addItem:upd];
  [app addItem:NSMenuItem.separatorItem];
  [app addItem:item([NSString stringWithFormat:@"Hide %@", name], @selector(hide:), @"h", nil)];
  NSMenuItem* others = item(@"Hide Others", @selector(hideOtherApplications:), @"h", nil);
  others.keyEquivalentModifierMask = NSEventModifierFlagCommand | NSEventModifierFlagOption;
  [app addItem:others];
  [app addItem:NSMenuItem.separatorItem];
  [app addItem:item([NSString stringWithFormat:@"Quit %@", name], @selector(terminate:), @"q", nil)];
  NSMenuItem* appItem = [NSMenuItem new];
  appItem.submenu = app;
  [bar addItem:appItem];

  // Edit: the native responders (without them copy and paste stop in the webview)
  NSMenu* edit = [[NSMenu alloc] initWithTitle:@"Edit"];
  [edit addItem:item(@"Undo", @selector(undo:), @"z", nil)];
  NSMenuItem* redo = item(@"Redo", @selector(redo:), @"z", nil);
  redo.keyEquivalentModifierMask = NSEventModifierFlagCommand | NSEventModifierFlagShift;
  [edit addItem:redo];
  [edit addItem:NSMenuItem.separatorItem];
  [edit addItem:item(@"Cut", @selector(cut:), @"x", nil)];
  [edit addItem:item(@"Copy", @selector(copy:), @"c", nil)];
  [edit addItem:item(@"Paste", @selector(paste:), @"v", nil)];
  [edit addItem:item(@"Select All", @selector(selectAll:), @"a", nil)];
  NSMenuItem* editItem = [NSMenuItem new];
  editItem.submenu = edit;
  [bar addItem:editItem];

  NSMenu* view = [[NSMenu alloc] initWithTitle:@"View"];
  [view addItem:item(@"Toggle Sidebar", @selector(menuSidebar:), @"\\", self)];
  NSMenuItem* fs = item(@"Enter Full Screen", @selector(toggleFullScreen:), @"f", nil);
  fs.keyEquivalentModifierMask = NSEventModifierFlagCommand | NSEventModifierFlagControl;
  [view addItem:fs];
#ifndef NDEBUG
  [view addItem:item(@"Reload", @selector(menuReload:), @"r", self)];
#endif
  NSMenuItem* viewItem = [NSMenuItem new];
  viewItem.submenu = view;
  [bar addItem:viewItem];

  NSMenu* win = [[NSMenu alloc] initWithTitle:@"Window"];
  [win addItem:item(@"Minimize", @selector(performMiniaturize:), @"m", nil)];
  [win addItem:item(@"Zoom", @selector(performZoom:), nil, nil)];
  NSMenuItem* winItem = [NSMenuItem new];
  winItem.submenu = win;
  [bar addItem:winItem];
  return bar;  // not NSApp.windowsMenu: AppKit would add its window list to it
}

// the same functions the page's own buttons call (main.rs menu_action)
- (void)eval:(NSString*)js {
  [self.web evaluateJavaScript:js completionHandler:nil];
}
- (void)menuAbout:(id)s {
  [self eval:@"typeof openAbout==='function'&&openAbout()"];
}
- (void)menuSettings:(id)s {
  [self eval:@"typeof openAbout==='function'&&openAbout()"];
}
- (void)menuUpdates:(id)s {
  [self eval:@"typeof openAbout==='function'&&(openAbout(),document.getElementById('about-check')&&document."
             @"getElementById('about-check').click())"];
}
- (void)menuImport:(id)s {
  [self eval:@"typeof impOpen==='function'&&(document.getElementById('about-veil').hidden=true,impOpen())"];
}
- (void)menuSidebar:(id)s {
  [self eval:@"typeof hzSidebarToggle==='function'&&hzSidebarToggle()"];
}
- (void)menuEngine:(id)s {
  if (!self.enginePanel) self.enginePanel = [CCEnginePanel new];
  [self.enginePanel show];
}
- (void)menuReload:(id)s {
  [self eval:@"location.reload()"];
}

// -------------------------------------------------------------- launch

- (void)applicationWillFinishLaunching:(NSNotification*)n {
  NSApp.mainMenu = [self buildMenu];
}

- (void)applicationDidFinishLaunching:(NSNotification*)n {
  mark("setup");
  std::string why = preview_guard();
  if (![self claimFolder]) {
    // a second launch on this folder: the first window comes forward
    exit(0);
  }
  [[NSDistributedNotificationCenter defaultCenter] addObserver:self
                                                      selector:@selector(focusAsked:)
                                                          name:focus_name()
                                                        object:nil
                                            suspensionBehavior:NSNotificationSuspensionBehaviorDeliverImmediately];
  std::string err;
  bool bound = why.empty() && _host->bind_front(&err);
  // Python first, the window while it boots (6b548): the server's start
  // is most of a launch
  if (bound) {
    __weak CCApp* weakSelf = self;
    HostCallbacks cb;
    cb.ready = [weakSelf](const std::string& url) {
      NSString* u = S(url);
      dispatch_async(dispatch_get_main_queue(), ^{
        [weakSelf.web loadRequest:[NSURLRequest requestWithURL:[NSURL URLWithString:u]]];
      });
    };
    cb.error = [weakSelf](const std::string& t, const std::string& w, const std::string& log) {
      NSString *a = S(t), *b = S(w), *c = S(log);
      dispatch_async(dispatch_get_main_queue(), ^{
        [weakSelf showError:a why:b log:c];
      });
    };
    cb.reconnecting = [weakSelf]() {
      dispatch_async(dispatch_get_main_queue(), ^{
        [weakSelf eval:S(mac::RECONNECTING)];
      });
    };
    cb.setup_page = [weakSelf]() {
      dispatch_async(dispatch_get_main_queue(), ^{
        [weakSelf.web loadHTMLString:S(mac::SETUP_PAGE) baseURL:nil];
      });
    };
    cb.setup_progress = [weakSelf](int i, int n, const std::string& name, int pct, const std::string& item) {
      NSString* js = [NSString stringWithFormat:@"window.__boot&&__boot(%d,%d,%@,%d,%@)", i, n, jsq(name), pct, jsq(item)];
      dispatch_async(dispatch_get_main_queue(), ^{
        [weakSelf eval:js];
      });
    };
    cb.setup_failed = [weakSelf](const std::string& title, const std::string& why) {
      NSString* js = [NSString stringWithFormat:@"window.__failed&&__failed(%@,%@)", jsq(title), jsq(why)];
      // the page may still be loading: said a few times, as main.rs does
      for (int k = 1; k <= 20; ++k)
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(k * 50 * NSEC_PER_MSEC)), dispatch_get_main_queue(), ^{
          [weakSelf eval:js];
        });
    };
    cb.exited = [](int code) {
      // the updater's clean exit quits the app, so its helper's wait ends
      // and the relaunch is the new bundle (6b537)
      dispatch_async(dispatch_get_main_queue(), ^{
        [NSApp terminate:nil];
      });
    };
    _host->start(cb);
  }
  [self openWindow];
  mark("window built");
  if (!why.empty()) {
    [self showError:@"This build is a preview" why:S(why) log:@""];
  } else if (!bound) {
    [self showError:@"ConcordeAI couldn't start" why:S("The front server can't listen: " + err) log:@""];
  }
  [self probeHook];
  if (auto q = env("CONCORDE_SHELL_QUIT_AFTER")) {
    double secs = std::atof(q->c_str());
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(secs * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
      [NSApp terminate:nil];
    });
  }
}

- (void)focusAsked:(NSNotification*)n {
  [self showMain];
}

- (void)showMain {
  if (self.window.miniaturized) [self.window deminiaturize:nil];
  [self.window makeKeyAndOrderFront:nil];
  if (@available(macOS 14.0, *)) {
    [NSApp activate];
  } else {
    [NSApp activateIgnoringOtherApps:YES];
  }
}

- (BOOL)applicationShouldHandleReopen:(NSApplication*)app hasVisibleWindows:(BOOL)flag {
  [self showMain];  // a Dock click with the window closed opens it again
  return YES;
}

- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication*)app {
  self.quitting = YES;
  [self.voice shutdown];
  [self saveWindowState];
  _host->stop();  // SIGTERM, 8 s, SIGKILL; nothing restarts it after this
  return NSTerminateNow;
}

- (void)applicationWillTerminate:(NSNotification*)n {
  [self saveWindowState];
}

// -------------------------------------------------------------- window

- (void)openWindow {
  WinState st = read_win_state();
  NSRect content = NSMakeRect(0, 0, 1320, 860);
  NSWindowStyleMask mask = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable |
                           NSWindowStyleMaskResizable | NSWindowStyleMaskFullSizeContentView;
  NSWindow* w = [[NSWindow alloc] initWithContentRect:content styleMask:mask backing:NSBackingStoreBuffered defer:NO];
  w.releasedWhenClosed = NO;
  w.title = @"ConcordeAI";
  // the page IS the bar: content under a transparent title bar, the title
  // hidden, the lights seated on the header row
  w.titleVisibility = NSWindowTitleHidden;
  w.titlebarAppearsTransparent = YES;
  w.backgroundColor = night();
  w.appearance = [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua];
  w.contentMinSize = NSMakeSize(940, 620);
  w.delegate = self;
  w.tabbingMode = NSWindowTabbingModeDisallowed;
  if (st.valid) {
    [w setFrame:NSMakeRect(st.x, primary_height() - st.y - st.h, st.w, st.h) display:NO];
  } else {
    [w center];
  }

  WKWebViewConfiguration* cfg = [WKWebViewConfiguration new];
  WKUserContentController* ucc = [WKUserContentController new];
  for (const char* js : {mac::BRIDGE_SHIM, mac::FADE_IN, mac::CHROME}) {
    [ucc addUserScript:[[WKUserScript alloc] initWithSource:S(js)
                                              injectionTime:WKUserScriptInjectionTimeAtDocumentStart
                                           forMainFrameOnly:YES]];
  }
  [ucc addScriptMessageHandlerWithReply:self contentWorld:WKContentWorld.pageWorld name:@"concorde"];
  cfg.userContentController = ucc;
  cfg.mediaTypesRequiringUserActionForPlayback = WKAudiovisualMediaTypeNone;
  if (@available(macOS 12.3, *)) cfg.preferences.elementFullscreenEnabled = YES;
  allow_full_frame_rate(cfg.preferences);
#ifdef CONCORDE_DEVTOOLS
  [cfg.preferences setValue:@YES forKey:@"developerExtrasEnabled"];
#endif

  CCContainer* box = [[CCContainer alloc] initWithFrame:content];
  box.wantsLayer = YES;
  box.layer.backgroundColor = night().CGColor;
  WKWebView* web = [[WKWebView alloc] initWithFrame:box.bounds configuration:cfg];
  web.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
  web.navigationDelegate = self;
  web.UIDelegate = self;
  // the night shows through until the page paints: no white frame
  [web setValue:@NO forKey:@"drawsBackground"];
  if (@available(macOS 12.0, *)) web.underPageBackgroundColor = night();
#ifdef CONCORDE_DEVTOOLS
  if (@available(macOS 13.3, *)) web.inspectable = YES;
#endif
  [box addSubview:web];
  w.contentView = box;
  self.window = w;
  self.web = web;
  self.container = box;
  // no first document at all: the boot URL is the first navigation, with
  // no initiator (6b532: a first page of the shell's own made WebKit treat
  // /?boot= as cross-site and withhold the SameSite=Strict cookie)
  [w makeKeyAndOrderFront:nil];
  [box seatLights];
  if (st.valid && st.zoomed && !w.zoomed) [w zoom:nil];
  if (@available(macOS 14.0, *)) {
    [NSApp activate];
  } else {
    [NSApp activateIgnoringOtherApps:YES];
  }
  // the drag starts from the press itself, so the last one is kept
  __weak CCApp* weakSelf = self;
  self.downMonitor = [NSEvent addLocalMonitorForEventsMatchingMask:NSEventMaskLeftMouseDown
                                                           handler:^NSEvent*(NSEvent* e) {
                                                             weakSelf.lastDown = e;
                                                             return e;
                                                           }];
  [self noteWindow];
}

- (void)markFullscreen {
  BOOL fs = (self.window.styleMask & NSWindowStyleMaskFullScreen) != 0;
  [self eval:[NSString stringWithFormat:@"document.documentElement.classList.toggle('tl-fs',%@)",
                                        fs ? @"true" : @"false"]];
}

- (void)noteWindow {
  NSWindow* w = self.window;
  if (!w || (w.styleMask & NSWindowStyleMaskFullScreen) || w.miniaturized || !w.visible) return;
  if (w.zoomed) {
    if (_last.valid) _last.zoomed = true;
    return;
  }
  NSRect f = w.frame;
  _last.x = f.origin.x;
  _last.y = primary_height() - (f.origin.y + f.size.height);
  NSRect c = [w contentRectForFrameRect:f];
  _last.w = c.size.width;
  _last.h = c.size.height;
  _last.zoomed = false;
  _last.valid = true;
}

- (void)saveWindowState {
  if (!_last.valid) return;
  NSDictionary* j = @{@"x" : @(_last.x), @"y" : @(_last.y), @"w" : @(_last.w), @"h" : @(_last.h),
                      @"zoomed" : @(_last.zoomed)};
  NSData* d = [NSJSONSerialization dataWithJSONObject:j options:0 error:nil];
  if (d) [d writeToFile:S(win_state_path()) atomically:YES];
}

- (void)windowDidResize:(NSNotification*)n {
  [self markFullscreen];
  [self noteWindow];
}
- (void)windowDidMove:(NSNotification*)n {
  [self noteWindow];
}
- (void)windowDidExitFullScreen:(NSNotification*)n {
  [self.container seatLights];
  [self markFullscreen];
}
- (void)windowDidEnterFullScreen:(NSNotification*)n {
  [self markFullscreen];
}

// CLOSING HIDES IT (6b548): the app stays in the Dock with its server and
// the page as they were; a Dock click or a second launch brings it back.
// Full screen is left first, so no empty Space is left behind.
- (BOOL)windowShouldClose:(NSWindow*)w {
  [self saveWindowState];
  if (w.styleMask & NSWindowStyleMaskFullScreen) {
    [w toggleFullScreen:nil];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.8 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
      [w orderOut:nil];
    });
  } else {
    [w orderOut:nil];
  }
  return NO;
}

- (void)showError:(NSString*)title why:(NSString*)why log:(NSString*)log {
  auto esc = ^NSString*(NSString* s) {
    return [[[s stringByReplacingOccurrencesOfString:@"&" withString:@"&amp;"]
        stringByReplacingOccurrencesOfString:@"<"
                                  withString:@"&lt;"] stringByReplacingOccurrencesOfString:@">"
                                                                                withString:@"&gt;"];
  };
  NSString* html = [NSString
      stringWithFormat:
          @"<!doctype html><html><head><meta charset=utf-8><style>"
           "html,body{margin:0;height:100%%;background:#03040a;color:#c9d0e2;"
           "font:14px/1.5 -apple-system,system-ui,sans-serif}"
           "main{max-width:640px;margin:18vh auto 0;padding:0 24px;white-space:pre-wrap}"
           "h1{font-weight:500;font-size:19px;color:#f1f3fa;margin:0 0 12px}"
           "pre{font:12px/1.5 ui-monospace,Menlo,monospace;color:#9aa6c6;"
           "background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.09);"
           "border-radius:12px;padding:14px;overflow:auto;max-height:40vh}"
           "</style></head><body><main><h1>%@</h1><p>%@</p><pre>%@</pre></main></body></html>",
          esc(title), esc(why), esc(log)];
  [self.web loadHTMLString:html baseURL:nil];
}

// -------------------------------------------------------------- the bridge

// Only this window's main frame, showing this app's own origin
// (http://127.0.0.1:<the front's port>), is ever answered: the
// _WindowBridge rule (6b321), the Tauri shell's api_token check.
- (BOOL)ownFrame:(WKFrameInfo*)f {
  if (!f.isMainFrame) return NO;
  WKSecurityOrigin* o = f.securityOrigin;
  if (![o.protocol isEqualToString:@"http"] || ![o.host isEqualToString:@"127.0.0.1"]) return NO;
  if (o.port != _host->public_port()) return NO;
  NSURL* u = self.web.URL;
  return u && [u.scheme isEqualToString:@"http"] && [u.host isEqualToString:@"127.0.0.1"] &&
         u.port.integerValue == _host->public_port();
}

- (void)userContentController:(WKUserContentController*)ucc
      didReceiveScriptMessage:(WKScriptMessage*)msg
                 replyHandler:(void (^)(id _Nullable, NSString* _Nullable))reply {
  NSString* op = [msg.body isKindOfClass:NSDictionary.class] ? msg.body[@"op"] : nil;
  // the window drags from the shell's own pages too (the setup page's body
  // is a drag region); nothing else is answered there
  BOOL shellPage = msg.frameInfo.isMainFrame && ![msg.frameInfo.securityOrigin.protocol isEqualToString:@"http"];
  if (shellPage && ([op isEqualToString:@"drag"] || [op isEqualToString:@"zoom"])) {
    if ([op isEqualToString:@"drag"] && self.lastDown && ([NSEvent pressedMouseButtons] & 1))
      [self.window performWindowDragWithEvent:self.lastDown];
    reply(nil, nil);
    return;
  }
  if (![op isKindOfClass:NSString.class] || ![self ownFrame:msg.frameInfo]) {
    reply(nil, nil);  // null: the page's apiTok asks again, as with a refusal
    return;
  }
  if ([op isEqualToString:@"pick_folder"]) {
    // the Workspace's "Choose folder…" (beta-4's pick_folder): the system's
    // folder panel, as a sheet on the window; the path, or null
    NSOpenPanel* p = [NSOpenPanel openPanel];
    p.canChooseDirectories = YES;
    p.canChooseFiles = NO;
    p.allowsMultipleSelection = NO;
    p.canCreateDirectories = YES;
    p.prompt = @"Choose";
    [p beginSheetModalForWindow:self.window
              completionHandler:^(NSModalResponse r) {
                NSString* path = r == NSModalResponseOK ? p.URL.path : nil;
                reply(path.length ? path : nil, nil);
              }];
    return;
  }
  if ([op isEqualToString:@"api_token"]) {
    std::string t = _host->token();
    reply(t.empty() ? nil : S(t), nil);
    return;
  }
  if ([op isEqualToString:@"voice"]) {  // dictation in the shell (voice.mm, 6b642)
    if (!self.voice) self.voice = [[CCVoice alloc] initWithWeb:self.web];
    [self.voice handle:msg.body
                 reply:^(id o) {
                   reply(o, nil);
                 }];
    return;
  }
  if ([op isEqualToString:@"drag"]) {
    if (self.lastDown && ([NSEvent pressedMouseButtons] & 1)) [self.window performWindowDragWithEvent:self.lastDown];
  } else if ([op isEqualToString:@"zoom"]) {
    // the system's own double-click rule for a title bar
    NSString* act = [[NSUserDefaults standardUserDefaults] stringForKey:@"AppleActionOnDoubleClick"];
    if ([act isEqualToString:@"Minimize"]) [self.window performMiniaturize:nil];
    else if (![act isEqualToString:@"None"]) [self.window zoom:nil];
  } else if ([op isEqualToString:@"paint"]) {
    NSNumber* at = msg.body[@"at"];
    NSString* name = msg.body[@"name"];
    if ([at isKindOfClass:NSNumber.class] && [name isKindOfClass:NSString.class] && !self.paintMarked) {
      double ms = at.doubleValue - start_epoch_ms();
      if ([name isEqualToString:@"first-contentful-paint"]) self.paintMarked = YES;
      if (env("CONCORDE_SHELL_TIMING"))
        fprintf(stderr, "[shell-timing] %7.1f ms  page %s\n", ms, name.UTF8String);
    }
  }
  reply(nil, nil);
}

// -------------------------------------------------------------- navigation

// only the app's own origin and the shell's own pages load in this window;
// anything else (a link in an answer) goes to the browser (main.rs on_navigation)
- (void)webView:(WKWebView*)wv
    decidePolicyForNavigationAction:(WKNavigationAction*)a
                    decisionHandler:(void (^)(WKNavigationActionPolicy))decide {
  if (a.shouldPerformDownload) {
    decide(WKNavigationActionPolicyDownload);
    return;
  }
  NSURL* u = a.request.URL;
  if ([u.scheme isEqualToString:@"concorde-shell"]) {
    // the setup page's two buttons (beta-4's shell): never a navigation
    if ([u.host isEqualToString:@"retry"]) {
      _host->retry_setup();
    } else if ([u.host isEqualToString:@"copy"]) {
      std::string d = _host->setup_details();
      NSString* os = NSProcessInfo.processInfo.operatingSystemVersionString;
      NSString* text = [NSString stringWithFormat:@"ConcordeAI %@ setup\n%@\n\n%@",
                        [NSBundle.mainBundle objectForInfoDictionaryKey:@"CFBundleShortVersionString"] ?: @"",
                        os, S(d)];
      [NSPasteboard.generalPasteboard clearContents];
      [NSPasteboard.generalPasteboard setString:text forType:NSPasteboardTypeString];
      [self eval:@"window.__copied&&__copied()"];
    }
    decide(WKNavigationActionPolicyCancel);
    return;
  }
  if (!a.targetFrame || !a.targetFrame.isMainFrame) {
    decide(WKNavigationActionPolicyAllow);  // a frame inside the page is the page's
    return;
  }
  NSString* sch = u.scheme.lowercaseString;
  BOOL own = ([sch isEqualToString:@"http"] && [u.host isEqualToString:@"127.0.0.1"]) ||
             [sch isEqualToString:@"about"] || [sch isEqualToString:@"data"];
  if (!own && ([sch isEqualToString:@"http"] || [sch isEqualToString:@"https"] || [sch isEqualToString:@"mailto"]))
    [NSWorkspace.sharedWorkspace openURL:u];
  decide(own ? WKNavigationActionPolicyAllow : WKNavigationActionPolicyCancel);
}

- (void)webView:(WKWebView*)wv
    decidePolicyForNavigationResponse:(WKNavigationResponse*)r
                      decisionHandler:(void (^)(WKNavigationResponsePolicy))decide {
  NSString* cd = nil;
  if ([r.response isKindOfClass:NSHTTPURLResponse.class])
    cd = [(NSHTTPURLResponse*)r.response valueForHTTPHeaderField:@"Content-Disposition"];
  if (!r.canShowMIMEType || [cd.lowercaseString hasPrefix:@"attachment"]) decide(WKNavigationResponsePolicyDownload);
  else decide(WKNavigationResponsePolicyAllow);
}

- (void)webView:(WKWebView*)wv navigationAction:(WKNavigationAction*)a didBecomeDownload:(WKDownload*)d {
  d.delegate = self;
}
- (void)webView:(WKWebView*)wv navigationResponse:(WKNavigationResponse*)r didBecomeDownload:(WKDownload*)d {
  d.delegate = self;
}

// exports download straight to the Downloads folder (6b295), never over a file
- (void)download:(WKDownload*)d
    decideDestinationUsingResponse:(NSURLResponse*)resp
                 suggestedFilename:(NSString*)name
                 completionHandler:(void (^)(NSURL* _Nullable))done {
  NSURL* dir = [NSFileManager.defaultManager URLsForDirectory:NSDownloadsDirectory inDomains:NSUserDomainMask].firstObject;
  if (!dir) dir = [NSURL fileURLWithPath:NSTemporaryDirectory()];
  NSString* base = name.length ? name.lastPathComponent : @"download";
  NSURL* p = [dir URLByAppendingPathComponent:base];
  for (int n = 1; [NSFileManager.defaultManager fileExistsAtPath:p.path]; ++n)
    p = [dir URLByAppendingPathComponent:[NSString stringWithFormat:@"%@ (%d)", base, n]];
  done(p);
}

- (void)webView:(WKWebView*)wv didFinishNavigation:(WKNavigation*)nav {
  [self markFullscreen];
  if ([wv.URL.host isEqualToString:@"127.0.0.1"] && !self.pageLoadedMarked) {
    self.pageLoadedMarked = YES;
    mark("page loaded");
  }
}

// a WebContent process that died (memory pressure, a WebKit crash) is
// reloaded instead of leaving a blank window
- (void)webViewWebContentProcessDidTerminate:(WKWebView*)wv {
  [self.voice shutdown];  // no page to hear it: the microphone let go
  [wv reload];
}

// -------------------------------------------------------------- UI delegate

// target=_blank and window.open: the browser, never a second window
- (WKWebView*)webView:(WKWebView*)wv
    createWebViewWithConfiguration:(WKWebViewConfiguration*)c
               forNavigationAction:(WKNavigationAction*)a
                    windowFeatures:(WKWindowFeatures*)f {
  NSURL* u = a.request.URL;
  NSString* sch = u.scheme.lowercaseString;
  if ([sch isEqualToString:@"http"] || [sch isEqualToString:@"https"] || [sch isEqualToString:@"mailto"]) {
    if ([u.host isEqualToString:@"127.0.0.1"]) [wv loadRequest:a.request];
    else [NSWorkspace.sharedWorkspace openURL:u];
  }
  return nil;
}

- (void)webView:(WKWebView*)wv
    runOpenPanelWithParameters:(WKOpenPanelParameters*)p
              initiatedByFrame:(WKFrameInfo*)f
             completionHandler:(void (^)(NSArray<NSURL*>* _Nullable))done {
  NSOpenPanel* panel = [NSOpenPanel openPanel];
  panel.allowsMultipleSelection = p.allowsMultipleSelection;
  panel.canChooseDirectories = p.allowsDirectories;
  panel.canChooseFiles = YES;
  [panel beginSheetModalForWindow:self.window
                completionHandler:^(NSModalResponse r) {
                  done(r == NSModalResponseOK ? panel.URLs : nil);
                }];
}

- (void)webView:(WKWebView*)wv
    runJavaScriptAlertPanelWithMessage:(NSString*)m
                      initiatedByFrame:(WKFrameInfo*)f
                     completionHandler:(void (^)(void))done {
  NSAlert* a = [NSAlert new];
  a.messageText = m;
  [a beginSheetModalForWindow:self.window
            completionHandler:^(NSModalResponse r) {
              done();
            }];
}

- (void)webView:(WKWebView*)wv
    runJavaScriptConfirmPanelWithMessage:(NSString*)m
                        initiatedByFrame:(WKFrameInfo*)f
                       completionHandler:(void (^)(BOOL))done {
  NSAlert* a = [NSAlert new];
  a.messageText = m;
  [a addButtonWithTitle:@"OK"];
  [a addButtonWithTitle:@"Cancel"];
  [a beginSheetModalForWindow:self.window
            completionHandler:^(NSModalResponse r) {
              done(r == NSAlertFirstButtonReturn);
            }];
}

// the page's microphone (dictation): granted to this app's own origin
// only; the system's own prompt (TCC, NSMicrophoneUsageDescription) still asks
- (void)webView:(WKWebView*)wv
    requestMediaCapturePermissionForOrigin:(WKSecurityOrigin*)o
                          initiatedByFrame:(WKFrameInfo*)f
                                      type:(WKMediaCaptureType)t
                           decisionHandler:(void (^)(WKPermissionDecision))decide API_AVAILABLE(macos(12.0)) {
  BOOL own = [o.protocol isEqualToString:@"http"] && [o.host isEqualToString:@"127.0.0.1"] &&
             o.port == _host->public_port() && t == WKMediaCaptureTypeMicrophone;
  decide(own ? WKPermissionDecisionGrant : WKPermissionDecisionDeny);
}

// -------------------------------------------------------------- the probe

// A measuring hook for dev builds (the Tauri shell's CONCORDE_SHELL_PROBE,
// compiled out of a release): each cmd-<n>.js dropped in the folder runs
// in the page as an async function body (so it may await), and its value,
// as JSON, lands in out-<n>.json.
- (void)probeHook {
#ifdef CONCORDE_PROBE
  auto dir = env("CONCORDE_SHELL_PROBE");
  if (!dir) return;
  std::string d = *dir;
  __weak CCApp* weakSelf = self;
  std::thread([d, weakSelf] {
    for (unsigned long n = 1;;) {
      std::string cmd = join(d, "cmd-" + std::to_string(n) + ".js");
      if (!file_exists(cmd)) {
        std::this_thread::sleep_for(std::chrono::milliseconds(40));
        continue;
      }
      std::string js = read_file(cmd);
      std::remove(cmd.c_str());
      NSString* out = S(join(d, "out-" + std::to_string(n) + ".json"));
      NSString* body = S(js);
      dispatch_async(dispatch_get_main_queue(), ^{
        CCApp* me = weakSelf;
        if (!me) return;
        if ([body hasPrefix:@"//native:state"] || [body hasPrefix:@"//native:close"]) {
          // the window's state (and, for close, the close button's own path:
          // windowShouldClose, so close hides as a person's click would)
          NSWindow* w = me.window;
          if ([body hasPrefix:@"//native:close"]) [w performClose:nil];
          NSRect f = w.frame;  // the place as shell-window.json keeps it: top-left, points
          NSRect c = [w contentRectForFrameRect:f];
          NSDictionary* o = @{@"visible" : @(w.visible), @"key" : @(w.keyWindow), @"mini" : @(w.miniaturized),
                              @"url" : me.web.URL.absoluteString ?: @"", @"title" : w.title ?: @"",
                              @"frame" : @[ @(f.origin.x), @(primary_height() - (f.origin.y + f.size.height)),
                                            @(c.size.width), @(c.size.height) ]};
          [[NSJSONSerialization dataWithJSONObject:o options:0 error:nil] writeToFile:out atomically:YES];
          return;
        }
        if ([body hasPrefix:@"//native:engine"]) {
          // the C++ engine's panel (6b614): "//native:engine-ask <question>" opens it and asks;
          // "//native:engine-state" reads it back
          if (!me.enginePanel) me.enginePanel = [CCEnginePanel new];
          if ([body hasPrefix:@"//native:engine-ask "])
            [me.enginePanel askText:[body substringFromIndex:@"//native:engine-ask ".length]];
          [[NSJSONSerialization dataWithJSONObject:[me.enginePanel state] options:0 error:nil] writeToFile:out
                                                                                                atomically:YES];
          return;
        }
        if ([body hasPrefix:@"//native:voice-file "]) {
          // dictation's test input (6b642): the next voice start reads this
          // file in real time instead of the microphone; empty: the mic again
          NSString* f = [[body substringFromIndex:@"//native:voice-file ".length]
              stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
          if (f.length) setenv("CONCORDE_VOICE_FILE", f.fileSystemRepresentation, 1);
          else unsetenv("CONCORDE_VOICE_FILE");
          [@"true" writeToFile:out atomically:YES encoding:NSUTF8StringEncoding error:nil];
          return;
        }
        if ([body hasPrefix:@"//native:lights"]) {
          // the traffic lights' frames, in points from the window's top-left
          NSWindow* w = me.window;
          NSMutableDictionary* o = [NSMutableDictionary new];
          CGFloat H = w.frame.size.height;
          NSDictionary* names = @{@"close" : @(NSWindowCloseButton), @"mini" : @(NSWindowMiniaturizeButton),
                                  @"zoom" : @(NSWindowZoomButton)};
          for (NSString* k in names) {
            NSButton* b = [w standardWindowButton:(NSWindowButton)[names[k] integerValue]];
            NSRect r = [b convertRect:b.bounds toView:nil];
            o[k] = @[ @(r.origin.x), @(H - r.origin.y - r.size.height), @(r.size.width), @(r.size.height) ];
          }
          o[@"window"] = @[ @(w.frame.size.width), @(H) ];
          [[NSJSONSerialization dataWithJSONObject:o options:0 error:nil] writeToFile:out atomically:YES];
          return;
        }
        [me.web callAsyncJavaScript:body
                          arguments:@{}
                            inFrame:nil
                     inContentWorld:WKContentWorld.pageWorld
                  completionHandler:^(id val, NSError* err) {
                    id o = err ? @{@"error" : err.localizedDescription ?: @"error"} : (val ?: [NSNull null]);
                    NSData* data = [NSJSONSerialization dataWithJSONObject:o
                                                                   options:NSJSONWritingFragmentsAllowed
                                                                     error:nil];
                    [data ?: [@"null" dataUsingEncoding:NSUTF8StringEncoding] writeToFile:out atomically:YES];
                  }];
      });
      ++n;
    }
  }).detach();
#endif
}

@end

int main(int argc, const char* argv[]) {
  mark("process start");
  @autoreleasepool {
    g_resource_dir = NSBundle.mainBundle.resourcePath.UTF8String ?: "";
#ifdef CONCORDE_PREVIEW
    // THE PREVIEW IS ALWAYS A DEV COPY OF ITS OWN (6b614): opened from the
    // Finder it has no environment, so it names its own folder
    // (~/Library/Application Support/MillenAI-cpp, beside the real one,
    // never inside it), its own port, and an update feed that can't answer
    // (MILLENAI_UPDATE_API is honoured only in a dev copy): it never sees the
    // real data, the real app's port, a paired server or an update.
    // Anything already set (a terminal launch) is left as it is.
    if (!getenv("MILLENAI_DEV")) {
      std::string home = expand_tilde("~/Library/Application Support/MillenAI-cpp");
      setenv("MILLENAI_DEV", "1", 1);
      setenv("MILLENAI_HOME", home.c_str(), 1);
      if (!getenv("MILLENAI_PORT") && !getenv("CONCORDE_PUBLIC_PORT")) setenv("MILLENAI_PORT", "9861", 1);
      setenv("MILLENAI_UPDATE_API", "http://127.0.0.1:9", 1);
    }
#endif
    NSApplication* app = [NSApplication sharedApplication];
    app.activationPolicy = NSApplicationActivationPolicyRegular;
    CCApp* d = [CCApp new];
    app.delegate = d;
    // SIGTERM (a script, launchd) quits the way Cmd+Q does: the server is
    // stopped and the window's place written, never left to the parent watch
    signal(SIGTERM, SIG_IGN);
    dispatch_source_t term = dispatch_source_create(DISPATCH_SOURCE_TYPE_SIGNAL, SIGTERM, 0, dispatch_get_main_queue());
    dispatch_source_set_event_handler(term, ^{
      [NSApp terminate:nil];
    });
    dispatch_resume(term);
    [app run];
  }
  return 0;
}
