# Changelog

All notable changes to this project are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-10-27

First public release.

### Added
- Music-only DLNA/UPnP MediaServer: SSDP on every local interface, ContentDirectory (Browse, Search),
  ConnectionManager and GENA change notifications.
- Live folder browsing with no up-front indexing, a SQLite tag cache, a fast FLAC header reader and idle prefetch
  of tags and covers.
- Tag views: Artists, Albums, Genres, Decades, Recently added, Hi-Res and Random, with A–Z grouping,
  "▶ All tracks" entries and song counts.
- Resized cover art (DLNA `JPEG_SM`), one cover per album, folder images or embedded pictures.
- Original-file streaming with HTTP range requests (no transcoding).
- Web control panel and a separate dashboard (now playing, listening history, transfers, stats).
- English and Spanish interface, including the folder names shown on players.
- Windows tray app, start with Windows, installer and portable builds; one-click scripts to run from source;
  `start.sh` and a systemd unit for Linux.
