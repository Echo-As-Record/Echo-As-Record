import Foundation

func epochMS() -> Int64 { Int64(Date().timeIntervalSince1970 * 1000) }
func iso(_ ms: Int64) -> String { ISO8601DateFormatter().string(from: Date(timeIntervalSince1970: Double(ms) / 1000)) }
struct RecorderError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}
enum SessionState: String, Codable { case recording, paused, finalizing, upload_pending, completed }
enum TransferState: String, Codable { case pending, uploading, retry, uploaded, manual_recovery_required }
struct Transfer: Codable {
    var state: TransferState = .pending
    var attempts = 0
    var nextAttemptMs: Int64 = 0
    var uploadedAt: Int64?
    var error: String?
    mutating func finish(success: Bool, now: Int64, error: String? = nil, transient: Bool = false) {
        self.error = error
        if success { state = .uploaded; uploadedAt = now; nextAttemptMs = 0 }
        else { attempts += 1; state = .retry; nextAttemptMs = now + Int64(min(transient ? 60 : 3600, pow(2, Double(min(attempts, 12))))) * 1000 }
    }
    func ready(at now: Int64) -> Bool { (state == .pending || state == .retry) && now >= nextAttemptMs }
}
struct Segment: Codable {
    var index: Int
    var startMs: Int64
    var endMs: Int64?
    var file: String { String(format: "segment_%04d.m4a", index) }
    var contextFile: String { String(format: "context_%04d.jsonl.gz", index) }
    var audio = Transfer()
    var context = Transfer()
    var recovery: String?
    // Filenames are stored as well as derived, for inspection by other tools.
    enum CodingKeys: String, CodingKey { case index, startMs, endMs, audio, context, recovery, file, contextFile }
    init(index: Int, startMs: Int64) { self.index = index; self.startMs = startMs }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        index = try c.decode(Int.self, forKey: .index); startMs = try c.decode(Int64.self, forKey: .startMs)
        endMs = try c.decodeIfPresent(Int64.self, forKey: .endMs)
        audio = try c.decode(Transfer.self, forKey: .audio); context = try c.decode(Transfer.self, forKey: .context)
        recovery = try c.decodeIfPresent(String.self, forKey: .recovery)
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(index, forKey: .index); try c.encode(startMs, forKey: .startMs); try c.encodeIfPresent(endMs, forKey: .endMs)
        try c.encode(file, forKey: .file); try c.encode(contextFile, forKey: .contextFile)
        try c.encode(audio, forKey: .audio); try c.encode(context, forKey: .context); try c.encodeIfPresent(recovery, forKey: .recovery)
    }
}
struct Interruption: Codable {
    var id = UUID().uuidString.lowercased()
    let trigger: String
    let observedAt: Int64
    let audioEndedAt: Int64?
}
struct Manifest: Codable {
    var interruption: Interruption?
    var groupId = UUID().uuidString.lowercased()
    var startedAt = epochMS()
    var status: SessionState = .recording
    var userStopped = false
    var segments: [Segment] = []
    var continuedFrom: String?
    static func shouldContinue(_ latest: Manifest?, isRecording: Bool) -> Bool { !isRecording && latest?.userStopped == false }
    static func duration(pcmBytes: Int64) -> Int64 { (pcmBytes / 2) * 1000 / 16_000 }
}
final class Store {
    static let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("EarRecorder")
    let root: URL
    private let lock = NSRecursiveLock()
    private var cache: [String: Manifest] = [:] // Only unfinished manifests stay resident.
    private var latestManifest: Manifest?
    private var retention: [URL: Int64] = [:] // Small cleanup index, separate from upload scans.
    private(set) var diskReadCount = 0
    init(root: URL = Store.root) throws {
        self.root = root
        try FileManager.default.createDirectory(at: root.appendingPathComponent("sessions"), withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: root.path)
        var url = root; var values = URLResourceValues(); values.isExcludedFromBackup = true; try url.setResourceValues(values)
        // One startup scan, never from a timer. Completed manifests are discarded after indexing.
        for directory in try FileManager.default.contentsOfDirectory(at: root.appendingPathComponent("sessions"), includingPropertiesForKeys: nil)
        where UUID(uuidString: directory.lastPathComponent) != nil {
            index(try load(directory.lastPathComponent))
        }

    }
    func directory(_ id: String) -> URL { root.appendingPathComponent("sessions").appendingPathComponent(id) }
    func read(_ id: String) throws -> Manifest {
        lock.lock(); defer { lock.unlock() }
        if let manifest = cache[id] { return manifest }
        if let manifest = latestManifest, manifest.groupId == id { return manifest }
        return try load(id) // Explicit historical access only; never used by the upload pump.
    }
    private func load(_ id: String) throws -> Manifest {
        diskReadCount += 1
        return try JSONDecoder().decode(Manifest.self, from: Data(contentsOf: directory(id).appendingPathComponent("manifest.json")))
    }
    private func index(_ manifest: Manifest) {
        if manifest.status == .completed { cache.removeValue(forKey: manifest.groupId) }
        else { cache[manifest.groupId] = manifest }
        if latestManifest == nil || manifest.startedAt >= latestManifest!.startedAt || latestManifest?.groupId == manifest.groupId { latestManifest = manifest }
        for segment in manifest.segments where segment.audio.state == .uploaded {
            if let at = segment.audio.uploadedAt { retention[directory(manifest.groupId).appendingPathComponent(segment.file)] = at }
        }
    }
    func save(_ manifest: Manifest) throws {
        lock.lock(); defer { lock.unlock() }
        let dir = directory(manifest.groupId)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(manifest).write(to: dir.appendingPathComponent("manifest.json"), options: .atomic)
        index(manifest) // Update memory only after the atomic disk write succeeds.
    }
    @discardableResult func update(_ id: String, _ change: (inout Manifest) -> Void) throws -> Manifest {
        lock.lock(); defer { lock.unlock() }
        var m = try read(id); change(&m); try save(m); return m
    }
    func unfinished() -> [Manifest] {
        lock.lock(); defer { lock.unlock() }
        return cache.values.sorted { $0.startedAt < $1.startedAt }
    }
    func latest() -> Manifest? {
        lock.lock(); defer { lock.unlock() }; return latestManifest
    }
    func cleanAudio(retainDays: Int, now: Int64) throws {
        guard retainDays >= 0 else { return }
        lock.lock(); defer { lock.unlock() }
        for (url, uploadedAt) in retention where now - uploadedAt >= Int64(retainDays) * 86_400_000 {
            do { try FileManager.default.removeItem(at: url) }
            catch let error as CocoaError where error.code == .fileNoSuchFile { }
            retention.removeValue(forKey: url)
        }
    }
}
struct Settings: Codable, Equatable {
    var receiver: String?
    var pin: String?
    var credentialKey: String?
    var direct: String { receiver.map(Receiver.direct) ?? server }
    var credential: String { credentialKey.map(Receiver.credential) ?? token }
    var server = ""
    var fallbackServer: String?
    var token = ""
    var userId = ""
    var segmentMs: Int64 = 1_800_000
    var distanceFilter = 25.0
    var bluetooth = true
    var retainDays = 3 // -1 means keep forever; 0 means delete after successful upload.
    static func load() -> Settings { UserDefaults.standard.data(forKey: "settings").flatMap { try? JSONDecoder().decode(Self.self, from: $0) } ?? Settings() }
    func save() throws {
        let data = try JSONEncoder().encode(self)
        UserDefaults.standard.set(data, forKey: "settings")
    }
    static var deviceId: String {
        if let id = UserDefaults.standard.string(forKey: "deviceId") { return id }
        let id = "ear-ios-" + UUID().uuidString.lowercased(); UserDefaults.standard.set(id, forKey: "deviceId"); return id
    }
}
