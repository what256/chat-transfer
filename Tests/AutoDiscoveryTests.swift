#if CHAT_DISCOVERY_TESTS
import CoreServices
import Foundation

@main
struct AutoDiscoveryTests {
    @MainActor static var checks = 0
    @MainActor static func check(_ value: @autoclosure () -> Bool, _ label: String) throws {
        guard value() else { throw NSError(domain: "AutoDiscoveryTests", code: 1, userInfo: [NSLocalizedDescriptionKey: label]) }
        checks += 1
    }
    static func pause(_ seconds: TimeInterval) async throws {
        try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
    }
    @MainActor static func awaitChange(_ monitor: LocalChangeMonitor, after revision: UInt64, timeout: TimeInterval = 3, label: String = "Relevant local change") async throws {
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        while monitor.change.revision == revision, ProcessInfo.processInfo.systemUptime < deadline { try await pause(0.03) }
        try check(monitor.change.revision > revision, "\(label) was not detected before the deadline")
    }
    static func event(_ path: String, _ flags: UInt32 = UInt32(kFSEventStreamEventFlagItemModified)) -> DiscoveryFileEvent {
        DiscoveryFileEvent(path: path, flags: flags)
    }
    @MainActor static func main() async {
        let fm = FileManager.default
        let temporary = fm.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("chat-discovery-tests-" + UUID().uuidString)
        do {
            try fm.createDirectory(at: temporary, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            defer { try? fm.removeItem(at: temporary) }
            let codex = temporary.appendingPathComponent("codex")
            let claude = temporary.appendingPathComponent("claude")
            let records = temporary.appendingPathComponent("records")
            let backups = temporary.appendingPathComponent("backups")
            for root in [codex,claude,records,backups] { try fm.createDirectory(at: root, withIntermediateDirectories: true) }
            let roots = [DiscoveryWatchRoot(url: codex, scope: .chatgpt, kind: .codex),
                         DiscoveryWatchRoot(url: claude, scope: .claude, kind: .claude),
                         DiscoveryWatchRoot(url: records, scope: .records, kind: .records),
                         DiscoveryWatchRoot(url: backups, scope: .records, kind: .backups)]
            func scopes(_ events: [DiscoveryFileEvent]) -> Set<DiscoveryScope> { DiscoveryPathFilter.scopes(for: events, roots: roots) }
            try check(scopes([event(codex.appendingPathComponent("state_5.sqlite").path)]) == [.chatgpt], "Database modification")
            try check(scopes([event(codex.appendingPathComponent("state_5.sqlite-wal").path)]) == [.chatgpt], "WAL modification")
            try check(scopes([event(codex.appendingPathComponent("thread_history_2.sqlite").path)]) == [.chatgpt], "Saved history database modification")
            try check(scopes([event(codex.appendingPathComponent("thread_history_2.sqlite-wal").path)]) == [.chatgpt], "Saved history WAL modification")
            try check(scopes([event(codex.appendingPathComponent("session_index.jsonl").path)]) == [.chatgpt], "Index modification")
            try check(scopes([event(codex.appendingPathComponent("sessions/2026/10/13/rollout.jsonl").path)]) == [.chatgpt], "Saved rollout modification")
            try check(scopes([event(codex.appendingPathComponent("archived_sessions/rollout.jsonl").path)]) == [.chatgpt], "Archived rollout modification")
            for ignored in ["auth.json", "config.toml", "models_cache.json", "state_5.sqlite-shm", "credentials.sqlite", "thread_history_2.sqlite-shm", "logs/live.log", "sessions/example.txt"] {
                try check(scopes([event(codex.appendingPathComponent(ignored).path)]).isEmpty, "Unrelated Codex file must not trigger refresh")
            }
            try check(scopes([event(codex.path + "-other/state_5.sqlite")]).isEmpty, "Root boundary must exclude similarly named directory")
            try check(scopes([event(claude.appendingPathComponent("project/session.jsonl").path)]) == [.claude], "Claude session modification")
            try check(scopes([event(claude.appendingPathComponent("project/settings.json").path)]).isEmpty, "Claude settings excluded")
            let structural = UInt32(kFSEventStreamEventFlagItemRenamed) | UInt32(kFSEventStreamEventFlagItemIsDir)
            try check(scopes([event(claude.appendingPathComponent("project").path, structural)]) == [.claude], "Project rename")
            try check(scopes([event(codex.path, UInt32(kFSEventStreamEventFlagItemModified) | UInt32(kFSEventStreamEventFlagItemIsDir))]).isEmpty, "Unrelated root mtime excluded")
            try check(scopes([event(codex.path, structural)]) == [.chatgpt], "Root replacement detected")
            try check(scopes([event(records.appendingPathComponent("activity/operation.json").path)]) == [.records], "Activity detected")
            try check(scopes([event(records.appendingPathComponent("preferences.json").path)]).isEmpty, "Preferences excluded")
            try check(scopes([event("", DiscoveryPathFilter.lostEvents)]) == [.chatgpt,.claude,.records], "Lost native batch catches up all monitored roots")

            let monitor = LocalChangeMonitor(roots: roots, debounceInterval: 0.07, maximumCoalescingInterval: 0.16,
                                              fallbackInterval: 60, enableNativeEvents: false)
            monitor.start(); monitor.start()
            defer { monitor.stop() }
            try check(monitor.isWatching && !monitor.usesNativeEvents, "Explicit metadata fallback starts once")
            try await pause(0.15)
            monitor.receive([event(codex.appendingPathComponent("state_5.sqlite").path)])
            try await pause(0.02)
            monitor.receive([event(claude.appendingPathComponent("project/session.jsonl").path)])
            try await pause(0.03)
            try check(monitor.change.revision == 0, "Quiet debounce must not publish prematurely")
            try await awaitChange(monitor, after: 0)
            try check(monitor.change.revision == 1 && monitor.change.scopes == [.chatgpt,.claude], "Debounce coalesces scopes into one revision")
            let initialRevision = monitor.change.revision
            for _ in 0..<14 {
                monitor.receive([event(codex.appendingPathComponent("state_5.sqlite-wal").path)])
                try await pause(0.02)
            }
            try check(monitor.change.revision > initialRevision, "Continuous streaming must not starve updates")
            try await pause(0.2)
            let stableRevision = monitor.change.revision
            try Data("synthetic credentials".utf8).write(to: codex.appendingPathComponent("auth.json"))
            try Data("synthetic settings".utf8).write(to: codex.appendingPathComponent("config.toml"))
            monitor.catchUp()
            try await pause(0.2)
            try check(monitor.change.revision == stableRevision, "Metadata fallback ignores credential/config changes")

            let database = codex.appendingPathComponent("state_5.sqlite")
            try Data("synthetic database bytes".utf8).write(to: database)
            monitor.catchUp(); monitor.catchUp()
            try await awaitChange(monitor, after: stableRevision)
            try check(monitor.change.scopes == [.chatgpt], "New Codex state metadata detected")
            try await pause(0.15)
            let historyRevision = monitor.change.revision
            try Data("synthetic history WAL".utf8).write(to: codex.appendingPathComponent("thread_history_2.sqlite-wal"))
            monitor.catchUp()
            try await awaitChange(monitor, after: historyRevision)
            try check(monitor.change.scopes == [.chatgpt], "Saved history WAL metadata detected when the state database is unchanged")
            try await pause(0.15)
            let addRevision = monitor.change.revision
            let project = claude.appendingPathComponent("project")
            try fm.createDirectory(at: project, withIntermediateDirectories: true)
            let session = project.appendingPathComponent("session.jsonl")
            try Data("synthetic session".utf8).write(to: session)
            monitor.catchUp()
            try await awaitChange(monitor, after: addRevision)
            try check(monitor.change.scopes == [.claude], "New Claude chat detected by bounded metadata fallback")
            try await pause(0.15)
            let appendedRevision = monitor.change.revision
            let writer = try FileHandle(forWritingTo: session)
            try writer.seekToEnd(); try writer.write(contentsOf: Data("\nmore synthetic content".utf8)); try writer.close()
            monitor.catchUp()
            try await awaitChange(monitor, after: appendedRevision)
            try check(monitor.change.scopes == [.claude], "Existing Claude chat update detected")
            try await pause(0.15)
            let renameRevision = monitor.change.revision
            let renamed = project.appendingPathComponent("renamed.jsonl")
            try fm.moveItem(at: session, to: renamed)
            monitor.catchUp()
            try await awaitChange(monitor, after: renameRevision)
            try check(monitor.change.scopes == [.claude], "Chat rename detected")
            try await pause(0.15)
            let removeRevision = monitor.change.revision
            try fm.removeItem(at: renamed)
            monitor.catchUp()
            try await awaitChange(monitor, after: removeRevision)
            try check(monitor.change.scopes == [.claude], "Chat removal detected")
            try await pause(0.15)
            let backupRevision = monitor.change.revision
            try fm.createDirectory(at: backups.appendingPathComponent("synthetic-backup.noindex"), withIntermediateDirectories: true)
            monitor.catchUp()
            try await awaitChange(monitor, after: backupRevision)
            try check(monitor.change.scopes == [.records], "New backup metadata detected without scanning its contents")
            try await pause(0.15)
            let stoppedRevision = monitor.change.revision
            monitor.receive([event(database.path)])
            monitor.stop()
            try await pause(0.2)
            try check(!monitor.isWatching && !monitor.usesNativeEvents && monitor.change.revision == stoppedRevision, "Stopping cancels pending delivery and native resources")
            monitor.receive([event(database.path)]); monitor.catchUp()
            try await pause(0.1)
            try check(monitor.change.revision == stoppedRevision, "Stopped monitor ignores future events/catch-up")

            let periodicRoot = temporary.appendingPathComponent("periodic")
            try fm.createDirectory(at: periodicRoot, withIntermediateDirectories: true)
            let periodic = LocalChangeMonitor(roots: [.init(url: periodicRoot, scope: .personal, kind: .claude)],
                debounceInterval: 0.03, fallbackInterval: 0.1, enableNativeEvents: false)
            periodic.start()
            try await pause(0.15)
            try Data("synthetic automatic catch-up".utf8).write(to: periodicRoot.appendingPathComponent("periodic.jsonl"))
            try await awaitChange(periodic, after: 0, label: "Periodic metadata fallback")
            try check(periodic.change.scopes == [.personal], "Fallback timer detects changes without an explicit refresh or catch-up call")
            periodic.stop()

            var lifecycle: LocalChangeMonitor? = LocalChangeMonitor(roots: roots, fallbackInterval: 0.1)
            weak let weakLifecycle = lifecycle
            lifecycle?.start()
            try await pause(0.15)
            lifecycle = nil
            try await pause(0.2)
            try check(weakLifecycle == nil, "Polling task and event relay must not retain the monitor after its owner releases it")

            let absent = temporary.appendingPathComponent("not-yet-created")
            let absentMonitor = LocalChangeMonitor(roots: [.init(url: absent, scope: .arbeit, kind: .codex)],
                debounceInterval: 0.03, fallbackInterval: 60, enableNativeEvents: false)
            absentMonitor.start()
            try await pause(0.1)
            try fm.createDirectory(at: absent, withIntermediateDirectories: true)
            absentMonitor.catchUp()
            try await awaitChange(absentMonitor, after: 0)
            try check(absentMonitor.change.scopes == [.arbeit], "Profile created after monitor launch is detected")
            absentMonitor.stop()

            let nativeRoot = temporary.appendingPathComponent("native")
            try fm.createDirectory(at: nativeRoot, withIntermediateDirectories: true)
            let native = LocalChangeMonitor(roots: [.init(url: nativeRoot, scope: .claude, kind: .claude)],
                debounceInterval: 0.03, maximumCoalescingInterval: 0.1, fallbackInterval: 60)
            native.start()
            try check(native.usesNativeEvents, "Native FSEvents stream actually starts")
            try await pause(0.4)
            let nativeCreationRevision = native.change.revision
            let nativeChat = nativeRoot.appendingPathComponent("new-session.jsonl")
            try Data("synthetic native chat".utf8).write(to: nativeChat)
            try await awaitChange(native, after: nativeCreationRevision, timeout: 5, label: "Native creation")
            try check(native.change.scopes == [.claude], "Actual filesystem creation reaches filtered native event stream")
            try await pause(0.35)
            let nativeAppendRevision = native.change.revision
            let nativeWriter = try FileHandle(forWritingTo: nativeChat)
            try nativeWriter.seekToEnd(); try nativeWriter.write(contentsOf: Data("\nnew saved message".utf8)); try nativeWriter.close()
            try await awaitChange(native, after: nativeAppendRevision, timeout: 5, label: "Native append")
            try check(native.change.scopes == [.claude], "Actual native transcript append is detected")
            try await pause(0.35)
            let nativeRenameRevision = native.change.revision
            let nativeRenamed = nativeRoot.appendingPathComponent("renamed-session.jsonl")
            try fm.moveItem(at: nativeChat, to: nativeRenamed)
            try await awaitChange(native, after: nativeRenameRevision, timeout: 5, label: "Native rename")
            try check(native.change.scopes == [.claude], "Actual native transcript rename is detected")
            try await pause(0.35)
            let nativeRemoveRevision = native.change.revision
            try fm.removeItem(at: nativeRenamed)
            try await awaitChange(native, after: nativeRemoveRevision, timeout: 5, label: "Native removal")
            try check(native.change.scopes == [.claude], "Actual native transcript removal is detected")
            try await pause(0.35)
            let nativeStableRevision = native.change.revision
            try Data("synthetic unrelated settings".utf8).write(to: nativeRoot.appendingPathComponent("settings.json"))
            try await pause(0.5)
            try check(native.change.revision == nativeStableRevision, "Actual unrelated native file event is ignored")
            native.stop()
            try check(!native.isWatching && !native.usesNativeEvents, "Native stream released on stop")
            print("{\"ok\":true,\"checks\":\(checks),\"native_fsevents\":true,\"synthetic_only\":true}")
        } catch {
            try? fm.removeItem(at: temporary)
            print("AutoDiscovery tests failed: \(error.localizedDescription)")
            exit(1)
        }
    }
}
#endif
