import AVFoundation

protocol AudioInput: AnyObject {
    var running: Bool { get }
    var onFailure: ((Error) -> Void)? { get set }
    func activate(bluetooth: Bool) throws
    func start(at url: URL) throws
    func rotate(to url: URL) throws -> Int64
    func stop() throws -> Int64
}
final class Audio: AudioInput {
    static let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16_000, channels: 1, interleaved: true)!
    static let settings: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 16_000, AVNumberOfChannelsKey: 1, AVEncoderBitRateKey: 32_000]
    static func sessionOptions(bluetoothInput: Bool) -> AVAudioSession.CategoryOptions {
        var options: AVAudioSession.CategoryOptions = [.mixWithOthers, .defaultToSpeaker, .allowBluetoothA2DP]
        if bluetoothInput { options.insert(.allowBluetoothHFP) }
        return options
    }
    private var sessionActive = false
    private var engine: AVAudioEngine?
    private let lock = NSLock()
    private var sink: Sink?
    var onFailure: ((Error) -> Void)?
    var running: Bool { engine?.isRunning == true }
    func owns(_ object: AnyObject?) -> Bool { object === engine }
    var activeFile: URL? { lock.lock(); defer { lock.unlock() }; return sink?.final }
    final class Sink {
        var file: AVAudioFile?
        let backup: FileHandle
        let final: URL
        let part: URL
        let pcm: URL
        var bytes: Int64 = 0
        init(_ final: URL) throws {
            self.final = final; part = final.appendingPathExtension("part")
            pcm = final.deletingPathExtension().appendingPathExtension("pcm.part")
            let staging = final.deletingPathExtension().appendingPathExtension("opening.m4a")
            guard !FileManager.default.fileExists(atPath: part.path), !FileManager.default.fileExists(atPath: pcm.path) else { throw RecorderError("活动文件已存在，需先恢复") }
            // AVAudioFile needs an m4a extension when opening; rename the open inode immediately.
            file = try AVAudioFile(forWriting: staging, settings: Audio.settings, commonFormat: .pcmFormatInt16, interleaved: true)
            try FileManager.default.moveItem(at: staging, to: part)
            guard FileManager.default.createFile(atPath: pcm.path, contents: nil) else { throw RecorderError("无法创建 PCM 备份") }
            backup = try FileHandle(forWritingTo: pcm)
        }
        func append(_ buffer: AVAudioPCMBuffer) throws {
            let count = Int(buffer.frameLength) * 2
            guard count > 0, let data = buffer.int16ChannelData else { return }
            try backup.write(contentsOf: Data(bytes: data[0], count: count)); bytes += Int64(count)
            try file?.write(from: buffer)
        }
        func finish() throws -> Int64 {
            file = nil
            try backup.synchronize(); try backup.close()
            guard bytes > 0 else { return 0 } // Recovery inspects the closed files and records the gap.
            guard try Recovery.validDuration(part) != nil else { throw RecorderError("空段或 AAC 无效，保留 PCM 备份") }
            try FileManager.default.moveItem(at: part, to: final)
            try FileManager.default.removeItem(at: pcm)
            return Manifest.duration(pcmBytes: bytes)
        }
    }
    func activate(bluetooth: Bool) throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, mode: .default, options: Self.sessionOptions(bluetoothInput: bluetooth))
        try session.setActive(true)
        sessionActive = true
        if !bluetooth, let mic = session.availableInputs?.first(where: { $0.portType == .builtInMic }) { try session.setPreferredInput(mic) }
        guard !session.currentRoute.inputs.isEmpty else { throw RecorderError("等待系统恢复麦克风路由") }
    }
    func start(at url: URL) throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode.outputFormat(forBus: 0)
        guard input.sampleRate > 0, input.channelCount > 0, let converter = AVAudioConverter(from: input, to: Self.format) else { throw RecorderError("音频输入不可用") }
        let next = try Sink(url)
        lock.lock(); sink = next; lock.unlock()
        engine.inputNode.installTap(onBus: 0, bufferSize: 2048, format: input) { [weak self] buffer, _ in
            guard let self else { return }
            // Synchronous bounded conversion/write: no queued audio buffers can accumulate.
            self.lock.lock(); defer { self.lock.unlock() }
            guard let sink = self.sink else { return }
            let capacity = AVAudioFrameCount(Double(buffer.frameLength) * 16_000 / input.sampleRate + 32)
            guard let output = AVAudioPCMBuffer(pcmFormat: Self.format, frameCapacity: capacity) else { return }
            var supplied = false; var error: NSError?
            let status = converter.convert(to: output, error: &error) { _, status in
                if supplied { status.pointee = .noDataNow; return nil }
                supplied = true; status.pointee = .haveData; return buffer
            }
            do {
                if let error { throw error }
                if status == .error { throw RecorderError("PCM 转换失败") }
                try sink.append(output)
            }
            catch { self.sink = nil; self.onFailure?(error) }
        }
        self.engine = engine
        do { engine.prepare(); try engine.start() }
        catch { engine.inputNode.removeTap(onBus: 0); engine.stop(); self.engine = nil; lock.lock(); sink = nil; lock.unlock(); throw error }
    }
    func rotate(to url: URL) throws -> Int64 {
        let next = try Sink(url)
        lock.lock(); let previous = sink; sink = next; lock.unlock()
        return try previous?.finish() ?? 0
    }
    func stop() throws -> Int64 {
        if let engine { engine.inputNode.removeTap(onBus: 0); engine.stop() }
        engine = nil
        if sessionActive {
            do {
                try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
                sessionActive = false
            } catch { NSLog("EarRecorder audio session deactivation failed: %@", error.localizedDescription) }
        }
        lock.lock(); let previous = sink; sink = nil; lock.unlock()
        return try previous?.finish() ?? 0
    }
}

