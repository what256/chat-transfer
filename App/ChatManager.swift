import AppKit
import Foundation
import SwiftUI

private enum TransferPanel: String, CaseIterable {
    case profiles, coding
    var title: String { self == .profiles ? "Between profiles" : "Claude Code ↔ Codex" }
}

private enum ChatProfile: String, Codable, CaseIterable, Identifiable, Sendable {
    case chatgpt, arbeit, personal
    var id: String { rawValue }
    var title: String { switch self { case .chatgpt: "ChatGPT"; case .arbeit: "Arbeit"; case .personal: "Personal" } }
    var other: ChatProfile { self == .chatgpt ? .arbeit : .chatgpt }
    var vendorURL: URL {
        URL(fileURLWithPath: self == .chatgpt ? "/Applications/ChatGPT.app" : (self == .arbeit ? "/Applications/ChatGPT Arbeit.app/Contents/Helpers/ChatGPT.app" : "/Applications/ChatGPT Personal.app/Contents/Helpers/ChatGPT.app"))
    }
    var launchURL: URL {
        URL(fileURLWithPath: self == .chatgpt ? "/Applications/ChatGPT.app" : (self == .arbeit ? "/Applications/ChatGPT Arbeit.app" : "/Applications/ChatGPT Personal.app"))
    }
    static func fromDisplayName(_ value: String) -> ChatProfile? {
        switch value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "chatgpt": .chatgpt
        case "arbeit", "chatgpt arbeit": .arbeit
        case "personal", "chatgpt personal": .personal
        default: nil
        }
    }
}

private enum ChatAction: String, Codable, Sendable {
    case sync, copy, move
    var title: String { rawValue.capitalized }
    var explanation: String {
        switch self {
        case .sync: "Updates the selected chats when their saved histories can be safely reconciled. Conflicting histories are kept for review."
        case .copy: "Copies saved messages and tool history into the destination. The source chats stay available."
        case .move: "Copies and verifies saved history, then hides the source chats in its archive. You can restore them later."
        }
    }
}

private struct ChatRow: Identifiable, Decodable, Sendable {
    var id: String
    var title: String
    var presentInBoth: Bool
    var running: Bool
    var updatedAt: Double?
    var turnCount: Int?
    var itemCount: Int?
    var archived: Bool
    var cwd: String?
    var eligibility: String

    private enum CodingKeys: String, CodingKey {
        case id, thread_id, title, name, present_in_both, other_present, running, running_or_unfinished
        case updated_at, turn_count, turns, item_count, items, archived, eligibility, cwd
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decodeIfPresent(String.self, forKey: .id) ?? c.decode(String.self, forKey: .thread_id)
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? c.decodeIfPresent(String.self, forKey: .name) ?? "Untitled chat"
        presentInBoth = try c.decodeIfPresent(Bool.self, forKey: .present_in_both) ?? c.decodeIfPresent(Bool.self, forKey: .other_present) ?? false
        running = try c.decodeIfPresent(Bool.self, forKey: .running) ?? c.decodeIfPresent(Bool.self, forKey: .running_or_unfinished) ?? false
        updatedAt = try c.decodeIfPresent(Double.self, forKey: .updated_at)
        turnCount = try c.decodeIfPresent(Int.self, forKey: .turn_count) ?? c.decodeIfPresent(Int.self, forKey: .turns)
        itemCount = try c.decodeIfPresent(Int.self, forKey: .item_count) ?? c.decodeIfPresent(Int.self, forKey: .items)
        archived = try c.decodeIfPresent(Bool.self, forKey: .archived) ?? false
        cwd = try c.decodeIfPresent(String.self, forKey: .cwd)
        eligibility = try c.decodeIfPresent(String.self, forKey: .eligibility) ?? ""
    }
}

private struct ManagerResponse: Decodable, Sendable {
    var ok: Bool
    var message: String
    var chats: [ChatRow]
    var needsClose: [ChatProfile]
    var canRestart: [ChatProfile]
    var blocked: Bool
    var preview: [OperationPreview]
    var requiresReplacement: Bool
    var selectionToken: String?
    var results: [TransferResult]

    private enum CodingKeys: String, CodingKey {
        case ok, message, chats, needsClose, needs_close, canRestart, can_restart, blocked, preview, requiresReplacement, selection_token, results
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        message = try c.decodeIfPresent(String.self, forKey: .message) ?? (ok ? "Finished" : "The manager is waiting")
        chats = try c.decodeIfPresent([ChatRow].self, forKey: .chats) ?? []
        needsClose = try c.decodeIfPresent([ChatProfile].self, forKey: .needsClose) ?? c.decodeIfPresent([ChatProfile].self, forKey: .needs_close) ?? []
        canRestart = try c.decodeIfPresent([ChatProfile].self, forKey: .canRestart) ?? c.decodeIfPresent([ChatProfile].self, forKey: .can_restart) ?? []
        blocked = try c.decodeIfPresent(Bool.self, forKey: .blocked) ?? false
        preview = try c.decodeIfPresent([OperationPreview].self, forKey: .preview) ?? []
        requiresReplacement = try c.decodeIfPresent(Bool.self, forKey: .requiresReplacement) ?? false
        selectionToken = try c.decodeIfPresent(String.self, forKey: .selection_token)
        results = try c.decodeIfPresent([TransferResult].self, forKey: .results) ?? []
    }
}

private struct OperationPreview: Identifiable, Decodable, Sendable {
    var id: String
    var title: String
    var direction: String?
    var effect: String
    var status: String?
    var message: String?
}

private struct TransferResult: Decodable, Identifiable, Sendable {
    var id: String
    var origin: String?
    var target: String?
    var verified: Bool?
}

private struct ManagerRequest: Codable, Sendable {
    var command: String
    var operation: ChatAction?
    var source: ChatProfile
    var ids: [String]
    var replace_existing: Bool = false
    var destination: ChatProfile? = nil
    var selection_token: String? = nil
}

private struct ManagerFailure: Error, Sendable { var message: String }

private func helperScript(_ name:String)->URL {
    if let bundled=Bundle.main.url(forResource:name,withExtension:"py",subdirectory:"Backend") { return bundled }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Arbeit/chat-manager",isDirectory:true)
        .appendingPathComponent(name + ".py")
}


