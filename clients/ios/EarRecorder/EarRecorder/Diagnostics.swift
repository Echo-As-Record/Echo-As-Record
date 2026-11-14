import Foundation
import Darwin
import os

final class Diagnostics {
    let root: URL
    private let lock = NSLock()
    init(root: URL) { self.root = root }
    func log(_ event: String, _ fields: [String: Any] = [:]) {
        lock.lock(); defer { lock.unlock() }
        do {
            let url = root.appendingPathComponent("diag.jsonl")
            if let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size]) as? NSNumber, size.intValue > 5 * 1024 * 1024 {
                let old = root.appendingPathComponent("diag.previous.jsonl")
                try? FileManager.default.removeItem(at: old); try FileManager.default.moveItem(at: url, to: old)
            }
            if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
            var row = fields; row["event"] = event; row["timestamp"] = iso(epochMS()); row["module"] = "recorder"
            let handle = try FileHandle(forWritingTo: url); defer { try? handle.close() }; try handle.seekToEnd()
            var data = try JSONSerialization.data(withJSONObject: row); data.append(10); try handle.write(contentsOf: data)
        } catch { NSLog("Ear diagnostics write failed: %@", error.localizedDescription) }
    }
    func memory() {
        var info = task_vm_info_data_t(); var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &info) { ptr in ptr.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count) } }
        var values: [String: Any] = ["available_bytes": os_proc_available_memory()]
        if result == KERN_SUCCESS { values["footprint_bytes"] = info.phys_footprint; values["resident_bytes"] = info.resident_size }
        log("memory", values)
    }
}
