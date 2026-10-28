# Cómo publicar SupaVic DLNA en GitHub

Guía para el dueño del repositorio. Los usuarios no la necesitan.

## 1. Crear el repositorio y subir el código

1. En GitHub: **New repository** → nombre `supavic-dlna`, **Public**, sin README ni licencia (ya vienen aquí).
2. En la carpeta del proyecto:

```bat
git init -b main
git add .
git commit -m "SupaVic DLNA 1.0.0"
git remote add origin https://github.com/<tu-usuario>/supavic-dlna.git
git push -u origin main
```

`.gitignore` deja fuera `.venv\`, `datos\`, `config.json`, `dist\` y los temporales de compilación. Así tu
configuración, tus rutas de música y tu caché nunca se suben.

## 2. Completar la página del repositorio

**About** (engrane ⚙ a la derecha de la página principal):

- *Description*: `Tiny, fast music-only DLNA/UPnP server for WiiM and any UPnP player. Live folders + artist/album/genre views, no indexing wait, no transcoding.`
- *Website*: déjalo vacío o pon el enlace a Releases.
- *Topics*: `dlna` `upnp` `media-server` `music-server` `wiim` `flac` `hi-res-audio` `bubbleupnp` `windows` `python`
- Marca **Releases** y desmarca *Packages* y *Deployments* si no los usas.

**Social preview** (Settings → General → Social preview → Edit): sube `docs/img/social-preview.png` (1280×640).
Es la imagen que aparece al compartir el enlace en WhatsApp, X, Reddit, etc.

**Opcional:**

- Settings → General → Features → activa **Issues** (las plantillas ya están en `.github/ISSUE_TEMPLATE`) y
  **Discussions** si quieres un foro para preguntas.
- Si el paso *GitHub Release* falla con error 403: Settings → Actions → General → *Workflow permissions* → **Read and write**.

## 3. Publicar la primera versión (instalador + portable)

```bat
git tag v1.0.0
git push origin v1.0.0
```

En la pestaña **Actions** se ejecuta *Build Windows release* (unos 5 minutos). Al terminar, en **Releases** aparece
"SupaVic DLNA 1.0.0" con:

- `SupaVicDLNA-Setup-1.0.0.exe`: el instalador
- `SupaVicDLNA-1.0.0-portable-win64.zip`: la versión portable
- `SHA256SUMS.txt`

El enlace **Download for Windows** del README apunta a `releases/latest`, así que siempre lleva a la última versión.

> Antes del primer tag conviene probar el `.exe` en tu PC: ejecuta `build\build.bat` (o en Actions → *Build Windows
> release* → **Run workflow**, que genera los archivos como *artifacts* sin crear un Release).

## 4. Siguientes versiones

1. Cambia `VERSION = "x.y.z"` en `main.py`.
2. Agrega la sección nueva en `CHANGELOG.md` y, si quieres, ajusta `docs/release-notes.md`.
3. `git commit`, luego `git tag vx.y.z` y `git push origin main vx.y.z`.

Si la etiqueta no coincide con `VERSION`, el workflow se detiene con un error, a propósito.

## 5. Firma de código (opcional, más adelante)

Sin firma, Windows SmartScreen muestra "Windows protegió su PC" hasta que el ejecutable gana reputación. Para
quitarlo hace falta un certificado de firma de código. Para proyectos open source existe
[SignPath Foundation](https://signpath.org/), que es gratuito. Mientras tanto, el README explica a los usuarios
cómo continuar y cómo verificar el SHA256.
