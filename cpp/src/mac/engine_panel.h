// The C++ engine's panel (NOTES 6b614): see engine_panel.mm.
#pragma once
#import <Cocoa/Cocoa.h>

@interface CCEnginePanel : NSObject
+ (BOOL)available;  // Apple silicon, and the bundled helper is there
- (void)show;
// for the probe (dev builds): ask as a person would, and read the panel back
- (void)askText:(NSString*)q;
- (NSDictionary*)state;
@end
