// voice_sa.swift's class as Objective-C sees it (NOTES 6b642). Written by
// hand to match `swiftc -emit-objc-header` for that file; the class symbol
// is the Swift object's own (@objc(CCSAEngine)).
#pragma once
#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN
@interface CCSAEngine : NSObject
+ (BOOL)available;
@property(nonatomic, readonly) BOOL ready;
@property(nonatomic, readonly, copy) NSString* locale;
@property(nonatomic, readonly, copy) NSString* module;  // dictation | speech
- (void)warmWithLang:(NSString*)lang done:(void (^)(NSString*, BOOL, NSString* _Nullable))done;
- (BOOL)beginWithLang:(NSString*)lang
                input:(AVAudioFormat*)input
              located:(void (^)(NSString*))located
             onResult:(void (^)(BOOL, NSString*))onResult
                onEnd:(void (^)(NSString* _Nullable))onEnd;
- (void)feed:(AVAudioPCMBuffer*)buf;
- (void)finish;
- (void)cancel;
@end
NS_ASSUME_NONNULL_END
