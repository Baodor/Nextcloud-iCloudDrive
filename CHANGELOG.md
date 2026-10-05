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
- Worker safety tests, browser checks, optional real rclone tests and a Nextcloud integration CI matrix.

Apple authentication and account-specific iCloud/ADP behavior require a real-account installation test. This is an initial experimental release, not a signed Nextcloud App Store package.
