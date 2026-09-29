// 桌面壳：一个窗口，里面是这套东西自己的界面。
//
// ## 它做什么
//
// 挑一个空端口 → 起后端子进程 → 等它就绪 → 把 webview 指过去 → 关窗时收摊。
// 一共就这四件事。
//
// ## 它不做什么
//
// 不管路由、不管状态、不管权限。壳里一旦开始有产品逻辑，同一件事就有了两个
// 答案（一个在网页里、一个在壳里），而分叉的时候两边都不报错。
//
// ## 为什么是 WKWebView 而不是让人开浏览器
//
// 界面本来就是网页（Next.js 静态导出），这一点不变。变的是用户看到什么：
// 一个 Dock 图标、一个窗口，而不是"某个 localhost 端口的标签页"。浏览器那条
// 路上，关掉标签页 = 应用还在后台跑、而用户以为退了；换台机器换个默认浏览器
// 就是另一套行为。壳把这些收回来。
//
// ## 为什么不是 Tauri（RFC §4 定的是 Tauri）
//
// Tauri 要 Rust 工具链，而这台机器上没有、且它不该是"装一个 Mac 应用"的前提。
// WKWebView + Swift 只用系统自带的东西。代价是 Windows / Linux 各要一个壳
// —— 那两个平台本来就还没开工（WP-20 要一台 Win11 机器），到时候再决定是各写
// 一个薄壳还是统一上 Tauri。这个文件 300 行，换掉不心疼。

import AppKit
import CryptoKit
import WebKit

// MARK: - 后端子进程

/// 起后端、等它活过来、收摊。
///
/// 三件事都要在**这一个地方**：它们共享同一个事实（端口、进程、日志文件），
/// 拆开就得把这些事实抄来抄去。
final class Backend {
    private let process = Process()
    private let port: UInt16
    private let logURL: URL
    private var logHandle: FileHandle?
    /// 我们自己叫它停的。停之后的退出不是"后端死了"，别当事故报。
    private var stopping = false
    /// 后端**自己**退出时（不是我们叫停的）会被调一次：`(退出码)`。
    /// 退出码 3 的意思只有一个 —— 「请重新拉起我」（自更新装好之后用）。
    var onExit: ((Int32) -> Void)?

    /// 后端的地址。窗口标题栏不显示它 —— 用户不需要知道自己在跟一个本机
    /// HTTP 服务说话。
    var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    init(python: URL, logDirectory: URL) throws {
        port = try Backend.freePort()
        try FileManager.default.createDirectory(
            at: logDirectory, withIntermediateDirectories: true)
        logURL = logDirectory.appendingPathComponent("backend.log")
        // 追加，不截断。自更新会让后端退出再拉起 —— 截断的话，刚退出的那个后端
        // 的日志（正是更新出问题时要看的那份）在新后端起来的一瞬间就没了。
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        logHandle = try FileHandle(forWritingTo: logURL)
        logHandle?.seekToEndOfFile()

        process.executableURL = python
        // `-m app.launcher`：跟命令行 `research-platform` 走的是同一个入口。
        // 壳里另起一条启动路径的话，两条路会各自演化，而"我在终端里能跑、
        // 双击就不行"这类报告没有任何东西能解释。
        // `--exit-with-parent`：壳没了后端也收摊。
        //
        // 下面 `stop()` 那条路只在 AppKit 正常退出时走得到；用户按「强制退出」、
        // 壳崩了、或者别人 `kill` 了它，`applicationWillTerminate` 一律不跑。
        // 2026-09-06 真机实测，那种时候后端子进程会活下来 —— 一次清出 9 个孤儿。
        //
        // `-B`：`.app` 是签好名的产物，包里那个 python 往 `Resources/` 写一个 .pyc
        // 签名就不再成立，而这件事一声不响 —— 要等下一次 Gatekeeper 校验才说"应用已
        // 损坏"。环境变量下面也设了一份，两道都要：子进程重建环境时变量会掉，命令行
        // 标志掉不了。不加 `-I`：isolated 会把这里设的 PYTHON* 一起关掉。
        process.arguments = ["-B", "-m", "app.launcher", "start",
                             "--port", String(port), "--no-browser",
                             "--exit-with-parent"]
        var environment = ProcessInfo.processInfo.environment
        // 壳自己不决定数据放哪 —— 那个答案在 app/config.py 里，只有一份。
        // 这里只保证子进程拿得到一个干净的、非交互的环境。
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        process.environment = environment
        if let handle = logHandle {
            process.standardOutput = handle
            process.standardError = handle
        }
    }