private func managerCall(_ request: ManagerRequest, helperURL: URL? = nil) async throws -> ManagerResponse {
    let body = try JSONEncoder().encode(request)
    return try await withCheckedThrowingContinuation { continuation in
        DispatchQueue.global(qos: .utility).async {
            let process = Process()
            let input = Pipe()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = [(helperURL ?? helperScript("manager")).path]
            process.standardInput = input
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            var environment = ProcessInfo.processInfo.environment
            for key in environment.keys where key.hasPrefix("CODEX_") || key == "ELECTRON_RUN_AS_NODE" {
                environment.removeValue(forKey: key)
            }
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            do {
                try process.run()
                try input.fileHandleForWriting.write(contentsOf: body)
                try input.fileHandleForWriting.close()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                guard data.count < 16 * 1024 * 1024 else {
                    throw ManagerFailure(message: "The chat list is larger than the manager can display.")
                }
                let response = try JSONDecoder().decode(ManagerResponse.self, from: data)
                // Structured expected rejections may use a nonzero exit status.
                // Their message and required-close data are still reviewable.
                continuation.resume(returning: response)
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }
}

@MainActor
private final class ChatManagerModel: ObservableObject {
    @Published var panel: TransferPanel = .profiles
    @Published var source: ChatProfile = .chatgpt
    @Published var destination: ChatProfile = .arbeit
    @Published var stage = "Reading chats"
    @Published var results: [TransferResult] = []
    @Published var inventoryLoaded = false
    @Published var query = ""
    @Published var showArchived = false
    @Published var chats: [ChatRow] = []
    @Published var selected: Set<String> = []
    @Published var busy = false
    @Published var message = "Choose chats to move, copy, or sync."
    @Published var action: ChatAction?
    @Published var showPreview = false
    @Published var awaitingClose: [ChatProfile] = []
    @Published var restartable: [ChatProfile] = []
    @Published var finished = false
    @Published var blocked = false
    @Published var operationChats: [ChatRow] = []
    @Published var previewRows: [OperationPreview] = []
    @Published var requiresReplacement = false
    @Published var replaceExisting = false
    private var operationSource: ChatProfile = .chatgpt
    private var operationDestination: ChatProfile = .arbeit
    private var reviewedToken: String?
    private var operationIDs: [String] = []

    var visibleChats: [ChatRow] {
        let inventory = chats.filter { showArchived || !$0.archived }
        guard !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return inventory }
        return inventory.filter { $0.title.localizedStandardContains(query) }
    }
    var selectedChats: [ChatRow] { chats.filter { selected.contains($0.id) && (showArchived || !$0.archived) } }
    var canReload: Bool { !awaitingClose.isEmpty && Set(awaitingClose).isSubset(of: Set(restartable)) }
    var canExecute: Bool { !blocked && (!requiresReplacement || replaceExisting) }

    func refresh() async {
        guard !busy else { return }
        busy = true
        stage = "Reading chats"
        inventoryLoaded = false
        defer { busy = false }
        do {
            let response = try await managerCall(ManagerRequest(command: "list", operation: nil, source: source, ids: [], destination: destination))
            guard response.ok else { message = response.message; return }
            chats = response.chats
            inventoryLoaded = true
            selected.formIntersection(Set(chats.map(\.id)))
            message = response.message
        } catch {
            message = (error as? ManagerFailure)?.message ?? "Could not read chats. Check that the manager helper is installed."
        }
    }

    func preview(_ chosenAction: ChatAction) async {
        guard !busy, !selectedChats.isEmpty else { return }
        action = chosenAction
        operationSource = source
        operationDestination = destination
        reviewedToken = nil
        results = []
        operationIDs = selectedChats.map(\.id)
        operationChats = selectedChats
        previewRows = []
        awaitingClose = []
        restartable = []
        finished = false
        blocked = false
        requiresReplacement = false
        replaceExisting = false
        showPreview = true
        await preparePreview()
    }

    func preparePreview() async {
        guard !busy, let action else { return }
        busy = true
        stage = "Checking saved histories"
        defer { busy = false }
        do {
            let response = try await managerCall(ManagerRequest(command: "prepare", operation: action, source: operationSource,
                                                              ids: operationIDs, replace_existing: replaceExisting, destination: operationDestination))
            reviewedToken = response.selectionToken
            apply(response)
        } catch {
            blocked = true
            message = "Could not prepare this transfer. Refresh the chat list and retry."
        }
    }

    private func apply(_ response: ManagerResponse) {
        message = response.message
        awaitingClose = response.needsClose
        restartable = response.canRestart
        blocked = response.blocked || (!response.ok && response.needsClose.isEmpty)
        requiresReplacement = response.requiresReplacement || replaceExisting
        if !response.preview.isEmpty { previewRows = response.preview }
    }

    func execute(reload: Bool = false) async {
        guard !busy, let action, !operationIDs.isEmpty, let reviewedToken else { return }
        busy = true
        defer { busy = false }
        var closedProfiles: [ChatProfile] = []
        do {
            let preparation = ManagerRequest(command: "prepare", operation: action, source: operationSource,
                                             ids: operationIDs, replace_existing: replaceExisting, destination: operationDestination)
            stage = "Checking the reviewed transfer"
            let response = try await managerCall(preparation)
            apply(response)
            guard response.selectionToken == reviewedToken else {
                blocked = true
                message = "The saved history changed after review. Review this transfer again."
                return
            }
            guard canExecute else { return }
            if !response.needsClose.isEmpty {
                guard reload, Set(response.needsClose).isSubset(of: Set(response.canRestart)) else { return }
            }
            stage = "Reloading idle apps"
            for profile in response.needsClose {
                guard managerIsForeground() else {
                    throw ManagerFailure(message: "Reload paused because you switched to another app. Return to Chat Manager and retry.")
                }
                let immediatelyBeforeQuit = try await managerCall(preparation)
                apply(immediatelyBeforeQuit)
                guard immediatelyBeforeQuit.selectionToken == reviewedToken else {
                    message = "The saved history changed before reload. Review the transfer again."
                    blocked = true
                    await reopen(closedProfiles)
                    return
                }
                if !immediatelyBeforeQuit.needsClose.contains(profile) { continue }
                guard immediatelyBeforeQuit.selectionToken == reviewedToken,
                      !immediatelyBeforeQuit.blocked, !immediatelyBeforeQuit.requiresReplacement,
                      immediatelyBeforeQuit.canRestart.contains(profile) else {
                    await reopen(closedProfiles)
                    return
                }
                let applications = NSWorkspace.shared.runningApplications.filter {
                    $0.bundleURL?.standardizedFileURL.path == profile.vendorURL.path
                }
                for app in applications {
                    guard managerIsForeground() else {
                        throw ManagerFailure(message: "Reload paused because you switched to another app. Return to Chat Manager and retry.")
                    }
                    guard app.terminate() else {
                        throw ManagerFailure(message: "\(profile.title) could not quit. Quit it manually and retry.")
                    }
                }
                if !applications.isEmpty { closedProfiles.append(profile) }
                let deadline = Date().addingTimeInterval(15)
                while Date() < deadline && profileRunning(profile) {
                    try await Task.sleep(for: .milliseconds(250))
                }
                guard !profileRunning(profile) else {
                    throw ManagerFailure(message: "\(profile.title) is still open. Quit it manually and retry.")
                }
            }
            guard managerIsForeground() else {
                throw ManagerFailure(message: "Transfer paused because you switched apps. Return to Chat Manager and retry.")
            }
            stage = "Saving and verifying histories"
            let completed = try await managerCall(ManagerRequest(command: "execute", operation: action,
                                                                source: operationSource, ids: operationIDs,
                                                                replace_existing: replaceExisting, destination: operationDestination, selection_token: reviewedToken))
            apply(completed)
            results = completed.results
            finished = completed.ok
            if completed.ok {
                let destinations = previewRows.compactMap { row in
                    row.direction?.components(separatedBy: "→").last.flatMap { ChatProfile.fromDisplayName($0) }
                }
                let requested = destinations.isEmpty ? [operationDestination] : destinations
                await reopen(Array(Set(closedProfiles + requested)))
            } else { await reopen(closedProfiles) }
            closedProfiles = []
            if completed.ok { await refreshAfterOperation() }
        } catch {
            message = (error as? ManagerFailure)?.message ?? "The transfer stopped. Both saved histories and private backups are available."
            await reopen(closedProfiles)
        }
    }

    private func refreshAfterOperation() async {
        if let response = try? await managerCall(ManagerRequest(command: "list", operation: nil, source: source, ids: [], destination: destination)), response.ok {
            chats = response.chats
            selected.formIntersection(Set(chats.map(\.id)))
        }
    }

    private func managerIsForeground() -> Bool {
        NSWorkspace.shared.frontmostApplication?.processIdentifier == ProcessInfo.processInfo.processIdentifier
    }
    private func profileRunning(_ profile: ChatProfile) -> Bool {
        NSWorkspace.shared.runningApplications.contains { $0.bundleURL?.standardizedFileURL.path == profile.vendorURL.path }
    }
    private func reopen(_ profiles: [ChatProfile]) async {
        guard managerIsForeground() else { return }
        for profile in profiles {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.allowsRunningApplicationSubstitution = false
            configuration.createsNewApplicationInstance = false
            configuration.activates = false
            _ = try? await withCheckedThrowingContinuation { (continuation: CheckedContinuation<NSRunningApplication, Error>) in
                NSWorkspace.shared.openApplication(at: profile.launchURL, configuration: configuration) { app, error in
                    if let error { continuation.resume(throwing: error) }
                    else if let app { continuation.resume(returning: app) }
                    else { continuation.resume(throwing: ManagerFailure(message: "The app could not reopen.")) }
                }
            }
        }
    }
}

private enum BridgeEndpoint: String, Codable, CaseIterable, Identifiable, Sendable {
    case claude, chatgpt, arbeit, personal
    var id: String { rawValue }
    var title: String {
        switch self {
        case .claude: "Claude Code"
        case .chatgpt: "ChatGPT"
        case .arbeit: "Arbeit"
        case .personal: "Personal"
        }
    }
    var symbol: String { self == .claude ? "terminal" : "sparkle" }
    var color: Color { .primary }
}

private struct BridgeChat: Identifiable, Decodable, Sendable {
    var id: String
    var title: String
    var cwd: String?
    var updated_at: Double?
    var archived: Bool? = nil
    var running: Bool? = nil
}

private struct BridgeRequest: Encodable, Sendable {
    var command: String
    var source: BridgeEndpoint
    var destination: BridgeEndpoint
    var ids: [String]
    var selection_token: String?
}

private struct BridgeResponse: Decodable, Sendable {
    var ok: Bool
    var message: String
    var chats: [BridgeChat]?
    var preview: [BridgeChat]?
    var summary: String?
    var direction: String?
    var warnings: [String]?
    var output_directory: String?
    var launch_file: String?
    var imported_ids: [String]?
    var selection_token: String?
    var counts: HistoryCounts?
}

private func bridgeCall(_ request: BridgeRequest, helperURL: URL? = nil) async throws -> BridgeResponse {
    let body = try JSONEncoder().encode(request)
    return try await withCheckedThrowingContinuation { continuation in
        DispatchQueue.global(qos: .utility).async {
            let process = Process()
            let input = Pipe()
            let output = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = [(helperURL ?? helperScript("bridge")).path]
            process.standardInput = input
            process.standardOutput = output
            process.standardError = FileHandle.nullDevice
            var environment = ProcessInfo.processInfo.environment
            for key in environment.keys where key.hasPrefix("CODEX_") || key == "ELECTRON_RUN_AS_NODE" {
                environment.removeValue(forKey: key)
            }
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            do {
                try process.run()
                try input.fileHandleForWriting.write(contentsOf: body)
                try input.fileHandleForWriting.close()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                guard data.count < 16 * 1024 * 1024 else {
                    throw ManagerFailure(message: "The session list is larger than the app can display.")
                }
                continuation.resume(returning: try JSONDecoder().decode(BridgeResponse.self, from: data))
            } catch { continuation.resume(throwing: error) }
        }
    }
}

@MainActor
private final class BridgeModel: ObservableObject {
    @Published var source: BridgeEndpoint = .claude
    @Published var stage = "Reading sessions"
    @Published var inventoryLoaded = false
    @Published var destination: BridgeEndpoint = .arbeit
    @Published var chats: [BridgeChat] = []
    @Published var selected: Set<String> = []
    @Published var query = ""
    @Published var busy = false
    @Published var message = "Choose saved coding sessions to continue in the other app."
    @Published var showPreview = false
    @Published var response: BridgeResponse?
    @Published var finished = false
    private var preparedRequest: BridgeRequest?

    var visibleChats: [BridgeChat] {
        chats.filter { query.isEmpty || $0.title.localizedStandardContains(query) || ($0.cwd?.localizedStandardContains(query) ?? false) }
    }
    var selectedChats: [BridgeChat] { chats.filter { selected.contains($0.id) } }
    var destinations: [BridgeEndpoint] { source == .claude ? [.chatgpt, .arbeit, .personal] : [.claude] }

    func sourceChanged() async {
        if !destinations.contains(destination) { destination = destinations[0] }
        selected = []
        chats = []
        await refresh()
    }
    func refresh() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            inventoryLoaded = false
            let result = try await bridgeCall(BridgeRequest(command: "bridge_list", source: source,
                                                          destination: destination, ids: [], selection_token: nil))
            message = result.message
            if result.ok {
                chats = result.chats ?? []
                inventoryLoaded = true
                selected.formIntersection(Set(chats.map(\.id)))
            }
        } catch { message = "Could not read saved sessions. Refresh to try again." }
    }
    func toggle(_ chat: BridgeChat, selected value: Bool) {
        if !value { selected.remove(chat.id) }
        else if source == .claude {
            if selected.count < 50 { selected.insert(chat.id) }
            else { message = "Choose up to 50 sessions for one import." }
        }
        else { selected = [chat.id] }
    }
    func prepare() async {
        guard !busy, !selectedChats.isEmpty else { return }
        busy = true
        stage = "Checking selected sessions"
        finished = false
        response = nil
        preparedRequest = nil
        showPreview = true
        defer { busy = false }
        do {
            var request = BridgeRequest(command: "bridge_prepare", source: source, destination: destination,
                                        ids: selectedChats.map(\.id), selection_token: nil)
            let result = try await bridgeCall(request)
            response = result
            message = result.message
            if result.ok, let token = result.selection_token {
                request.command = "bridge_execute"
                request.selection_token = token
                preparedRequest = request
            }
        } catch { message = "Could not prepare this transfer. Refresh and try again." }
    }
    func execute() async {
        guard !busy, let request = preparedRequest, response?.ok == true else { return }
        busy = true
        stage = source == .claude ? "Importing and verifying sessions" : "Saving the continuation"
        defer { busy = false }
        do {
            let result = try await bridgeCall(request)
            response = result
            message = result.message
            finished = result.ok
            if result.ok { preparedRequest = nil }
        } catch { message = "The transfer could not finish. The source session remains available." }
    }
    var canExecute: Bool { preparedRequest != nil && response?.ok == true }

    private func privateOutputURL(_ path: String) -> URL? {
        let root = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Arbeit/chat-manager", isDirectory: true)
            .resolvingSymlinksInPath().path + "/"
        let url = URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath()
        return url.path.hasPrefix(root) && FileManager.default.fileExists(atPath: url.path) ? url : nil
    }
    func reviewTranscript() {
        guard let directory = response?.output_directory,
              let url = privateOutputURL(directory + "/transcript.md") else { return }
        NSWorkspace.shared.open(url)
    }
    func revealOutput() {
        guard let directory = response?.output_directory, let url = privateOutputURL(directory) else { return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
    func openClaudeCode() {
        guard finished, let path = response?.launch_file, path.hasSuffix(".command"),
              let url = privateOutputURL(path) else { return }
        if !NSWorkspace.shared.open(url) { message = "Could not open the Claude Code launcher. Use Show files to open it in Terminal." }
    }
    func openDestination() {
        guard finished, destination != .claude else { return }
        let path = ChatProfile(rawValue: destination.rawValue)?.launchURL.path ?? "/Applications/ChatGPT.app"
        let config = NSWorkspace.OpenConfiguration()
        config.allowsRunningApplicationSubstitution = false
        NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: path), configuration: config) { _, _ in }
    }
}

private let transferAccent = Color(white: 0.88)
private let transferSurface = Color(white: 0.105)

