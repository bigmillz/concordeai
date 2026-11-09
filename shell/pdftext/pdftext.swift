// concorde-pdftext (6b546): reads the text of a PDF on this Mac, page by
// page, with Apple's PDFKit, the system's own PDF reader. NOTHING LEAVES THIS
// COMPUTER: no network code is in this file (the gauntlet lints it), the
// reader is the OS's, and no download is needed.
//
//   concorde-pdftext <file.pdf> <max seconds> <max characters>
//
// stdout, JSON lines, one per event, flushed as they come:
//   {"ev":"open","pages":N}                 the file opened
//   {"ev":"page","n":i,"text":"...","ink":B} one per page, in order; ink is
//       true when a page with no text still has something drawn on it
//       (a scan), false for a blank page or a page with text
//   {"ev":"done","read":k,"why":"end"|"time"|"chars"}
//   {"ev":"locked"}                          a password is needed
//   {"ev":"err","why":"open"}                not a PDF this reader can open
// It stops itself at the time or character cap; the server also kills it.
import Foundation
import PDFKit
import CoreGraphics

let out = FileHandle.standardOutput
func emit(_ d: [String: Any]) {
    guard let j = try? JSONSerialization.data(withJSONObject: d) else { return }
    out.write(j)
    out.write(Data([10]))
}

let args = CommandLine.arguments
guard args.count >= 2 else { emit(["ev": "err", "why": "usage"]); exit(2) }
let maxS = args.count > 2 ? (Double(args[2]) ?? 30) : 30
let maxChars = args.count > 3 ? (Int(args[3]) ?? 2_000_000) : 2_000_000

// a page with no text: is anything drawn on it? Rendered small in grey on
// white; any pixel darker than near-white counts as ink
func hasInk(_ page: PDFPage) -> Bool {
    guard let ref = page.pageRef else { return false }
    let w = 64, h = 64
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: w, space: CGColorSpaceCreateDeviceGray(),
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return false }
    ctx.setFillColor(gray: 1, alpha: 1)
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    let box = ref.getBoxRect(.cropBox)
    guard box.width > 0, box.height > 0 else { return false }
    ctx.scaleBy(x: CGFloat(w) / box.width, y: CGFloat(h) / box.height)
    ctx.translateBy(x: -box.minX, y: -box.minY)
    ctx.drawPDFPage(ref)
    guard let data = ctx.data else { return false }
    let px = data.bindMemory(to: UInt8.self, capacity: w * h)
    for k in 0..<(w * h) where px[k] < 245 { return true }
    return false
}

guard let doc = PDFDocument(url: URL(fileURLWithPath: args[1])) else {
    emit(["ev": "err", "why": "open"]); exit(0)
}
if doc.isLocked { emit(["ev": "locked"]); exit(0) }
let n = doc.pageCount
emit(["ev": "open", "pages": n])
let t0 = Date()
var chars = 0
for i in 0..<n {
    if Date().timeIntervalSince(t0) > maxS { emit(["ev": "done", "read": i, "why": "time"]); exit(0) }
    autoreleasepool {
        guard let page = doc.page(at: i) else {
            emit(["ev": "page", "n": i + 1, "text": "", "ink": false]); return
        }
        let s = page.string ?? ""
        let empty = s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        emit(["ev": "page", "n": i + 1, "text": s, "ink": empty ? hasInk(page) : false])
        chars += s.count
    }
    if chars >= maxChars { emit(["ev": "done", "read": i + 1, "why": "chars"]); exit(0) }
}
emit(["ev": "done", "read": n, "why": "end"])
