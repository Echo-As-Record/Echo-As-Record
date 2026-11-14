import Foundation
import zlib
import CoreLocation
import CoreMotion
import UIKit

final class Context {
    private var gzip: gzFile?
    private let final: URL
    private let part: URL
    private let index: Int
    private var snapshot: [String: Any] = [:]
    private var deltas = 0, events = 0, arrivals = 0
    init(url: URL, index: Int, elapsed: Int64, baseline: [String: Any]) throws {
        final = url; part = url.appendingPathExtension("part"); self.index = index
        guard !FileManager.default.fileExists(atPath: part.path), let file = gzopen(part.path, "wb") else { throw RecorderError("无法创建 Context") }
        gzip = file; snapshot = baseline
        try line(type: "baseline", elapsed: elapsed, payload: ["context": baseline])
    }
    deinit { if let gzip { gzclose(gzip) } }
    private func line(type: String, elapsed: Int64, payload: [String: Any]) throws {
        var value = payload; value["type"] = type; value["segmentIndex"] = index - 1
        value["elapsedMs"] = max(0, elapsed); value["timestamp"] = iso(epochMS())
        var data = try JSONSerialization.data(withJSONObject: value, options: .sortedKeys); data.append(10)
        guard let gzip else { throw RecorderError("Context 已关闭") }
        let written = data.withUnsafeBytes { gzwrite(gzip, $0.baseAddress, UInt32($0.count)) }
        guard written == data.count, gzflush(gzip, Z_SYNC_FLUSH) == Z_OK else { throw RecorderError("Context 写入失败") }
    }
    func delta(_ values: [String: Any], elapsed: Int64) throws {
        var changed: [String: Any] = [:]
        for (key, value) in values {
            if !NSDictionary(dictionary: ["value": snapshot[key] ?? NSNull()]).isEqual(to: ["value": value]) { changed[key] = value }
            snapshot[key] = value
        }
        guard !changed.isEmpty else { return }
        var payload: [String: Any] = ["changed": changed]
        // Legacy Ear reads location observations from context, including unchanged coordinates.
        let locationKeys = ["latitude", "longitude", "locationTimestamp", "locationAccuracy"]
        if locationKeys.contains(where: { changed[$0] != nil }),
           snapshot["latitude"] is NSNumber, snapshot["longitude"] is NSNumber {
            payload["context"] = snapshot.filter { locationKeys.contains($0.key) }
        }
        try line(type: "delta", elapsed: elapsed, payload: payload)
        deltas += 1
    }
    func event(_ name: String, reason: String, changed: [String: Any] = [:], elapsed: Int64) throws {
        try line(type: "event", elapsed: elapsed, payload: ["event": name, "changed": changed, "meta": ["reason": reason]])
        events += 1; if reason == "visit_arrival" { arrivals += 1 }
    }
    func finish(elapsed: Int64) throws {
        try line(type: "segment_end", elapsed: elapsed, payload: ["summary": ["deltaCount": deltas, "eventCount": events, "arrivalCount": arrivals]])
        let result = gzclose(gzip); gzip = nil
        guard result == Z_OK else { throw RecorderError("Context gzip 封口失败") }
        try FileManager.default.moveItem(at: part, to: final)
    }
    /// Rewrite a repaired stream so a killed process still yields one gzip member with a summary.
    /// Normalize the wire index; otherwise preserve captured rows and distinguish recovery from capture.
    static func closeRecovered(_ url: URL, index: Int, elapsed: Int64) throws {
        guard let input = gzopen(url.path, "rb") else { throw RecorderError("无法读取恢复后的 Context") }
        defer { gzclose(input) }
        let temporary = url.appendingPathExtension("closing")
        guard let output = gzopen(temporary.path, "wb") else { throw RecorderError("无法封口恢复后的 Context") }
        var outputOpen = true
        defer { if outputOpen { gzclose(output) }; try? FileManager.default.removeItem(at: temporary) }
        var buffer = [CChar](repeating: 0, count: 65536)
        var deltas = 0, events = 0, arrivals = 0, ended = false
        var lastElapsed = elapsed
        func write(_ data: Data) throws {
            let count = data.withUnsafeBytes { gzwrite(output, $0.baseAddress, UInt32($0.count)) }
            guard count == data.count, gzflush(output, Z_SYNC_FLUSH) == Z_OK else { throw RecorderError("Context 恢复写入失败") }
        }
        while gzgets(input, &buffer, Int32(buffer.count)) != nil {
            let text = String(cString: buffer)
            guard text.hasSuffix("\n"), let data = text.data(using: .utf8), var row = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw RecorderError("Context 恢复行无效或过长") }
            row["segmentIndex"] = index - 1
            var normalized = try JSONSerialization.data(withJSONObject: row); normalized.append(10)
            try write(normalized)
            let type = row["type"] as? String
            if type == "delta" { deltas += 1 }
            if type == "event" { events += 1; if (row["meta"] as? [String: Any])?["reason"] as? String == "visit_arrival" { arrivals += 1 } }
            if type == "segment_end" { ended = true }
            lastElapsed = max(lastElapsed, (row["elapsedMs"] as? NSNumber)?.int64Value ?? 0)
        }
        if !ended {
            let common: [String: Any] = ["segmentIndex": index - 1, "elapsedMs": lastElapsed, "timestamp": iso(epochMS())]
            for payload: [String: Any] in [
                ["type": "event", "event": "recovery", "changed": [:], "meta": ["reason": "process_interrupted"]],
                ["type": "segment_end", "summary": ["deltaCount": deltas, "eventCount": events + 1, "arrivalCount": arrivals]]
            ] {
                var data = try JSONSerialization.data(withJSONObject: common.merging(payload) { _, n in n }); data.append(10); try write(data)
            }
        }
        let result = gzclose(output); outputOpen = false
        guard result == Z_OK else { throw RecorderError("Context 恢复封口失败") }
        _ = try FileManager.default.replaceItemAt(url, withItemAt: temporary)
    }
}

