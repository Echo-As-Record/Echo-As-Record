import SwiftUI
import AVFoundation
import AppIntents
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        #if DEBUG
        if let fallback = ProcessInfo.processInfo.environment["EAR_FALLBACK_URL"], URL(string: fallback)?.scheme == "https" {
            var settings = Settings.load()
            settings.fallbackServer = fallback
            try? settings.save()
        }
        #endif
        Runtime.shared.boot(reason: launchOptions?[.location] != nil ? "location" : "normal")
        #if DEBUG
        if ProcessInfo.processInfo.environment["EAR_NETWORK_DIAG"] == "1" { Runtime.shared.upload?.diagnose() }
        #endif
        return true
    }
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String, completionHandler: @escaping () -> Void) {
        guard identifier == Upload.identifier else { completionHandler(); return }
        Runtime.shared.diagnostics.log("background_upload_wake")
        guard let upload = Runtime.shared.upload else { completionHandler(); return }
        upload.backgroundCompletion = completionHandler
    }
}
final class Model: ObservableObject {
    @Published var snapshot = Snapshot()
    @Published var settings = Settings.load() {
        didSet { if settings != oldValue { saveMessage = "" } }
    }
    @Published private(set) var saveMessage = ""
    private let permissions = Location()
    init() { Runtime.shared.onSnapshot = { [weak self] in self?.snapshot = $0 } }
    func requestPermissions() {
        AVAudioApplication.requestRecordPermission { _ in }
        permissions.permissions()
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }
    func setBluetooth(_ enabled: Bool) {
        var saved = Settings.load()
        settings.bluetooth = enabled
        guard saved.bluetooth != enabled else { return }
        saved.bluetooth = enabled
        do {
            try saved.save()
            Runtime.shared.bluetoothSettingChanged()
        } catch {
            settings.bluetooth = Settings.load().bluetooth
            saveMessage = "保存失败：" + error.localizedDescription
        }
    }
    func save() {
        settings.segmentMs = max(1000, settings.segmentMs); settings.distanceFilter = max(0, settings.distanceFilter)
        settings.retainDays = max(-1, settings.retainDays)
        do {
            try settings.save()
            saveMessage = "已保存"
            Runtime.shared.upload?.kick()
        } catch {
            saveMessage = "保存失败：" + error.localizedDescription
        }
    }
}
@main struct EarRecorderApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = Model()
    @State private var invitation: Invitation?
    @State private var pairingMessage = ""
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                Form {
                    Section("录音") {
                        Text(model.snapshot.status)
                        if model.snapshot.active && !model.snapshot.route.isEmpty {
                            LabeledContent("输入 → 输出", value: model.snapshot.route)
                        }
                        Text("段 \(model.snapshot.index) · \(model.snapshot.elapsed / 1000) 秒")
                        Text("本次 GPS 点：\(model.snapshot.gps) · 待上传文件：\(model.snapshot.pending)")
                        Button(model.snapshot.active ? "停止录音" : "开始录音") { if model.snapshot.active { Runtime.shared.stop() } else { Runtime.shared.start() } }
                        if !model.snapshot.error.isEmpty { Text(model.snapshot.error).foregroundStyle(.red).textSelection(.enabled) }
                    }
                    if !model.snapshot.fileError.isEmpty {
                        Section("录音文件问题") { Text(model.snapshot.fileError).textSelection(.enabled) }
                    }
                    Section("权限") {
                        Button("申请麦克风、定位 Always、运动、通知权限") { model.requestPermissions() }
                        Button("打开系统设置") { if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) } }
                    }
                    Section {
                        Toggle("蓝牙麦克风输入", isOn: Binding(get: { model.settings.bluetooth }, set: model.setBluetooth))
                    } footer: {
                        Text("切换后自动保存。录音中会结束当前段并继续录音，无需重启 App。")
                    }
                    Section("设置") {
                        if !pairingMessage.isEmpty { Text(pairingMessage) }
                        if let id = model.settings.receiver {
                            LabeledContent("已配对的 Mac", value: id)
                        } else {
                        TextField("服务地址，例如 http://my-mac.local:8791", text: $model.settings.server).textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                        TextField("备用 HTTPS 地址", text: Binding(get: { model.settings.fallbackServer ?? "" }, set: { model.settings.fallbackServer = $0 }))
                            .textInputAutocapitalization(.never).autocorrectionDisabled().keyboardType(.URL)
                        SecureField("Bearer token", text: $model.settings.token)
                        }
                        TextField("用户 ID（可空）", text: $model.settings.userId).textInputAutocapitalization(.never).autocorrectionDisabled()
                        LabeledContent("分段毫秒") { TextField("1800000", value: $model.settings.segmentMs, format: .number).keyboardType(.numberPad).multilineTextAlignment(.trailing) }
                        LabeledContent("GPS 距离（米）") { TextField("25", value: $model.settings.distanceFilter, format: .number).keyboardType(.decimalPad).multilineTextAlignment(.trailing) }
                        LabeledContent("成功上传后保留天数") { TextField("3", value: $model.settings.retainDays, format: .number).keyboardType(.numbersAndPunctuation).multilineTextAlignment(.trailing) }
                        Text("-1 永久保留，0 上传后删除。分段时长下次切段后生效，GPS 距离下次开始录音时生效。").font(.caption)
                        Button("测试已保存的直连和备用地址") { Runtime.shared.upload?.diagnose() }
                        Button(action: model.save) {
                            HStack {
                                Text("保存设置")
                                Spacer()
                                if !model.saveMessage.isEmpty {
                                    Text(model.saveMessage).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                }.navigationTitle("Ear Recorder")
            }.onOpenURL { invitation = Invitation($0) }
            .alert(item: $invitation) { invite in
                Alert(title: Text("配对接收端"), message: Text("今后的录音将发送到 Mac " + invite.id.uuidString),
                      primaryButton: .default(Text("配对")) {
                        pairingMessage = "正在连接接收端…"
                        Task { @MainActor in
                            do { try await Receiver.shared.pair(invite); model.settings = Settings.load(); pairingMessage = "已配对" }
                            catch { pairingMessage = error.localizedDescription }
                        }
                      }, secondaryButton: .cancel())
            }.onAppear { Runtime.shared.setActive(phase == .active) }.onChange(of: phase) { _, phase in Runtime.shared.setActive(phase == .active) }
        }
    }
}
struct StartRecordingIntent: AppIntent {
    static var title: LocalizedStringResource = "开始 Ear 录音"
    static var description = IntentDescription("开始或恢复手机录音")
    static var openAppWhenRun = true
    @MainActor func perform() async throws -> some IntentResult { Runtime.shared.boot(reason: "start_intent"); Runtime.shared.start(); return .result() }
}
struct StopRecordingIntent: AppIntent {
    static var title: LocalizedStringResource = "停止 Ear 录音"
    static var openAppWhenRun = true
    @MainActor func perform() async throws -> some IntentResult { Runtime.shared.stop(); return .result() }
}
struct EarShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: StartRecordingIntent(), phrases: ["开始 \(.applicationName) 录音"], shortTitle: "开始录音", systemImageName: "mic")
        AppShortcut(intent: StopRecordingIntent(), phrases: ["停止 \(.applicationName) 录音"], shortTitle: "停止录音", systemImageName: "stop.circle")
    }
}