enum Recovery {
    // Decode all frames in bounded chunks; a valid header alone isn't a healthy recording.
    static func validDuration(_ url: URL) throws -> Int64? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        do {
            let file = try AVAudioFile(forReading: url)
            guard file.length > 0, let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 8192) else { return nil }
            var frames: Int64 = 0
            // read(into:) throws at EOF on Apple platforms instead of returning 0 frames,
            // so stop on framePosition rather than relying on an empty read.
            while file.framePosition < file.length {
                try file.read(into: buffer)
                if buffer.frameLength == 0 { break }
                frames += Int64(buffer.frameLength)
            }
            guard frames > 0 else { return nil }
            return Int64(Double(frames) * 1000 / file.processingFormat.sampleRate)
        } catch { return nil }
    }
    static func encode(_ pcm: URL, to output: URL) throws -> Int64 {
        let size = (try FileManager.default.attributesOfItem(atPath: pcm.path)[.size] as? NSNumber)?.int64Value ?? 0
        guard size >= 2, size % 2 == 0 else { throw RecorderError("PCM 字节数无效") }
        let input = try FileHandle(forReadingFrom: pcm); defer { try? input.close() }
        var file: AVAudioFile? = try AVAudioFile(forWriting: output, settings: Audio.settings, commonFormat: .pcmFormatInt16, interleaved: true)
        while let data = try input.read(upToCount: 65536), !data.isEmpty {
            let buffer = AVAudioPCMBuffer(pcmFormat: Audio.format, frameCapacity: AVAudioFrameCount(data.count / 2))!
            buffer.frameLength = buffer.frameCapacity
            _ = data.withUnsafeBytes { raw in memcpy(buffer.int16ChannelData![0], raw.baseAddress!, data.count) }
            try file?.write(from: buffer)
        }
        file = nil
        guard try validDuration(output) != nil else { throw RecorderError("PCM 重编码校验失败") }
        return Manifest.duration(pcmBytes: size)
    }
    static func audio(directory: URL, segment: inout Segment) {
        let final = directory.appendingPathComponent(segment.file), part = final.appendingPathExtension("part")
        let pcm = final.deletingPathExtension().appendingPathExtension("pcm.part")
        do {
            var duration: Int64?
            let backupBytes = ((try? FileManager.default.attributesOfItem(atPath: pcm.path)[.size]) as? NSNumber)?.int64Value ?? 0
            let backupDuration = Manifest.duration(pcmBytes: backupBytes)
            for candidate in [final, part] {
                if let d = try validDuration(candidate) {
                    // A decodable prefix after an encoder failure is not the complete PCM backup.
                    if backupDuration > d + 100 { continue }
                    duration = d
                    if candidate != final {
                        if FileManager.default.fileExists(atPath: final.path) {
                            try FileManager.default.moveItem(at: final, to: final.appendingPathExtension(UUID().uuidString + ".damaged"))
                        }
                        try FileManager.default.moveItem(at: candidate, to: final)
                    }
                    break
                }
            }
            if duration == nil {
                guard FileManager.default.fileExists(atPath: pcm.path) else {
                    throw RecorderError("未找到可解码的音频或 PCM 备份")
                }
                let size = try pcm.resourceValues(forKeys: [.fileSizeKey]).fileSize
                guard size != 0 else {
                    throw RecorderError("未取得音频样本（PCM 为 0 字节），此段没有可恢复的录音")
                }
                let output = final.deletingPathExtension().appendingPathExtension("recovered.m4a")
                if FileManager.default.fileExists(atPath: output.path) { try FileManager.default.removeItem(at: output) }
                duration = try encode(pcm, to: output)
                if FileManager.default.fileExists(atPath: final.path) {
                    try FileManager.default.moveItem(at: final, to: final.appendingPathExtension(UUID().uuidString + ".damaged"))
                }
                try FileManager.default.moveItem(at: output, to: final)
            } else if backupBytes > 0 {
                duration = backupDuration
            }
            segment.endMs = segment.startMs + duration!
            segment.recovery = "recovered"; segment.audio = Transfer()
            try? FileManager.default.removeItem(at: pcm)
        } catch {
            segment.recovery = "manual_recovery_required"; segment.audio.state = .manual_recovery_required
            segment.audio.error = error.localizedDescription
        }
    }
}
