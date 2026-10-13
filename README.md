# Chat Transfer

A small macOS app for choosing, inspecting, and transferring saved local coding
chats. The interface uses a compact monochrome toolbar, a source sidebar, a chat
library, and a destination inspector.

Chat Transfer is an independent project. It is not an OpenAI or Anthropic app and
does not contain either vendor's application or CLI binaries.

## What it does

- Detects newly saved chats and changes while Chat Transfer is open. The library
  updates when the app is active and idle, and waits during reviews or transfers.
  Detection never starts a transfer. Manual refresh remains available with **⌘R**.
- Searches and filters chats by project, archive status, stars, and selection.
  A details preview shows saved visible user and assistant messages.
- Reviews the selected source, destination, direction, conflicts, and required
  reloads before any transfer. Changed history requires a fresh review.
- Keeps a local activity list and links to private backup and continuation files.

### Supported transfers

| Route | Available action | What arrives |
| --- | --- | --- |
| Codex profile → Codex profile | Copy, Move, Sync | Selected saved history and required recorded fork/subagent dependencies |
| Claude Code → Codex profile | Native import | Selected local Claude Code sessions, through the installed Codex CLI's native importer |
| Codex profile → Claude Code | Continuation | A private transcript of saved visible user/assistant text, plus a launcher for a fresh interactive Claude Code session |

**Copy** keeps the source available. Replacing different destination work requires
explicit consent in the review, and the previous state is backed up. **Move**
verifies the destination and archives the visible source; recordings remain in
place to preserve inherited history. **Sync** needs a verified baseline and
refuses divergent changes rather than choosing a version silently.

A Claude continuation is reference context for a new session. It does not recreate
the original message timeline, tool execution, attachments, hidden reasoning,
approvals, or filesystem checkpoints. Preparing it sends no model prompt. The
launcher waits for your next message. Cross-provider Move and Sync are not offered.

Cloud ChatGPT and Claude conversations are outside this app's local coding-chat
library. Chats with attached resources, goals, memberships, or incomplete history
can require preparation; the transfer review explains blockers. Incoming history
may require reopening an idle destination app. Chat Transfer does not force
termination of active work.

## Requirements

- macOS 13 or later on Apple silicon.
- Xcode or Command Line Tools with a **Swift 6** compiler.
- `/usr/bin/python3`, version 3.9 or later, with the standard library. The app uses
  that executable; no third-party Python packages are required.
- For native Claude imports and Codex continuations, a user-installed Codex CLI in
  the configured ChatGPT application location. It is a separate vendor dependency
  and is not redistributed here.
- For opening a Claude continuation, the user-installed Claude Code CLI at
  `~/.local/bin/claude`.

The current profile registry has fixed defaults:

| Source | Profile directory | Application |
| --- | --- | --- |
| ChatGPT / Codex | `~/.codex` | `/Applications/ChatGPT.app` |
| Arbeit | `~/Library/Application Support/Arbeit/codex` | `/Applications/ChatGPT Arbeit.app` |
| Personal | `~/Library/Application Support/ChatGPT Copy/codex` | `/Applications/ChatGPT Personal.app` |
| Claude Code | `~/.claude/projects` | `~/.local/bin/claude` |

Arbeit and Personal entries support separately configured local profiles and the
corresponding launcher paths. Installing Chat Transfer does not create those
wrappers, change their settings, or provide their accounts. It also does not create
a proprietary ChatGPT/Codex copy. If your installation uses different paths, edit
the source profile registry before building; there is no general profile-path
settings UI yet.

## Build

```sh
./scripts/build.sh
```

This creates `build.noindex/Chat Transfer.app` and includes only the five Python helper
source files needed at runtime. It compiles the app and monochrome icon locally,
then applies an ad-hoc signature. The build does not read chats, install files,
change vendor apps, or open model sessions. It is not a notarized distribution.

## Test

```sh
./scripts/test.sh
```

The default suite uses synthetic temporary profiles, mocked subprocesses, and
headless Swift protocol/discovery tests. It does not transfer real chats or start
model turns. Test output and build products remain ignored by Git.

`Tests/run-native-bridge-integration.py` is an optional developer diagnostic. Its
`--stage-disposable-sources` mode deliberately stages marked synthetic Claude
fixtures under a temporary folder inside `~/.claude/projects`, imports them into
a temporary Codex profile, then removes the staged fixtures. It is excluded from
the default test script and should only be invoked intentionally after reading
its source. It uses the separately installed native CLI, not a bundled vendor
binary.

## Install

Quit Chat Transfer first, then run:

```sh
./scripts/install.sh
```

The installer updates an existing Chat Transfer installation when exactly one is
found in `/Applications` or `~/Applications`, including the former `Chat Manager`
filename. A new installation defaults to `~/Applications/Chat Transfer.app`.
If several installations exist, choose one explicitly:

```sh
./scripts/install.sh --destination "/absolute/path/Chat Transfer.app"
```

The installer copies the app and exactly five helper files. For compatibility with
existing local activity and backups, those helpers are installed under
`~/Library/Application Support/Arbeit/chat-manager`. Existing helper code and any
previous Chat Transfer app at the chosen destination are backed up before
replacement. Other files in that runtime directory are kept. Vendor apps, profiles,
authentication, settings, and chat data are not copied by the installer.

Build products stay in `build.noindex/` so Spotlight does not list them as another
installed app.

The app's bundle identifier is `local.chatgpt.chat-manager`. The installer refuses
to replace another app, refuses redirected paths, and requires Chat Transfer to
be closed. It prints the backup location. Open the installed app normally from
Finder when installation finishes.

## Privacy and source contents

This repository is limited to application/helper source, synthetic tests,
packaging scripts, and documentation. User profiles, transcripts, screenshots,
runtime databases, credentials, exported continuations, and backups do not belong
in it. The Git ignore rules and `PUBLIC_FILES.txt` use an explicit public file
allowlist.

Local transfer backups can contain sensitive chat history. Keep runtime folders
outside this repository. See [PRIVACY.md](PRIVACY.md) for what the app reads and
writes, and what to remove from bug reports.

Detection uses native filesystem events, combines rapid changes, and checks saved
file metadata every 20 seconds as a fallback. Updates are spaced at least three
seconds apart and catch up when you return to the app. You can pause detection
from the app menu.

## License

Chat Transfer's source is available under the [MIT License](LICENSE). OpenAI,
Anthropic, their applications/CLIs, trademarks, accounts, and services remain
separate from this project's license.
