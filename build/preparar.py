#!/usr/bin/env python3
"""Prepara la compilación: lee VERSION de main.py y escribe build/version_info.txt (recurso de versión del .exe).

  python build/preparar.py             escribe version_info.txt e imprime la versión
  python build/preparar.py --version   solo imprime la versión
"""
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
version = re.search(r'^VERSION = "([^"]+)"', (RAIZ / "main.py").read_text(encoding="utf-8"), re.M).group(1)
if "--version" in sys.argv:
    print(version)
    sys.exit(0)
nums = [int(x) for x in re.findall(r"\d+", version)[:4]]
nums += [0] * (4 - len(nums))
t = tuple(nums)
(RAIZ / "build" / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'SupaVic'),
      StringStruct('FileDescription', 'SupaVic DLNA - music DLNA server'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'SupaVicDLNA'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('OriginalFilename', 'SupaVicDLNA.exe'),
      StringStruct('ProductName', 'SupaVic DLNA'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
print(version)
