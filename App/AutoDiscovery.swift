import Combine
import CoreServices
import Foundation

enum DiscoveryScope: String, CaseIterable, Sendable {
    case chatgpt, arbeit, personal, claude, records
}

struct LocalDiscoveryChange: Equatable, Sendable {
    let revision: UInt64
    let scopes: Set<DiscoveryScope>
}

struct DiscoveryWatchRoot: Sendable {
    enum Kind: Sendable { case codex, claude, records, backups }
    let url: URL
    let scope: DiscoveryScope
    let kind: Kind

    static func defaults(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> [Self] {
        [
            Self(url: home.appendingPathComponent(".codex"), scope: .chatgpt, kind: .codex),
            Self(url: home.appendingPathComponent("Library/Application Support/Arbeit/codex"), scope: .arbeit, kind: .codex),
            Self(url: home.appendingPathComponent("Library/Application Support/ChatGPT Copy/codex"), scope: .personal, kind: .codex),
            Self(url: home.appendingPathComponent(".claude/projects"), scope: .claude, kind: .claude),
            Self(url: home.appendingPathComponent("Library/Application Support/Arbeit/chat-manager"), scope: .records, kind: .records),
            Self(url: home.appendingPathComponent("Library/Application Support/Arbeit/maintenance-backups"), scope: .records, kind: .backups),
            Self(url: home.appendingPathComponent("Library/Application Support/ChatGPT Copy/maintenance-backups"), scope: .records, kind: .backups),
            Self(url: home.appendingPathComponent("Library/Application Support/Arbeit/chat-handoff/backups"), scope: .records, kind: .backups)
        ]
    }
}

struct DiscoveryFileEvent: Sendable {
    let path: String
    let flags: UInt32
}

enum DiscoveryPathFilter {
    static let lostEvents: UInt32 = UInt32(kFSEventStreamEventFlagMustScanSubDirs)
        | UInt32(kFSEventStreamEventFlagUserDropped) | UInt32(kFSEventStreamEventFlagKernelDropped)
        | UInt32(kFSEventStreamEventFlagEventIdsWrapped)

    private static func comparisonPath(_ value: String) -> String {
        // FSEvents may use the physical /private/var path while Foundation
        // switches to /var only for existing files. Use lexical normalization
        // so deleting a file cannot change which profile its event belongs to.
        var parts: [Substring] = []
        for part in value.split(separator: "/") {
            if part == "." { continue }
            if part == ".." { if !parts.isEmpty { parts.removeLast() }; continue }
            parts.append(part)
        }
        if parts.count > 1, parts[0] == "private", ["var", "tmp", "etc"].contains(String(parts[1])) { parts.removeFirst() }
        return "/" + parts.joined(separator: "/")
    }

    static func scopes(for events: [DiscoveryFileEvent], roots: [DiscoveryWatchRoot]) -> Set<DiscoveryScope> {
        var result: Set<DiscoveryScope> = []
        for event in events {
            // A dropped event batch can refer to an ancestor outside an individual
            // profile. Catch up all roots rather than trusting incomplete paths.
            if event.flags & lostEvents != 0 {
                result.formUnion(roots.map(\.scope))
                continue
            }
            for root in roots where relevant(event, to: root) { result.insert(root.scope) }
        }
        return result
    }

    private static func relevant(_ event: DiscoveryFileEvent, to root: DiscoveryWatchRoot) -> Bool {
        let base = comparisonPath(root.url.path)
        let path = comparisonPath(event.path)
        guard path == base || path.hasPrefix(base + "/") else { return false }
        let relative = path == base ? "" : String(path.dropFirst(base.count + 1))
        let parts = relative.split(separator: "/").map(String.init)
        let directory = event.flags & UInt32(kFSEventStreamEventFlagItemIsDir) != 0
        let structural = event.flags & (UInt32(kFSEventStreamEventFlagItemCreated)
            | UInt32(kFSEventStreamEventFlagItemRemoved) | UInt32(kFSEventStreamEventFlagItemRenamed)
            | UInt32(kFSEventStreamEventFlagRootChanged)) != 0
        if parts.isEmpty { return structural }
        switch root.kind {
        case .codex:
            if parts.count == 1 {
                let name = parts[0]
                if name == "session_index.jsonl" { return true }
                if name.hasPrefix("state_") || name.hasPrefix("thread_history_"),
                   name.hasSuffix(".sqlite") || name.hasSuffix(".sqlite-wal") { return true }
            }
            guard parts[0] == "sessions" || parts[0] == "archived_sessions" else { return false }
            return directory ? structural : path.hasSuffix(".jsonl")
        case .claude:
            return directory ? structural : path.hasSuffix(".jsonl")
        case .records:
            guard ["activity", "bridge", "chats", "pairs", "backups"].contains(parts[0]) else { return false }
            if directory { return structural }
            return path.hasSuffix(".json") || path.hasSuffix(".result.json")
        case .backups:
            return directory ? structural : path.hasSuffix(".json")
        }
    }
}

private struct DiscoveryStamp: Equatable, Sendable {
    let size: Int64
    let modified: TimeInterval
    let created: TimeInterval
}

private struct DiscoverySnapshot: Equatable, Sendable {
    let exists: Bool
    let entries: [String: DiscoveryStamp]
    let limitReached: Bool

