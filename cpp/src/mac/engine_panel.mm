// THE C++ ENGINE, VISIBLE (NOTES 6b614): a small panel that asks Llama 3.2 1B
// a question and streams the answer from the bundled concorde-llama, the MLX
// spike (cpp/spikes/mlx): MLX's C++ API, the chat template, the encode, the
// model and the decode all in C++, no Python anywhere in that path. The
// weights are the ones the app already keeps in the Hugging Face cache; the
// panel says so plainly when they aren't there. Apple silicon only (MLX).
#import "engine_panel.h"

#include <string>

@implementation CCEnginePanel {
  NSPanel* _panel;
  NSTextField* _input;
  NSButton* _ask;
  NSTextView* _out;
  NSTextField* _status;
  NSTask* _task;
  NSMutableString* _err;
}

+ (NSString*)helperPath {
  NSString* p = [NSBundle.mainBundle.resourcePath stringByAppendingPathComponent:@"engine/concorde-llama"];
  return [NSFileManager.defaultManager isExecutableFileAtPath:p] ? p : nil;
}

+ (BOOL)available {
#if defined(__arm64__)
  return [self helperPath] != nil;
#else
  return NO;
#endif
}

// the model where the app's own downloads put it (HF_HUB_CACHE, HF_HOME, ~/.cache)
+ (NSString*)modelDir {
  NSDictionary* env = NSProcessInfo.processInfo.environment;
  NSString* hub = env[@"HF_HUB_CACHE"];
  if (!hub.length && [env[@"HF_HOME"] length]) hub = [env[@"HF_HOME"] stringByAppendingPathComponent:@"hub"];
  if (!hub.length) hub = [NSHomeDirectory() stringByAppendingPathComponent:@".cache/huggingface/hub"];
  NSString* snaps =
      [hub stringByAppendingPathComponent:@"models--mlx-community--Llama-3.2-1B-Instruct-4bit/snapshots"];
  for (NSString* s in [NSFileManager.defaultManager contentsOfDirectoryAtPath:snaps error:nil]) {
    NSString* d = [snaps stringByAppendingPathComponent:s];
    if ([NSFileManager.defaultManager fileExistsAtPath:[d stringByAppendingPathComponent:@"model.safetensors"]])
      return d;
  }
  return nil;
}

- (void)show {
  if (!_panel) [self build];
  [_panel makeKeyAndOrderFront:nil];
  [_panel makeFirstResponder:_input];
}

- (void)build {
  _panel = [[NSPanel alloc] initWithContentRect:NSMakeRect(0, 0, 620, 460)
                                      styleMask:NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                                NSWindowStyleMaskResizable | NSWindowStyleMaskUtilityWindow
                                        backing:NSBackingStoreBuffered
                                          defer:NO];
  _panel.title = @"C++ Engine";
  _panel.appearance = [NSAppearance appearanceNamed:NSAppearanceNameDarkAqua];
  _panel.releasedWhenClosed = NO;
  _panel.contentMinSize = NSMakeSize(420, 300);
  [_panel center];
  NSView* v = _panel.contentView;

  NSTextField* about = [NSTextField wrappingLabelWithString:
      @"Llama 3.2 1B, run by MLX straight from C++: the template, the tokenizer, the model and the "
      @"words you see. No Python in this path."];
  about.textColor = NSColor.secondaryLabelColor;
  about.frame = NSMakeRect(16, 412, 588, 36);
  about.autoresizingMask = NSViewWidthSizable | NSViewMinYMargin;
  [v addSubview:about];

  _input = [[NSTextField alloc] initWithFrame:NSMakeRect(16, 376, 500, 26)];
  _input.placeholderString = @"Ask something";
  _input.autoresizingMask = NSViewWidthSizable | NSViewMinYMargin;
  _input.target = self;
  _input.action = @selector(ask:);
  [v addSubview:_input];

  _ask = [NSButton buttonWithTitle:@"Ask" target:self action:@selector(ask:)];
  _ask.frame = NSMakeRect(524, 374, 80, 30);
  _ask.autoresizingMask = NSViewMinXMargin | NSViewMinYMargin;
  _ask.keyEquivalent = @"\r";
  [v addSubview:_ask];

  NSScrollView* sc = [[NSScrollView alloc] initWithFrame:NSMakeRect(16, 44, 588, 320)];
  sc.hasVerticalScroller = YES;
  sc.autoresizingMask = NSViewWidthSizable | NSViewHeightSizable;
  sc.borderType = NSNoBorder;
  _out = [[NSTextView alloc] initWithFrame:sc.contentView.bounds];
  _out.editable = NO;
  _out.font = [NSFont systemFontOfSize:14];
  _out.textContainerInset = NSMakeSize(4, 6);
  _out.autoresizingMask = NSViewWidthSizable;
  sc.documentView = _out;
  [v addSubview:sc];

  _status = [NSTextField labelWithString:@""];
  _status.textColor = NSColor.secondaryLabelColor;
  _status.font = [NSFont monospacedDigitSystemFontOfSize:12 weight:NSFontWeightRegular];
  _status.frame = NSMakeRect(16, 14, 588, 18);
  _status.autoresizingMask = NSViewWidthSizable | NSViewMaxYMargin;
  [v addSubview:_status];
}

