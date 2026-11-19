import Foundation
import Security
import CryptoKit

struct Invitation: Identifiable {
    let id: UUID
    let code: String
    let pin: String
    let relay: String

    init?(_ url: URL) {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme == "earrecorder", parts.host == "pair" else { return nil }
        func field(_ name: String) -> String? {
            let matches = (parts.queryItems ?? []).filter { $0.name == name }
            return matches.count == 1 ? matches.first?.value : nil
        }
        guard let id = UUID(uuidString: field("receiver") ?? ""),
              let code = field("code"), let pin = field("pin"),
              [code, pin].allSatisfy({ $0.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil }),
              let relay = field("relay") else { return nil }
        if !relay.isEmpty {
            guard let base = URLComponents(string: relay), (base.scheme == "https" || Relay.accepts(base.url)),
                  base.host?.isEmpty == false, base.user == nil, base.password == nil,
                  ["", "/"].contains(base.path), base.query == nil, base.fragment == nil else { return nil }
        }
        self.id = id; self.pin = pin; self.code = code; self.relay = relay
    }
}

final class Receiver: NSObject, NetServiceDelegate {
    static let shared = Receiver()
    private var service: NetService?
    private var resolved: ((String?) -> Void)?

    // DNS-SD resolves the named instance directly, including after DHCP changes.
    func resolve(_ id: String, completion: @escaping (String?) -> Void) {
        service?.stop()
        resolved?(nil)
        let service = NetService(domain: "local.", type: "_ear-ingest._tcp.", name: id)
        self.service = service; resolved = completion
        service.delegate = self; service.resolve(withTimeout: 4)
    }
    func netServiceDidResolveAddress(_ sender: NetService) {
        guard service === sender else { return }
        var url = URLComponents()
        url.scheme = "https"; url.host = sender.hostName?.trimmingCharacters(in: CharacterSet(charactersIn: ".")); url.port = sender.port
        finish(url.host == nil ? nil : url.url?.absoluteString)
    }
    func netService(_ sender: NetService, didNotResolve errorDict: [String: NSNumber]) {
        if service === sender { finish(nil) }
    }
    private func finish(_ url: String?) {
        let callback = resolved
        resolved = nil; service?.stop(); service = nil; callback?(url)
    }

    static func direct(_ id: String) -> String { UserDefaults.standard.string(forKey: "receiver." + id) ?? "" }
    static func cache(_ url: String, id: String) { UserDefaults.standard.set(url, forKey: "receiver." + id) }

    struct Claim: Decodable { let receiver: String; let token: String }
    @MainActor func pair(_ invite: Invitation) async throws {
        let id = invite.id.uuidString.lowercased()
        let local: String? = await withCheckedContinuation { continuation in
            resolve(id) { continuation.resume(returning: $0) }
        }
        let session = URLSession(configuration: .ephemeral, delegate: PinnedSession(pin: invite.pin), delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        for endpoint in [local, invite.relay].compactMap({ $0 }).filter({ !$0.isEmpty }) {
            guard let base = URL(string: endpoint) else { continue }
            var request = URLRequest(url: base.appendingPathComponent("api/v1/pair"))
            request.timeoutInterval = 6; request.httpMethod = "POST"
            request.setValue(invite.code, forHTTPHeaderField: "X-Ear-Invitation")
            request.setValue(Settings.deviceId, forHTTPHeaderField: "X-Device-Id")
            let relay = Relay.accepts(base) ? try Relay(secret: invite.code) : nil
            let transport = try relay?.prepare(request, invitation: true) ?? request
            guard let (wire, response) = try? await session.data(for: transport),
                  let data = try? relay?.open(wire, response) ?? wire,
                  (response as? HTTPURLResponse)?.statusCode == 200,
                  let claim = try? JSONDecoder().decode(Claim.self, from: data),
                  claim.receiver == id, claim.token.count == 64 else { continue }
            let key = UUID().uuidString
            let status = SecItemAdd([kSecClass: kSecClassGenericPassword, kSecAttrService: "EarRecorder.pair",
                                    kSecAttrAccount: key, kSecValueData: Data(claim.token.utf8),
                                    kSecAttrAccessible: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly] as CFDictionary, nil)
            guard status == errSecSuccess else { throw RecorderError("配对凭据保存失败") }
            var settings = Settings.load()
            let previous = settings.credentialKey
            settings.receiver = id; settings.pin = invite.pin; settings.credentialKey = key
            settings.server = ""; settings.token = ""; settings.fallbackServer = invite.relay
            if let local { Self.cache(local, id: id) }
            try settings.save()
            if let previous { SecItemDelete(Self.query(previous) as CFDictionary) }
            Runtime.shared.upload?.kick()
            return
        }
        throw RecorderError("配对失败，请确认接收端在线，或重新生成五分钟有效的二维码。")
    }

    private static func query(_ key: String) -> [CFString: Any] {
        [kSecClass: kSecClassGenericPassword, kSecAttrService: "EarRecorder.pair", kSecAttrAccount: key]
    }
    static func credential(_ key: String) -> String {
        var q = query(key); q[kSecReturnData] = true
        var result: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return "" }
        return String(decoding: data, as: UTF8.self)
    }
}

final class PinnedSession: NSObject, URLSessionTaskDelegate {
    let pin: String
    init(pin: String) { self.pin = pin }
    static func authenticate(_ challenge: URLAuthenticationChallenge, pin: String?,
                             completion: (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard let pin else { completion(.performDefaultHandling, nil); return }
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate], let leaf = chain.first,
              SHA256.hash(data: SecCertificateCopyData(leaf) as Data).map({ String(format: "%02x", $0) }).joined() == pin
        else { completion(.cancelAuthenticationChallenge, nil); return }
        completion(.useCredential, URLCredential(trust: trust))
    }
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        Self.authenticate(challenge, pin: pin, completion: completionHandler)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) { completionHandler(nil) }
}
