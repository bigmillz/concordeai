// ConcordeAI's native main executable (6b443, 6b455). build_macos_app.sh
// compiles it into Contents/MacOS/MillenAI.
//
// WHY IT EXISTS (6b443): macOS asks for Local Network permission on behalf
// of the "responsible code", the bundle whose main executable
// LaunchServices started (Apple TN3179). It tracks that code by its code
// signature and its main executable's Mach-O UUID. A zsh script has
// neither, and when the script exec'd the venv's python the process
// BECAME Homebrew's Python.app (org.python.python, a UUID shared with
// every Homebrew python program), so LAN connections were refused with no
// alert and no entry in System Settings (per Patrick: "Neither Concord AI
// nor Python is listed under local network and privacy and security
// settings").
//
// WHY IT RUNS PYTHON ITSELF (6b455): 6b443 kept python a child process,
// and that child was still Python.app, so the window belonged to
// org.python.python: a rocket in the Dock, no running dot under the
// ConcordeAI icon, and a click on that icon started a second copy (per
// Patrick: "opening the app with the icon to the far left in the
// screenshot doesn't show it as open. It just opens another Python
// instance on the right"). Now this process IS the interpreter: it loads
// the Python framework dylib behind the venv's python3 and calls
// Py_BytesMain, the same way the venv's python3 stub (Mac/Tools/pythonw.c)
// would, only in this process instead of exec'ing Python.app. So
// NSBundle.mainBundle is ConcordeAI.app: its icon, its name in the menu
// bar and Cmd-Tab, one Dock entry with a dot, a Dock click that reopens
// the running copy, and the Local Network identity of 6b443 kept, since
// this signed Mach-O is still the responsible code for everything python
// starts.
//
// The venv is still found by __PYVENV_LAUNCHER__, which is what that stub
// sets before it execs: getpath reads it as the executable, finds
// pyvenv.cfg beside it, and unsets it, so sys.executable is the venv's
// python3 and subprocesses don't inherit it.
//
// Resources/launch.sh (the old launcher) still does the first-run venv
// setup; it is run with --bootstrap first and must exit 0 before the
// interpreter is loaded. A python whose dylib can't be found (not a
// framework build) falls back to 6b443's layout: launch.sh as a child
// that execs python, this process waiting as the responsible parent,
// with no Dock entry of its own (the plist no longer says LSUIElement).
#import <Cocoa/Cocoa.h>
#include <dlfcn.h>
#include <errno.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <sys/stat.h>
#include <sys/wait.h>

#ifndef LAUNCHER_TAG
#define LAUNCHER_TAG "com.millen.millenai"
#endif
// the bundle id baked into the binary keeps its UUID unique to this app
__attribute__((used)) static const char kTag[] = "launcher:" LAUNCHER_TAG;

extern char **environ;
static pid_t child = 0;

static int spawnScript(NSString *script, const char *arg, pid_t *pid) {
  const char *cargv[] = {"/bin/zsh", script.fileSystemRepresentation, arg,
                         NULL};
  return posix_spawn(pid, "/bin/zsh", NULL, NULL, (char *const *)cargv,
                     environ);
}

static int exitStatus(int st) {
  return WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st);
}

// ---- the fallback (6b443's layout): python as a child, this process the
// responsible parent, no Dock entry of its own
@interface Launcher : NSObject <NSApplicationDelegate>
@end

@implementation Launcher
- (void)front {
  if (child <= 0) return;
  NSRunningApplication *a =
      [NSRunningApplication runningApplicationWithProcessIdentifier:child];
  if (!a) return;
  if (@available(macOS 14.0, *)) [NSApp yieldActivationToApplication:a];
  [a activateWithOptions:NSApplicationActivateAllWindows];
}
- (BOOL)applicationShouldHandleReopen:(NSApplication *)s
                    hasVisibleWindows:(BOOL)f {
  [self front];
  return NO;
}
- (void)applicationDidBecomeActive:(NSNotification *)n {
  [self front];
}
- (void)childLaunched:(NSNotification *)n {
  NSRunningApplication *a = n.userInfo[NSWorkspaceApplicationKey];
  if (a.processIdentifier == child) [self front];
}
- (NSApplicationTerminateReply)applicationShouldTerminate:(NSApplication *)s {
  // a logout asks python to quit on its own; never kill it from here
  return NSTerminateNow;
}
@end

