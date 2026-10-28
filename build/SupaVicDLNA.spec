# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: un solo ejecutable (carpeta) para el servidor, la bandeja y el dashboard.
#   python build/preparar.py
#   pyinstaller --noconfirm --clean build/SupaVicDLNA.spec --distpath dist --workpath build/tmp
import sys
from pathlib import Path

RAIZ = Path(SPECPATH).parent
ICONO = str(RAIZ / "assets" / "icon.ico")
VERSION_INFO = RAIZ / "build" / "version_info.txt"

a = Analysis(
    [str(RAIZ / "launcher.py")],
    pathex=[str(RAIZ)],
    datas=[(str(RAIZ / "assets" / "icon.ico"), "assets"), (str(RAIZ / "assets" / "icon.png"), "assets")],
    hiddenimports=["main", "dashboard", "i18n"] + (["pystray._win32"] if sys.platform == "win32" else []),
    excludes=["tkinter", "unittest", "pydoc", "doctest", "lib2to3", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SupaVicDLNA",
    console=False,
    icon=ICONO,
    version=str(VERSION_INFO) if VERSION_INFO.exists() and sys.platform == "win32" else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="SupaVicDLNA", upx=False)