    // Metadata only: no database connections, transcript reads, or auth reads.
    // Codex saves changes in its state database/WAL, so its fallback never walks
    // thousands of rollout files. Claude has no equivalent central database.
    static func capture(_ root: DiscoveryWatchRoot) -> Self {
        let fm = FileManager.default
        let keys: Set<URLResourceKey> = [.isDirectoryKey, .isRegularFileKey, .isSymbolicLinkKey,
                                       .fileSizeKey, .contentModificationDateKey, .creationDateKey]
        guard let rootValues = try? root.url.resourceValues(forKeys: keys),
              rootValues.isDirectory == true, rootValues.isSymbolicLink != true else {
            return Self(exists: false, entries: [:], limitReached: false)
        }
        var entries: [String: DiscoveryStamp] = [:]
        func record(_ url: URL, _ values: URLResourceValues, relative: String) {
            entries[relative] = DiscoveryStamp(size: Int64(values.fileSize ?? 0),
                modified: values.contentModificationDate?.timeIntervalSince1970 ?? 0,
                created: values.creationDate?.timeIntervalSince1970 ?? 0)
        }
        if root.kind == .codex {
            let children = (try? fm.contentsOfDirectory(at: root.url, includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles])) ?? []
            let relevant = children.filter { url in
                let name = url.lastPathComponent
                return name == "session_index.jsonl"
                    || ((name.hasPrefix("state_") || name.hasPrefix("thread_history_"))
                        && (name.hasSuffix(".sqlite") || name.hasSuffix(".sqlite-wal")))
            }
            for url in relevant.prefix(512) {
                let name = url.lastPathComponent
                guard let values = try? url.resourceValues(forKeys: keys), values.isSymbolicLink != true else { continue }
                record(url, values, relative: name)
            }
            return Self(exists: true, entries: entries, limitReached: relevant.count > 512)
        }
        if root.kind == .backups {
            let children = (try? fm.contentsOfDirectory(at: root.url, includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles])) ?? []
            for url in children.prefix(2048) {
                guard let values = try? url.resourceValues(forKeys: keys), values.isDirectory == true, values.isSymbolicLink != true else { continue }
                record(url, values, relative: url.lastPathComponent)
            }
            return Self(exists: true, entries: entries, limitReached: children.count > 2048)
        }
        guard let iterator = fm.enumerator(at: root.url, includingPropertiesForKeys: Array(keys), options: [.skipsHiddenFiles, .skipsPackageDescendants]) else {
            return Self(exists: true, entries: [:], limitReached: false)
        }
        var visited = 0
        var limited = false
        while let url = iterator.nextObject() as? URL {
            visited += 1
            if visited > 8192 { limited = true; break }
            let relative = String(url.path.dropFirst(root.url.path.count + 1))
            let parts = relative.split(separator: "/")
            guard let values = try? url.resourceValues(forKeys: keys) else { continue }
            if values.isSymbolicLink == true { iterator.skipDescendants(); continue }
            if root.kind == .records {
                if !["activity", "bridge", "chats", "pairs", "backups"].contains(String(parts.first ?? "")) {
                    iterator.skipDescendants(); continue
                }
                // These fixed layouts expose operation metadata, never stored
                // rollouts, app bundles, or a recursive full-backup scan.
                if parts.contains("rollouts") || parts.contains("sessions") || parts.contains("archived_sessions") || parts.count > 8 {
                    iterator.skipDescendants(); continue
                }
                if values.isDirectory == true {
                    record(url, values, relative: relative)
                } else if url.pathExtension == "json" {
                    record(url, values, relative: relative)
                }
            } else if values.isRegularFile == true, url.pathExtension == "jsonl" {
                record(url, values, relative: relative)
            }
        }
        return Self(exists: true, entries: entries, limitReached: limited)
    }
}