private extension BridgeEndpoint {
    var shortTitle: String { title }
    var profile: ChatProfile? { ChatProfile(rawValue: rawValue) }
    var tint: Color {
        transferAccent
    }
    var icon: String { self == .claude ? "terminal" : (self == .arbeit ? "briefcase" : (self == .personal ? "person.crop.circle" : "bubble.left.and.bubble.right")) }
    var subtitle: String { self == .claude ? "Coding sessions" : "Codex profile" }
}
private struct LibrarySession: Identifiable {
    var id: String
    var title: String
    var cwd: String?
    var updated: Double?
    var archived: Bool
    var running: Bool
    var present: Bool
    var eligibility: String
    var project: String { cwd.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "No project folder" }
}
private struct HistoryMessage: Decodable, Identifiable, Sendable {
    var role: String
    var text: String
    var id: String { role + ":" + text }
}
private struct HistoryCounts: Decodable, Sendable {
    var messages: Int?
    var text_bytes: Int?
    var attachments_omitted: Int?
}
private struct SessionDetail: Decodable, Sendable {
    var id: String
    var source: String
    var title: String
    var cwd: String?
    var updated_at: Double?
    var archived: Bool?
    var running: Bool?
    var messages: [HistoryMessage]
    var counts: HistoryCounts?
    var truncated: Bool?
    var note: String?
}
private struct ActivityItem: Decodable, Identifiable, Sendable {
    var id: String
    var title: String
    var status: String
    var verified: Bool
}
private struct ActivityRecord: Decodable, Identifiable, Sendable {
    var id: String
    var created_at: Double
    var source: String
    var destination: String
    var action: String
    var title: String
    var status: String
    var message: String
    var items: [ActivityItem]
    var paths: [String]
}
private struct BackupRecord: Decodable, Identifiable, Sendable {
    var id: String
    var title: String
    var kind: String
    var created_at: Double
    var path: String
    var bytes: Int?
}
private struct LibraryResponse: Decodable, Sendable {
    var ok: Bool
    var message: String
    var detail: SessionDetail?
    var activities: [ActivityRecord]?
    var backups: [BackupRecord]?
}
private struct LibraryRequest: Encodable, Sendable {
    var command: String
    var source: String? = nil
    var id: String? = nil
}
private func libraryCall(_ request: LibraryRequest, helperURL: URL? = nil) async throws -> LibraryResponse {
    let body = try JSONEncoder().encode(request)
    return try await withCheckedThrowingContinuation { continuation in
        DispatchQueue.global(qos: .utility).async {
            let process = Process(), input = Pipe(), output = Pipe()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            process.arguments = [(helperURL ?? helperScript("library")).path]
            process.standardInput = input; process.standardOutput = output; process.standardError = FileHandle.nullDevice
            var environment = ProcessInfo.processInfo.environment
            for key in environment.keys where key.hasPrefix("CODEX_") || key == "ELECTRON_RUN_AS_NODE" { environment.removeValue(forKey: key) }
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            process.environment = environment
            do {
                try process.run(); try input.fileHandleForWriting.write(contentsOf: body); try input.fileHandleForWriting.close()
                let data = output.fileHandleForReading.readDataToEndOfFile(); process.waitUntilExit()
                guard data.count < 16 * 1024 * 1024 else { throw ManagerFailure(message: "This preview is too large to display.") }
                continuation.resume(returning: try JSONDecoder().decode(LibraryResponse.self, from: data))
            } catch { continuation.resume(throwing: error) }
        }
    }
}

@MainActor
private final class WorkspaceModel: ObservableObject {
    @Published var source: BridgeEndpoint = .chatgpt
    @Published var destination: BridgeEndpoint = .arbeit
    @Published var section = "library"
    @Published var query = ""
    @Published var project = "all"
    @Published var visibility = "active"
    @Published var sort = "recent"
    @Published var starredOnly = false
    @Published var selectionOnly = false
    @Published var selected: Set<String> = []
    @Published var focused: String?
    @Published var action: ChatAction = .copy
    @Published var detail: SessionDetail?
    @Published var detailBusy = false
    @Published var detailMessage = "Choose a chat to see its saved messages."
    @Published var activities: [ActivityRecord] = []
    @Published var backups: [BackupRecord] = []
    @Published var recordsBusy = false
    @Published var recordsMessage = ""
    @Published var starred: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "StarredChats") ?? [])
    @Published var notice = ""
    @Published var automaticDetectionEnabled = UserDefaults.standard.object(forKey:"AutomaticChatDetection") as? Bool ?? true
    @Published var automaticUpdating = false
    @Published var automaticNote = ""
    private var pendingDiscoveryScopes: Set<DiscoveryScope> = []
    private var automaticTask: Task<Void,Never>?
    private var lastAutomaticRefresh = Date.distantPast
    private var automaticGeneration: UInt64 = 0
    private var automaticRetryCount = 0
    private var detailGeneration: UInt64 = 0
    var isProfileRoute: Bool { source != .claude && destination != .claude }
    var destinations: [BridgeEndpoint] { BridgeEndpoint.allCases.filter { $0 != source && !(source == .claude && $0 == .claude) } }
    var selectionLimit: Int { source != .claude && destination == .claude ? 1 : 50 }
    var fidelity: String { isProfileRoute ? "Saved history" : (source == .claude ? "Native import" : "Saved context") }
    var routeNote: String {
        if isProfileRoute { return action == .move ? "Verified history arrives in the destination. The source chat moves to its archive." : (action == .sync ? "Updates the side with older history when a verified baseline supports it. Conflicts stay available for review." : "Copies saved messages and recorded tool history. Your source stays available.") }
        return source == .claude ? "Imports the selected Claude Code sessions into Codex. Source sessions stay available." : "Creates a new Claude Code session with saved user and assistant messages as reference context."
    }
    var routeFootnote: String {
        if isProfileRoute { return "Private backups are kept. Open apps may need to reload when idle." }
        return source == .claude ? "Credentials, settings and connections stay separate." : "Live tools, hidden reasoning and checkpoints stay in Codex. Preparing sends no model turn."
    }
    func sessions(_ manager: ChatManagerModel, _ bridge: BridgeModel) -> [LibrarySession] {
        if source == .claude {
            return bridge.chats.map { LibrarySession(id: $0.id, title: $0.title, cwd: $0.cwd, updated: $0.updated_at, archived: $0.archived ?? false, running: $0.running ?? false, present: false, eligibility: "") }
        }
        return manager.chats.map { LibrarySession(id: $0.id, title: $0.title, cwd: $0.cwd, updated: $0.updatedAt, archived: $0.archived, running: $0.running, present: $0.presentInBoth, eligibility: $0.eligibility) }
    }
    func visible(_ sessions: [LibrarySession]) -> [LibrarySession] {
        sessions.filter { row in
            let archiveMatch = visibility == "all" || (visibility == "archived" ? row.archived : !row.archived)
            return archiveMatch && (query.isEmpty || row.title.localizedStandardContains(query) || (row.cwd?.localizedStandardContains(query) ?? false))
                && (project == "all" || row.cwd == project) && (!starredOnly || isStarred(row.id)) && (!selectionOnly || selected.contains(row.id))
        }.sorted { a,b in sort == "title" ? a.title.localizedStandardCompare(b.title) == .orderedAscending : (a.updated ?? 0) > (b.updated ?? 0) }
    }
    func isStarred(_ id: String) -> Bool { starred.contains(source.rawValue + ":" + id) }
    func star(_ id: String) {
        let key = source.rawValue + ":" + id
        if starred.contains(key) { starred.remove(key) } else { starred.insert(key) }
        UserDefaults.standard.set(Array(starred).sorted(), forKey: "StarredChats")
    }
    func toggle(_ id: String) {
        if selected.contains(id) { selected.remove(id) }
        else if selectionLimit == 1 { selected = [id] }
        else if selected.count < selectionLimit { selected.insert(id) }
        else { notice = "Choose up to \(selectionLimit) chats for one transfer." }
    }
    func selectVisible(_ rows: [LibrarySession]) {
        selected = Set(rows.prefix(selectionLimit).map(\.id))
        notice = rows.count > selectionLimit ? "Selected the first \(selectionLimit) visible chats." : ""
    }
    func changeSource(_ endpoint: BridgeEndpoint, manager: ChatManagerModel, bridge: BridgeModel) async {
        source = endpoint; section = "library"; selected = []; focused = nil; detail = nil; detailBusy = false;detailGeneration += 1
        query = ""; project = "all"; selectionOnly = false; starredOnly = false; notice = ""
        manager.chats = []; bridge.chats = []
        if destination == source { destination = source == .arbeit ? .chatgpt : .arbeit }
        await refresh(manager, bridge)
    }
    func refresh(_ manager: ChatManagerModel, _ bridge: BridgeModel) async {
        guard !manager.busy && !bridge.busy else { return }
        if source == .claude {
            bridge.source = .claude; bridge.destination = destination
            await bridge.refresh()
            if !bridge.inventoryLoaded { bridge.chats = []; notice = bridge.message }
        } else if let profile = source.profile {
            manager.source = profile
            manager.destination = destination.profile ?? profile.other
            await manager.refresh()
            if !manager.inventoryLoaded { manager.chats = []; notice = manager.message }
        }
        selected.formIntersection(Set(sessions(manager,bridge).map(\.id)))
        if let focused, !sessions(manager,bridge).contains(where: { $0.id == focused }) { self.focused = nil; detail = nil }
    }
    func changeDestination(_ manager: ChatManagerModel, _ bridge: BridgeModel) async {
        notice = ""; manager.showPreview = false; bridge.showPreview = false
        if selectionLimit == 1, selected.count > 1 {
            let first = sessions(manager,bridge).first { selected.contains($0.id) }
            selected = Set(first.map { [$0.id] } ?? [])
            notice = "Claude continuations use one chat and its project folder at a time."
        }
        await refresh(manager, bridge)
    }
    func review(_ manager: ChatManagerModel, _ bridge: BridgeModel) async {
        guard !selected.isEmpty && !manager.busy && !bridge.busy else { return }
        if isProfileRoute, let source = source.profile, let target = destination.profile {
            manager.source = source; manager.destination = target; manager.selected = selected; manager.showArchived = true
            await manager.preview(action)
        } else {
            bridge.source = source; bridge.destination = destination
            bridge.chats = sessions(manager,bridge).map { BridgeChat(id: $0.id, title: $0.title, cwd: $0.cwd, updated_at: $0.updated, archived: $0.archived, running: $0.running) }
            bridge.selected = selected
            await bridge.prepare()
        }
    }
    func loadDetail(preserveCurrent:Bool=false) async {
        guard let focused else { detail = nil; detailBusy=false;detailGeneration += 1;return }
        let endpoint = source
        detailGeneration += 1
        let generation=detailGeneration
        detailBusy = true; if !preserveCurrent { detail = nil }; detailMessage = "Reading saved messages…"
        defer { if self.focused == focused && source == endpoint && generation == detailGeneration { detailBusy=false } }
        do {
            let result = try await libraryCall(LibraryRequest(command: "details", source: endpoint.rawValue, id: focused))
            guard self.focused == focused && source == endpoint && generation == detailGeneration && !Task.isCancelled else { return }
            if result.ok || !preserveCurrent { detail = result.detail }; detailMessage = result.message
        } catch {
            guard self.focused == focused && source == endpoint && generation == detailGeneration && !Task.isCancelled else { return }
            detailMessage = "Could not read this preview. Try refreshing the library."
        }
    }
    func loadRecords(expectedAutomaticGeneration:UInt64?=nil) async {
        guard !recordsBusy else { return }
        recordsBusy = true; defer { recordsBusy = false }
        do {
            async let activity = libraryCall(LibraryRequest(command: "activity"))
            async let recovery = libraryCall(LibraryRequest(command: "backups"))
            let (a,b) = try await (activity,recovery)
            guard !Task.isCancelled else { return }
            if let expectedAutomaticGeneration,expectedAutomaticGeneration != automaticGeneration || !automaticDetectionEnabled { return }
            activities = a.activities ?? []; backups = b.backups ?? []; recordsMessage = !a.ok ? a.message : (!b.ok ? b.message : "")
        } catch {
            guard !Task.isCancelled else { return }
            if let expectedAutomaticGeneration,expectedAutomaticGeneration != automaticGeneration || !automaticDetectionEnabled { return }
            recordsMessage = "Could not read saved activity. Refresh to try again."
        }
    }
    func requestAutomaticRefresh(_ scopes:Set<DiscoveryScope>, manager:ChatManagerModel, bridge:BridgeModel) {
        guard automaticDetectionEnabled else { return }
        pendingDiscoveryScopes.formUnion(scopes)
        automaticRetryCount=0
        resumeAutomaticRefresh(manager:manager,bridge:bridge)
    }
    func automaticChangesAllowed(manager:ChatManagerModel,bridge:BridgeModel,isActive:Bool)->Bool {
        automaticDetectionEnabled && isActive && !manager.busy && !bridge.busy && !manager.showPreview && !bridge.showPreview
    }
    func resumeAutomaticRefresh(manager:ChatManagerModel,bridge:BridgeModel) {
        guard automaticDetectionEnabled,!pendingDiscoveryScopes.isEmpty,automaticTask == nil,!automaticUpdating else { return }
        guard automaticChangesAllowed(manager:manager,bridge:bridge,isActive:NSApplication.shared.isActive) else { return }
        let interval=min(30.0,3.0 * pow(2.0,Double(automaticRetryCount)))
        let delay=max(0.35,interval-Date().timeIntervalSince(lastAutomaticRefresh))
        let generation=automaticGeneration
        automaticTask=Task { [weak self,weak manager,weak bridge] in
            guard let self,let manager,let bridge else { return }
            defer {
                if generation == self.automaticGeneration {
                    self.automaticTask=nil
                }
                self.resumeAutomaticRefresh(manager:manager,bridge:bridge)
            }
            do { try await Task.sleep(for:.seconds(delay)) } catch { return }
            guard !Task.isCancelled,generation == self.automaticGeneration,
                  self.automaticChangesAllowed(manager:manager,bridge:bridge,isActive:NSApplication.shared.isActive) else { return }
            await self.refreshDetectedChanges(manager:manager,bridge:bridge,generation:generation)
        }
    }
    func stopAutomaticRefresh() {
        automaticGeneration += 1
        automaticTask?.cancel();automaticTask=nil
        pendingDiscoveryScopes=[];automaticNote="";automaticRetryCount=0
    }
    @discardableResult
    func applyAutomaticInventory(_ response:ManagerResponse,source expectedSource:BridgeEndpoint,destination expectedDestination:BridgeEndpoint,manager:ChatManagerModel,bridge:BridgeModel,isActive:Bool)->Bool {
        guard response.ok,source == expectedSource,destination == expectedDestination,
              automaticChangesAllowed(manager:manager,bridge:bridge,isActive:isActive) else { return false }
        manager.chats=response.chats;manager.inventoryLoaded=true
        selected.formIntersection(Set(response.chats.map(\.id)))
        if let focused,!response.chats.contains(where:{ $0.id == focused }) { self.focused=nil;detail=nil;detailBusy=false;detailGeneration += 1 }
        return true
    }
    private func refreshDetectedChanges(manager:ChatManagerModel,bridge:BridgeModel,generation:UInt64) async {
        let scopes=pendingDiscoveryScopes
        pendingDiscoveryScopes=[]
        let expectedSource=source,expectedDestination=destination
        let sourceScope=DiscoveryScope(rawValue:source.rawValue)
        let targetScope=DiscoveryScope(rawValue:destination.rawValue)
        let inventoryChanged=scopes.contains(where:{ $0 == sourceScope || $0 == targetScope })
        automaticUpdating=true;lastAutomaticRefresh=Date()
        defer { automaticUpdating=false }
        if inventoryChanged {
            do {
                if expectedSource == .claude {
                    let result=try await bridgeCall(BridgeRequest(command:"bridge_list",source:expectedSource,destination:expectedDestination,ids:[],selection_token:nil))
                    guard result.ok,source == expectedSource,destination == expectedDestination,
                          automaticChangesAllowed(manager:manager,bridge:bridge,isActive:NSApplication.shared.isActive),!Task.isCancelled,generation == automaticGeneration else {
                        if !Task.isCancelled,generation == automaticGeneration { pendingDiscoveryScopes.formUnion(scopes);automaticRetryCount=min(4,automaticRetryCount+1);automaticNote="Updates are waiting. Saved chats stay available." };return
                    }
                    bridge.chats=result.chats ?? [];bridge.inventoryLoaded=true
                    selected.formIntersection(Set(bridge.chats.map(\.id)))
                    if let focused,!bridge.chats.contains(where:{ $0.id == focused }) { self.focused=nil;detail=nil;detailBusy=false;detailGeneration += 1 }
                } else if let profile=expectedSource.profile {
                    let result=try await managerCall(ManagerRequest(command:"list",operation:nil,source:profile,ids:[],destination:expectedDestination.profile ?? profile.other))
                    guard !Task.isCancelled,generation == automaticGeneration,applyAutomaticInventory(result,source:expectedSource,destination:expectedDestination,manager:manager,bridge:bridge,isActive:NSApplication.shared.isActive) else {
                        if !Task.isCancelled,generation == automaticGeneration { pendingDiscoveryScopes.formUnion(scopes);automaticRetryCount=min(4,automaticRetryCount+1);automaticNote="Updates are waiting. Saved chats stay available." };return
                    }
                }
                automaticNote="";automaticRetryCount=0
                if focused != nil,scopes.contains(where:{ $0 == sourceScope }) {
                    if detailBusy,let sourceScope { pendingDiscoveryScopes.insert(sourceScope) }
                    else { await loadDetail(preserveCurrent:true) }
                }
            } catch {
                if !Task.isCancelled,generation == automaticGeneration { pendingDiscoveryScopes.formUnion(scopes);automaticRetryCount=min(4,automaticRetryCount+1);automaticNote="Updates are delayed; detection will retry." }
            }
        }
        guard !Task.isCancelled,generation == automaticGeneration else { return }
        if scopes.contains(.records) {
            if recordsBusy { pendingDiscoveryScopes.insert(.records) }
            else { await loadRecords(expectedAutomaticGeneration:generation) }
        }
    }

    func reveal(_ path: String) {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let roots = ["Library/Application Support/Arbeit/chat-manager", "Library/Application Support/Arbeit/maintenance-backups", "Library/Application Support/Arbeit/chat-handoff", "Library/Application Support/ChatGPT Copy/maintenance-backups"].map { home.appendingPathComponent($0).standardizedFileURL.resolvingSymlinksInPath().path + "/" }
        let url = URL(fileURLWithPath: path).standardizedFileURL
        guard url.path == url.resolvingSymlinksInPath().path, roots.contains(where: { url.path.hasPrefix($0) }), FileManager.default.fileExists(atPath: url.path) else { notice = "This saved file is no longer available."; return }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
}

