import XCTest
import Network
import AVFoundation
import CoreLocation
import zlib
@testable import EarRecorder

final class RecorderTests: XCTestCase {
    var root: URL!
    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }
    override func tearDownWithError() throws { try FileManager.default.removeItem(at: root) }
    func testManifestAtomicRoundTripAndConcurrentChanges() throws {
        let store = try Store(root: root); var m = Manifest(); m.segments = [Segment(index: 1, startMs: 0)]
        try store.save(m)
        let failures = NSLock(); var errors = [Error]()
        DispatchQueue.concurrentPerform(iterations: 30) { _ in
            do { try store.update(m.groupId) { $0.segments[0].audio.attempts += 1 }; _ = try store.read(m.groupId) }
            catch { failures.lock(); errors.append(error); failures.unlock() }
        }
        XCTAssertTrue(errors.isEmpty)
        XCTAssertEqual(try store.read(m.groupId).segments[0].audio.attempts, 30)
        XCTAssertEqual(try Store(root: root).read(m.groupId).segments[0].audio.attempts, 30)
        let raw = try String(contentsOf: store.directory(m.groupId).appendingPathComponent("manifest.json"))
        XCTAssertTrue(raw.contains("segment_0001.m4a")); XCTAssertEqual(store.unfinished().count, 1)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: store.directory(m.groupId).path), ["manifest.json"])
    }
    func testBluetoothMusicRouteWithPhoneMicrophone() {
        let options = Audio.sessionOptions(bluetoothInput: false)
        XCTAssertTrue(options.contains([.mixWithOthers, .allowBluetoothA2DP]))
        XCTAssertFalse(options.contains(.allowBluetoothHFP))
        XCTAssertTrue(Audio.sessionOptions(bluetoothInput: true).contains(.allowBluetoothHFP))
    }
    func testRelativeTimeAndContinuation() {
        XCTAssertEqual(Manifest.duration(pcmBytes: 32_000 * 1800), 1_800_000)
        XCTAssertEqual(Manifest.duration(pcmBytes: 32032), 1001)
        var m = Manifest(); XCTAssertTrue(Manifest.shouldContinue(m, isRecording: false))
        XCTAssertFalse(Manifest.shouldContinue(m, isRecording: true))
        m.userStopped = true; XCTAssertFalse(Manifest.shouldContinue(m, isRecording: false))
        XCTAssertFalse(Manifest.shouldContinue(nil, isRecording: false))
    }
    func testTransientRetryDelayIsCapped() {
        var t = Transfer()
        t.attempts = 12
        t.finish(success: false, now: 1000, transient: true)
        XCTAssertEqual(t.nextAttemptMs, 61_000)
        t.finish(success: false, now: 1000)
        XCTAssertEqual(t.nextAttemptMs, 3_601_000)
    }

    func testTransferBackoffAndSuccess() {
        var t = Transfer(); XCTAssertTrue(t.ready(at: 0)); t.state = .uploading; XCTAssertFalse(t.ready(at: 0))
        t.finish(success: false, now: 1000, error: "offline"); XCTAssertEqual(t.state, .retry)
        XCTAssertFalse(t.ready(at: 2999)); XCTAssertTrue(t.ready(at: 3000))
        t.finish(success: false, now: 3000); XCTAssertEqual(t.nextAttemptMs, 7000)
        t.finish(success: true, now: 8000); XCTAssertEqual(t.state, .uploaded); XCTAssertEqual(t.uploadedAt, 8000)
        XCTAssertFalse(t.ready(at: Int64.max)); XCTAssertNil(t.error)
    }
    func testMultipartFieldsHeadersAndPayloadForBothTypes() throws {
        var m = Manifest(); m.startedAt = 1_700_000_000_000
        var segment = Segment(index: 2, startMs: 1_800_000); segment.endMs = 3_600_000
        var settings = Settings(); settings.server = "http://192.168.1.2:8787"; settings.token = "test-token"; settings.userId = "test-user"
        for context in [false, true] {
            var first = Segment(index: 1, startMs: 0); first.endMs = 1000
            let firstFields = Dictionary(uniqueKeysWithValues: try Multipart.fields(manifest: m, segment: first, context: context))
            let firstMeta = try JSONSerialization.jsonObject(with: Data(firstFields["segment_meta"]!.utf8)) as! [String: Any]
            XCTAssertEqual(firstMeta["segmentIndex"] as? Int, 0)
            XCTAssertEqual(first.file, "segment_0001.m4a"); XCTAssertEqual(first.contextFile, "context_0001.jsonl.gz")
            let name = context ? segment.contextFile : segment.file
            try Data("payload".utf8).write(to: root.appendingPathComponent(name))
            let body = try Multipart.make(manifest: m, segment: segment, context: context, settings: settings, deviceId: "ear-ios-test", directory: root, temporary: root)
            XCTAssertEqual(body.request.url?.absoluteString, "http://192.168.1.2:8787/api/v1/record")
            for (header, value) in ["Authorization": "Bearer test-token", "X-Channel-Id": "brain", "X-Device-Id": "ear-ios-test", "X-Instance-Id": "ear-ios-test", "X-User-Id": "test-user"] { XCTAssertEqual(body.request.value(forHTTPHeaderField: header), value) }
            let text = try String(contentsOf: body.body)
            let fields = Dictionary(uniqueKeysWithValues: try Multipart.fields(manifest: m, segment: segment, context: context))
            XCTAssertEqual(Set(fields.keys), Set(["name", "text", "type", "source", "group_id", "segment_meta", "segment_start_ms", "segment_end_ms", "timestamp"]))
            XCTAssertEqual(fields["source"], "local"); XCTAssertEqual(fields["name"], "PhoneVoice")
            XCTAssertEqual(fields["timestamp"], "2023-11-14T22:13:20Z")
            XCTAssertEqual(fields["type"], context ? "phoneContext" : "richVoice")
            let meta = try JSONSerialization.jsonObject(with: Data(fields["segment_meta"]!.utf8)) as! [String: Any]
            XCTAssertEqual(meta["segmentIndex"] as? Int, 1); XCTAssertEqual(meta["source"] as? String, "phone_local_auto")
            XCTAssertEqual(meta["segmentStartMs"] as? Int, 1_800_000); XCTAssertEqual(meta["segmentEndMs"] as? Int, 3_600_000)
            for (key, value) in fields { XCTAssertTrue(text.contains("name=\"\(key)\"\r\n\r\n\(value)\r\n")) }
            XCTAssertTrue(text.contains("filename=\"\(name)\"")); XCTAssertTrue(text.contains(context ? "application/gzip" : "audio/mp4"))
            XCTAssertEqual(Int(body.request.value(forHTTPHeaderField: "Content-Length")!), try Data(contentsOf: body.body).count)
            XCTAssertTrue(text.contains("\r\n\r\npayload\r\n--"))
        }
    }
    private func rows(_ url: URL) throws -> [[String: Any]] {
        let file = try XCTUnwrap(gzopen(url.path, "rb")); defer { gzclose(file) }
        var bytes = [UInt8](repeating: 0, count: 4096), data = Data()
        while true { let n = gzread(file, &bytes, UInt32(bytes.count)); XCTAssertGreaterThanOrEqual(n, 0); if n <= 0 { break }; data.append(contentsOf: bytes.prefix(Int(n))) }
        var code: Int32 = 0; _ = gzerror(file, &code); XCTAssertEqual(code, Z_OK)
        return try data.split(separator: 10).map { try JSONSerialization.jsonObject(with: Data($0)) as! [String: Any] }
    }
    func testContextSampleCompatibleGzipAndChangedOnly() throws {
        let file = root.appendingPathComponent("context_0002.jsonl.gz")
        let baseline: [String: Any] = ["battery": 35, "isCharging": false, "device_model": "iPhone15,4", "os_version": "26.6.2", "platform": "ios", "latitude": 40.0, "longitude": 116.0, "locationAccuracy": 10.0, "locationTimestamp": 1234, "locationMeta": ["trigger": "continuous", "source": "core_location_update"]]
        let writer = try Context(url: file, index: 2, elapsed: 1_800_000, baseline: baseline)
        try writer.delta(["battery": 35, "latitude": 41.0, "locationTimestamp": 5678], elapsed: 1_801_000)
        try writer.event("location_arrived", reason: "visit_arrival", changed: ["latitude": 41.0], elapsed: 1_802_000)
        try writer.finish(elapsed: 3_600_000)
        let lines = try rows(file); XCTAssertEqual(lines.count, 4)
        XCTAssertEqual(lines[0]["type"] as? String, "baseline"); XCTAssertEqual(Set((lines[0]["context"] as! [String: Any]).keys), Set(baseline.keys))
        XCTAssertTrue(lines.allSatisfy { $0["segmentIndex"] as? Int == 1 })
        XCTAssertEqual(lines[0]["elapsedMs"] as? Int, 1_800_000)
        XCTAssertEqual(Set((lines[1]["changed"] as! [String: Any]).keys), Set(["latitude", "locationTimestamp"]))
        let location = try XCTUnwrap(lines[1]["context"] as? [String: NSNumber])
        XCTAssertEqual(location, ["latitude": 41, "longitude": 116, "locationAccuracy": 10, "locationTimestamp": 5678])
        XCTAssertEqual((lines[2]["meta"] as? [String: String])?["reason"], "visit_arrival")
        XCTAssertEqual(lines[3]["summary"] as? [String: Int], ["deltaCount": 1, "eventCount": 1, "arrivalCount": 1])
    }
    func testTruncatedGzipRecoveryPreservesCompleteRows() throws {
        let url = root.appendingPathComponent("context.jsonl.gz")
        do { let writer = try Context(url: url, index: 1, elapsed: 0, baseline: ["platform": "ios"]); try writer.finish(elapsed: 1000) }
        var data = try Data(contentsOf: url); data.removeLast(8); try data.write(to: url)
        XCTAssertNotNil(GzipRecovery().recover(url)); XCTAssertEqual(try rows(url).count, 2)
    }
    func testKilledContextGetsRecoveryEventAndSummary() throws {
        let url = root.appendingPathComponent("context_0001.jsonl.gz")
        var writer: Context? = try Context(url: url, index: 1, elapsed: 0, baseline: ["platform": "ios"])
        try writer?.delta(["battery": 50], elapsed: 1000)
        writer = nil // No segment_end, as after process termination.
        let part = url.appendingPathExtension("part")
        XCTAssertNotNil(GzipRecovery().recover(part))
        try Context.closeRecovered(part, index: 1, elapsed: 1500)
        let lines = try rows(part)
        XCTAssertEqual(lines.count, 4)
        XCTAssertTrue(lines.allSatisfy { $0["segmentIndex"] as? Int == 0 })
        XCTAssertEqual(lines[2]["event"] as? String, "recovery")
        XCTAssertEqual(lines[3]["summary"] as? [String: Int], ["deltaCount": 1, "eventCount": 1, "arrivalCount": 0])
    }
    func testStoppedBeforeFirstBufferPreservesEmptySegmentEvidence() throws {
        let sink = try Audio.Sink(root.appendingPathComponent("segment_0004.m4a"))
        XCTAssertEqual(try sink.finish(), 0)
        var segment = Segment(index: 4, startMs: 200)
        Recovery.audio(directory: root, segment: &segment)
        XCTAssertEqual(segment.audio.state, .manual_recovery_required)
        XCTAssertTrue(segment.audio.error?.contains("0 字节") == true)
        XCTAssertNil(segment.endMs)
        XCTAssertTrue(FileManager.default.fileExists(atPath: sink.pcm.path))
    }
    func testRecoveryGoodM4aAndPCMAndDamaged() throws {
        var segment = Segment(index: 1, startMs: 50)
        let pcm = root.appendingPathComponent("segment_0001.pcm.part")
        try Data(repeating: 0, count: 32_000).write(to: pcm)
        Recovery.audio(directory: root, segment: &segment)
        XCTAssertEqual(segment.endMs, 1050); XCTAssertEqual(segment.recovery, "recovered")
        XCTAssertFalse(FileManager.default.fileExists(atPath: pcm.path))
        var good = Segment(index: 1, startMs: 50); Recovery.audio(directory: root, segment: &good)
        XCTAssertEqual(good.recovery, "recovered"); XCTAssertGreaterThan(good.endMs ?? 0, 50)
        var damaged = Segment(index: 2, startMs: 99)
        let broken = root.appendingPathComponent("segment_0002.m4a.part"), odd = root.appendingPathComponent("segment_0002.pcm.part")
        try Data("broken".utf8).write(to: broken); try Data([0]).write(to: odd)
        Recovery.audio(directory: root, segment: &damaged)
        XCTAssertEqual(damaged.audio.state, .manual_recovery_required); XCTAssertNil(damaged.endMs)
        XCTAssertEqual(try Data(contentsOf: broken), Data("broken".utf8)); XCTAssertEqual(try Data(contentsOf: odd), Data([0]))
    }
    func testCompletedSessionsLeaveCacheAndRetentionStillWorks() throws {
        let store = try Store(root: root)
        var m = Manifest(); m.startedAt = 1; m.userStopped = true; m.status = .upload_pending
        var segment = Segment(index: 1, startMs: 0); segment.endMs = 1000
        segment.audio.finish(success: true, now: 1000); segment.context.finish(success: true, now: 1000)
        m.segments = [segment]; try store.save(m)
        let audio = store.directory(m.groupId).appendingPathComponent(segment.file)
        try Data([1]).write(to: audio)
        try store.update(m.groupId) { $0.status = .completed }
        let reopened = try Store(root: root), reads = reopened.diskReadCount
        for _ in 0..<200 { XCTAssertTrue(reopened.unfinished().isEmpty); XCTAssertEqual(reopened.latest()?.groupId, m.groupId) }
        XCTAssertEqual(reopened.diskReadCount, reads)
        try reopened.cleanAudio(retainDays: 3, now: 1000 + 2 * 86_400_000)
        XCTAssertTrue(FileManager.default.fileExists(atPath: audio.path))
        try reopened.cleanAudio(retainDays: 3, now: 1000 + 3 * 86_400_000)
        XCTAssertFalse(FileManager.default.fileExists(atPath: audio.path))
        XCTAssertEqual(reopened.diskReadCount, reads)
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.directory(m.groupId).appendingPathComponent("manifest.json").path))
        XCTAssertEqual(Upload.fallbackInterval, 60)
    }
    func testBackgroundTicksDoNotReadManifestsRecountOrPublish() throws {
        let store = try Store(root: root), audio = FakeAudio()
        // This test exercises ticks within a segment, even on a configured simulator.
        let previous = UserDefaults.standard.data(forKey: "settings")
        defer { UserDefaults.standard.set(previous, forKey: "settings") }
        try Settings().save()
        var time: Int64 = 100_000
        let runtime = Runtime(store: store, audio: audio, location: FakeLocation(), now: { time })
        runtime.start(); runtime.queue.sync {}
        let reads = store.diskReadCount, refreshes = runtime.pendingRefreshCount
        let hidden = expectation(description: "no background UI delivery"); hidden.isInverted = true
        runtime.onSnapshot = { _ in hidden.fulfill() }
        runtime.queue.sync {
            for _ in 0..<120 { time += 1000; runtime.tick() }
            XCTAssertEqual(runtime.elapsed(), 120_000)
        }
        wait(for: [hidden], timeout: 0.1)
        XCTAssertEqual(store.diskReadCount, reads); XCTAssertEqual(runtime.pendingRefreshCount, refreshes)
        let visible = expectation(description: "foreground snapshot")
        runtime.onSnapshot = { value in
            XCTAssertEqual(value.elapsed, 120_000); XCTAssertEqual(value.pending, 2); visible.fulfill()
        }
        runtime.setActive(true); runtime.queue.sync {}
        wait(for: [visible], timeout: 1)
        runtime.setActive(false); runtime.queue.sync {}
        // Upload state changes refresh pending even while UI is hidden.
        let id = try XCTUnwrap(store.latest()?.groupId)
        try store.update(id) { $0.segments[0].audio.finish(success: true, now: time) }
        runtime.upload?.onChange?(); runtime.queue.sync {}
        XCTAssertEqual(runtime.pendingRefreshCount, refreshes + 1)
        XCTAssertEqual(store.diskReadCount, reads)
        runtime.stop(); runtime.queue.sync {}
    }
    func testRouteNotificationsKeepSegmentButStoppedEngineStillSeals() throws {
        let store = try Store(root: root), audio = FakeAudio()
        let runtime = Runtime(store: store, audio: audio, location: FakeLocation())
        runtime.start(); runtime.queue.sync {}
        let id = try XCTUnwrap(store.latest()?.groupId)
        runtime.queue.sync {
            for reason in [AVAudioSession.RouteChangeReason.categoryChange, .newDeviceAvailable, .oldDeviceUnavailable, .override, .routeConfigurationChange] {
                runtime.handleAudioNotification(Notification(name: AVAudioSession.routeChangeNotification, userInfo: [AVAudioSessionRouteChangeReasonKey: reason.rawValue]))
            }
        }
        XCTAssertEqual(try store.read(id).segments.count, 1); XCTAssertEqual(audio.stops, 0); XCTAssertTrue(audio.running)
        runtime.queue.sync { audio.running = false; runtime.tick() }
        XCTAssertEqual(audio.stops, 1); XCTAssertEqual(try store.read(id).status, .paused)
        runtime.start(); runtime.queue.sync {}
        XCTAssertEqual(try store.read(id).segments.count, 2)
        runtime.queue.sync { runtime.handleAudioNotification(Notification(name: .AVAudioEngineConfigurationChange)) }
        XCTAssertEqual(audio.stops, 2)
        runtime.stop(); runtime.queue.sync {}
    }
    func testCallEndRetriesWhileRouteReturnsWithoutAudioEndNotification() throws {
        let store = try Store(root: root), audio = FakeAudio()
        var clock: Int64 = 1000
        let runtime = Runtime(store: store, audio: audio, location: FakeLocation(), now: { clock })
        runtime.start(); runtime.queue.sync {}
        runtime.queue.sync {
            runtime.updateCalls(active: true)
            XCTAssertTrue(audio.running)
            let began = Notification(name: AVAudioSession.interruptionNotification, userInfo:
                [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.began.rawValue])
            runtime.handleAudioNotification(began)
            runtime.handleAudioNotification(began)
            runtime.handleAudioNotification(Notification(name: AVAudioSession.interruptionNotification,
                userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.ended.rawValue]))
            XCTAssertFalse(audio.running)
            XCTAssertEqual(audio.stops, 1)
            runtime.updateCalls(active: true) // A second call is still active.
            runtime.updateCalls(active: false)
            runtime.updateCalls(active: false)
            XCTAssertFalse(audio.running)
            audio.activationDenied = true; clock += 1000; runtime.tick()
            XCTAssertFalse(audio.running)
            audio.activationDenied = false; clock += 1000; runtime.tick()
            XCTAssertTrue(audio.running)
        }
        XCTAssertEqual(store.latest()?.segments.count, 2)
        runtime.stop(); runtime.queue.sync {}
        runtime.queue.sync { runtime.updateCalls(active: true); runtime.updateCalls(active: false) }
        XCTAssertFalse(audio.running)
    }
    func testRegionRebuildThresholdAndExistingCenter() {
        let region = CLCircularRegion(center: CLLocationCoordinate2D(latitude: 0, longitude: 0), radius: 150, identifier: "ear-latest")
        let origin = CLLocation(latitude: 0, longitude: 0)
        XCTAssertTrue(Location.needsRegion(at: origin, existing: nil))
        XCTAssertFalse(Location.needsRegion(at: origin, existing: region))
        for latitude in [0.0001, 0.0005, 0.00089] { XCTAssertFalse(Location.needsRegion(at: CLLocation(latitude: latitude, longitude: 0), existing: region)) }
        XCTAssertTrue(Location.needsRegion(at: CLLocation(latitude: 0.001, longitude: 0), existing: region))
        let moved = CLCircularRegion(center: CLLocationCoordinate2D(latitude: 0.001, longitude: 0), radius: 150, identifier: "ear-latest")
        XCTAssertFalse(Location.needsRegion(at: CLLocation(latitude: 0.0011, longitude: 0), existing: moved))
    }
    func testMotionConfidenceJitterProducesOnlyMeaningfulContextRows() throws {
        let location = Location(), file = root.appendingPathComponent("motion.jsonl.gz")
        let context = try Context(url: file, index: 1, elapsed: 0, baseline: [:])
        var emissions = 0
        location.onDelta = { values in
            emissions += 1
            do { try context.delta(values, elapsed: Int64(emissions)) } catch { XCTFail(error.localizedDescription) }
        }
        for confidence in ["low", "low", "medium", "low", "high", "medium", "low", "high"] { location.receiveMotion(type: "walking", confidence: confidence) }
        location.receiveMotion(type: "cycling", confidence: "high")
        for confidence in ["medium", "low", "high", "low", "high"] { location.receiveMotion(type: "cycling", confidence: confidence) }
        location.receiveMotion(type: "stationary", confidence: "low")
        location.receiveMotion(type: "stationary", confidence: "high")
        try context.finish(elapsed: 10)
        XCTAssertEqual(emissions, 5)
        let lines = try rows(file), deltas = lines.filter { $0["type"] as? String == "delta" }
        XCTAssertEqual(deltas.count, 5)
        XCTAssertTrue(deltas.allSatisfy { $0["context"] == nil })
        XCTAssertEqual((lines.last?["summary"] as? [String: Int])?["deltaCount"], 5)
        XCTAssertEqual(((deltas[1]["changed"] as? [String: Any])?["motionActivity"] as? [String: String])?["confidence"], "medium")
    }
    func testBluetoothSettingChangeSealsSegmentAndResumesSameSession() throws {
        let store = try Store(root: root), audio = FakeAudio()
        let runtime = Runtime(store: store, audio: audio, location: FakeLocation())
        runtime.start(); runtime.queue.sync {}
        let id = try XCTUnwrap(store.latest()?.groupId)
        runtime.bluetoothSettingChanged(); runtime.queue.sync {}
        let segments = try store.read(id).segments
        XCTAssertEqual(segments.map(\.index), [1, 2])
        XCTAssertNotNil(segments.first?.endMs)
        XCTAssertTrue(audio.running)
        runtime.stop(); runtime.queue.sync {}
    }
    func testStalledUploadDeadlineResetsOnProgress() {
        var sample = Upload.ProgressSample(bytes: 0, time: 0)
        XCTAssertFalse(sample.stalled(bytes: 0, expected: 2048, now: 600))
        XCTAssertFalse(sample.stalled(bytes: 1024, expected: 2048, now: 300))
        XCTAssertFalse(sample.stalled(bytes: 1024, expected: 2048, now: 599))
        XCTAssertFalse(sample.stalled(bytes: 1024, expected: -1, now: 600))
        XCTAssertFalse(sample.stalled(bytes: 1024, expected: 1024, now: 600))
        XCTAssertTrue(sample.stalled(bytes: 1024, expected: 2048, now: 600))
        XCTAssertFalse(sample.stalled(bytes: 1024, expected: 2048, now: 660))
    }
    func testFailedActivationDoesNotAllocateSegment() throws {
        let store = try Store(root: root), audio = FakeAudio()
        audio.activationDenied = true
        let runtime = Runtime(store: store, audio: audio, location: FakeLocation())
        runtime.start(); runtime.queue.sync {}
        XCTAssertEqual(store.latest()?.segments.count, 0)
        audio.activationDenied = false
        runtime.start(); runtime.queue.sync {}
        XCTAssertEqual(store.latest()?.segments.map(\.index), [1])
        runtime.stop(); runtime.queue.sync {}
    }
    func testInjectedCaptureStartsAndHonorsStop() throws {
        let store = try Store(root: root), audio = FakeAudio(), location = FakeLocation()
        let runtime = Runtime(store: store, audio: audio, location: location)
        runtime.start(); runtime.queue.sync {}
        XCTAssertTrue(audio.running); XCTAssertEqual(store.latest()?.userStopped, false)
        location.onDelta?(["latitude": 40.0, "longitude": 116.0, "locationTimestamp": 1000])
        runtime.queue.sync {}
        runtime.stop(); runtime.queue.sync {}
        XCTAssertFalse(audio.running); XCTAssertEqual(store.latest()?.userStopped, true)
        let manifest = try XCTUnwrap(store.latest())
        let lines = try rows(store.directory(manifest.groupId).appendingPathComponent("context_0001.jsonl.gz"))
        XCTAssertEqual((lines.first { $0["type"] as? String == "delta" }?["changed"] as? [String: Any])?["latitude"] as? Double, 40.0)
    }
}
private final class FakeAudio: AudioInput {
    var running = false
    var stops = 0
    var onFailure: ((Error) -> Void)?
    var activationDenied = false
    func activate(bluetooth: Bool) throws {
        if activationDenied { throw RecorderError("activation denied") }
    }
    func start(at url: URL) throws { running = true }
    func rotate(to url: URL) throws -> Int64 { 1000 }
    func stop() throws -> Int64 { stops += 1; running = false; return 1000 }
}
private final class FakeLocation: LocationInput {
    var onDelta: (([String: Any]) -> Void)?
    var onEvent: ((String, String, [String: Any]) -> Void)?
    func start(distance: Double) {}
    func stop() {}
}

