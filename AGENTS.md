# SupaVic DLNA — notas para desarrolladores y agentes de IA

Servidor DLNA/UPnP (MediaServer) solo para música. Código e identificadores en español; textos visibles en es/en
(`i18n.py`); logs en inglés. README.md / README.es.md son la documentación de usuario.

## Archivos

| Archivo | Rol |
|---|---|
| `main.py` | Servidor completo: config, tags, caché, biblioteca en vivo, índice por etiquetas, SOAP/DIDL, SSDP, GENA, streaming, panel web (`PANEL_HTML`), `arrancar()` / `detener()` |
| `dashboard.py` | Proceso independiente (solo stdlib + `i18n`): lee `datos/eventos.jsonl` y sondea a los renderers por AVTransport |
| `launcher.py` | Entrada del `.exe` y de los `.bat`: instancia única, bandeja (pystray), autoinicio (HKCU\…\Run), firewall del portable, `--dashboard` |
| `i18n.py` | `T(clave)`; `LANG` se fija desde `servidor.idioma` al cargar la config |
| `start.bat` / `stop.bat` / `uninstall.bat` / `start.sh` | Ejecutar desde el código (ASCII + CRLF los .bat) |
| `build/` | `SupaVicDLNA.spec` (PyInstaller, carpeta), `installer.iss` (Inno Setup 6), `preparar.py` (versión → version_info.txt), `build.bat` |
| `.github/workflows/release.yml` | Tag `vX.Y.Z` (= `VERSION` de main.py) → instalador + zip portable + SHA256SUMS en un Release |

## Rutas de datos (`_dir_app()` en main.py y dashboard.py, deben coincidir)

`SUPAVIC_HOME` si existe → `%LOCALAPPDATA%\SupaVic DLNA` si es ejecutable (frozen) → junto al código.
Ahí van `config.json` y `datos/` (log, pid, eventos, historial y, si `cache.directorio` está vacío, `cache.db` + `portadas/`).

## Arranque y reinicio

- `launcher.py`: si `ping()` responde → abre el panel y sale (instancia única). Si no: `srv.RELANZAR = comando("--reinicio", …)`,
  `srv.arrancar()`, dashboard hijo (si `dashboard.con_servidor` y su puerto está libre), navegador (no con `--inicio`
  salvo que no haya carpetas), bandeja en el hilo principal.
- Cambiar `servidor.*` o `cache.*` en el panel reinicia: `App.reiniciar()` → `AL_SALIR()` (quita el icono) → `Popen(RELANZAR)`
  → `os._exit(0)`. `--reinicio` evita la comprobación de instancia única (el proceso viejo aún responde).
- El dashboard sobrevive al reinicio del servidor; "Salir" lo mata por `datos/dashboard.pid`.
- `main.py` sigue funcionando solo (`python main.py`): sin bandeja ni dashboard.

## Servidor (resumen)

- IDs sin estado: `d<raiz>:<rel>` carpeta, `t<raiz>:<rel>` pista, `<raiz>` = sha1[:8] de la ruta configurada.
  Vistas: `v:<vista>[@letra]`, `v:ar:`, `v:al:`, `v:ge:[…:ar:]`, `v:de:`, `…:todas`, `v:carpeta:<raiz>:<rel>:todas`.
- UDN = uuid5(`servidorMusicaDLNA:<host>:<DIR_BASE>`) — prefijo histórico, no cambiarlo (los reproductores verían otro servidor).
- La carpeta navegada siempre se lee del disco (`listar(fresco=True)`): en Windows la fecha de una carpeta no cambia
  de forma fiable al mover subcarpetas. Conteos/portadas usan caché.
- `CacheTags`: SQLite validado por `mtime_ns` + tamaño; si la ruta cambió, busca por identidad (nombre + tamaño + mtime).
- `Indice`: se construye desde `cache.db` (sin leer audio), en segundo plano, máx. 1/min; verifica existencia con un
  `scandir` por carpeta. Nombres visibles traducidos (`VISTAS` se rellena en `aplicar_idioma`).
- `Precargador`: en reposo (`precarga.inactividad_min` sin Browse/portadas ni transferencias) recorre todo; cualquier
  actividad la pausa.
- `esc()` / DIDL: `upnp:albumArtURI` con `dlna:profileID="JPEG_SM"` si la portada ≤ 640 px.
- Los POST de `/api/*` solo desde IPs privadas.

## Pruebas

Sin suite automática. Verificación usada:
- Linux: `SUPAVIC_HOME=/tmp/x python launcher.py --sin-bandeja` con una biblioteca de prueba; SOAP Browse a `/ctl/cds`
  (raíz, vistas, "Todas"), `/api/estado`, reinicio por `/api/reiniciar`, SIGTERM.
- Build congelado: `pyinstaller build/SupaVicDLNA.spec` en Linux valida el spec y las rutas frozen (`LOCALAPPDATA=…`).
- UI: Playwright sobre `:8200` y `:8202` en es/en, claro/oscuro, sin errores JS.
- Windows real (bandeja, registro, MessageBox, instalador) se prueba a mano.

## Convenciones

- Python ≥ 3.9, sin dependencias nuevas sin necesidad (stdlib primero).
- .bat en ASCII + CRLF con pasos numerados; sin VBS.
- Un cambio visible para el usuario → textos en `i18n.py` (servidor) y en los diccionarios `TXT` del panel/dashboard.
- Subir versión: `VERSION` en main.py + CHANGELOG.md + docs/release-notes.md, luego tag `vX.Y.Z`.