private final class DiscoveryEventRelay: @unchecked Sendable {
    let deliver: @Sendable ([DiscoveryFileEvent]) -> Void
    init(deliver: @escaping @Sendable ([DiscoveryFileEvent]) -> Void) { self.deliver = deliver }
}

private final class DiscoveryEventStream: @unchecked Sendable {
    private let lock = NSLock()
    private var stream: FSEventStreamRef?
    // Keep a dedicated serial queue alive as long as the native stream.
    private let queue = DispatchQueue(label: "ChatTransfer.LocalDiscovery", qos: .utility)

    init?(roots: [DiscoveryWatchRoot], deliver: @escaping @Sendable ([DiscoveryFileEvent]) -> Void) {
        let fm = FileManager.default
        var paths: Set<String> = []
        for root in roots {
            var path = root.url.standardizedFileURL
            while !fm.fileExists(atPath: path.path), path.path != "/" { path.deleteLastPathComponent() }
            paths.insert(path.path)
        }
        guard !paths.isEmpty else { return nil }
        let relay = DiscoveryEventRelay(deliver: deliver)
        var context = FSEventStreamContext(version: 0, info: Unmanaged.passUnretained(relay).toOpaque(),
            retain: { info in
                guard let info else { return nil }
                return UnsafeRawPointer(Unmanaged<DiscoveryEventRelay>.fromOpaque(info).retain().toOpaque())
            }, release: { info in
                guard let info else { return }
                Unmanaged<DiscoveryEventRelay>.fromOpaque(info).release()
            }, copyDescription: nil)
        let flags = UInt32(kFSEventStreamCreateFlagFileEvents) | UInt32(kFSEventStreamCreateFlagWatchRoot)
            | UInt32(kFSEventStreamCreateFlagNoDefer)
        guard let created = FSEventStreamCreate(kCFAllocatorDefault, { _, info, count, rawPaths, flags, _ in
            guard let info else { return }
            let relay = Unmanaged<DiscoveryEventRelay>.fromOpaque(info).takeUnretainedValue()
            let paths = rawPaths.assumingMemoryBound(to: UnsafePointer<CChar>.self)
            var events: [DiscoveryFileEvent] = []
            events.reserveCapacity(min(count, 4096))
            // A bounded batch preserves dropped-event semantics without retaining
            // arbitrary filenames or conversation data anywhere on disk.
            if count > 4096 {
                events.append(DiscoveryFileEvent(path: "", flags: DiscoveryPathFilter.lostEvents))
            } else {
                for index in 0..<count { events.append(DiscoveryFileEvent(path: String(cString: paths[index]), flags: flags[index])) }
            }
            relay.deliver(events)
        }, &context, Array(paths).sorted() as CFArray, FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 0.15, flags) else { return nil }
        FSEventStreamSetDispatchQueue(created, queue)
        guard FSEventStreamStart(created) else {
            FSEventStreamInvalidate(created); FSEventStreamRelease(created); return nil
        }
        stream = created
    }

    func stop() {
        lock.lock()
        let previous = stream
        stream = nil
        lock.unlock()
        if let previous {
            FSEventStreamStop(previous)
            FSEventStreamInvalidate(previous)
            FSEventStreamRelease(previous)
        }
    }
    deinit { stop() }
}

/// Observes saved local data while Chat Transfer is open. Events publish only
/// source identifiers; callers decide when a safe read-only refresh may run.
/// Nothing here opens apps, transfers chats, logs paths, or persists content.
@MainActor
final class LocalChangeMonitor: ObservableObject {
    @Published private(set) var change = LocalDiscoveryChange(revision: 0, scopes: [])
    @Published private(set) var isWatching = false
    @Published private(set) var usesNativeEvents = false
    private let roots: [DiscoveryWatchRoot]
    private let debounceInterval: TimeInterval
    private let maximumCoalescingInterval: TimeInterval
    private let fallbackInterval: TimeInterval
    private let enableNativeEvents: Bool
    private var stream: DiscoveryEventStream?
    private var pollingTask: Task<Void, Never>?
    private var debounceTask: Task<Void, Never>?
    private var snapshotTask: Task<Void, Never>?
    private var snapshots: [DiscoverySnapshot]?
    private var pending: Set<DiscoveryScope> = []
    private var firstPendingAt: TimeInterval?
    private var generation: UInt64 = 0
    private var needsCatchUp = false