extension RecorderTests {
    func testPairingInvitation() throws {
        let id = UUID()
        let hex = String(repeating: "a", count: 64)
        let prefix = "earrecorder://pair?receiver=\(id)&code=\(hex)&pin=\(hex)&relay="
        XCTAssertEqual(Invitation(try XCTUnwrap(URL(string: prefix)))?.id, id)
        for invalid in ["http://example.com", "https://user@example.com", "https://example.com/redirect", "&code=invalid"] {
            XCTAssertNil(Invitation(try XCTUnwrap(URL(string: prefix + invalid))))
        }
    }
}

extension RecorderTests {
    @MainActor func testLocalReceiverWhenInvitationProvided() async throws {
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("ear-test-invitation")
        guard FileManager.default.fileExists(atPath: file.path) else { throw XCTSkip("Requires a running Mac receiver") }
        defer { try? FileManager.default.removeItem(at: file) }
        let saved = Settings.load()
        defer { try? saved.save() }
        let link = try String(contentsOf: file, encoding: .utf8)
        let invite = try XCTUnwrap(Invitation(try XCTUnwrap(URL(string: link))))
        try await Receiver.shared.pair(invite)
        let settings = Settings.load()
        XCTAssertEqual(settings.receiver, invite.id.uuidString.lowercased())
        XCTAssertEqual(settings.credential.count, 64)
        guard !invite.relay.isEmpty else { XCTAssertFalse(settings.direct.isEmpty); return }
        // The optional relay fixture uses a disposable inbox and loopback origin.
        let base = try XCTUnwrap(URL(string: invite.relay))
        guard base.host == "127.0.0.1" || Relay.accepts(base) else { throw XCTSkip("Synthetic upload requires loopback") }
        let testSession = URLSession(configuration: .ephemeral, delegate: PinnedSession(pin: invite.pin), delegateQueue: nil)
        defer { testSession.finishTasksAndInvalidate() }
        let nonce = String(repeating: "1", count: 32)
        let url = base.appendingPathComponent("health").appending(queryItems: [
            URLQueryItem(name: "device", value: Settings.deviceId), URLQueryItem(name: "challenge", value: nonce)])
        let (body, response) = try await testSession.data(from: url)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertNotNil((try JSONSerialization.jsonObject(with: body) as? [String: Any])?["proof"])
        var fixture = settings
        fixture.server = invite.relay
        var segment = Segment(index: 1, startMs: 0); segment.endMs = 1000
        try Data("synthetic-pairing-upload".utf8).write(to: root.appendingPathComponent(segment.file))
        let payload = try Multipart.make(manifest: Manifest(), segment: segment, context: false, settings: fixture,
                                         deviceId: Settings.deviceId, directory: root, temporary: root)
        let (_, receipt) = try await testSession.upload(for: payload.request, fromFile: payload.body)
        XCTAssertEqual((receipt as? HTTPURLResponse)?.statusCode, 200)
        if Relay.accepts(base) {
            let ticket = try XCTUnwrap(payload.request.value(forHTTPHeaderField: "X-Ear-Request"))
            XCTAssertTrue(try Relay(secret: settings.credential, ticket: ticket).verified(receipt as? HTTPURLResponse))
            XCTAssertFalse(try Relay(secret: settings.credential).verified(receipt as? HTTPURLResponse))
            XCTAssertNil(payload.request.value(forHTTPHeaderField: "Authorization"))
            XCTAssertNil(try Data(contentsOf: payload.body).range(of: Data("synthetic-pairing-upload".utf8)))
        }
    }
}