@MainActor
private func saveInterfacePreview() {
    guard let view=NSApp.keyWindow?.contentView,
          let bitmap=view.bitmapImageRepForCachingDisplay(in:view.bounds) else { return }
    view.cacheDisplay(in:view.bounds,to:bitmap)
    guard let data=bitmap.representation(using:.png,properties:[:]) else { return }
    let directory=FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Arbeit/chat-manager",isDirectory:true)
    let path=directory.appendingPathComponent("interface-preview.png")
    do { try data.write(to:path,options:.atomic); try FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:path.path) }
    catch { return }
}

private struct WorkspacePill: View {
    var text: String
    var color: Color = .secondary
    var body: some View { Text(text).font(.system(size: 10, weight: .medium)).foregroundStyle(color).padding(.horizontal,7).padding(.vertical,3).background(color.opacity(0.10),in:Capsule()) }
}
private struct MonochromePrimaryStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled
    func makeBody(configuration:Configuration)->some View {
        configuration.label
            .foregroundStyle(enabled ? Color.black : Color.white.opacity(0.35))
            .padding(.horizontal,12).padding(.vertical,10)
            .background(enabled ? Color(white:configuration.isPressed ? 0.73 : 0.92) : Color.white.opacity(0.045),in:RoundedRectangle(cornerRadius:9))
    }
}
private struct ChatManagerView: View {
    @EnvironmentObject private var model: ChatManagerModel
    @EnvironmentObject private var bridge: BridgeModel
    @EnvironmentObject private var workspace: WorkspaceModel
    @StateObject private var discovery = LocalChangeMonitor()
    @FocusState private var searchFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showingSearch = false
    @State private var hoveredHeader: String?
    @State private var selectedExpanded = false
    private var busy: Bool { model.busy || bridge.busy }
    private var sessions: [LibrarySession] { workspace.sessions(model,bridge) }
    private var visible: [LibrarySession] { workspace.visible(sessions) }
    private var selectedRows: [LibrarySession] { sessions.filter { workspace.selected.contains($0.id) } }
    private var hiddenSelection: Int { workspace.selected.subtracting(Set(visible.map(\.id))).count }

