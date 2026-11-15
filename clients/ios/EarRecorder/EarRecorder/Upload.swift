import Foundation
import Network
import CryptoKit

struct Multipart {
    let request: URLRequest
    let body: URL
    static func fields(manifest: Manifest, segment: Segment, context: Bool) throws -> [(String, String)] {
        guard let end = segment.endMs, end > segment.startMs else { throw RecorderError("分段时长无效") }
        let meta: [String: Any] = ["segmentIndex": segment.index - 1, "segmentStartMs": segment.startMs, "segmentEndMs": end, "source": "phone_local_auto"]
        return [("name", "PhoneVoice"), ("text", ""), ("type", context ? "phoneContext" : "richVoice"), ("source", "local"),
                ("group_id", manifest.groupId), ("segment_meta", String(data: try JSONSerialization.data(withJSONObject: meta, options: .sortedKeys), encoding: .utf8)!),
                ("segment_start_ms", String(segment.startMs)), ("segment_end_ms", String(end)), ("timestamp", iso(manifest.startedAt))]
    }
    static func make(manifest: Manifest, segment: Segment, context: Bool, settings: Settings, deviceId: String, directory: URL, temporary: URL) throws -> Multipart {
        guard var components = URLComponents(string: settings.server), ["http", "https"].contains(components.scheme ?? ""), components.host != nil, components.user == nil, components.password == nil else { throw RecorderError("请设置有效的 http/https 服务地址") }
        components.path = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/api/v1/record"
        if !components.path.hasPrefix("/") { components.path = "/" + components.path }
        components.query = nil; components.fragment = nil
        guard let url = components.url else { throw RecorderError("服务地址无效") }
        let boundary = "ear-" + UUID().uuidString
        var request = URLRequest(url: url); request.httpMethod = "POST"
        request.setValue("brain", forHTTPHeaderField: "X-Channel-Id")
        request.setValue(deviceId, forHTTPHeaderField: "X-Device-Id"); request.setValue(deviceId, forHTTPHeaderField: "X-Instance-Id")
        request.setValue(settings.userId, forHTTPHeaderField: "X-User-Id")
        request.setValue("Bearer " + settings.credential, forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let body = temporary.appendingPathComponent(UUID().uuidString + ".multipart")
        FileManager.default.createFile(atPath: body.path, contents: nil)
        let output = try FileHandle(forWritingTo: body)
        do {
            func write(_ text: String) throws { try output.write(contentsOf: Data(text.utf8)) }
            for (key, value) in try fields(manifest: manifest, segment: segment, context: context) {
                try write("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(key)\"\r\n\r\n\(value)\r\n")
            }
            let filename = context ? segment.contextFile : segment.file
            try write("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\nContent-Type: \(context ? "application/gzip" : "audio/mp4")\r\n\r\n")
            let input = try FileHandle(forReadingFrom: directory.appendingPathComponent(filename)); defer { try? input.close() }
            while let data = try input.read(upToCount: 65536), !data.isEmpty { try output.write(contentsOf: data) }
            try write("\r\n--\(boundary)--\r\n"); try output.synchronize(); try output.close()
            let size = try FileManager.default.attributesOfItem(atPath: body.path)[.size] as! NSNumber
            request.setValue(size.stringValue, forHTTPHeaderField: "Content-Length")
            if Relay.accepts(url) {
                guard settings.receiver != nil else { throw RecorderError("请先扫码配对中继") }
                let encrypted = temporary.appendingPathComponent(UUID().uuidString + ".sealed")
                FileManager.default.createFile(atPath: encrypted.path, contents: nil)
                do {
                    let output = try FileHandle(forWritingTo: encrypted)
                    defer { try? output.close() }
                    let outer = try Relay(secret: settings.credential).prepare(request, file: body, output: output)
                    try FileManager.default.removeItem(at: body)
                    return Multipart(request: outer, body: encrypted)
                } catch { try? FileManager.default.removeItem(at: encrypted); throw error }
            }
            return Multipart(request: request, body: body)
        } catch { try? output.close(); try? FileManager.default.removeItem(at: body); throw error }
    }
}

final class Upload: NSObject, URLSessionTaskDelegate, URLSessionDataDelegate {
    static let fallbackInterval: TimeInterval = 60
    static let identifier = "com.prts.ear.recorder.upload"
    private let store: Store
    private let diagnostics: Diagnostics
    private let queue = DispatchQueue(label: "ear.upload")
    private let monitor = NWPathMonitor()
    private var pathRevision = 0
    private var diagnosticRunning = false
    private var online = false, reconciled = false
    private var cleanupAt: Int64 = 0
    private var timer: DispatchSourceTimer?
    private var tasks: Set<String> = []
    private var progress: [Int: ProgressSample] = [:]
    private var bodies: [Int: URL] = [:]
    var onChange: (() -> Void)?
    var backgroundCompletion: (() -> Void)?
    private lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.identifier)
        config.sessionSendsLaunchEvents = true; config.isDiscretionary = false; config.waitsForConnectivity = true
        config.httpMaximumConnectionsPerHost = 2
        config.timeoutIntervalForResource = 1800
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()
    init(store: Store, diagnostics: Diagnostics) { self.store = store; self.diagnostics = diagnostics; super.init() }
    func start() {
        session.getAllTasks { existing in
            self.queue.async {
                self.tasks = Set(existing.compactMap(\.taskDescription))
                do {
                    for manifest in self.store.unfinished() {
                        try self.store.update(manifest.groupId) { m in
                            for i in m.segments.indices {
                                for context in [false, true] {
                                    let key = self.key(m.groupId, m.segments[i].index, context)
                                    if !self.tasks.contains(key) {
                                        if context, m.segments[i].context.state == .uploading { m.segments[i].context.state = .retry }
                                        if !context, m.segments[i].audio.state == .uploading { m.segments[i].audio.state = .retry }
                                    }
                                }
                            }
                        }
                    }
                    // Background daemon owns copies of existing bodies. Remove leftovers from prior runs.
                    let dir = self.bodyDirectory
                    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                    for file in try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil) { try? FileManager.default.removeItem(at: file) }
                    self.reconciled = true; self.pump()
                } catch { self.diagnostics.log("upload_reconcile_failed", ["error": error.localizedDescription]) }
            }
        }
        monitor.pathUpdateHandler = { path in
            self.selected = nil
            if let id = Settings.load().receiver {
                DispatchQueue.main.async {
                    Receiver.shared.resolve(id) { local in
                        if let local { Receiver.cache(local, id: id); self.kick() }
                    }
                }
            }
            self.pathRevision += 1
            self.online = path.status == .satisfied
            let types: [(NWInterface.InterfaceType, String)] = [
                (.wifi, "wifi"), (.cellular, "cellular"), (.wiredEthernet, "ethernet"),
                (.other, "other"), (.loopback, "loopback")
            ]
            self.diagnostics.log("network_path", [
                "revision": self.pathRevision, "status": String(describing: path.status),
                "uses": types.filter { path.usesInterfaceType($0.0) }.map { $0.1 },
                "interfaces": path.availableInterfaces.map { $0.name },
                "expensive": path.isExpensive, "constrained": path.isConstrained,
                "dns": path.supportsDNS, "ipv4": path.supportsIPv4, "ipv6": path.supportsIPv6
            ])
            self.pump()
        }
        monitor.start(queue: queue)
        let timer = DispatchSource.makeTimerSource(queue: queue); timer.schedule(deadline: .now() + Self.fallbackInterval, repeating: Self.fallbackInterval)
        timer.setEventHandler { [weak self] in self?.checkProgress() }; self.timer = timer; timer.resume()
    }
    struct ProgressSample {
        var bytes: Int64
        var time: TimeInterval
        var cancelledForStall = false
        mutating func stalled(bytes current: Int64, expected: Int64, now: TimeInterval) -> Bool {
            if current != bytes { bytes = current; time = now }
            guard current > 0, current < expected, now - time >= 300, !cancelledForStall else { return false }
            cancelledForStall = true
            return true
        }
    }
    private func checkProgress() {
        guard reconciled, !tasks.isEmpty else { pump(); return }
        session.getAllTasks { existing in
            self.queue.async {
                let now = ProcessInfo.processInfo.systemUptime
                for task in existing where task.state == .running {
                    let id = task.taskIdentifier
                    var sample = self.progress[id] ?? ProgressSample(bytes: task.countOfBytesSent, time: now)
                    if sample.stalled(bytes: task.countOfBytesSent, expected: task.countOfBytesExpectedToSend, now: now) {
                        self.diagnostics.log("upload_stalled", ["bytes": task.countOfBytesSent, "expected": task.countOfBytesExpectedToSend])
                        task.cancel()
                    }
                    self.progress[id] = sample
                }
                self.pump()
            }
        }
    }
    private var bodyDirectory: URL { store.root.appendingPathComponent("uploads") }
    private func key(_ id: String, _ index: Int, _ context: Bool) -> String { "\(id)|\(index)|\(context ? "context" : "audio")" }
    func kick() { queue.async { self.pump() } }
    private var selected: (server: String, primary: String, fallback: String, token: String, until: Date)?
    #if DEBUG
    private var isolateNextDirectProbe = ProcessInfo.processInfo.environment["EAR_TEST_ISOLATE_DIRECT"] == "1"
    #endif
    private var probing = false
    private var retryProbeAfter = Date.distantPast
    private var failedServer: String?
    private lazy var probeSession: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForResource = 6
        config.timeoutIntervalForRequest = 5
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    func diagnose() {
        queue.async {
            guard !self.diagnosticRunning else { return }
            let settings = Settings.load()
            let endpoints = [("direct", settings.direct), ("fallback", settings.fallbackServer ?? "")]
                .filter { !$0.1.isEmpty }
            self.diagnosticRunning = !endpoints.isEmpty
            var remaining = endpoints.count
            for (route, server) in endpoints {
                self.probe(server, token: settings.credential, route: route, source: "manual") { _ in
                    remaining -= 1
                    self.diagnosticRunning = remaining > 0
                }
            }
        }
    }

    private func probe(_ server: String, token: String, route: String, source: String = "upload", completion: @escaping (Bool) -> Void) {
        let probeID = UUID().uuidString
        let revision = pathRevision
        guard let base = URL(string: server), base.host != nil,
              base.user == nil, base.password == nil,
              var url = URLComponents(url: base.appendingPathComponent("health"), resolvingAgainstBaseURL: false) else {
            diagnostics.log("upload_probe", ["id": probeID, "route": route, "ok": false, "reason": "invalid_url", "source": source]); completion(false); return
        }
        let nonce = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        url.queryItems = [URLQueryItem(name: "challenge", value: nonce)]
        if Settings.load().receiver != nil { url.queryItems?.append(URLQueryItem(name: "device", value: Settings.deviceId)) }
        guard let target = url.url else { completion(false); return }
        let key = SymmetricKey(data: Data(token.utf8))
        let proof = HMAC<SHA256>.authenticationCode(for: Data(("ear-health-v1:" + nonce).utf8), using: key)
            .map { String(format: "%02x", $0) }.joined()
        let started = ProcessInfo.processInfo.systemUptime
        var request = URLRequest(url: target)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let task = probeSession.dataTask(with: request) { data, response, error in
            let payload = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let ok = (response as? HTTPURLResponse)?.statusCode == 200 && payload?["proof"] as? String == proof
            self.queue.async {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                let reason = error != nil ? "transport" : code != 200 ? "http" : ok ? "verified" : "invalid_proof"
                var fields: [String: Any] = ["id": probeID, "route": route, "source": source,
                    "host": base.host ?? "", "ok": ok, "http": code, "reason": reason,
                    "start_path": revision, "end_path": self.pathRevision,
                    "elapsed_ms": Int((ProcessInfo.processInfo.systemUptime - started) * 1000)]
                if let e = error as NSError? { fields["error_domain"] = e.domain; fields["error_code"] = e.code }
                self.diagnostics.log("upload_probe", fields)
                completion(ok)
            }
        }
        task.taskDescription = "probe-" + probeID
        task.resume()
    }

    private func route(_ settings: Settings) -> String? {
        let primary = settings.direct
        let fallback = settings.fallbackServer ?? ""
        if settings.receiver == nil && fallback.isEmpty { return primary }
        if primary.isEmpty && fallback.isEmpty { return nil }
        if !fallback.isEmpty && URL(string: fallback)?.scheme != "https" && !(settings.receiver != nil && Relay.accepts(URL(string: fallback))) { return nil }
        if let selected, selected.primary == primary, selected.fallback == fallback,
           selected.token == settings.credential, selected.until > Date() { return selected.server }
        guard !probing, Date() >= retryProbeAfter else { return nil }
        probing = true
        let choices = (failedServer == primary ? [fallback, primary] : [primary, fallback]).filter { !$0.isEmpty }
        func attempt(_ index: Int) {
            let server = choices[index]
            var probeServer = server
            #if DEBUG
            if server == primary && self.isolateNextDirectProbe {
                self.isolateNextDirectProbe = false
                probeServer = "http://127.0.0.1:1"
            }
            #endif
            self.probe(probeServer, token: settings.credential, route: server == primary ? "direct" : "fallback") { ok in
                if ok {
                    self.selected = (server, primary, fallback, settings.credential, Date().addingTimeInterval(60))
                    self.failedServer = nil
                    self.probing = false
                    self.pump()
                } else if index + 1 < choices.count {
                    attempt(1)
                } else {
                    self.probing = false
                    self.retryProbeAfter = Date().addingTimeInterval(15)
                }
            }
        }
        attempt(0)
        return nil
    }
    private func pump() {
        guard reconciled, online, tasks.count < 2 else { return }
        var settings = Settings.load(); guard !settings.direct.isEmpty || settings.receiver != nil else { return }
        if tasks.isEmpty, epochMS() >= cleanupAt {
            do { try store.cleanAudio(retainDays: settings.retainDays, now: epochMS()) }
            catch { diagnostics.log("audio_cleanup_failed", ["error": error.localizedDescription]) }
            cleanupAt = epochMS() + 3_600_000
        }
        for manifest in store.unfinished().reversed() {
            for segment in manifest.segments where segment.endMs != nil {
                for context in [false, true] where (context ? segment.context : segment.audio).ready(at: epochMS()) {
                    guard let server = route(Settings.load()) else { return }
                    settings.server = server
                    let taskKey = key(manifest.groupId, segment.index, context)
                    do {
                        let multipart = try Multipart.make(manifest: manifest, segment: segment, context: context, settings: settings, deviceId: Settings.deviceId, directory: store.directory(manifest.groupId), temporary: bodyDirectory)
                        try store.update(manifest.groupId) { m in
                            if let i = m.segments.firstIndex(where: { $0.index == segment.index }) {
                                if context { m.segments[i].context.state = .uploading } else { m.segments[i].audio.state = .uploading }
                            }
                        }
                        let task = session.uploadTask(with: multipart.request, fromFile: multipart.body)
                        diagnostics.log("upload_dispatch", ["key": taskKey, "route": server == settings.fallbackServer ? "fallback" : "direct"])
                        task.taskDescription = taskKey; tasks.insert(taskKey); bodies[task.taskIdentifier] = multipart.body; task.resume(); onChange?()
                        if tasks.count >= 2 { return }
                    } catch { finish(key: taskKey, success: false, error: error.localizedDescription); return }
                }
            }
        }
    }
    private func finish(key: String, success: Bool, error: String?, transient: Bool = false) {
        let parts = key.split(separator: "|").map(String.init)
        guard parts.count == 3, let index = Int(parts[1]) else { return }
        do {
            try store.update(parts[0]) { m in
                if let i = m.segments.firstIndex(where: { $0.index == index }) {
                    if parts[2] == "context" { m.segments[i].context.finish(success: success, now: epochMS(), error: error, transient: transient) }
                    else { m.segments[i].audio.finish(success: success, now: epochMS(), error: error, transient: transient) }
                }
                if m.status == .upload_pending, m.segments.allSatisfy({ $0.audio.state == .uploaded && $0.context.state == .uploaded }) { m.status = .completed }
            }
            if success, parts[2] == "audio", Settings.load().retainDays == 0 { cleanupAt = 0 }
            diagnostics.log("upload_result", ["key": key, "success": success, "error": error ?? ""]); onChange?()
        } catch { diagnostics.log("upload_manifest_failed", ["error": error.localizedDescription]) }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard session === self.session else { return }
        queue.async {
            let stalled = self.progress.removeValue(forKey: task.taskIdentifier)?.cancelledForStall == true
            if let body = self.bodies.removeValue(forKey: task.taskIdentifier) { try? FileManager.default.removeItem(at: body) }
            guard let key = task.taskDescription else { return }
            let code = (task.response as? HTTPURLResponse)?.statusCode ?? 0
            var authenticated = true
            if Relay.accepts(task.originalRequest?.url) {
                let ticket = task.originalRequest?.value(forHTTPHeaderField: "X-Ear-Request") ?? ""
                authenticated = (try? Relay(secret: Settings.load().credential, ticket: ticket).verified(task.response as? HTTPURLResponse)) == true
            }
            let success = error == nil && authenticated && (200...299).contains(code)
            let urlError = error as? URLError
            let transientCodes: [URLError.Code] = [.timedOut, .networkConnectionLost, .notConnectedToInternet, .cannotFindHost, .cannotConnectToHost, .dnsLookupFailed]
            let transient = stalled || urlError.map { transientCodes.contains($0.code) } == true
            if transient || (500...599).contains(code) || urlError?.code == .appTransportSecurityRequiresSecureConnection {
                if let selected = self.selected,
                   task.originalRequest?.url == URL(string: selected.server)?.appendingPathComponent("api/v1/record") {
                    self.failedServer = selected.server
                    self.selected = nil
                }
            }
            let message = !authenticated && error == nil ? "中继回执无效，等待重试" : stalled ? "上传中途停滞，已排队重试" : error?.localizedDescription
            self.finish(key: key, success: success, error: success ? nil : (message ?? "HTTP \(code)"), transient: transient)
            self.tasks.remove(key); self.pump()
        }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didFinishCollecting metrics: URLSessionTaskMetrics) {
        guard let key = task.taskDescription else { return }
        for transaction in metrics.transactionMetrics {
            var fields: [String: Any] = ["task": key, "proxy": transaction.isProxyConnection,
                "cellular": transaction.isCellular, "reused": transaction.isReusedConnection,
                "expensive": transaction.isExpensive, "constrained": transaction.isConstrained]
            fields["peer"] = transaction.remoteAddress
            fields["local"] = transaction.localAddress
            for (stage, start, end) in [
                ("dns", transaction.domainLookupStartDate, transaction.domainLookupEndDate),
                ("tcp", transaction.connectStartDate, transaction.connectEndDate),
                ("tls", transaction.secureConnectionStartDate, transaction.secureConnectionEndDate)
            ] {
                if let start {
                    fields[stage + "_started"] = true
                    if let end { fields[stage + "_ms"] = Int(end.timeIntervalSince(start) * 1000) }
                }
            }
            diagnostics.log("network_transaction", fields)
        }
    }

    // Never forward bearer credentials through an HTTP redirect.
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        PinnedSession.authenticate(challenge, pin: Settings.load().pin, completion: completionHandler)
    }
    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        queue.async { DispatchQueue.main.async { self.backgroundCompletion?(); self.backgroundCompletion = nil } }
    }
}