- (void)setText:(NSString*)s {
  [_out.textStorage setAttributedString:[[NSAttributedString alloc]
                                            initWithString:s
                                                attributes:@{NSForegroundColorAttributeName : NSColor.labelColor,
                                                             NSFontAttributeName : [NSFont systemFontOfSize:14]}]];
}

- (void)append:(NSString*)s {
  [_out.textStorage appendAttributedString:[[NSAttributedString alloc]
                                               initWithString:s
                                                   attributes:@{NSForegroundColorAttributeName : NSColor.labelColor,
                                                                NSFontAttributeName : [NSFont systemFontOfSize:14]}]];
  [_out scrollToEndOfDocument:nil];
}

- (void)ask:(id)sender {
  NSString* q = [_input.stringValue stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
  if (!q.length || _task.running) return;
  NSString* helper = [CCEnginePanel helperPath];
  NSString* model = [CCEnginePanel modelDir];
  if (!helper || !model) {
    [self setText:model ? @"The engine isn't in this build."
                        : @"Llama 3.2 1B isn't on this Mac yet. Download it in the app (Settings, Models), "
                          @"then ask again."];
    return;
  }
  [self setText:@""];
  _status.stringValue = @"Thinking…";
  _ask.enabled = NO;
  _err = [NSMutableString new];
  NSTask* t = [NSTask new];
  t.executableURL = [NSURL fileURLWithPath:helper];
  t.arguments = @[ model, @"--text", q, @"512", @"--json-stderr" ];
  NSPipe* out = [NSPipe pipe];
  NSPipe* err = [NSPipe pipe];
  t.standardOutput = out;
  t.standardError = err;
  t.standardInput = [NSFileHandle fileHandleWithNullDevice];
  __weak CCEnginePanel* weakSelf = self;
  out.fileHandleForReading.readabilityHandler = ^(NSFileHandle* h) {
    NSData* d = h.availableData;
    if (!d.length) return;
    NSString* s = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] ?: @"";
    dispatch_async(dispatch_get_main_queue(), ^{
      [weakSelf append:s];
    });
  };
  err.fileHandleForReading.readabilityHandler = ^(NSFileHandle* h) {
    NSData* d = h.availableData;
    if (!d.length) return;
    NSString* s = [[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding] ?: @"";
    dispatch_async(dispatch_get_main_queue(), ^{
      CCEnginePanel* me = weakSelf;
      if (me) [me->_err appendString:s];
    });
  };
  t.terminationHandler = ^(NSTask* done) {
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(0.2 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
      CCEnginePanel* me = weakSelf;
      if (!me) return;
      out.fileHandleForReading.readabilityHandler = nil;
      err.fileHandleForReading.readabilityHandler = nil;
      me->_ask.enabled = YES;
      [me showStats:done.terminationStatus];
    });
  };
  NSError* e = nil;
  if (![t launchAndReturnError:&e]) {
    _status.stringValue = [NSString stringWithFormat:@"The engine didn't start: %@", e.localizedDescription];
    _ask.enabled = YES;
    return;
  }
  _task = t;
}

- (void)askText:(NSString*)q {
  [self show];
  _input.stringValue = q;
  [self ask:nil];
}

- (NSDictionary*)state {
  return @{@"visible" : @(_panel.visible), @"running" : @(_task.running), @"text" : _out.string ?: @"",
           @"status" : _status.stringValue ?: @""};
}

- (void)showStats:(int)code {
  NSRange r = [_err rangeOfString:@"{\"impl\"" options:NSBackwardsSearch];
  NSDictionary* j = nil;
  if (r.location != NSNotFound) {
    NSData* d = [[_err substringFromIndex:r.location] dataUsingEncoding:NSUTF8StringEncoding];
    j = [NSJSONSerialization JSONObjectWithData:d options:0 error:nil];
  }
  if (![j isKindOfClass:NSDictionary.class]) {
    _status.stringValue =
        code ? [NSString stringWithFormat:@"The engine stopped (exit %d).", code] : @"Done.";
    return;
  }
  _status.stringValue = [NSString
      stringWithFormat:@"C++ · MLX · first token %.0f ms · %.0f tokens/s · %d tokens · loaded in %.0f ms",
                       [j[@"ttft_ms"] doubleValue], [j[@"decode_tps"] doubleValue], [j[@"gen_tokens"] intValue],
                       [j[@"load_ms"] doubleValue]];
}

@end