static int runAsParent(NSString *script) {
  if (spawnScript(script, NULL, &child) != 0) return 1;

  // quit signals go to python; this process leaves when python does
  int sigs[] = {SIGTERM, SIGINT, SIGHUP, SIGQUIT};
  for (size_t i = 0; i < sizeof sigs / sizeof *sigs; i++) {
    int sig = sigs[i];
    signal(sig, SIG_IGN);
    dispatch_source_t s = dispatch_source_create(
        DISPATCH_SOURCE_TYPE_SIGNAL, sig, 0, dispatch_get_main_queue());
    dispatch_source_set_event_handler(s, ^{ kill(child, sig); });
    dispatch_resume(s);
    CFBridgingRetain(s);
  }
  dispatch_source_t ex = dispatch_source_create(
      DISPATCH_SOURCE_TYPE_PROC, child, DISPATCH_PROC_EXIT,
      dispatch_get_main_queue());
  dispatch_source_set_event_handler(ex, ^{
    int st = 0;
    waitpid(child, &st, 0);
    exit(exitStatus(st));
  });
  dispatch_resume(ex);
  CFBridgingRetain(ex);
  // the child may already be gone before the source was armed
  int st = 0;
  if (waitpid(child, &st, WNOHANG) == child) exit(exitStatus(st));

  NSApplication *app = [NSApplication sharedApplication];
  // python's window is the app here; this process stays out of the Dock
  [app setActivationPolicy:NSApplicationActivationPolicyAccessory];
  Launcher *d = [Launcher new];
  app.delegate = d;
  [[NSWorkspace sharedWorkspace].notificationCenter
      addObserver:d
         selector:@selector(childLaunched:)
             name:NSWorkspaceDidLaunchApplicationNotification
           object:nil];
  [app run];
  return 0;
}

// ---- the interpreter in this process: the framework dylib behind the
// venv's python3. NULL when it isn't a framework build (then the fallback).
static void *pythonLibrary(NSString *venvPython, NSString **why) {
  char real[PATH_MAX];
  if (!realpath(venvPython.fileSystemRepresentation, real)) {
    *why = @"the venv's python3 doesn't resolve";
    return NULL;
  }
  // <framework>/Versions/3.x/bin/python3.x -> <framework>/Versions/3.x/Python
  NSString *ver = [[[NSString stringWithUTF8String:real]
      stringByDeletingLastPathComponent] stringByDeletingLastPathComponent];
  NSString *dylib = [ver stringByAppendingPathComponent:@"Python"];
  struct stat sb;
  if (stat(dylib.fileSystemRepresentation, &sb) != 0 || !S_ISREG(sb.st_mode)) {
    *why = [NSString stringWithFormat:@"no framework dylib at %@", dylib];
    return NULL;
  }
  // RTLD_GLOBAL: extension modules resolve the interpreter's symbols by
  // lookup, as they do when python's own executable links the dylib
  void *lib = dlopen(dylib.fileSystemRepresentation, RTLD_NOW | RTLD_GLOBAL);
  if (!lib) {
    *why = [NSString stringWithUTF8String:dlerror() ?: "dlopen failed"];
    return NULL;
  }
  return lib;
}

int main(int argc, char *argv[]) {
  (void)argc; (void)argv;
  @autoreleasepool {
    char exe[PATH_MAX], real[PATH_MAX];
    uint32_t n = sizeof exe;
    if (_NSGetExecutablePath(exe, &n) != 0 || !realpath(exe, real)) return 1;
    NSString *res = [[[[NSString stringWithUTF8String:real]
        stringByDeletingLastPathComponent] stringByDeletingLastPathComponent]
        stringByAppendingPathComponent:@"Resources"];
    NSString *script = [res stringByAppendingPathComponent:@"launch.sh"];
    NSString *main = [res stringByAppendingPathComponent:@"millenai.py"];
    // the same venv launch.sh makes and runs ($HOME, as the script reads it)
    const char *home = getenv("HOME");
    NSString *venvPython = [(home && *home ? [NSString stringWithUTF8String:home]
                                           : NSHomeDirectory())
        stringByAppendingPathComponent:
            @"Library/Application Support/MillenAI/venv/bin/python3"];

    // first run: launch.sh builds the venv; --bootstrap stops it short of
    // running python. Its own exit status is ours when it fails.
    pid_t boot = 0;
    if (spawnScript(script, "--bootstrap", &boot) != 0) return 1;
    int st = 0;
    while (waitpid(boot, &st, 0) < 0 && errno == EINTR) {}
    if (exitStatus(st) != 0) return exitStatus(st);

    NSString *why = nil;
    void *lib = pythonLibrary(venvPython, &why);
    int (*bytesMain)(int, char **) =
        lib ? (int (*)(int, char **))dlsym(lib, "Py_BytesMain") : NULL;
    if (!bytesMain) {
      fprintf(stderr, "ConcordeAI: python runs as a child (%s)\n",
              why ? why.UTF8String : "no Py_BytesMain");
      return runAsParent(script);
    }
    // what the venv's python3 stub sets before it execs Python.app:
    // getpath takes it as the executable, finds pyvenv.cfg beside it, and
    // unsets it so subprocesses don't inherit it
    setenv("__PYVENV_LAUNCHER__", venvPython.fileSystemRepresentation, 1);
    char *pyargv[] = {(char *)venvPython.fileSystemRepresentation,
                      (char *)main.fileSystemRepresentation, NULL};
    return bytesMain(2, pyargv);
  }
}