    var body: some View {
        VStack(spacing:0) {
            topBar.padding(.top,12).padding(.bottom,10)
            HStack(spacing:0) {
                sidebar.frame(width:144)
                Divider().opacity(0.55)
                if workspace.section == "library" {
                    library.frame(maxWidth:.infinity,maxHeight:.infinity)
                    Divider().opacity(0.55)
                    inspector.frame(width:280)
                } else { records.frame(maxWidth:.infinity,maxHeight:.infinity) }
            }
        }
        .frame(minWidth:960,minHeight:560)
        .background(Color(white:0.075))
        .preferredColorScheme(.dark).tint(transferAccent)
         .task {
            if workspace.automaticDetectionEnabled { discovery.start() }
            await workspace.refresh(model,bridge);await workspace.loadRecords()
            workspace.resumeAutomaticRefresh(manager:model,bridge:bridge)
        }
        .onDisappear { discovery.stop();workspace.stopAutomaticRefresh() }
        .onReceive(discovery.$change) { change in
            guard change.revision > 0 else { return }
            workspace.requestAutomaticRefresh(change.scopes,manager:model,bridge:bridge)
        }
        .onReceive(NotificationCenter.default.publisher(for:NSApplication.didBecomeActiveNotification)) { _ in
            if workspace.automaticDetectionEnabled {
                discovery.catchUp()
                workspace.requestAutomaticRefresh(Set(DiscoveryScope.allCases),manager:model,bridge:bridge)
            }
        }
        .onReceive(NSWorkspace.shared.notificationCenter.publisher(for:NSWorkspace.didWakeNotification)) { _ in
            if workspace.automaticDetectionEnabled { discovery.catchUp();workspace.requestAutomaticRefresh(Set(DiscoveryScope.allCases),manager:model,bridge:bridge) }
        }
        .onChange(of:workspace.automaticDetectionEnabled) { enabled in
            UserDefaults.standard.set(enabled,forKey:"AutomaticChatDetection")
            if enabled { discovery.start();workspace.requestAutomaticRefresh(Set(DiscoveryScope.allCases),manager:model,bridge:bridge) }
            else { discovery.stop();workspace.stopAutomaticRefresh() }
        }
        .onChange(of:model.busy) { busy in if !busy { workspace.resumeAutomaticRefresh(manager:model,bridge:bridge) } }
        .onChange(of:bridge.busy) { busy in if !busy { workspace.resumeAutomaticRefresh(manager:model,bridge:bridge) } }
        .onChange(of:model.showPreview) { shown in if !shown { discovery.catchUp();workspace.resumeAutomaticRefresh(manager:model,bridge:bridge) } }
        .onChange(of:bridge.showPreview) { shown in if !shown { discovery.catchUp();workspace.resumeAutomaticRefresh(manager:model,bridge:bridge) } }
        .onChange(of:workspace.recordsBusy) { busy in if !busy { workspace.resumeAutomaticRefresh(manager:model,bridge:bridge) } }
        .onChange(of:workspace.detailBusy) { busy in if !busy { workspace.resumeAutomaticRefresh(manager:model,bridge:bridge) } }
        .onChange(of:workspace.destination) { _ in Task { await workspace.changeDestination(model,bridge) } }
        .onChange(of:workspace.focused) { _ in Task { await workspace.loadDetail() } }
        .onChange(of:model.finished) { finished in if finished { Task { await workspace.loadRecords() } } }
        .onChange(of:bridge.finished) { finished in if finished { Task { await workspace.loadRecords() } } }
        .onChange(of:workspace.section) { value in if value != "library" { Task { await workspace.loadRecords() } } }
        .onReceive(NotificationCenter.default.publisher(for:Notification.Name("ChatTransferFind"))) { _ in beginSearch() }
        .onReceive(NotificationCenter.default.publisher(for:Notification.Name("ChatTransferRefresh"))) { _ in Task { await refresh() } }
        .onReceive(NotificationCenter.default.publisher(for:Notification.Name("ChatTransferSelect"))) { _ in if !busy && workspace.section == "library" { workspace.selectVisible(visible) } }
        .onReceive(NotificationCenter.default.publisher(for:Notification.Name("ChatTransferClear"))) { _ in if !busy { workspace.selected=[] } }
        .onReceive(NotificationCenter.default.publisher(for:Notification.Name("ChatTransferToggle"))) { _ in if !busy,let focused=workspace.focused { workspace.toggle(focused) } }
        .sheet(isPresented:$model.showPreview) { ActionPreview().environmentObject(model) }
        .sheet(isPresented:$bridge.showPreview) { BridgePreview().environmentObject(bridge) }
    }
    private func refresh() async {
        if workspace.section == "library" { await workspace.refresh(model,bridge) } else { await workspace.loadRecords() }
    }
    private func beginSearch() {
        workspace.section="library"
        if showingSearch { searchFocused=true } else { showingSearch=true }
    }
    private var topBar: some View {
        HStack(spacing:4) {
            navigationButton("library",title:"Library",symbol:"bubble.left.and.bubble.right",shortcut:"1")
            navigationButton("activity",title:"Activity",symbol:"clock.arrow.circlepath",shortcut:"2")
            navigationButton("backups",title:"Backups",symbol:"externaldrive",shortcut:"3")
            Rectangle().fill(Color.primary.opacity(0.12)).frame(width:1,height:18).padding(.horizontal,5)
            Menu {
                Button("Select visible chats") { workspace.selectVisible(visible) }.disabled(busy || workspace.section != "library" || visible.isEmpty)
                Button("Clear selection") { workspace.selected=[] }.disabled(busy || workspace.selected.isEmpty)
                Divider()
                Button("Show selected chats") { workspace.section="library"; workspace.selectionOnly.toggle() }
                Button("Show starred chats") { workspace.section="library"; workspace.starredOnly.toggle() }
                Divider()
                Toggle("Automatic detection",isOn:$workspace.automaticDetectionEnabled)
                Button("Refresh") { Task { await refresh() } }.disabled(busy || workspace.recordsBusy)
                Button("Find a chat") { beginSearch() }
            } label: { Image(systemName:"plus").font(.system(size:13,weight:.semibold)).foregroundStyle(.primary) }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).frame(width:34,height:34)
                .background(Color.primary.opacity(hoveredHeader == "actions" ? 0.13 : 0.065),in:Circle())
                .contentShape(Circle()).accessibilityLabel("Chat actions").help("Chat actions")
                .onHover { hoveredHeader=$0 ? "actions" : nil }
                .animation(reduceMotion ? nil : .easeOut(duration:0.12),value:hoveredHeader)
            Button { beginSearch() } label: { Image(systemName:"magnifyingglass").font(.system(size:12,weight:.medium)).frame(width:34,height:34) }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .background(Color.primary.opacity(hoveredHeader == "find" ? 0.10 : 0),in:Circle())
                .onHover { hoveredHeader=$0 ? "find" : nil }.accessibilityLabel("Find a chat").help("Find (⌘F)")
            if showingSearch && workspace.section == "library" {
                HStack(spacing:6) {
                    TextField("Find a chat or project",text:$workspace.query).textFieldStyle(.plain).font(.system(size:12)).focused($searchFocused)
                        .task { await Task.yield(); searchFocused=true }
                }.padding(.horizontal,9).frame(width:210,height:30).background(Color.primary.opacity(0.05),in:Capsule())
                Button { workspace.query=""; showingSearch=false; searchFocused=false } label: {
                    Image(systemName:"xmark").font(.system(size:10,weight:.semibold)).frame(width:28,height:28).contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel("Close search")
            }
        }
        .padding(6).fixedSize()
        .background(.regularMaterial,in:Capsule())
        .overlay { Capsule().strokeBorder(Color.primary.opacity(0.12),lineWidth:1) }
        .shadow(color:.black.opacity(0.14),radius:12,y:5)
        .frame(maxWidth:.infinity)
    }
    private func navigationButton(_ target:String,title:String,symbol:String,shortcut:KeyEquivalent)->some View {
        let selected=workspace.section == target
        return Button { workspace.section=target } label: {
            Label(title,systemImage:symbol)
                .font(.system(size:12,weight:selected ? .semibold : .medium))
                .padding(.horizontal,12).frame(height:34)
                .foregroundStyle(selected ? Color.primary : Color.secondary)
                .background(selected ? Color.primary.opacity(0.10) : (hoveredHeader == target ? Color.primary.opacity(0.055) : .clear),in:Capsule())
        }.buttonStyle(.plain).keyboardShortcut(shortcut,modifiers:.command).disabled(busy)
            .onHover { hoveredHeader=$0 ? target : nil }
            .animation(reduceMotion ? nil : .easeOut(duration:0.12),value:hoveredHeader)
            .accessibilityAddTraits(selected ? .isSelected : [])
    }
    private var sidebar: some View {
        VStack(alignment:.leading,spacing:3) {
            Text("Sources").font(.system(size:11,weight:.medium)).foregroundStyle(.secondary).padding(.horizontal,10).padding(.top,16).padding(.bottom,8)
            ForEach([BridgeEndpoint.chatgpt,.arbeit,.personal,.claude]) { endpoint in
                Button { Task { await workspace.changeSource(endpoint,manager:model,bridge:bridge) } } label: {
                    HStack(spacing:8) {
                        Image(systemName:endpoint.icon).font(.system(size:13)).frame(width:19)
                        Text(endpoint.title).font(.system(size:12,weight:workspace.source == endpoint ? .medium : .regular)).lineLimit(1)
                        Spacer(minLength:0)
                    }.padding(.horizontal,10).frame(height:34)
                        .foregroundStyle(workspace.source == endpoint ? Color.primary : Color.secondary)
                        .background(workspace.source == endpoint ? Color.white.opacity(0.07) : .clear,in:RoundedRectangle(cornerRadius:7))
                }.buttonStyle(.plain).disabled(busy).accessibilityLabel("Library: \(endpoint.title)")
            }
            Spacer()
            HStack(spacing:5) {
                Circle().fill(Color.white.opacity(workspace.automaticDetectionEnabled ? 0.6 : 0.2)).frame(width:4,height:4)
                Text(workspace.automaticDetectionEnabled ? "Live · on this Mac" : "Detection paused").font(.system(size:10))
            }.foregroundStyle(.secondary).help(workspace.automaticNote.isEmpty ? "Detects saved changes locally. Transfers require review." : workspace.automaticNote).padding(.horizontal,10).padding(.bottom,14)
        }.padding(.horizontal,8).frame(maxHeight:.infinity).background(Color(white:0.075))
    }
    private var library: some View {
        VStack(alignment:.leading,spacing:0) {
            HStack(alignment:.firstTextBaseline) {
                Text(workspace.source.title).font(.system(size:17,weight:.semibold))
                Spacer()
                Text("\(visible.count) of \(sessions.count) chats").font(.system(size:11)).foregroundStyle(.secondary)
            }.padding(.horizontal,18).padding(.top,17).padding(.bottom,13)
            HStack(spacing:12) {
                Menu { Picker("Visibility",selection:$workspace.visibility) { Text("Active chats").tag("active");Text("Archived chats").tag("archived");Text("All chats").tag("all") } } label: {
                    Label(workspace.visibility == "active" ? "Active" : (workspace.visibility == "archived" ? "Archived" : "All chats"),systemImage:"line.3.horizontal.decrease")
                }.menuStyle(.borderlessButton).fixedSize()
                Menu {
                    Button("All projects") { workspace.project="all" }
                    ForEach(Array(Set(sessions.compactMap(\.cwd))).sorted(),id:\.self) { path in Button(URL(fileURLWithPath:path).lastPathComponent) { workspace.project=path } }
                } label: { Text(workspace.project == "all" ? "All projects" : URL(fileURLWithPath:workspace.project).lastPathComponent).lineLimit(1) }
                    .menuStyle(.borderlessButton).frame(maxWidth:130)
                Spacer(minLength:0)
                Button { workspace.starredOnly.toggle() } label: { Image(systemName:workspace.starredOnly ? "star.fill" : "star") }.buttonStyle(.plain).foregroundStyle(workspace.starredOnly ? .primary : .secondary).help("Show starred chats").accessibilityLabel("Filter starred chats")
                Menu { Picker("Sort",selection:$workspace.sort) { Text("Recently updated").tag("recent");Text("Title A–Z").tag("title") } } label: { Image(systemName:"arrow.up.arrow.down") }.menuStyle(.borderlessButton).fixedSize().help("Sort chats")
            }.font(.system(size:11)).foregroundStyle(.secondary).padding(.horizontal,18).padding(.bottom,12).disabled(busy)
            Divider().opacity(0.5)
            if visible.isEmpty {
                VStack(spacing:10) {
                    if busy { ProgressView();Text("Reading saved chats…").font(.callout).foregroundStyle(.secondary) }
                    else {
                        Image(systemName:"text.bubble").font(.system(size:25)).foregroundStyle(.tertiary)
                        Text(sessions.isEmpty ? "No saved chats" : "No matching chats").font(.system(size:13,weight:.medium))
                        Button("Reset filters") { workspace.query="";workspace.project="all";workspace.visibility="all";workspace.starredOnly=false;workspace.selectionOnly=false }.font(.system(size:11))
                    }
                }.frame(maxWidth:.infinity,maxHeight:.infinity)
            } else {
                List(selection:$workspace.focused) {
                    ForEach(visible) { row in sessionRow(row).tag(row.id).listRowSeparator(.hidden).listRowBackground(workspace.selected.contains(row.id) ? Color.white.opacity(0.045) : .clear) }
                }.listStyle(.inset).scrollContentBackground(.hidden).disabled(busy)
            }
            Divider().opacity(0.5)
            HStack(spacing:7) {
                Button { workspace.selectionOnly.toggle() } label: { Text("\(workspace.selected.count) selected").font(.system(size:11,weight:.medium)) }.buttonStyle(.plain).help("Show selected chats; up to \(workspace.selectionLimit)")
                if hiddenSelection > 0 { Text("· \(hiddenSelection) outside filters").font(.system(size:10)).foregroundStyle(.secondary) }
                Spacer(minLength:0)
                Button("Select visible") { workspace.selectVisible(visible) }.buttonStyle(.borderless).font(.system(size:10)).disabled(busy || visible.isEmpty)
                Button("Clear") { workspace.selected=[] }.buttonStyle(.borderless).font(.system(size:10)).disabled(busy || workspace.selected.isEmpty)
            }.padding(.horizontal,18).padding(.vertical,12)
        }.background(transferSurface)
    }
    private func sessionRow(_ row:LibrarySession)->some View {
        HStack(spacing:10) {
            Toggle("Select \(row.title)",isOn:Binding(get:{ workspace.selected.contains(row.id) },set:{ _ in workspace.toggle(row.id) })).toggleStyle(.checkbox).labelsHidden().accessibilityLabel("Select \(row.title)")
            VStack(alignment:.leading,spacing:4) {
                HStack(spacing:5) { Text(row.title).font(.system(size:12,weight:.medium)).lineLimit(1); if workspace.isStarred(row.id) { Image(systemName:"star.fill").font(.system(size:9)).foregroundStyle(.secondary) } }
                HStack(spacing:5) { Text(row.project).lineLimit(1); if row.archived { Text("· Archived") }; if row.running { Label("Unfinished",systemImage:"circle.dashed").font(.system(size:10)) } }.font(.system(size:11)).foregroundStyle(.secondary)
            }
            Spacer(minLength:4)
            if row.present && workspace.isProfileRoute { Image(systemName:"checkmark.circle").font(.system(size:11)).foregroundStyle(.secondary).help("Also saved in \(workspace.destination.title)") }
            if let updated=row.updated { Text(Date(timeIntervalSince1970:updated),format:.dateTime.month(.abbreviated).day()).font(.system(size:10)).foregroundStyle(.tertiary) }
        }.padding(.vertical,6).contentShape(Rectangle())
            .contextMenu { Button(workspace.isStarred(row.id) ? "Remove star" : "Star chat") { workspace.star(row.id) };Button(workspace.selected.contains(row.id) ? "Remove from transfer" : "Add to transfer") { workspace.toggle(row.id) };if let cwd=row.cwd { Button("Show project folder") { NSWorkspace.shared.selectFile(nil,inFileViewerRootedAtPath:cwd) } } }
    }
    private var shortRouteNote: String {
        if !workspace.isProfileRoute { return workspace.source == .claude ? "Import saved sessions. Originals stay available." : "A new Claude Code session with saved text as context." }
        switch workspace.action { case .copy:return "The source stays available.";case .move:return "Archives the source after verification.";case .sync:return "Reconciles saved history; conflicts need review." }
    }
    private var inspector: some View {
        VStack(alignment:.leading,spacing:0) {
            ScrollView {
                VStack(alignment:.leading,spacing:13) {
                    Text("Transfer to").font(.system(size:13,weight:.medium))
                    Picker("Destination",selection:$workspace.destination) { ForEach(workspace.destinations) { endpoint in Label(endpoint.title,systemImage:endpoint.icon).tag(endpoint) } }.labelsHidden().controlSize(.regular).disabled(busy).accessibilityLabel("Transfer destination")
                    if workspace.isProfileRoute { Picker("Transfer action",selection:$workspace.action) { Text("Copy").tag(ChatAction.copy);Text("Move").tag(ChatAction.move);Text("Sync").tag(ChatAction.sync) }.pickerStyle(.segmented).labelsHidden().disabled(busy) }
                    Text(shortRouteNote).font(.system(size:11)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true).lineSpacing(2)
                    if !selectedRows.isEmpty {
                        DisclosureGroup(isExpanded:$selectedExpanded) {
                            VStack(alignment:.leading,spacing:7) { ForEach(selectedRows) { row in Text(row.title).font(.system(size:11)).lineLimit(1) } }.padding(.top,6)
                        } label: { Text("\(selectedRows.count) chat\(selectedRows.count == 1 ? "" : "s") selected").font(.system(size:11,weight:.medium)) }
                    }
                    Divider().opacity(0.5).padding(.vertical,2)
                    detailView
                }.padding(17)
            }
            VStack(alignment:.leading,spacing:10) {
                if busy { HStack(spacing:8) { ProgressView().controlSize(.small);Text(model.busy ? model.stage : bridge.stage).font(.system(size:10)) } }
                else if !workspace.notice.isEmpty { Label(workspace.notice,systemImage:"info.circle").font(.system(size:10)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true) }
                Button { Task { await workspace.review(model,bridge) } } label: { HStack { Text("Review transfer");Spacer();Image(systemName:"arrow.right") }.font(.system(size:12,weight:.semibold)) }.buttonStyle(MonochromePrimaryStyle()).disabled(busy || workspace.selected.isEmpty).keyboardShortcut(.defaultAction)
                Text("Review before anything transfers.").font(.system(size:10)).foregroundStyle(.tertiary)
            }.padding(16)
        }.background(Color(white:0.09))
    }
    @ViewBuilder private var detailView: some View {
        if let focused=workspace.focused,let row=sessions.first(where:{ $0.id == focused }) {
            VStack(alignment:.leading,spacing:11) {
                HStack { Text(row.title).font(.system(size:12,weight:.medium)).fixedSize(horizontal:false,vertical:true);Spacer(minLength:6);Button { workspace.star(focused) } label: { Image(systemName:workspace.isStarred(focused) ? "star.fill" : "star") }.buttonStyle(.plain).foregroundStyle(.secondary).accessibilityLabel("Star selected chat") }
                if let cwd=row.cwd { Text((cwd as NSString).abbreviatingWithTildeInPath).font(.system(size:10)).foregroundStyle(.tertiary).lineLimit(2).help(cwd) }
                if !row.eligibility.isEmpty { Label(row.eligibility,systemImage:"info.circle").font(.system(size:10)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true) }
                if workspace.detailBusy { ProgressView("Reading messages…").font(.system(size:10)) }
                else if let detail=workspace.detail {
                    if let count=detail.counts?.messages { Text("\(count) saved messages").font(.system(size:10)).foregroundStyle(.secondary) }
                    ForEach(Array(detail.messages.enumerated()),id:\.offset) { _,message in
                        VStack(alignment:.leading,spacing:5) {
                            Text(message.role == "user" ? "You" : "Assistant").font(.system(size:10,weight:.semibold)).foregroundStyle(.secondary)
                            Text(message.text).font(.system(size:11)).lineSpacing(3).textSelection(.enabled).fixedSize(horizontal:false,vertical:true)
                        }.padding(.vertical,9).frame(maxWidth:.infinity,alignment:.leading)
                        Divider().opacity(0.4)
                    }
                    if let note=detail.note { Text(note).font(.system(size:10)).foregroundStyle(.tertiary).fixedSize(horizontal:false,vertical:true) }
                } else { Text(workspace.detailMessage).font(.system(size:11)).foregroundStyle(.secondary) }
            }
        } else {
            VStack(alignment:.leading,spacing:7) { Text("Chat preview").font(.system(size:12,weight:.medium));Text("Click a chat to read its saved messages.").font(.system(size:11)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true) }.padding(.vertical,2)
        }
    }
    private var records: some View {
        VStack(alignment:.leading,spacing:16) {
            VStack(alignment:.leading,spacing:5) {
                Text(workspace.section == "activity" ? "Activity" : "Backups & files").font(.system(size:18,weight:.semibold))
                Text(workspace.section == "activity" ? "Transfer results and saved recovery files." : "Private backups and saved continuations on this Mac.").font(.system(size:11)).foregroundStyle(.secondary)
            }
            if workspace.recordsBusy { ProgressView().controlSize(.small) }
            if !workspace.recordsMessage.isEmpty { Text(workspace.recordsMessage).font(.system(size:11)).foregroundStyle(.secondary) }
            ScrollView {
                LazyVStack(alignment:.leading,spacing:0) {
                    if workspace.section == "activity" {
                        if workspace.activities.isEmpty { recordEmpty("No transfers yet",body:"Reviewed transfers and their results appear here.",symbol:"clock.arrow.circlepath") }
                        ForEach(workspace.activities) { record in activityRow(record);Divider().opacity(0.45) }
                    } else {
                        Text("Show files in Finder to inspect them. Restoring a database requires review because it can affect other chats.").font(.system(size:11)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true).padding(.bottom,16)
                        if workspace.backups.isEmpty { recordEmpty("No saved files",body:"Backups and prepared continuations appear here.",symbol:"externaldrive") }
                        ForEach(workspace.backups) { backup in
                            HStack(spacing:12) {
                                Image(systemName:backup.kind == "continuation" ? "doc.text" : "externaldrive").font(.system(size:17)).foregroundStyle(.secondary).frame(width:28)
                                VStack(alignment:.leading,spacing:5) { Text(backupTitle(backup)).font(.system(size:12,weight:.medium));Text(backup.kind == "app-backup" ? "App backup" : (backup.kind == "continuation" ? "Claude Code continuation" : "Saved chat history")).font(.system(size:10)).foregroundStyle(.tertiary).help(backup.path) }
                                Spacer();Text(Date(timeIntervalSince1970:backup.created_at),format:.dateTime.month(.abbreviated).day().hour().minute()).font(.system(size:10)).foregroundStyle(.secondary);Button("Show in Finder") { workspace.reveal(backup.path) }.controlSize(.small)
                            }.padding(.vertical,16)
                            Divider().opacity(0.45)
                        }
                    }
                }
            }
        }.padding(24).background(transferSurface)
    }
    private func backupTitle(_ backup:BackupRecord)->String {
        let names=[("chat-transfer-v31", "Chat Transfer 3.1"), ("chat-transfer-v3", "Chat Transfer 3.0"), ("chat-transfer-v2", "Chat Transfer 2.0"), ("chat-transfer-v1", "Chat Transfer 1.0"), ("settings-repair", "Arbeit settings"), ("launcher-routing", "Arbeit launcher"), ("payload-upgrade", "App update")]
        if let known=names.first(where:{ backup.title.contains($0.0) }) { return known.1 }
        return backup.kind == "chat-backup" ? "Chat recovery" : backup.title
    }
    private func activityRow(_ record:ActivityRecord)->some View {
        VStack(alignment:.leading,spacing:10) {
            HStack { Text(record.title).font(.system(size:13,weight:.medium));Spacer();Label(record.status.capitalized,systemImage:record.status == "completed" ? "checkmark.circle" : "exclamationmark.circle").font(.system(size:10)).foregroundStyle(.secondary) }
            HStack(spacing:8) { Text(BridgeEndpoint(rawValue:record.source)?.title ?? record.source);Image(systemName:"arrow.right");Text(BridgeEndpoint(rawValue:record.destination)?.title ?? record.destination);Spacer();Text(Date(timeIntervalSince1970:record.created_at),format:.dateTime.month(.abbreviated).day().hour().minute()) }.font(.system(size:11)).foregroundStyle(.secondary)
            Text(record.message).font(.system(size:11)).textSelection(.enabled)
            ForEach(record.items) { item in HStack(spacing:7) { Image(systemName:item.verified ? "checkmark.circle" : "circle.dashed");Text(item.title);Spacer();Text(item.status.capitalized) }.font(.system(size:11)).foregroundStyle(.secondary) }
            if !record.paths.isEmpty { HStack { ForEach(Array(record.paths.prefix(3).enumerated()),id:\.offset) { index,path in Button(index == 0 ? "Show saved files" : "Show backup \(index)") { workspace.reveal(path) }.controlSize(.small) } } }
            if record.status != "completed" { Button("Review again") { if let source=BridgeEndpoint(rawValue:record.source),let destination=BridgeEndpoint(rawValue:record.destination) { Task { await workspace.changeSource(source,manager:model,bridge:bridge);workspace.destination=destination;if let action=ChatAction(rawValue:record.action) { workspace.action=action };workspace.selected=Set(record.items.map(\.id));workspace.notice="Review current histories before retrying." } } }.controlSize(.small) }
        }.padding(.vertical,18).frame(maxWidth:.infinity,alignment:.leading)
    }
    private func recordEmpty(_ title:String,body:String,symbol:String)->some View {
        VStack(spacing:10) { Image(systemName:symbol).font(.system(size:25)).foregroundStyle(.tertiary);Text(title).font(.system(size:13,weight:.medium));Text(body).font(.system(size:11)).foregroundStyle(.secondary).multilineTextAlignment(.center) }.frame(maxWidth:.infinity).padding(.vertical,70)
    }
}

