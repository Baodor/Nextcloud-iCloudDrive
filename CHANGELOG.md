# Changelog

## 0.1.0 — 2026-10-05

- Native Nextcloud app with English/German UI, responsive layout and administrator-only service configuration.
- Apple authentication challenge flow through rclone and a Nextcloud WebDAV app-password connection.
- Per-user folder selection, Nextcloud destination-folder creation and non-overlapping job validation.
- Daily/weekday/interval schedules, copy in either direction and explicit bisync initialization after preview.
- Isolated dry-run checkpoints, access-check files, conservative deletion limits and retained conflict copies.
- Persistent state, backup directories, live progress, graceful stop requests and run history.
- Encrypted account credentials and rclone config with separately stored keys.
- Read-only per-user WebDAV gateway for Nextcloud External storage.
- Docker deployment, installation/package/account-cleanup scripts and extensive English/German documentation.
- Worker image normalizes private-checkout and installed-dependency permissions, sets an explicit Python import path and verifies imports as UID 10001 during the build; CI covers a restrictive build umask and production Compose startup.
- Automatic setup script installs the app and External storage and configures an encrypted bridge token; Nextcloud integration is checked on versions 30, 34 and 35.
- Empty stop/disconnect requests retain their JSON object shape across the Nextcloud bridge; the UI shows pending cancellation and tests cover OCS cancellation and stopping a real rclone transfer.
- Phase-aware progress separates folder scanning, comparison, known transfer queues and final validation. The UI displays measured percentages, per-file progress, direction, counts, speed and queue ETA; only successful runs reach 100%. Final JSON statistics survive RC shutdown/timeouts, and file/directory conflicts are explained explicitly.
- Automatic, default-enabled Pages/Numbers/Keynote package preparation preserves every package member, validates CRC/SHA-256 and source versions, retains original Nextcloud packages and journals guarded WebDAV renames for recovery. Previews remain read-only; UI package counts and tests cover cancellation, recovery and both sync directions with real Nextcloud WebDAV/rclone.
- iWork preflight scans both directory rows and contained file paths, uses cloud parent listings, logs effective package settings/counts and verifies the resulting Nextcloud file type before starting rclone. A deployment helper verifies worker source/API and Nextcloud routing, enables one existing job and removes only former package workaround filters. Integration tests cover nested paths with spaces/parentheses, root-level packages, empty package directories and already complete documents.
- Worker safety tests, browser checks, optional real rclone tests and a Nextcloud integration CI matrix.
- A pinned bridge build of rclone corrects iCloud package-token downloads by privately caching and verifying the original ZIP before WebDAV upload, using actual ZIP sizes instead of Apple's bundle sizes. Versioned per-account caches, range/cancellation handling, lower-bound preview sizes, download activity and explicit completed-copy/upload-failure indicators are covered by backend HTTP and UI regression tests; deployment checks reject older binaries.

Apple authentication and account-specific iCloud/ADP behavior require a real-account installation test. This is an initial experimental release, not a signed Nextcloud App Store package.
