import Foundation
import CryptoKit

struct Relay {
    let ticket: String
    let secret: String

    static func accepts(_ url: URL?) -> Bool {
        url?.scheme == "http" && url?.host != nil && url?.user == nil && url?.password == nil
    }

    func key(_ direction: String) -> SymmetricKey {
        let label = Data(("ear-relay-v1/" + direction + "/" + ticket).utf8)
        let bytes = HMAC<SHA256>.authenticationCode(for: label, using: SymmetricKey(data: Data(secret.utf8)))
        return SymmetricKey(data: bytes)
    }

    init(secret: String, ticket: String = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()) throws {
        guard secret.count == 64 else { throw RecorderError("请重新配对接收端") }
        self.secret = secret; self.ticket = ticket
    }

    func prepare(_ inner: URLRequest, file: URL? = nil, output: FileHandle? = nil, invitation: Bool = false) throws -> URLRequest {
        guard let url = inner.url, Self.accepts(url), let device = inner.value(forHTTPHeaderField: "X-Device-Id"),
              var base = URLComponents(url: url, resolvingAgainstBaseURL: false) else { throw RecorderError("中继地址无效") }
        let path = base.percentEncodedPath + (base.percentEncodedQuery.map { "?" + $0 } ?? "")
        var metadata = ["method": inner.httpMethod ?? "GET", "path": path, "device": device]
        for name in ["Content-Type", "X-User-Id", "X-Ear-Final"] { metadata[name] = inner.value(forHTTPHeaderField: name) }
        base.path = "/api/v1/relay"; base.query = nil
        guard let target = base.url else { throw RecorderError("中继地址无效") }
        var outer = URLRequest(url: target, timeoutInterval: inner.timeoutInterval)
        outer.httpMethod = "POST"
        outer.setValue(device, forHTTPHeaderField: "X-Device-Id")
        outer.setValue(ticket, forHTTPHeaderField: "X-Ear-Request")
        outer.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        if invitation {
            outer.setValue(SHA256.hash(data: Data(secret.utf8)).map { String(format: "%02x", $0) }.joined(), forHTTPHeaderField: "X-Ear-Invite-Id")
        }
        let encryption = key("request")
        var counter: UInt64 = 0
        var body = Data()
        func frame(_ data: Data) throws {
            let suffix = withUnsafeBytes(of: counter.bigEndian) { Data($0) }
            let nonce = try AES.GCM.Nonce(data: Data(repeating: 0, count: 4) + suffix)
            let box = try AES.GCM.seal(data, using: encryption, nonce: nonce)
            let size = withUnsafeBytes(of: UInt32(data.count).bigEndian) { Data($0) }
            let encoded = size + box.ciphertext + box.tag
            if let output { try output.write(contentsOf: encoded) } else { body.append(encoded) }
            counter += 1
        }
        try frame(try JSONSerialization.data(withJSONObject: metadata) + Data([10]))
        if let file {
            let input = try FileHandle(forReadingFrom: file); defer { try? input.close() }
            while let part = try input.read(upToCount: 65536), !part.isEmpty { try frame(part) }
        } else if let payload = inner.httpBody, !payload.isEmpty {
            for start in stride(from: 0, to: payload.count, by: 65536) {
                try frame(payload.subdata(in: start..<min(payload.count, start + 65536)))
            }
        }
        try frame(Data())
        if output == nil { outer.httpBody = body }
        return outer
    }

    func verified(_ response: HTTPURLResponse?) -> Bool {
        guard let response, let receipt = response.value(forHTTPHeaderField: "X-Ear-Receipt") else { return false }
        let expected = HMAC<SHA256>.authenticationCode(for: Data(String(response.statusCode).utf8), using: key("receipt"))
        return receipt == expected.map { String(format: "%02x", $0) }.joined()
    }

    func open(_ bytes: Data, _ response: URLResponse) throws -> Data {
        guard let http = response as? HTTPURLResponse, verified(http) else { throw RecorderError("中继回执无效") }
        return try AES.GCM.open(AES.GCM.SealedBox(combined: bytes), using: key("response"), authenticating: Data(String(http.statusCode).utf8))
    }
}