    func start() throws {
        process.terminationHandler = { [weak self] finished in
            guard let self = self, !self.stopping else { return }
            // 只有正常 exit 的码才有含义；被信号杀掉的没有"请重启"这回事。
            let code: Int32 = finished.terminationReason == .exit ? finished.terminationStatus : -1
            self.onExit?(code)
        }
        try process.run()
    }

    /// 后端要求重新拉起时用的退出码。与 app/services/self_update.py 的
    /// RESTART_EXIT_CODE 是同一个数 —— 壳只认这一个，别的码一律当事故。
    static let restartExitCode: Int32 = 3

    /// 等到 `/health/ready` 答 200 为止。
    ///
    /// 判据不是"进程还活着"：后端可以起来、健康检查却一直不过（库开不了、
    /// 数据根建不出来）。那种时候要把日志给人看，而不是把一个空白窗口给人看。
    func waitUntilReady(timeout: TimeInterval) -> Result<Void, ShellError> {
        let deadline = Date().addingTimeInterval(timeout)
        let probe = url.appendingPathComponent("health/ready")
        while Date() < deadline {
            if !process.isRunning {
                return .failure(.backendExited(code: process.terminationStatus, log: tailOfLog()))
            }
            var request = URLRequest(url: probe)
            request.timeoutInterval = 3
            let semaphore = DispatchSemaphore(value: 0)
            var ready = false
            URLSession.shared.dataTask(with: request) { _, response, _ in
                ready = (response as? HTTPURLResponse)?.statusCode == 200
                semaphore.signal()
            }.resume()
            _ = semaphore.wait(timeout: .now() + 5)
            if ready { return .success(()) }
            Thread.sleep(forTimeInterval: 0.3)
        }
        return .failure(.neverBecameReady(log: tailOfLog()))
    }

    /// 关窗 = 退出 = 后端也收摊。
    ///
    /// 先 SIGTERM 给它收尾的机会（在飞的那一轮要落盘），两秒不走再 SIGKILL。
    /// 不收摊的代价不是"多一个进程"：下次打开会撞上一个还占着数据根的旧实例。
    func stop() {
        stopping = true
        guard process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(2.0)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        try? logHandle?.close()
    }

    func tailOfLog() -> String {
        guard let text = try? String(contentsOf: logURL, encoding: .utf8) else { return "" }
        return text.split(separator: "\n").suffix(25).joined(separator: "\n")
    }

    /// 让系统挑一个空端口。写死一个的话，个人电脑上十有八九已经被占了，
    /// 而"端口被占"是最不该让用户自己去查的一类失败。
    private static func freePort() throws -> UInt16 {
        let handle = socket(AF_INET, SOCK_STREAM, 0)
        guard handle >= 0 else { throw ShellError.noPort }
        defer { close(handle) }
        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        address.sin_port = 0
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(handle, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { throw ShellError.noPort }
        var assigned = sockaddr_in()
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &assigned) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                getsockname(handle, $0, &length)
            }
        }
        guard named == 0 else { throw ShellError.noPort }
        return UInt16(bigEndian: assigned.sin_port)
    }
}

enum ShellError: Error {
    case noPort
    case backendExited(code: Int32, log: String)
    case neverBecameReady(log: String)

    /// 给人看的一句话 + 现场。壳自己修不了这些问题，它的职责是**不隐瞒**。
    var message: String {
        switch self {
        case .noPort:
            return "找不到可用的本机端口。"
        case .backendExited(let code, let log):
            return "后端启动后退出了（exit \(code)）。\n\n\(log)"
        case .neverBecameReady(let log):
            return "后端起来了，但一直没有就绪。\n\n\(log)"
        }
    }
}

