# Telegram notifications implementation plan

Goal: Add opt-in entry/exit fill, startup and important error alerts to the three existing bots.
Architecture: A bounded background queue verifies order fills and sends plain Telegram messages.
Existing strategy decisions and trading order calls remain unchanged.

- [x] Snapshot upstream revisions into separate local repositories; inspect journal and startup hooks.
- [x] Write failing notification tests: confirmed vs unconfirmed fills, deduplication, timeout,
  bounded queues, HTTP failure isolation, disabled configuration and trade journal integration.
- [x] Add notifications.py, hook trade journals and startup; cover ST market/mismatch paths.
- [x] Add local-only environment settings and a setup/test command requiring no exchange credentials.
- [x] Run all repository tests and inspect diffs before publishing.
Delivery: publish reviewed changes with the original upstream commit as parent and a head lease.

Review focus: avoid secret forwarding, false fill announcements, blocking the trading loop,
recursive error logging, and accidental changes to strategy or amounts.