protocol LocationInput: AnyObject {
    var onDelta: (([String: Any]) -> Void)? { get set }
    var onEvent: ((String, String, [String: Any]) -> Void)? { get set }
    func start(distance: Double)
    func stop()
}
final class Location: NSObject, LocationInput, CLLocationManagerDelegate {
    var onDelta: (([String: Any]) -> Void)?
    var onEvent: ((String, String, [String: Any]) -> Void)?
    let manager = CLLocationManager()
    private let motion = CMMotionActivityManager()
    private var recording = false
    private var motionState = MotionState()
    private var latestRegion: CLCircularRegion?
    override init() { super.init(); manager.delegate = self }
    func permissions() {
        if manager.authorizationStatus == .notDetermined { manager.requestWhenInUseAuthorization() }
        else { manager.requestAlwaysAuthorization() }
        if CMMotionActivityManager.isActivityAvailable() {
            motion.queryActivityStarting(from: Date().addingTimeInterval(-60), to: Date(), to: .main) { _, _ in }
        }
    }
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        if manager.authorizationStatus == .authorizedWhenInUse { manager.requestAlwaysAuthorization() }
        if recording { configure() }
    }
    func start(distance: Double) {
        motionState = MotionState(); recording = true; manager.distanceFilter = max(0, distance); configure()
        if CMMotionActivityManager.isActivityAvailable() {
            motion.startActivityUpdates(to: .main) { [weak self] value in
                guard let value else { return }
                let name = value.walking ? "walking" : value.running ? "running" : value.cycling ? "cycling" : value.automotive ? "automotive" : value.stationary ? "stationary" : "unknown"
                self?.receiveMotion(type: name, confidence: value.confidence == .high ? "high" : value.confidence == .medium ? "medium" : "low")
            }
        }
    }
    func receiveMotion(type: String, confidence: String) {
        if let value = motionState.accept(type: type, confidence: confidence) { onDelta?(["motionActivity": value]) }
    }
    static func needsRegion(at point: CLLocation, existing: CLCircularRegion?) -> Bool {
        guard let existing else { return true }
        return point.distance(from: CLLocation(latitude: existing.center.latitude, longitude: existing.center.longitude)) > 100
    }
    private func configure() {
        guard manager.authorizationStatus == .authorizedAlways || manager.authorizationStatus == .authorizedWhenInUse else { return }
        manager.desiredAccuracy = kCLLocationAccuracyNearestTenMeters
        manager.pausesLocationUpdatesAutomatically = true; manager.activityType = .other
        manager.allowsBackgroundLocationUpdates = true; manager.showsBackgroundLocationIndicator = false
        manager.startUpdatingLocation()
        if CLLocationManager.significantLocationChangeMonitoringAvailable() { manager.startMonitoringSignificantLocationChanges() }
        manager.startMonitoringVisits()
    }
    func stop() {
        recording = false; manager.stopUpdatingLocation(); manager.stopMonitoringSignificantLocationChanges(); manager.stopMonitoringVisits()
        for region in manager.monitoredRegions { manager.stopMonitoring(for: region) }
        latestRegion = nil
        motion.stopActivityUpdates()
    }
    static func fields(_ point: CLLocation, trigger: String = "continuous") -> [String: Any] {
        ["latitude": point.coordinate.latitude, "longitude": point.coordinate.longitude, "locationAccuracy": point.horizontalAccuracy,
         "locationTimestamp": Int64(point.timestamp.timeIntervalSince1970 * 1000),
         "locationMeta": ["trigger": trigger, "source": "core_location_update", "provider": "core_location", "isFromLastKnown": false,
                          "ageMs": max(0, Int64(-point.timestamp.timeIntervalSinceNow * 1000)), "isMock": point.sourceInformation?.isSimulatedBySoftware ?? false]]
    }
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        for point in locations where point.horizontalAccuracy >= 0 {
            onDelta?(Self.fields(point))
            if recording, manager.authorizationStatus == .authorizedAlways, CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self) {
                let existing = latestRegion ?? (manager.monitoredRegions.first { $0.identifier == "ear-latest" } as? CLCircularRegion)
                guard Self.needsRegion(at: point, existing: existing) else { continue }
                if let existing { manager.stopMonitoring(for: existing) }
                let region = CLCircularRegion(center: point.coordinate, radius: 150, identifier: "ear-latest")
                region.notifyOnEntry = false; region.notifyOnExit = true; manager.startMonitoring(for: region)
                latestRegion = region // Also gates batched points before Core Location updates monitoredRegions.
            }
        }
    }
    func locationManager(_ manager: CLLocationManager, didVisit visit: CLVisit) {
        let departure = visit.departureDate != .distantFuture
        let date = departure ? visit.departureDate : visit.arrivalDate
        let fields: [String: Any] = ["latitude": visit.coordinate.latitude, "longitude": visit.coordinate.longitude, "locationAccuracy": visit.horizontalAccuracy, "locationTimestamp": Int64(date.timeIntervalSince1970 * 1000)]
        onEvent?(departure ? "location_departed" : "location_arrived", departure ? "visit_departure" : "visit_arrival", fields)
    }
    func locationManager(_ manager: CLLocationManager, didExitRegion region: CLRegion) { onEvent?("lifecycle", "region_exit", [:]); if recording { manager.startUpdatingLocation() } }
    func locationManagerDidResumeLocationUpdates(_ manager: CLLocationManager) { onEvent?("lifecycle", "location_resumed", [:]) }
    func locationManager(_ manager: CLLocationManager, monitoringDidFailFor region: CLRegion?, withError error: Error) {
        latestRegion = nil; onEvent?("location_error", error.localizedDescription, [:])
    }
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) { onEvent?("location_error", error.localizedDescription, [:]) }
}

/// Compare against the last emitted state, not the last callback: high/low jitter cannot re-upgrade repeatedly.
struct MotionState {
    private var type: String?
    private var confidence: String?
    mutating func accept(type: String, confidence: String) -> [String: String]? {
        guard self.type != type || (self.confidence == "low" && (confidence == "medium" || confidence == "high")) else { return nil }
        self.type = type; self.confidence = confidence
        return ["type": type, "confidence": confidence]
    }
}