private struct BridgePreview: View {
    @EnvironmentObject private var model: BridgeModel
    var body: some View {
        VStack(alignment:.leading,spacing:18) {
            HStack(spacing:13) {
                Image(systemName:model.finished ? "checkmark.circle.fill" : "arrow.left.arrow.right.circle").font(.system(size:32)).foregroundStyle(transferAccent)
                VStack(alignment:.leading,spacing:5) { Text(model.finished ? "Ready to continue" : "Review transfer").font(.title2.weight(.semibold)); Text(model.response?.direction ?? "\(model.source.title) → \(model.destination.title)").font(.callout).foregroundStyle(.secondary) }
            }
            if model.busy { HStack(spacing:10) { ProgressView().controlSize(.small); Text(model.stage).font(.callout) }.padding(12).frame(maxWidth:.infinity,alignment:.leading).background(transferAccent.opacity(0.07),in:RoundedRectangle(cornerRadius:12)) }
            if let summary=model.response?.summary { Text(summary).font(.system(size:14,weight:.medium)).fixedSize(horizontal:false,vertical:true) }
            if let counts=model.response?.counts, let messages=counts.messages {
                HStack(spacing:10) { WorkspacePill(text:"\(messages) saved messages",color:transferAccent); if let bytes=counts.text_bytes { WorkspacePill(text:ByteCountFormatter.string(fromByteCount:Int64(bytes),countStyle:.file)); }; if let omitted=counts.attachments_omitted, omitted > 0 { WorkspacePill(text:"\(omitted) attachment references",color:.secondary) } }
            }
            ScrollView {
                VStack(alignment:.leading,spacing:12) {
                    ForEach(model.response?.preview ?? model.selectedChats) { chat in
                        HStack(alignment:.top,spacing:10) { Image(systemName:model.finished ? "checkmark.circle" : "text.bubble").foregroundStyle(transferAccent); VStack(alignment:.leading,spacing:5) { Text(chat.title).font(.callout.weight(.medium)); if let cwd=chat.cwd { Text((cwd as NSString).abbreviatingWithTildeInPath).font(.caption).foregroundStyle(.secondary) } } }.padding(12).frame(maxWidth:.infinity,alignment:.leading).background(Color.primary.opacity(0.035),in:RoundedRectangle(cornerRadius:12))
                    }
                    ForEach(Array((model.response?.warnings ?? []).enumerated()),id:\.offset) { _,warning in Label(warning,systemImage:"info.circle").font(.system(size:11)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true).lineSpacing(3) }
                }
            }.frame(maxHeight:300)
            Text(model.message).font(.callout).textSelection(.enabled).fixedSize(horizontal:false,vertical:true)
            if model.finished, model.response?.launch_file != nil {
                HStack { Button("Review transcript") { model.reviewTranscript() }; Button("Show files") { model.revealOutput() } }
                Text("Claude Code opens in Terminal and waits for your next message.").font(.caption).foregroundStyle(.secondary)
            }
            Divider()
            HStack {
                Button(model.finished ? "Done" : "Cancel") { model.showPreview=false }.keyboardShortcut(.cancelAction).disabled(model.busy)
                Spacer()
                if model.finished {
                    if model.response?.launch_file != nil { Button("Open Claude Code") { model.openClaudeCode() }.buttonStyle(MonochromePrimaryStyle()) }
                    else { Button("Open destination") { model.openDestination() }.buttonStyle(MonochromePrimaryStyle()) }
                } else if model.canExecute {
                    Button(model.source == .claude ? "Import selected sessions" : "Create continuation") { Task { await model.execute() } }.buttonStyle(MonochromePrimaryStyle()).disabled(model.busy).keyboardShortcut(.defaultAction)
                } else if !model.busy { Button("Review again") { Task { await model.prepare() } } }
            }
        }.padding(26).frame(width:620).background(transferSurface).preferredColorScheme(.dark).tint(transferAccent).interactiveDismissDisabled(model.busy)
    }
}
private struct ActionPreview: View {
    @EnvironmentObject private var model: ChatManagerModel
    var body: some View {
        VStack(alignment:.leading,spacing:17) {
            HStack(spacing:13) {
                Image(systemName:model.finished ? "checkmark.circle.fill" : "arrow.left.arrow.right.circle").font(.system(size:32)).foregroundStyle(transferAccent)
                VStack(alignment:.leading,spacing:5) { Text(model.finished ? "Transfer verified" : "Review \(model.action?.title.lowercased() ?? "transfer")").font(.title2.weight(.semibold)); Text("\(model.source.title) → \(model.destination.title) · \(model.operationChats.count) selected").font(.callout).foregroundStyle(.secondary) }
            }
            if let action=model.action { Text(action.explanation).font(.system(size:12)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true).lineSpacing(3) }
            if model.busy { HStack(spacing:10) { ProgressView().controlSize(.small); Text(model.stage).font(.callout) }.padding(12).frame(maxWidth:.infinity,alignment:.leading).background(transferAccent.opacity(0.07),in:RoundedRectangle(cornerRadius:12)) }
            ScrollView {
                VStack(alignment:.leading,spacing:10) {
                    ForEach(model.previewRows) { row in
                        HStack(alignment:.top,spacing:10) {
                            Image(systemName:model.finished ? "checkmark.circle" : (row.status == "blocked" ? "exclamationmark.circle" : "text.bubble")).foregroundStyle(row.status == "blocked" ? .secondary : transferAccent)
                            VStack(alignment:.leading,spacing:5) {
                                Text(row.title).font(.callout.weight(.medium))
                                if let direction=row.direction { Text(direction).font(.caption).foregroundStyle(.secondary) }
                                if let message=row.message, !message.isEmpty { Text(message).font(.system(size:11)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true) }
                                if model.results.contains(where:{ $0.id == row.id && $0.verified == true }) { Label("Verified in destination",systemImage:"checkmark.shield").font(.caption).foregroundStyle(transferAccent) }
                            }
                        }.padding(12).frame(maxWidth:.infinity,alignment:.leading).background(Color.primary.opacity(0.035),in:RoundedRectangle(cornerRadius:12))
                    }
                }
            }.frame(maxHeight:240)
            if model.requiresReplacement, model.action != .sync {
                Toggle("Replace different destination history and keep a backup",isOn:$model.replaceExisting).toggleStyle(.checkbox).disabled(model.busy).onChange(of:model.replaceExisting) { _ in Task { await model.preparePreview() } }
            }
            if !model.awaitingClose.isEmpty {
                VStack(alignment:.leading,spacing:7) {
                    Label("Reload needed: \(model.awaitingClose.map(\.title).joined(separator:", "))",systemImage:"arrow.clockwise").font(.callout.weight(.medium))
                    Text(model.canReload ? "The manager can close and reopen the required idle apps. Active tasks are never interrupted." : "The required app has unfinished work or cannot be reloaded safely. Stop its tasks and quit it yourself, then review again.").font(.system(size:11)).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
                }.padding(13).frame(maxWidth:.infinity,alignment:.leading).background(Color.white.opacity(0.045),in:RoundedRectangle(cornerRadius:12))
            }
            Text(model.message).font(.callout).textSelection(.enabled).fixedSize(horizontal:false,vertical:true)
            Divider()
            HStack {
                Button(model.finished ? "Done" : "Cancel") { model.showPreview=false }.keyboardShortcut(.cancelAction).disabled(model.busy)
                Spacer()
                if !model.finished {
                    if model.blocked || !model.awaitingClose.isEmpty {
                        Button("Review again") { Task { await model.preparePreview() } }.disabled(model.busy)
                        if !model.awaitingClose.isEmpty { Button("Reload idle apps & transfer") { Task { await model.execute(reload:true) } }.buttonStyle(MonochromePrimaryStyle()).disabled(model.busy || !model.canExecute || !model.canReload) }
                    } else { Button("\(model.action?.title ?? "Transfer") selected chats") { Task { await model.execute() } }.buttonStyle(MonochromePrimaryStyle()).keyboardShortcut(.defaultAction).disabled(model.busy || !model.canExecute) }
                }
            }
        }.padding(26).frame(width:620).background(transferSurface).preferredColorScheme(.dark).tint(transferAccent).interactiveDismissDisabled(model.busy)
    }
}