// MARK: - 应用

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backend: Backend?
    func applicationDidFinishLaunching(_ notification: Notification) {
        // 起任何东西之前先看数据根里有没有比我新的壳（上次更新留下的）。有就换成它并重新
        // 拉起自己 —— 此刻还没起后端、没开窗口，是壳能被自更新换掉的唯一时机（#953 ④）。
        cleanupOldShellFiles()
        if replaceMyselfIfANewerShellIsStaged() { return }

        let configuration = WKWebViewConfiguration()
        // 界面是本机服务的同源页面，用不着任何跨站能力。
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        installMenus()
        // 页面自己画标题栏与滚动，壳不插手。
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 840),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "ScienceMate"
        window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 900, height: 600)
        window.center()
        window.setFrameAutosaveName("main")
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        declareMyself()
        startBackend()
    }

    /// 把「装着的壳是哪一份」写给后端看：`<数据根>/shell.json`。
    ///
    /// 自更新只换 harness 与界面，换不了壳（#953）。后端要回答「这次更新含不含壳的改动、
    /// 装着的壳会不会自己换」，就得知道装着的壳到底是哪一份 —— 只有壳自己最清楚。
    /// 数据根的规则和 `startBackend` 里那条完全一样（不另抄第二份）。
    /// `self_replace` 现在是 false：这一版的壳还不会自己换；那一步落地时翻成 true。
    /// 写不出来不拦启动。
    /// 数据根：和 config.py 那条规则一样（HARNESS_FRAMEWORK_HOME，没有就是 ~/.harness-framework）。
    /// 壳里三处要用（日志、自报家门、自替换），只算这一次。
    private func dataRoot() -> URL {
        ProcessInfo.processInfo.environment["HARNESS_FRAMEWORK_HOME"]
            .map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) }
            ?? FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".harness-framework")
    }

    /// 日志在哪：`<数据根>/logs`。后端的 stdout/stderr 接到这里的 backend.log（`startBackend`），
    /// 「打开日志文件夹」打开的也是这里 —— 一个答案，两处使用。
    private func logsDirectory() -> URL { dataRoot().appendingPathComponent("logs") }

    /// 出错页上那个按钮的地址。只在这个壳里有意义：点了由 `decidePolicyFor` 截下来，
    /// 打开日志文件夹，不做任何导航。
    private static let openLogsURL = "sciencemate-logs:open"

    /// 「帮助 → 打开日志文件夹」和出错页上的按钮都走这里。
    ///
    /// 界面里的「下载诊断包」要后端活着才点得到。后端起不来、中途退出的时候 —— 正是
    /// 最需要日志的时候 —— 只剩壳这一条路。有 backend.log 就在访达里选中它，没有就
    /// 打开目录（目录不在先建出来，别让人对着一个"找不到"发愣）。
    @objc func openLogsFolder(_ sender: Any?) {
        let logs = logsDirectory()
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let backendLog = logs.appendingPathComponent("backend.log")
        if FileManager.default.fileExists(atPath: backendLog.path) {
            NSWorkspace.shared.activateFileViewerSelecting([backendLog])
        } else {
            NSWorkspace.shared.open(logs)
        }
    }

    private func sha256(of url: URL) -> String? {
        guard let bytes = try? Data(contentsOf: url) else { return nil }
        return SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
    }

    /// "0.4.6" → [0,4,6]；和后端 self_update.parse_version 同一把尺（数字段，非数字当 0）。
    private func parseVersion(_ text: String) -> [Int] {
        let head = text.split(separator: "-", maxSplits: 1).first.map(String.init) ?? ""
        var parts = head.split(separator: "+", maxSplits: 1).first.map(String.init)!
            .split(separator: ".", omittingEmptySubsequences: false)
            .map { Int(String($0.prefix { $0.isNumber })) ?? 0 }
        while parts.count < 3 { parts.append(0) }
        return parts
    }

    private func isNewer(_ candidate: String, than installed: String) -> Bool {
        if installed.isEmpty { return false }   // 不知道自己哪一版 → 不动
        let a = parseVersion(candidate), b = parseVersion(installed)
        for i in 0..<max(a.count, b.count) {
            let x = i < a.count ? a[i] : 0, y = i < b.count ? b[i] : 0
            if x != y { return x > y }
        }
        return false
    }

    /// 上一次自替换留下的 .old 收掉；收不掉就下次再收。
    private func cleanupOldShellFiles() {
        guard let exe = Bundle.main.executableURL else { return }
        try? FileManager.default.removeItem(at: exe.appendingPathExtension("old"))
    }

    /// 数据根里有比我新的壳就换成它并重新拉起自己。返回 true = 已拉起新的，调用方该收摊。
    ///
    /// 自更新把新壳放在 `<数据根>/payload/<版本>/extras/shell/macos/ScienceMate`。运行中的
    /// 二进制在 macOS 上可以改名、不能覆盖 —— 改名成 .old、把新的拷上来、拉起新的自己。
    /// 两道闸缺一不可：**版本**（载荷比 Info.plist 里我随哪一版装的新，别把重装的新壳换成旧的）
    /// 和**哈希**（不是同一份字节才换 —— 这是循环的终止条件）。
    /// 任何一步失败都回滚到「旧的还能用」。
    private func replaceMyselfIfANewerShellIsStaged() -> Bool {
        let root = dataRoot()
        let pointer = root.appendingPathComponent("payload/current.json")
        guard let data = try? Data(contentsOf: pointer),
              let doc = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let version = doc["version"] as? String, !version.isEmpty else { return false }
        let mine = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? ""
        guard isNewer(version, than: mine) else {
            NSLog("SHELL-SWAP skip: payload %@ 不比我（%@）新", version, mine); return false
        }
        let candidate = root.appendingPathComponent("payload/\(version)/extras/shell/macos/ScienceMate")
        guard FileManager.default.fileExists(atPath: candidate.path),
              let exe = Bundle.main.executableURL,
              let theirs = sha256(of: candidate), let ours = sha256(of: exe) else { return false }
        if theirs == ours { NSLog("SHELL-SWAP skip: 载荷里的壳就是我"); return false }

        let old = exe.appendingPathExtension("old")
        do {
            try? FileManager.default.removeItem(at: old)
            try FileManager.default.moveItem(at: exe, to: old)
            do {
                try FileManager.default.copyItem(at: candidate, to: exe)
                // 载荷里的文件按普通文件打包（0644），可执行位要自己补回来。
                try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: exe.path)
            } catch {
                try? FileManager.default.removeItem(at: exe)
                try? FileManager.default.moveItem(at: old, to: exe)
                throw error
            }
        } catch {
            NSLog("SHELL-SWAP failed, rolled back: %@", "\(error)"); return false
        }
        NSLog("SHELL-SWAP done: %@ → %@，拉起新的自己", mine, version)
        // 用 open(1) 起新的自己：它走 LaunchServices，和用户双击是同一条路；
        // 等它真起来再退出，别让 Dock 上空一拍。
        let relaunch = Process()
        relaunch.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        relaunch.arguments = ["-n", Bundle.main.bundleURL.path]
        try? relaunch.run()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { NSApp.terminate(nil) }
        return true
    }

    private func declareMyself() {
        guard let exe = Bundle.main.executableURL,
              let bytes = try? Data(contentsOf: exe) else { return }
        let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let home = dataRoot()
        let record: [String: Any] = [
            "platform": "macos",
            "path": exe.path,
            "sha256": digest,
            "size": bytes.count,
            "self_replace": true,
            "declared_at": ISO8601DateFormatter().string(from: Date()),
        ]
        guard let json = try? JSONSerialization.data(withJSONObject: record, options: [.prettyPrinted, .sortedKeys]) else { return }
        try? FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        // 原子写：后端可能正好在读，别让它读到半个 JSON。
        try? json.write(to: home.appendingPathComponent("shell.json"), options: .atomic)
    }

    /// 起本机后端。**总是起** —— 这个应用永远是这台机器上的一个实例。
    ///
    /// 从前这里有个分支：数据根里有 `server.json` 就不起后端，webview 整个指向
    /// 那台组织服务器。于是专业版打开就是别人家的登录页 —— 想在本机开个项目都
    /// 不行，而一台服务器上住着好几个组织，登录还得先挑一个（2026-09-22 wangd
    /// 逐条否掉）。病根是把"项目住哪"做成了"应用是什么"。
    ///
    /// 现在：组织是本机后端握着的几条连接，项目出生时挑一个家。壳只管起后端。
    private func startBackend() {
        let resources = Bundle.main.resourceURL!
        let python = resources.appendingPathComponent("python/bin/python3")
        // 日志落在数据根里，跟别的东西一起。数据根的规则只有一条（config.py：
        // HARNESS_FRAMEWORK_HOME，没有就是 ~/.harness-framework），壳照抄那一条
        // ——写死 ~/.harness-framework 的话，换了数据根的人会在一个地方找数据、
        // 在另一个地方找日志，而两边都"存在"。
        let logs = logsDirectory()
        // 起后端会阻塞几秒（建库、跑迁移）。放后台线程，别让窗口在这几秒里
        // 变成一个不响应的灰块 —— 那正是用户会强退的时刻。
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            do {
                let backend = try Backend(python: python, logDirectory: logs)
                try backend.start()
                switch backend.waitUntilReady(timeout: 120) {
                case .success:
                    DispatchQueue.main.async {
                        self?.backend = backend
                        // 后端刚起来就把暂存的更新搬成了当前载荷 —— 那一版带了新壳的话，
                        // 此刻数据根里已经有它。换成它并重新拉起自己（applicationWillTerminate
                        // 会收掉这个后端，新壳再起一个）。首次启动时 didFinishLaunching 已经
                        // 查过一遍，这里再查是为「更新 → 重启」那条路：那时窗口早就开着了。
                        if self?.replaceMyselfIfANewerShellIsStaged() == true { return }
                        // 后端自己退出：码 3 = 装好了更新、请重新拉起（重挑端口、重指
                        // webview，走的就是这同一条 startBackend）。别的码 = 事故，把
                        // 日志尾巴给人看，而不是留一个失联的白窗口。
                        backend.onExit = { [weak self] code in
                            DispatchQueue.main.async {
                                self?.backend = nil
                                if code == Backend.restartExitCode {
                                    self?.startBackend()
                                } else {
                                    self?.showMessage("后端退出了（code \(code)）。\n\n" + backend.tailOfLog())
                                }
                            }
                        }
                        self?.webView.load(URLRequest(url: backend.url))
                    }
                case .failure(let error):
                    DispatchQueue.main.async { self?.show(error) }
                }
            } catch let error as ShellError {
                DispatchQueue.main.async { self?.show(error) }
            } catch {
                DispatchQueue.main.async {
                    self?.showMessage("起不来：\(error.localizedDescription)")
                }
            }
        }
    }

    private func show(_ error: ShellError) { showMessage(error.message) }

    /// 失败也要有个界面。一个白窗口 + 一个 Dock 图标是最难查的一种失败。
    private func showMessage(_ text: String) {
        func escape(_ raw: String) -> String {
            raw.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        }
        let escaped = escape(text)
        let logsPath = escape(logsDirectory().path)
        webView.loadHTMLString("""
        <html><head><meta charset="utf-8"><style>
          :root { color-scheme: light dark; }
          body { font: 13px/1.6 -apple-system, system-ui, sans-serif;
                 margin: 0; padding: 48px; }
          h1 { font-size: 15px; margin: 0 0 16px; }
          pre { white-space: pre-wrap; font-size: 11px; opacity: .75;
                border-left: 2px solid currentColor; padding-left: 12px; }
        </style></head><body>
          <h1>没能启动</h1><pre>\(escaped)</pre>
          <p>完整的日志在 <code>\(logsPath)</code>。找人帮忙时，把里面的 backend.log 一起发过去。</p>
          <p><a href="\(Self.openLogsURL)">打开日志文件夹</a>（菜单栏「帮助」里也有）</p>
        </body></html>
        """, baseURL: nil)
    }

    /// 菜单栏。没有它，⌘C/⌘V 在 webview 里**一个都不响应** —— AppKit 的编辑快捷键
    /// 是挂在菜单项上的，没菜单就没快捷键。用户会以为是界面坏了。
    private func installMenus() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 ScienceMate", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("重做", "redo:", "Z"),
                                     ("剪切", "cut:", "x"), ("复制", "copy:", "c"),
                                     ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        editItem.submenu = edit
        main.addItem(editItem)
        // 「帮助」：出了事去哪找日志。放这里是因为它不依赖后端 —— 后端死了它也在。
        let helpItem = NSMenuItem()
        let help = NSMenu(title: "帮助")
        let openLogs = NSMenuItem(title: "打开日志文件夹", action: #selector(openLogsFolder(_:)), keyEquivalent: "")
        openLogs.target = self
        help.addItem(openLogs)
        helpItem.submenu = help
        main.addItem(helpItem)
        NSApp.mainMenu = main
        NSApp.helpMenu = help
    }

    private func isLocal(_ url: URL) -> Bool {
        if url.scheme == "about" { return true }
        // 本机后端就是这个窗口唯一的源 —— 它总是起着（`startBackend`）。
        guard let origin = backend?.url else { return false }
        if url.scheme == "blob" {
            return url.absoluteString.hasPrefix("blob:" + origin.absoluteString)
        }
        return url.scheme == origin.scheme && url.host == origin.host && url.port == origin.port
    }

    /// 外链交给系统浏览器，别让壳变成一个没有地址栏、没有返回键的浏览器。
    ///
    /// 页面里有论文、仓库、mailto 这类链接。在这个窗口里打开它们，用户就**回不来了**
    /// ——没有后退键，只能退出重开。别的协议一律拦下。
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if url.absoluteString == Self.openLogsURL {
            openLogsFolder(nil)
            decisionHandler(.cancel)
            return
        }
        if isLocal(url) {
            decisionHandler(action.shouldPerformDownload ? .download : .allow)
        } else {
            if action.navigationType == .linkActivated && ["https", "http", "mailto"].contains(url.scheme ?? "") {
                NSWorkspace.shared.open(url)
            }
            decisionHandler(.cancel)
        }
    }

    /// 浏览器显示不了的类型（zip、csv、pdf 视设置而定）就存盘，而不是什么都不发生。
    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(response.canShowMIMEType ? .allow : .download)
    }

    /// `target=_blank`：同源的就在本窗口开，外链交给系统浏览器。壳不开第二个窗口。
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, action.navigationType == .linkActivated {
            if isLocal(url) { webView.load(action.request) }
            else if ["https", "http", "mailto"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    /// 下载落到哪由用户说了算。没有这一段，交付物在壳里**点了没反应**。
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.title = "保存研究文件"
        panel.nameFieldStringValue = (suggestedFilename as NSString).lastPathComponent
        panel.canCreateDirectories = true
        panel.beginSheetModal(for: window) { answer in
            completionHandler(answer == .OK ? panel.url : nil)
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        guard (error as NSError).code != NSURLErrorCancelled else { return }
        let alert = NSAlert()
        alert.messageText = "文件下载失败"
        alert.informativeText = error.localizedDescription
        alert.beginSheetModal(for: window)
    }

    // alert/confirm/文件选择：WKWebView 默认**什么都不做**。页面上「确认删除」这类
    // 对话框于是静默消失，用户看到的是"点了没反应"。
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "确认")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { answer in completionHandler(answer == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.beginSheetModal(for: window) { answer in completionHandler(answer == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // 这一行是验收判据：装出来的包到底有没有把界面显示出来，
        // 除了人眼看，只有它说得清。
        webView.evaluateJavaScript("document.title") { title, _ in
            FileHandle.standardError.write(
                "shell: webview loaded: \(title ?? "(no title)")\n".data(using: .utf8)!)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) { backend?.stop() }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
