// concorde-dictate (6b543): the on-device speech helper ConcordeAI's server
// runs on macOS 26 and later. Apple's SpeechAnalyzer, the system's own
// recognizer, behind a pipe: NOTHING LEAVES THIS COMPUTER. No network code is
// in this file (the gauntlet lints it), the model is the OS's, and the only
// permission is the microphone's, which the page already holds.
//
// stdin, frames: u32 little-endian length + PCM int16 16 kHz mono; a zero
// length ends the utterance (the final follows). The process stays for the
// next one, the model kept loaded (modelRetention .processLifetime).
// stdout, JSON lines: {"ev":..,"text":..,"u":N,"ms":T} with ev one of
// ready, listening, partial, final, done, err; u numbers the utterances so
// a late line for an old one can be told apart.
import Foundation
import AVFoundation
import Speech

let t0 = Date()
let out = FileHandle.standardOutput
let lock = NSLock()
func emit(_ ev: String, _ text: String = "", _ n: Int = 0) {
    let d: [String: Any] = ["ev": ev, "text": text, "u": n, "ms": Int(Date().timeIntervalSince(t0) * 1000)]
    let j = try! JSONSerialization.data(withJSONObject: d)
    lock.lock(); out.write(j); out.write("\n".data(using: .utf8)!); lock.unlock()
}
let inFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
func readExact(_ n: Int) -> Data? {
    var d = Data()
    while d.count < n {
        let more = FileHandle.standardInput.readData(ofLength: n - d.count)
        if more.isEmpty { return nil }
        d.append(more)
    }
    return d
}
func readFrame() -> Data? {   // nil = EOF; empty = end of utterance
    guard let h = readExact(4) else { return nil }
    let n = Int(h.withUnsafeBytes { $0.load(as: UInt32.self) })   // host is little-endian
    if n == 0 { return Data() }
    return readExact(n)
}
func pcm(_ data: Data) -> AVAudioPCMBuffer {
    let frames = AVAudioFrameCount(data.count / 2)
    let buf = AVAudioPCMBuffer(pcmFormat: inFmt, frameCapacity: frames)!
    buf.frameLength = frames
    _ = data.withUnsafeBytes { p in memcpy(buf.int16ChannelData![0], p.baseAddress!, data.count) }
    return buf
}

@available(macOS 26, *)
func run() async {
    let locale = Locale(identifier: Locale.current.identifier)
    let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                        reportingOptions: [.volatileResults, .fastResults], attributeOptions: [])
    do {
        if let r = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
            emit("info", "installing assets"); try await r.downloadAndInstall()
        }
    } catch { emit("err", "assets: \(error)"); exit(1) }
    guard let fmt = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else { emit("err", "no format"); exit(1) }
    let conv = AVAudioConverter(from: inFmt, to: fmt)!
    let opts = SpeechAnalyzer.Options(priority: .userInitiated, modelRetention: .processLifetime)
    var n = 0
    emit("ready", locale.identifier)
    // utterance loop: a fresh analyzer per utterance, the model kept for the process
    while true {
        guard let first = readFrame() else { return }
        if first.isEmpty { continue }
        n += 1; let u = n
        let transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                            reportingOptions: [.volatileResults, .fastResults], attributeOptions: [])
        let analyzer = SpeechAnalyzer(modules: [transcriber], options: opts)
        let (stream, cont) = AsyncStream<AnalyzerInput>.makeStream()
        let results = Task {
            do { for try await r in transcriber.results { emit(r.isFinal ? "final" : "partial", String(r.text.characters), u) } }
            catch { emit("err", "\(error)", u) }
        }
        do { try await analyzer.start(inputSequence: stream) } catch { emit("err", "start: \(error)", u); continue }
        emit("listening", "", u)
        var data: Data? = first
        while let d = data, !d.isEmpty {
            let b = pcm(d)
            let o = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: AVAudioFrameCount(Double(b.frameLength) * fmt.sampleRate / 16000) + 16)!
            var err: NSError? = nil; var fed = false
            conv.convert(to: o, error: &err) { _, st in if fed { st.pointee = .noDataNow; return nil }; fed = true; st.pointee = .haveData; return b }
            cont.yield(AnalyzerInput(buffer: o))
            data = readFrame()
        }
        cont.finish()
        do { try await analyzer.finalizeAndFinishThroughEndOfInput() } catch { emit("err", "finish: \(error)", u) }
        _ = await results.result
        emit("done", "", u)
        if data == nil { return }
    }
}
if #available(macOS 26, *) {
    let g = DispatchGroup(); g.enter()
    Task { await run(); g.leave() }
    g.wait(); exit(0)
} else { emit("err", "needs macOS 26"); exit(2) }
