// Dictation, native in the C++ shell (NOTES 6b642): the microphone through
// AVAudioEngine, recognised on this computer (SpeechAnalyzer on macOS 26+,
// SFSpeechRecognizer on-device before it), the words streamed to the page
// over the window's own bridge. No helper process and no Python on the path.
#pragma once
#import <Foundation/Foundation.h>
#import <WebKit/WebKit.h>

NS_ASSUME_NONNULL_BEGIN
@interface CCVoice : NSObject
- (instancetype)initWithWeb:(WKWebView*)web;
// one window.pywebview.api.voice(cmd) call; reply runs on the main thread
- (void)handle:(NSDictionary*)cmd reply:(void (^)(id _Nullable))reply;
// the microphone let go (quit, a WebContent crash, the page navigating away)
- (void)shutdown;
@end
NS_ASSUME_NONNULL_END
