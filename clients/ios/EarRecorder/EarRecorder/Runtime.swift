import Foundation
import AVFoundation
import CallKit
import UIKit
import UserNotifications
import Combine

struct Snapshot {
    var status = "已停止", index = 0, gps = 0, pending = 0
    var elapsed: Int64 = 0
    var error = ""
    var fileError = ""
    var route = ""
    var active = false
}
final class Runtime: NSObject, CXCallObserverDelegate {
    static let shared = Runtime()
    let queue = DispatchQueue(label: "ear.runtime")
    let store: Store?
    let diagnostics: Diagnostics
    let upload: Upload?
    private let audio: AudioInput
    private let location: LocationInput
    private var id: String?
    private var startedAt: Int64?
    private let now: () -> Int64
    private let presentationLock = NSLock()
    private var presentationActive = false
    private(set) var pendingRefreshCount = 0
    private var interruption: Interruption?
    private var context: Context?
    private var snapshot = Snapshot()
    private var latest: [String: Any] = [:]
    private var timer: DispatchSourceTimer?
    private var retryAt: Int64 = 0, rotateAt: Int64 = 0, memoryAt: Int64 = 0
    private var retries = 0
    private var notified = false, booted = false
    private let calls = CXCallObserver()
    private var observingCalls = false
    private var recoveryTask: UIBackgroundTaskIdentifier = .invalid
    private(set) var recoveryID: UUID?
    private var hasLiveCall = false
    private var systemInterrupted = false
    private var observers: [NSObjectProtocol] = []
    var onSnapshot: ((Snapshot) -> Void)?
    init(store injected: Store? = nil, audio: AudioInput = Audio(), location: LocationInput = Location(), now: @escaping () -> Int64 = epochMS) {
        self.audio = audio; self.location = location; self.now = now
        var failure: Error?
        do { store = try injected ?? Store() } catch { store = nil; failure = error }
        diagnostics = Diagnostics(root: store?.root ?? Store.root)
        if let store { upload = Upload(store: store, diagnostics: diagnostics) } else { upload = nil }
        super.init()
        var system = utsname(); uname(&system)
        let model = withUnsafePointer(to: &system.machine) { $0.withMemoryRebound(to: CChar.self, capacity: 256) { String(cString: $0) } }
        UIDevice.current.isBatteryMonitoringEnabled = true
        let battery: Any = UIDevice.current.batteryLevel < 0 ? NSNull() : NSNumber(value: Int(UIDevice.current.batteryLevel * 100))
        latest = ["platform": "ios", "device_model": model, "os_version": UIDevice.current.systemVersion,
                  "battery": battery, "isCharging": UIDevice.current.batteryState == .charging || UIDevice.current.batteryState == .full, "latitude": NSNull(), "longitude": NSNull(),
                  "locationAccuracy": NSNull(), "locationTimestamp": NSNull(), "locationMeta": NSNull()]
        if let failure { snapshot.error = failure.localizedDescription }
        audio.onFailure = { [weak self] error in self?.queue.async { self?.pause("audio_write: " + error.localizedDescription) } }
        location.onDelta = { [weak self] values in self?.queue.async { self?.receive(values) } }
        location.onEvent = { [weak self] name, reason, values in self?.queue.async {
            guard let self else { return }; self.diagnostics.log(name, ["reason": reason])
            do { try self.context?.event(name, reason: reason, changed: values, elapsed: self.elapsed()) } catch { self.report(error) }
            if reason == "region_exit" { self.retryAt = 0; self.attempt() }
        } }
        upload?.onChange = { [weak self] in self?.queue.async { self?.refreshPending(); self?.publish() } }
    }
    func boot(reason: String) {
        queue.async {
            guard !self.booted else { return }; self.booted = true
            self.calls.setDelegate(self, queue: self.queue)
            self.observingCalls = true
            self.hasLiveCall = self.calls.calls.contains { !$0.hasEnded }
            self.register()
            self.diagnostics.log("launch", ["reason": reason]); self.diagnostics.memory()
            do {
                guard let store = self.store else { throw RecorderError(self.snapshot.error) }
                let last = store.latest()
                try self.recover(); self.refreshPending()
                self.upload?.start()
                if Manifest.shouldContinue(last, isRecording: self.audio.running) { self.begin(continuedFrom: last?.groupId) }
            } catch { self.report(error) }
            let timer = DispatchSource.makeTimerSource(queue: self.queue)
            timer.schedule(deadline: .now(), repeating: 1); timer.setEventHandler { [weak self] in self?.tick() }; self.timer = timer; timer.resume()
            self.publish()
        }
    }
    func start() { queue.async { self.systemInterrupted = false; if self.id == nil { self.begin(continuedFrom: nil) } else { self.retryAt = 0; self.attempt() } } }
    func stop() { queue.async {
        guard let id = self.id, let store = self.store else { return }
        do {
            // Persist the user decision before touching any capture resource.
            try store.update(id) { $0.userStopped = true; $0.status = .finalizing; $0.interruption = nil }
            self.diagnostic("user_stop")
            self.interruption = nil
            self.systemInterrupted = false
            self.endRecovery()
            self.seal()
            try store.update(id) { m in
                m.status = m.segments.allSatisfy { $0.audio.state == .uploaded && $0.context.state == .uploaded } ? .completed : .upload_pending
            }
            self.id = nil; self.startedAt = nil; self.snapshot.active = false; self.snapshot.status = "已停止"; self.retryAt = 0
            DispatchQueue.main.async { self.location.stop() }
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
            self.upload?.kick(); self.publish()
        } catch { self.report(error) }
    } }
    func bluetoothSettingChanged() {
        queue.async {
            guard self.id != nil, self.audio.running else { return }
            self.diagnostic("bluetooth_setting_changed")
            self.seal()
            self.retryAt = 0
            self.attempt()
        }
    }
    private func diagnostic(_ event: String, _ fields: [String: Any] = [:]) {
        var details = fields
        details["group_id"] = id
        details["segment_index"] = snapshot.index
        details["time_ms"] = now()
        details["interruption_id"] = interruption?.id
        diagnostics.log(event, details)
    }
    private func observeRoute(_ event: String, fields: [String: Any] = [:]) {
        let route = AVAudioSession.sharedInstance().currentRoute
        let inputs = route.inputs.map { $0.portType.rawValue }
        let outputs = route.outputs.map { $0.portType.rawValue }
        snapshot.route = inputs.joined(separator: ", ") + " → " + outputs.joined(separator: ", ")
        var details = fields
        details["inputs"] = inputs
        details["outputs"] = outputs
        diagnostic(event, details)
    }
    func setActive(_ active: Bool) {
        presentationLock.lock(); presentationActive = active; presentationLock.unlock()
        queue.async { if active { self.systemInterrupted = false; self.refreshCalls(); self.retryAt = 0; self.attempt(); self.publish() } }
    }
    private var isActive: Bool {
        presentationLock.lock(); defer { presentationLock.unlock() }; return presentationActive
    }
    private func begin(continuedFrom: String?) {
        guard id == nil, let store else { return }
        do {
            var manifest = Manifest(); manifest.startedAt = now(); manifest.continuedFrom = continuedFrom
            if let continuedFrom {
                let previous = try store.read(continuedFrom)
                let end = previous.segments.compactMap(\.endMs).max().map { previous.startedAt + $0 }
                manifest.interruption = previous.interruption ?? Interruption(trigger: "unknown_before_relaunch", observedAt: now(), audioEndedAt: end)
            }
            try store.save(manifest)
            interruption = manifest.interruption
            id = manifest.groupId; startedAt = manifest.startedAt; snapshot = Snapshot(); snapshot.active = true; notified = false; retries = 0
            if let continuedFrom { diagnostic("auto_continuation", ["previous_group_id": continuedFrom, "group_id": manifest.groupId]) }
            DispatchQueue.main.async {
                self.location.start(distance: Settings.load().distanceFilter)
            }
            attempt()
        } catch { report(error) }
    }
    func elapsed() -> Int64 {
        guard let startedAt else { return 0 }; return max(0, now() - startedAt)
    }
    private func attempt() {
        guard let id, let store, !audio.running, !hasLiveCall, !systemInterrupted, self.now() >= retryAt else { return }
        var stage = "manifest"
        do {
            let manifest = try store.read(id); guard !manifest.userStopped else { return }
            let bluetooth = Settings.load().bluetooth
            stage = "audio_activate"
            try audio.activate(bluetooth: bluetooth)
            stage = "segment_open"
            let segment = Segment(index: (manifest.segments.map(\.index).max() ?? 0) + 1, startMs: elapsed())
            try store.update(id) { $0.segments.append(segment); $0.status = .paused }
            snapshot.index = segment.index; refreshPending()
            context = try Context(url: store.directory(id).appendingPathComponent(segment.contextFile), index: segment.index, elapsed: segment.startMs, baseline: latest)
            if manifest.continuedFrom != nil && segment.index == 1 { try context?.event("auto_continuation", reason: "process_relaunch", elapsed: segment.startMs) }
            stage = "engine_start"
            try audio.start(at: store.directory(id).appendingPathComponent(segment.file))
            stage = "manifest_commit"
            try store.update(id) { $0.status = .recording; $0.interruption = nil }
            rotateAt = self.now() + max(1000, Settings.load().segmentMs); retries = 0; notified = false
            if let interruption {
                var details: [String: Any] = ["trigger": interruption.trigger, "observed_at_ms": interruption.observedAt]
                details["audio_gap_ms"] = interruption.audioEndedAt.map { max(0, manifest.startedAt + segment.startMs - $0) }
                diagnostic("interruption_resolved", details)
            }
            endRecovery(); snapshot.status = "录音中"; observeRoute("recording_resumed", fields: ["index": segment.index, "bluetooth_input_enabled": bluetooth]); interruption = nil; publish()
        } catch {
            let failure = error as NSError
            observeRoute("recording_attempt_failed", fields: ["stage": stage, "domain": failure.domain, "code": failure.code, "foreground": isActive])
            if stage == "audio_activate" {
                // Activation can succeed before an input route exists. Close capture
                // resources while retaining the interrupted session for resumption.
                _ = try? audio.stop(preservingSession: interruption != nil)
            } else { seal() }
            report(error); snapshot.status = "暂停：" + error.localizedDescription
            scheduleRetry(); notifyOnce(); publish()
        }
    }
    private func scheduleRetry() {
        let delays: [Int64] = [1, 2, 5, 10, 30, 60]
        retryAt = self.now() + delays[min(retries, delays.count - 1)] * 1000; retries += 1
        diagnostic("retry_scheduled", ["attempt": retries, "retryAt": retryAt])
    }
    private func seal() {
        guard let id, let store else { return }
        do {
            let duration = try audio.stop(preservingSession: interruption != nil)
            let index = snapshot.index
            try store.update(id) { m in
                if let i = m.segments.firstIndex(where: { $0.index == index }), duration > 0 { m.segments[i].endMs = m.segments[i].startMs + duration }
            }
        } catch { report(error) }
        do { try context?.finish(elapsed: elapsed()) } catch { report(error) }; context = nil
        do { try recover(id: id) } catch { report(error) }
        refreshPending(); upload?.kick()
    }
    private func rotate() {
        guard let id, let store else { return }
        do {
            let oldIndex = snapshot.index, now = elapsed()
            let segment = Segment(index: oldIndex + 1, startMs: now)
            try store.update(id) { $0.segments.append(segment) }
            // Switch the sink while the engine tap remains installed.
            snapshot.index = segment.index
            let duration = try audio.rotate(to: store.directory(id).appendingPathComponent(segment.file))
            try store.update(id) { m in if let i = m.segments.firstIndex(where: { $0.index == oldIndex }) { m.segments[i].endMs = m.segments[i].startMs + duration } }
            try context?.finish(elapsed: now)
            context = try Context(url: store.directory(id).appendingPathComponent(segment.contextFile), index: segment.index, elapsed: now, baseline: latest)
            rotateAt = self.now() + max(1000, Settings.load().segmentMs); refreshPending(); upload?.kick()
        } catch {
            // If opening the next sink failed, stop/finalize the still-active previous sink.
            if let active = (audio as? Audio)?.activeFile,
               let manifest = try? store.read(id),
               let segment = manifest.segments.first(where: { $0.file == active.lastPathComponent }) { snapshot.index = segment.index }
            pause("rotation: " + error.localizedDescription)
        }
    }
    private func pause(_ reason: String, fields: [String: Any] = [:]) {
        guard let id, let store else { return }
        if interruption == nil {
            interruption = Interruption(trigger: reason, observedAt: now(), audioEndedAt: nil)
            do { try store.update(id) { $0.interruption = interruption } } catch { report(error) }
        }
        var details = fields
        details["reason"] = reason
        details["foreground"] = isActive
        details["active_call"] = hasLiveCall
        if observingCalls {
            details["calls"] = calls.calls.filter { !$0.hasEnded }.map {
                ["outgoing": $0.isOutgoing, "connected": $0.hasConnected]
            }
        }
        observeRoute("interruption", fields: details)
        beginRecovery(reason)
        seal()
        do { try store.update(id) { manifest in
            manifest.status = .paused
            if let current = interruption, let end = manifest.segments.compactMap(\.endMs).max() {
                interruption = Interruption(id: current.id, trigger: current.trigger, observedAt: current.observedAt, audioEndedAt: manifest.startedAt + end)
                manifest.interruption = interruption
            }
        } } catch { report(error) }
        snapshot.status = "暂停：" + reason; scheduleRetry(); notifyOnce(); publish()
    }
    func tick() { // Called on queue; internal for deterministic clock/capture tests.
        let now = self.now()
        if now >= memoryAt { diagnostics.memory(); memoryAt = now + 300_000 }
        if audio.running { if now >= rotateAt { rotate() } }
        else if id != nil {
            refreshCalls()
            if !audio.running, context != nil { pause("engine_stopped") }
            attempt()
        }
        if now / 15_000 != (now - 1000) / 15_000 {
            DispatchQueue.main.async {
                let device = UIDevice.current
                let values: [String: Any] = ["battery": device.batteryLevel < 0 ? NSNull() : NSNumber(value: Int(device.batteryLevel * 100)), "isCharging": device.batteryState == .charging || device.batteryState == .full]
                self.queue.async { self.receive(values) }
            }
        }
        publish()
    }
    private func receive(_ values: [String: Any]) {
        latest.merge(values) { _, n in n }
        if values["latitude"] != nil {
            snapshot.gps += 1
            if !audio.running { refreshCalls(); attempt() }
        }
        do { try context?.delta(values, elapsed: elapsed()) } catch { pause("context_write: " + error.localizedDescription) }
    }
    private func recover(id only: String? = nil) throws {
        guard let store else { return }
        for manifest in (try only.map { [try store.read($0)] } ?? store.unfinished()) {
            var recovered = manifest.segments
            var contextChanges = Set<Int>()
            for i in recovered.indices {
                let dir = store.directory(manifest.groupId)
                if recovered[i].endMs == nil && recovered[i].audio.state != .uploaded { Recovery.audio(directory: dir, segment: &recovered[i]) }
                let final = dir.appendingPathComponent(recovered[i].contextFile), part = final.appendingPathExtension("part")
                if !FileManager.default.fileExists(atPath: final.path) {
                    contextChanges.insert(recovered[i].index)
                    if GzipRecovery().recover(part) != nil {
                        try Context.closeRecovered(part, index: recovered[i].index, elapsed: recovered[i].endMs ?? recovered[i].startMs)
                        try FileManager.default.moveItem(at: part, to: final); recovered[i].context = Transfer()
                    } else { recovered[i].context.state = .manual_recovery_required; recovered[i].context.error = "Context 不可恢复，保留源文件" }
                }
            }
            try store.update(manifest.groupId) { m in
                // Preserve upload transitions made concurrently with decoding.
                for i in m.segments.indices {
                    if m.segments[i].endMs == nil { m.segments[i].endMs = recovered[i].endMs; m.segments[i].audio = recovered[i].audio; m.segments[i].recovery = recovered[i].recovery }
                    if contextChanges.contains(m.segments[i].index), m.segments[i].context.state != .uploaded && m.segments[i].context.state != .uploading { m.segments[i].context = recovered[i].context }
                }
                if only == nil, m.status != .completed { m.status = .upload_pending }
            }
        }
    }
    private func refreshCalls() {
        if observingCalls { updateCalls(active: calls.calls.contains { !$0.hasEnded }) }
    }
    func callObserver(_ callObserver: CXCallObserver, callChanged call: CXCall) {
        guard id != nil else { refreshCalls(); return }
        diagnostic("call_changed", ["outgoing": call.isOutgoing, "connected": call.hasConnected,
                                         "ended": call.hasEnded])
        refreshCalls()
    }
    private func beginRecovery(_ reason: String) {
        guard id != nil, !hasLiveCall, recoveryID == nil else { return }
        let token = UUID()
        recoveryID = token
        recoveryTask = UIApplication.shared.beginBackgroundTask(withName: "Resume EarRecorder audio") { [weak self] in
            self?.queue.async {
                self?.expireRecovery(token)
            }
        }
        if recoveryTask == .invalid { recoveryID = nil }
        retries = 0
        diagnostic("audio_recovery_started", ["reason": reason, "background_time_granted": recoveryTask != .invalid])
    }
    func expireRecovery(_ token: UUID) {
        guard recoveryID == token else { return }
        diagnostic("audio_recovery_expired")
        endRecovery()
    }
    private func endRecovery() {
        recoveryID = nil
        if recoveryTask != .invalid {
            UIApplication.shared.endBackgroundTask(recoveryTask)
            recoveryTask = .invalid
        }
    }
    func updateCalls(active: Bool) { // Called on the recorder queue.
        let ended = hasLiveCall && !active
        hasLiveCall = active
        if active { endRecovery() }
        if ended, id != nil, !audio.running {
            systemInterrupted = false
            beginRecovery("call_ended")
            // Let the phone release its I/O route before starting the engine.
            retryAt = now() + 1000
        }
    }
    private func register() {
        let center = NotificationCenter.default
        for name in [AVAudioSession.interruptionNotification, AVAudioSession.routeChangeNotification, AVAudioSession.mediaServicesWereResetNotification, .AVAudioEngineConfigurationChange] {
            observers.append(center.addObserver(forName: name, object: nil, queue: nil) { [weak self] note in
                self?.queue.async { self?.handleAudioNotification(note) }
            })
        }
    }
    func handleAudioNotification(_ note: Notification) { // Called on queue.
        let name = note.name
        if name == AVAudioSession.routeChangeNotification {
            observeRoute("audio_route_change", fields: ["reason": (note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt) ?? 0])
            if !audio.running, context == nil, id != nil {
                refreshCalls()
                if let previous = note.userInfo?[AVAudioSessionRouteChangePreviousRouteKey] as? AVAudioSessionRouteDescription,
                   previous.inputs.isEmpty, !AVAudioSession.sharedInstance().currentRoute.inputs.isEmpty {
                    retryAt = 0
                }
                attempt()
            }
            return
        }
        if name == AVAudioSession.interruptionNotification {
            let began = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue
            if began {
                systemInterrupted = true
                if observingCalls { hasLiveCall = calls.calls.contains { !$0.hasEnded } }
                if context != nil || audio.running {
                    var fields: [String: Any] = [:]
                    let reason = note.userInfo?[AVAudioSessionInterruptionReasonKey] as? NSNumber
                    fields["system_reason_raw"] = reason
                    switch reason?.intValue {
                    case 0: fields["system_reason"] = "another_audio_session"
                    case 1: fields["system_reason"] = "app_suspended"
                    case 2: fields["system_reason"] = "built_in_mic_muted"
                    case 4: fields["system_reason"] = "route_disconnected"
                    default: fields["system_reason"] = "unknown"
                    }
                    pause("interruption_began", fields: fields)
                }
            } else {
                systemInterrupted = false
                diagnostic("audio_interruption_ended", ["options": (note.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt) ?? 0])
                refreshCalls()
                if !audio.running, !hasLiveCall {
                    beginRecovery("interruption_ended")
                    retryAt = 0
                    attempt()
                }
            }
        } else {
            diagnostics.log("audio_notification", ["name": name.rawValue])
            if name == .AVAudioEngineConfigurationChange, let audio = audio as? Audio, !audio.owns(note.object as AnyObject?) { return }
            if audio.running || context != nil { pause(name.rawValue) }
        }
    }
    private func notifyOnce() {
        guard !notified else { return }; notified = true
        let content = UNMutableNotificationContent(); content.title = "录音已中断，点一下继续"; content.body = "Ear 会继续尝试恢复录音。"; content.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "ear-interruption", content: content, trigger: nil)) { error in
            if let error { self.diagnostics.log("notification_failed", ["error": error.localizedDescription]) }
        }
    }
    private func report(_ error: Error) { snapshot.error = error.localizedDescription; diagnostics.log("error", ["error": error.localizedDescription]) }
    private func refreshPending() {
        pendingRefreshCount += 1
        let manifests = store?.unfinished() ?? []
        snapshot.pending = manifests.flatMap(\.segments).reduce(0) { $0 + ($1.audio.state == .uploaded ? 0 : 1) + ($1.context.state == .uploaded ? 0 : 1) }
        snapshot.fileError = ""
        for manifest in manifests.sorted(by: { $0.startedAt > $1.startedAt }) {
            if let segment = manifest.segments.last(where: { $0.audio.error != nil || $0.context.error != nil }),
               let message = segment.audio.error ?? segment.context.error {
                let start = Date(timeIntervalSince1970: Double(manifest.startedAt) / 1000)
                snapshot.fileError = "会话 " + start.formatted(date: .abbreviated, time: .standard)
                    + " · 第 " + String(segment.index) + " 段：" + message
                break
            }
        }
    }
    private func publish() {
        guard isActive else { return }
        snapshot.elapsed = elapsed(); snapshot.active = id != nil
        let value = snapshot
        DispatchQueue.main.async { if self.isActive { self.onSnapshot?(value) } }
    }
}
