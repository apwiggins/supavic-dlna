# Third-party notices

SupaVic DLNA's own code is MIT-licensed. The Windows builds (installer and portable ZIP) also bundle these
components, each under its own license:

| Component | Used for | License |
|---|---|---|
| [Python](https://www.python.org/) | Runtime | PSF License |
| [mutagen](https://github.com/quodlibet/mutagen) | Reading tags and embedded covers | GPL-2.0-or-later |
| [Pillow](https://python-pillow.org/) | Resizing covers, tray icon image | MIT-CMU (HPND) |
| [pystray](https://github.com/moses-palmer/pystray) | System tray icon (Windows) | LGPL-3.0 |
| [PyInstaller](https://pyinstaller.org/) bootloader | Windows executable | GPL-2.0 with bootloader exception |

Because the Windows builds include mutagen (GPL-2.0-or-later), those binaries as a whole are distributed under the
terms of the GPL. The complete corresponding source code is this repository plus the unmodified upstream packages
listed in `requirements.txt`. Running from source doesn't bundle anything: `pip` installs each package with its
own license.
