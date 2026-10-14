# Privacy

## Repository boundary

The public package contains app/helper source code, documentation, packaging
scripts, and synthetic test fixtures. It excludes local conversations, account
credentials, project files, model prompts from real chats, screenshots, runtime
databases, rollouts, logs, exported continuations, and backups. Home directories
are resolved on the user's own machine; the source must not embed a developer's
username, real chat IDs, session names, personal paths, or copied real chat text.
Tests contain only synthetic identifiers and conversation examples.

`PUBLIC_FILES.txt` is the release allowlist. `.gitignore` defaults to excluding
files unless explicitly allowlisted. Do not use `git add -f` to include runtime
data. Build products and test output stay under the ignored `build.noindex/` directory.

## What the app reads

Chat Transfer reads the configured local Codex profile databases and saved history,
plus local Claude Code session files. The library can display chat titles, project
paths, timestamps, archive status, and saved visible user/assistant text. These
values can contain personal information.

Automatic discovery observes local saved-history changes while the app is open.
It updates the library in the foreground or background, defers during reviews or
transfers, and catches up after sleep. It does not upload detected information, perform a
transfer, change profiles, start a model turn, or enable an app-switch sync service.

For a reviewed transfer, the app examines the selected chats and their necessary
recorded history dependencies. It does not migrate sign-in credentials, account
tokens, provider settings, MCP connections, or unrelated chats. Claude native import
uses the installed Codex CLI's public app-server interface; the app selects session
imports, not configuration or credential imports.

## What stays on disk

The compatibility runtime directory is:

```text
~/Library/Application Support/Arbeit/chat-manager
```

It contains helper code, pair baselines, activity
records, transfer journals, recovery references, and operation backups. Claude
continuations and native-import journals live in its `bridge/` subdirectory. App
and helper installation backups live under
`~/Library/Application Support/Arbeit/maintenance-backups`.

Stars and the detection preference use standard macOS app preferences.

Activity records avoid conversation text, but chat titles, IDs, routes, and file
references are still sensitive. Recovery backups and exported transcripts can
contain complete saved conversation content. Private runtime directories and
files use restrictive permissions. These folders must not be published, attached
to issues wholesale, or moved into a source repository.

Removing the app bundle alone does not delete those private files or any source
profile. That allows recovery after an app upgrade. Review the local backup and
continuation folders before deleting them; the app does not provide automatic
retention or cloud erasure.

## Network and other applications

Chat Transfer includes no analytics, telemetry uploads, or chat-sharing endpoint.
Its bundled helpers operate on local files and the user's installed CLIs. The
separate Codex, ChatGPT, and Claude applications/CLIs have their own account,
network, permissions, and privacy behavior. Opening a continuation or destination
does not change those products' policies.

Preparing a Claude continuation writes local context files. Opening Claude Desktop
uses its Code composer link with a short unsent draft referencing the complete
local transcript. The link contains file and project paths, but no transcript text
or source chat ID. Desktop asks the user to confirm the project folder. Chat
Transfer does not press Send or fabricate Desktop conversation records.

When the user sends the draft or works in Claude Code, conversation context may be
read and sent according to that product's selected provider and normal behavior.
The optional legacy terminal launcher remains available in older saved packages.
A continuation is not an offline guarantee for the destination application.

## Safe issue reports

Include the app version, macOS version, a generic reproduction, and a short
redacted error message. Remove usernames, home/project paths, chat titles and IDs,
account identifiers, tokens, transcript text, and command output containing
private data. Do not upload profile databases, source session files, operation
backups, screenshots of real chats, or complete runtime directories.
