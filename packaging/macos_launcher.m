// ConcordeAI's native main executable (6b443). build_macos_app.sh compiles
// it into Contents/MacOS/MillenAI.
//
// WHY IT EXISTS: macOS asks for Local Network permission on behalf of the
// "responsible code", the bundle whose main executable LaunchServices
// started (Apple TN3179). It tracks that code by its code signature and
// its main executable's Mach-O UUID. A zsh script has neither, and when
// the script exec'd the venv's python the process BECAME Homebrew's
// Python.app (org.python.python, a UUID shared with every Homebrew python
// program), so LAN connections were refused with no alert and no entry in
// System Settings (per Patrick: "Neither Concord AI nor Python is listed
// under local network and privacy and security settings").
//
// So this stays alive as the parent: it spawns Resources/launch.sh (the old
// launcher, unchanged) as a CHILD, which keeps this signed Mach-O the
// responsible code for python and everything python starts. The python
// window is still its own app (Python.app), exactly as before, so its
// WebKit store, menu and Dock icon don't move. This process is an
// LSUIElement: no Dock icon of its own. It brings python's window forward
// when ConcordeAI is opened again, forwards quit signals to it, and exits
// with python's status (the updater's swap script waits for this path).
#import <Cocoa/Cocoa.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>

#ifndef LAUNCHER_TAG
#define LAUNCHER_TAG "com.millen.millenai"
#endif
// the bundle id baked into the binary keeps its UUID unique to this app
__attribute__((used)) static const char kTag[] = "launcher:" LAUNCHER_TAG;

extern char **environ;
static pid_t child = 0;

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

int main(int argc, const char *argv[]) {
  (void)argc; (void)argv;
  @autoreleasepool {
    char exe[PATH_MAX], real[PATH_MAX];
    uint32_t n = sizeof exe;
    if (_NSGetExecutablePath(exe, &n) != 0 || !realpath(exe, real)) return 1;
    NSString *res = [[[[NSString stringWithUTF8String:real]
        stringByDeletingLastPathComponent] stringByDeletingLastPathComponent]
        stringByAppendingPathComponent:@"Resources"];
    NSString *script = [res stringByAppendingPathComponent:@"launch.sh"];
    const char *cargv[] = {"/bin/zsh", script.fileSystemRepresentation, NULL};
    if (posix_spawn(&child, "/bin/zsh", NULL, NULL, (char *const *)cargv,
                    environ) != 0)
      return 1;

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
      exit(WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st));
    });
    dispatch_resume(ex);
    CFBridgingRetain(ex);
    // the child may already be gone before the source was armed
    int st = 0;
    if (waitpid(child, &st, WNOHANG) == child)
      exit(WIFEXITED(st) ? WEXITSTATUS(st) : 128 + WTERMSIG(st));

    NSApplication *app = [NSApplication sharedApplication];
    Launcher *d = [Launcher new];
    app.delegate = d;
    [[NSWorkspace sharedWorkspace].notificationCenter
        addObserver:d
           selector:@selector(childLaunched:)
               name:NSWorkspaceDidLaunchApplicationNotification
             object:nil];
    [app run];
  }
  return 0;
}