    init(roots: [DiscoveryWatchRoot] = DiscoveryWatchRoot.defaults(),
         debounceInterval: TimeInterval = 0.5, maximumCoalescingInterval: TimeInterval = 2,
         fallbackInterval: TimeInterval = 20, enableNativeEvents: Bool = true) {
        self.roots = roots
        self.debounceInterval = max(0.01, debounceInterval)
        self.maximumCoalescingInterval = max(self.debounceInterval, maximumCoalescingInterval)
        self.fallbackInterval = max(0.05, fallbackInterval)
        self.enableNativeEvents = enableNativeEvents
    }

    func start() {
        guard !isWatching else { return }
        generation &+= 1
        let activeGeneration = generation
        isWatching = true
        if enableNativeEvents {
            stream = DiscoveryEventStream(roots: roots) { [weak self] events in
                Task { @MainActor [weak self] in
                    guard let self, self.isWatching, self.generation == activeGeneration else { return }
                    self.receive(events)
                }
            }
        }
        usesNativeEvents = stream != nil
        catchUp()
        let interval = fallbackInterval
        pollingTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: Self.nanoseconds(interval)) } catch { return }
                self?.catchUp()
            }
        }
    }

    func stop() {
        generation &+= 1
        isWatching = false
        usesNativeEvents = false
        stream?.stop(); stream = nil
        pollingTask?.cancel(); pollingTask = nil
        debounceTask?.cancel(); debounceTask = nil
        snapshotTask?.cancel(); snapshotTask = nil
        pending = []; firstPendingAt = nil; snapshots = nil; needsCatchUp = false
    }

    /// Also call after application activation or wake. Sleeping through native
    /// events is safe because this compares bounded file metadata, not content.
    func catchUp() {
        guard isWatching else { return }
        guard snapshotTask == nil else { needsCatchUp = true; return }
        let activeGeneration = generation
        let watchedRoots = roots
        snapshotTask = Task { [weak self] in
            let captured = await Task.detached(priority: .utility) {
                watchedRoots.map(DiscoverySnapshot.capture)
            }.value
            guard !Task.isCancelled, let self, self.isWatching, self.generation == activeGeneration else { return }
            self.snapshotTask = nil
            if let previous = self.snapshots {
                var scopes: Set<DiscoveryScope> = []
                for index in watchedRoots.indices where previous[index] != captured[index] || captured[index].limitReached {
                    scopes.insert(watchedRoots[index].scope)
                }
                self.enqueue(scopes)
            }
            self.snapshots = captured
            if self.needsCatchUp { self.needsCatchUp = false; self.catchUp() }
        }
    }

    // Internal entry point permits deterministic synthetic event tests. Native
    // callbacks go through the same filter and coalescing code.
    func receive(_ events: [DiscoveryFileEvent]) {
        guard isWatching else { return }
        enqueue(DiscoveryPathFilter.scopes(for: events, roots: roots))
    }

    private func enqueue(_ scopes: Set<DiscoveryScope>) {
        guard !scopes.isEmpty, isWatching else { return }
        pending.formUnion(scopes)
        let now = ProcessInfo.processInfo.systemUptime
        if firstPendingAt == nil { firstPendingAt = now }
        let deadline = min(now + debounceInterval, (firstPendingAt ?? now) + maximumCoalescingInterval)
        debounceTask?.cancel()
        let activeGeneration = generation
        debounceTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: Self.nanoseconds(max(0, deadline - now))) } catch { return }
            guard let self, self.isWatching, self.generation == activeGeneration else { return }
            self.flush()
        }
    }

    private func flush() {
        guard !pending.isEmpty else { return }
        let scopes = pending
        pending = []; firstPendingAt = nil; debounceTask = nil
        change = LocalDiscoveryChange(revision: change.revision &+ 1, scopes: scopes)
    }

    private static func nanoseconds(_ interval: TimeInterval) -> UInt64 {
        UInt64(min(max(interval, 0), 86400) * 1_000_000_000)
    }

    deinit {
        pollingTask?.cancel()
        debounceTask?.cancel()
        snapshotTask?.cancel()
        stream?.stop()
    }
}
