# Changelog

All notable changes to this project are documented here. Versions follow [Semantic Versioning](https://semver.org/).

## [1.1.0] - 2026-10-28

Thanks to the WiiM forum community (especially simbun) for the detailed first round of feedback.

### Fixed
- The app no longer crashes if Windows refuses to create the tray icon (`PermissionError: [WinError 5]`, which
  happens when the process starts on a desktop it isn't allowed to create windows on). The server keeps running,
  and the icon shows up as soon as Windows allows it (quick retries, then once a minute).
- Multi-valued tags: every value of repeated `ARTIST=`, `ALBUMARTIST=` and `GENRE=` fields (and multi-value ID3/MP4
  tags) is read, not only the first one.
- Albums with the same title and album artist in different folders (reissues, self-titled albums) are no longer
  merged into one. `CD1`/`Disc 2`-style subfolders still form a single album.
- Album years prefer `originaldate`/`originalyear` over `date`, so reissues sort by their original release.
- One broken file or folder no longer makes a whole page fail to browse: it's skipped and logged.
- The control panel could fail to load the list of views (and then save an empty list) in browsers that report an
  unusual language tag.

### Added
- Year after the album title, `Album [1999]` (on by default, can be turned off).
- Compilations / "Various Artists" albums are sorted by `albumsort` or title instead of year; the compilation flag
  (`COMPILATION`, `TCMP`, `cpil`) is honored.
- Optional "Track artists" view: everyone who appears on a track (featuring, compilations), with only their tracks.
- Count type: songs (default), items (an artist's albums, a genre's artists…; albums get no number) or none.
- Optional "Folders" entry at the root that holds your folders instead of listing them directly at the root.

### Note
- The tag cache is rebuilt once after updating (tags are read again on browse and during idle prefetch); covers
  are kept.

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
