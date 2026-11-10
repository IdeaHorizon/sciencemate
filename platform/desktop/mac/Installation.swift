// Installation shared by the DMG launch and install.sh. Never touches the data root.
import Foundation
import Darwin

struct DesktopInstallation {
    static let bundleID = "com.ieit.sciencemate"
    static let appName = "ScienceMate.app"
    struct Outcome {
        let application: URL
        let version: String
        let changed: Bool
        var warnings: [String] = []
    }

    static func samePath(_ left: URL, _ right: URL) -> Bool {
        left.resolvingSymlinksInPath().standardizedFileURL.path
            == right.resolvingSymlinksInPath().standardizedFileURL.path
    }

    static func compare(_ left: String, _ right: String) -> Int {
        func parts(_ value: String) -> [Int] {
            let head = value.trimmingCharacters(in: .whitespacesAndNewlines)
                .components(separatedBy: "-")[0].components(separatedBy: "+")[0]
            return head.components(separatedBy: ".").map {
                Int(String($0.prefix { $0 >= "0" && $0 <= "9" })) ?? 0
            }
        }
        let a = parts(left), b = parts(right)
        for i in 0..<max(a.count, b.count) {
            let x = i < a.count ? a[i] : 0, y = i < b.count ? b[i] : 0
            if x != y { return x < y ? -1 : 1 }
        }
        return 0
    }

    static func version(at app: URL) -> String? {
        guard let data = try? Data(contentsOf: app.appendingPathComponent("Contents/Info.plist")),
              let doc = (try? PropertyListSerialization.propertyList(from: data, options: [], format: nil)) as? [String: Any],
              doc["CFBundleIdentifier"] as? String == bundleID,
              let version = doc["CFBundleShortVersionString"] as? String, !version.isEmpty,
              FileManager.default.isExecutableFile(atPath: app.appendingPathComponent("Contents/MacOS/ScienceMate").path)
        else { return nil }
        return version
    }

    static func applications(in directories: [URL]) -> [URL] {
        let fm = FileManager.default
        return directories.flatMap { directory in
            (try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.isSymbolicLinkKey],
                                        options: [.skipsHiddenFiles])) ?? []
        }.filter {
            $0.pathExtension == "app" && (try? $0.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) != true
                && version(at: $0) != nil
        }.map { $0.standardizedFileURL }
    }

    static func newest(in directories: [URL]) -> URL? {
        let candidates: [(URL, String)] = applications(in: directories).compactMap { app in
            guard let installed = version(at: app) else { return nil }
            return (app, installed)
        }
        return candidates.sorted {
            let order = compare($0.1, $1.1)
            if order != 0 { return order > 0 }
            if ($0.0.lastPathComponent == appName) != ($1.0.lastPathComponent == appName) {
                return $0.0.lastPathComponent == appName
            }
            return $0.0.path < $1.0.path
        }.first?.0
    }

    static func failure(_ text: String) -> NSError {
        NSError(domain: "ScienceMate.Install", code: 1, userInfo: [NSLocalizedDescriptionKey: text])
    }

    static func install(source: URL, directories: [URL], preferredDirectory: URL,
                        isRunning: (URL) -> Bool) throws -> Outcome {
        let fm = FileManager.default
        guard let incoming = version(at: source) else { throw failure("Invalid ScienceMate application bundle.") }
        // A no-op needs no write permission (for example an admin-owned /Applications).
        if let existing = newest(in: directories), let installed = version(at: existing),
           compare(installed, incoming) >= 0 {
            return Outcome(application: existing, version: installed, changed: false)
        }
        let parent = newest(in: directories)?.deletingLastPathComponent() ?? preferredDirectory
        try fm.createDirectory(at: parent, withIntermediateDirectories: true)
        // flock is released even if the installer crashes; no stale lock directory.
        let fd = Darwin.open(parent.appendingPathComponent(".sciencemate-install.lock").path,
                             O_CREAT | O_RDWR | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw failure("Cannot lock the installation folder: \(parent.path)") }
        defer { Darwin.close(fd) }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { throw failure("Another ScienceMate installer is running.") }
        defer { flock(fd, LOCK_UN) }

        if let existing = newest(in: directories), let installed = version(at: existing),
           compare(installed, incoming) >= 0 {
            return Outcome(application: existing, version: installed, changed: false)
        }
        let target = parent.appendingPathComponent(appName).standardizedFileURL
        if fm.fileExists(atPath: target.path) && version(at: target) == nil {
            throw failure("The destination contains an unrelated application: \(target.path)")
        }
        // Refuse before copying, then check again before replacing the old installation.
        func checkRunning() throws {
            if applications(in: directories).contains(where: isRunning) {
                throw failure("Quit ScienceMate before upgrading. The current installation has been kept.")
            }
        }
        try checkRunning()
        let transaction = UUID().uuidString
        let staging = parent.appendingPathComponent(".ScienceMate-new-\(transaction).noindex")
        let retired = parent.appendingPathComponent(".ScienceMate-old-\(transaction).noindex")
        defer { try? fm.removeItem(at: staging) }
        let copy = Process()
        copy.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
        copy.arguments = [source.path, staging.path]
        try copy.run(); copy.waitUntilExit()
        guard copy.terminationStatus == 0, version(at: staging) == incoming else {
            throw failure("The new application could not be copied. The current installation has been kept.")
        }
        try checkRunning()
        let hadTarget = fm.fileExists(atPath: target.path)
        if hadTarget { try fm.moveItem(at: target, to: retired) }
        do { try fm.moveItem(at: staging, to: target) }
        catch {
            if hadTarget { try fm.moveItem(at: retired, to: target) }
            throw error
        }
        var outcome = Outcome(application: target, version: incoming, changed: true)
        if hadTarget {
            do { try fm.removeItem(at: retired) }
            catch { outcome.warnings.append("Could not remove the previous application: \(retired.path)") }
        }
        // Migrate version-named copies only after the canonical application is in place.
        // Only verified bundles are eligible; never follow symlinks or remove a running copy.
        for other in applications(in: directories) where !samePath(other, target) && !samePath(other, source) {
            guard let old = version(at: other), compare(old, incoming) <= 0, !isRunning(other) else { continue }
            do { try fm.removeItem(at: other) }
            catch { outcome.warnings.append("Could not remove the previous application: \(other.path)") }
        }
        return outcome
    }
}