@MainActor
private final class ManagerAppDelegate: NSObject, NSApplicationDelegate {
    weak var model: ChatManagerModel?
    weak var bridge: BridgeModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if model?.busy == true || bridge?.busy == true {
            let alert = NSAlert()
            alert.messageText = "Finish the chat transfer first"
            alert.informativeText = "The manager is saving or verifying history. You can quit when it finishes."
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return .terminateCancel
        }
        return .terminateNow
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

#if !CHAT_MANAGER_PROTOCOL_TESTS
@main
#endif
private struct ChatManagerApp: App {
    @NSApplicationDelegateAdaptor(ManagerAppDelegate.self) private var delegate
    @StateObject private var model = ChatManagerModel()
    @StateObject private var bridge = BridgeModel()
    @StateObject private var workspace = WorkspaceModel()
    var body: some Scene {
        WindowGroup("Chat Transfer") {
            ChatManagerView().environmentObject(model).environmentObject(bridge).environmentObject(workspace)
                .onAppear { delegate.model = model; delegate.bridge = bridge }
        }
        .defaultSize(width: 1080, height: 680)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(replacing: .help) {
                Button("Save interface preview") { saveInterfacePreview() }
            }
            CommandMenu("Chats") {
                Button("Refresh") {
                    NotificationCenter.default.post(name: Notification.Name("ChatTransferRefresh"), object: nil)
                }.keyboardShortcut("r", modifiers: .command).disabled(model.busy || bridge.busy)
                Button("Find") { NotificationCenter.default.post(name: Notification.Name("ChatTransferFind"), object: nil) }
                    .keyboardShortcut("f", modifiers: .command)
                Button("Select visible chats") { NotificationCenter.default.post(name: Notification.Name("ChatTransferSelect"), object: nil) }.keyboardShortcut("a", modifiers: .command).disabled(model.busy || bridge.busy)
                Button("Toggle focused chat") { NotificationCenter.default.post(name: Notification.Name("ChatTransferToggle"), object: nil) }.keyboardShortcut(" ", modifiers: []).disabled(model.busy || bridge.busy)
                Button("Clear selection") { NotificationCenter.default.post(name: Notification.Name("ChatTransferClear"), object: nil) }.keyboardShortcut(.escape, modifiers: []).disabled(model.busy || bridge.busy)

            }
        }
    }
}

#if CHAT_MANAGER_PROTOCOL_TESTS
@main
private struct ManagerProtocolTests {
    @MainActor static func main() async {
        var checks = 0
        func check(_ condition: Bool) throws {
            guard condition else { throw ManagerFailure(message: "Protocol check failed.") }
            checks += 1
        }
        do {
            let decoder = JSONDecoder()
            let list = try decoder.decode(ManagerResponse.self, from: Data("""
                {"ok":true,"message":"Read 2 chats","chats":[
                  {"id":"one","title":"First chat","present_in_both":true,"running":false,"updated_at":123},
                  {"id":"two","title":"Second chat","present_in_both":false,"running":true,"updated_at":124}
                ]}
                """.utf8))
            try check(list.ok && list.chats.count == 2)
            try check(list.chats[0].presentInBoth && !list.chats[0].running)
            try check(!list.chats[1].presentInBoth && list.chats[1].running)
            let pending = try decoder.decode(ManagerResponse.self, from: Data("""
                {"ok":false,"message":"Close the destination first","needsClose":["arbeit"],
                 "canRestart":["arbeit"],"blocked":false,"preview":[
                   {"id":"one","title":"First chat","direction":"chatgpt→arbeit",
                    "effect":"copy","status":"ready","message":"Saved history can transfer"}
                 ]}
                """.utf8))
            try check(pending.needsClose == [.arbeit] && pending.canRestart == [.arbeit])
            try check(!pending.blocked && pending.preview[0].direction == "chatgpt→arbeit")
            let model = ChatManagerModel()
            try check(model.selected.isEmpty)
            model.awaitingClose = pending.needsClose
            model.restartable = pending.canRestart
            try check(model.canReload)
            model.restartable = []
            try check(!model.canReload)
            model.requiresReplacement = true
            try check(!model.canExecute)
            model.replaceExisting = true
            try check(model.canExecute)
            model.blocked = true
            try check(!model.canExecute)
            let replacement = try decoder.decode(ManagerResponse.self, from: Data("{\"ok\":true,\"requiresReplacement\":true}".utf8))
            try check(replacement.requiresReplacement)
            try check(ChatProfile.fromDisplayName("ChatGPT Arbeit") == .arbeit)
            try check(ChatProfile.fromDisplayName(" ChatGPT ") == .chatgpt)
            model.chats = list.chats
            model.query = "second"
            try check(model.visibleChats.map(\.id) == ["two"])
            let temporary = FileManager.default.temporaryDirectory.appendingPathComponent("chat-manager-protocol-\(UUID().uuidString)", isDirectory: true)
            try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: false,
                                                   attributes: [.posixPermissions: 0o700])
            defer { try? FileManager.default.removeItem(at: temporary) }
            let mock = temporary.appendingPathComponent("manager.py")
            try Data("""
                import json,sys
                assert len(sys.argv) == 1
                request=json.load(sys.stdin)
                assert request['command'] in ['list','prepare','execute']
                assert request['source'] == 'chatgpt'
                assert request['ids'] == ['one']
                assert request['replace_existing'] == False
                if request['command'] != 'list':
                    assert request['operation'] == 'copy'
                print(json.dumps({'ok':True,'message':request['command']+' received','chats':[]}))
                """.utf8).write(to: mock)
            for command in ["list", "prepare", "execute"] {
                let response = try await managerCall(ManagerRequest(command: command, operation: command == "list" ? nil : .copy,
                                                                   source: .chatgpt, ids: ["one"]), helperURL: mock)
                try check(response.ok && response.message == command + " received")
            }
            let bridgeResponse = try decoder.decode(BridgeResponse.self, from: Data("""
                {"ok":true,"message":"Ready","selection_token":"reviewed-token",
                 "chats":[{"id":"claude:one","title":"Fixture session","cwd":"/private/tmp/fixture","updated_at":123}],
                 "preview":[{"id":"claude:one","title":"Fixture session","cwd":"/private/tmp/fixture"}],
                 "warnings":["Original stays available"],"direction":"Claude Code → Codex"}
                """.utf8))
            try check(bridgeResponse.chats?.first?.id == "claude:one")
            try check(bridgeResponse.preview?.first?.updated_at == nil)
            try check(bridgeResponse.selection_token == "reviewed-token")
            let bridgeModel = BridgeModel()
            try check(bridgeModel.destinations == [.chatgpt, .arbeit, .personal])
            bridgeModel.source = .chatgpt
            try check(bridgeModel.destinations == [.claude])
            let first = try decoder.decode(BridgeChat.self, from: Data("{\"id\":\"one\",\"title\":\"One\"}".utf8))
            let second = try decoder.decode(BridgeChat.self, from: Data("{\"id\":\"two\",\"title\":\"Two\"}".utf8))
            bridgeModel.toggle(first, selected: true)
            bridgeModel.toggle(second, selected: true)
            try check(bridgeModel.selected == ["two"])
            bridgeModel.source = .claude
            bridgeModel.toggle(first, selected: true)
            try check(bridgeModel.selected == ["one", "two"])
            let bridgeMock = temporary.appendingPathComponent("bridge.py")
            try Data("""
                import json,sys
                request=json.load(sys.stdin)
                assert request['source']=='claude' and request['destination']=='arbeit'
                assert request['ids']==['claude:one']
                if request['command']=='bridge_execute': assert request['selection_token']=='reviewed-token'
                else: assert 'selection_token' not in request
                print(json.dumps({'ok':True,'message':request['command']+' received','chats':[]}))
                """.utf8).write(to: bridgeMock)
            for command in ["bridge_list", "bridge_prepare", "bridge_execute"] {
                let response = try await bridgeCall(BridgeRequest(command: command, source: .claude, destination: .arbeit,
                                                                 ids: ["claude:one"], selection_token: command == "bridge_execute" ? "reviewed-token" : nil),
                                                    helperURL: bridgeMock)
                try check(response.ok && response.message == command + " received")
            }
            let workspace = WorkspaceModel()
            workspace.selected = []
            workspace.starred = []
            var rows: [LibrarySession] = []
            for index in 0..<55 {
                let cwd = index % 2 == 0 ? "/tmp/project-a" : "/tmp/project-b"
                rows.append(LibrarySession(id:"id-\(index)", title:"Chat \(index)", cwd:cwd, updated:Double(index), archived:index == 54, running:false, present:false, eligibility:""))
            }
            try check(workspace.destinations == [.claude, .arbeit, .personal])
            try check(workspace.isProfileRoute && workspace.selectionLimit == 50)
            workspace.selectVisible(rows)
            try check(workspace.selected.count == 50 && !workspace.selected.contains("id-50"))
            workspace.toggle("id-51")
            try check(workspace.selected.count == 50)
            workspace.query = "Chat 1"
            let filtered = workspace.visible(rows)
            try check(filtered.allSatisfy { $0.title.contains("Chat 1") })
            try check(workspace.selected.subtracting(Set(filtered.map(\.id))).count > 0)
            workspace.query = ""; workspace.project = "/tmp/project-a"
            try check(workspace.visible(rows).allSatisfy { $0.cwd == "/tmp/project-a" && !$0.archived })
            workspace.project = "all"; workspace.visibility = "archived"
            try check(workspace.visible(rows).map(\.id) == ["id-54"])
            workspace.visibility = "all"; workspace.sort = "recent"
            try check(workspace.visible(rows).first?.id == "id-54")
            workspace.selectionOnly = true
            try check(workspace.visible(rows).allSatisfy { workspace.selected.contains($0.id) })
            workspace.destination = .claude
            try check(!workspace.isProfileRoute && workspace.selectionLimit == 1 && workspace.fidelity == "Saved context")
            workspace.toggle("id-52")
            try check(workspace.selected == ["id-52"])
            workspace.source = .claude; workspace.destination = .personal
            try check(workspace.selectionLimit == 50 && workspace.fidelity == "Native import")
            try check(workspace.destinations == [.chatgpt, .arbeit, .personal])
            try check(ChatProfile.fromDisplayName("ChatGPT Personal") == .personal)
            try check(ChatProfile.personal.vendorURL.path == "/Applications/ChatGPT Personal.app/Contents/Helpers/ChatGPT.app")
            let countsResponse = try decoder.decode(BridgeResponse.self, from: Data("{\"ok\":true,\"message\":\"Ready\",\"counts\":{\"messages\":97,\"text_bytes\":1234,\"attachments_omitted\":2}}".utf8))
            try check(countsResponse.counts?.messages == 97 && countsResponse.counts?.attachments_omitted == 2)
            let activity = try decoder.decode(LibraryResponse.self, from: Data("""
                {"ok":true,"message":"Recorded","activities":[{"id":"activity-one","created_at":123,"source":"chatgpt","destination":"personal","action":"copy","title":"Copied chat","status":"completed","message":"Verified","items":[{"id":"one","title":"First chat","status":"completed","verified":true}],"paths":[]}]}
                """.utf8))
            try check(activity.activities?.first?.items.first?.verified == true)
            let libraryMock = temporary.appendingPathComponent("library.py")
            try Data("""
                import json,sys
                request=json.load(sys.stdin)
                assert request['command'] in ['activity','backups','details']
                if request['command']=='details': assert request['source']=='personal' and request['id']=='fixture'
                else: assert 'source' not in request and 'id' not in request
                print(json.dumps({'ok':True,'message':'Read only'}))
                """.utf8).write(to:libraryMock)
            for command in ["activity","backups","details"] {
                let result = try await libraryCall(LibraryRequest(command:command,source:command == "details" ? "personal" : nil,id:command == "details" ? "fixture" : nil),helperURL:libraryMock)
                try check(result.ok && result.message == "Read only")
            }
            let liveWorkspace=WorkspaceModel()
            liveWorkspace.automaticDetectionEnabled=true
            liveWorkspace.source = .chatgpt;liveWorkspace.destination = .arbeit
            liveWorkspace.query="First";liveWorkspace.project="project-filter";liveWorkspace.selected=["one"]
            let liveManager=ChatManagerModel(),liveBridge=BridgeModel()
            try check(liveWorkspace.automaticChangesAllowed(manager:liveManager,bridge:liveBridge,isActive:true))
            try check(!liveWorkspace.automaticChangesAllowed(manager:liveManager,bridge:liveBridge,isActive:false))
            try check(liveWorkspace.applyAutomaticInventory(list,source:.chatgpt,destination:.arbeit,manager:liveManager,bridge:liveBridge,isActive:true))
            try check(liveManager.chats.count == 2 && liveWorkspace.selected == ["one"])
            try check(liveWorkspace.query == "First" && liveWorkspace.project == "project-filter")
            liveManager.showPreview=true
            try check(!liveWorkspace.automaticChangesAllowed(manager:liveManager,bridge:liveBridge,isActive:true))
            try check(!liveWorkspace.applyAutomaticInventory(list,source:.chatgpt,destination:.arbeit,manager:liveManager,bridge:liveBridge,isActive:true))
            liveManager.showPreview=false;liveBridge.showPreview=true
            try check(!liveWorkspace.automaticChangesAllowed(manager:liveManager,bridge:liveBridge,isActive:true))
            liveBridge.showPreview=false;liveManager.busy=true
            try check(!liveWorkspace.automaticChangesAllowed(manager:liveManager,bridge:liveBridge,isActive:true))
            liveManager.busy=false
            try check(!liveWorkspace.applyAutomaticInventory(list,source:.personal,destination:.arbeit,manager:liveManager,bridge:liveBridge,isActive:true))
            try check(!liveWorkspace.applyAutomaticInventory(list,source:.chatgpt,destination:.personal,manager:liveManager,bridge:liveBridge,isActive:true))
            liveWorkspace.automaticDetectionEnabled=false
            try check(!liveWorkspace.automaticChangesAllowed(manager:liveManager,bridge:liveBridge,isActive:true))
            try check(liveWorkspace.selected == ["one"] && liveManager.chats.count == 2)
            print("{\"passed\":\(checks),\"mock_backend_calls\":9,\"app_operations\":0,\"profile_writes\":0}")
        } catch {
            fputs("Chat Manager protocol tests failed.\n", stderr)
            exit(1)
        }
    }
}
#endif
