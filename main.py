#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SupaVic DLNA — servidor DLNA/UPnP (MediaServer) solo para música.

- Navegación por carpetas en vivo: NO hay indexado previo obligatorio. Tags y portadas se leen
  al abrir una carpeta desde el control (WiiM Home, BubbleUPnP, etc.) y se guardan en caché;
  en reposo se precarga el resto. Además: vistas por Artistas, Álbumes, Géneros, Décadas…
- Configuración en config.json, editable desde el panel web: http://<ip>:<puerto>/
- Dependencias: mutagen (tags/portadas) y Pillow (portadas). Opcional: pystray (icono de bandeja, launcher.py).
"""
import base64
import fnmatch
import hashlib
import http.client
import io
import ipaddress
import json
import logging
import logging.handlers
import os
import random
import re
import socket
import sqlite3
import string
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter, OrderedDict, defaultdict
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _esc

import mutagen
import i18n
from i18n import T
from mutagen.flac import Picture

try:
    from PIL import Image
except ImportError:          # funciona sin Pillow, pero manda las portadas sin redimensionar
    Image = None

VERSION = "1.1.0"
APP = "SupaVic DLNA"


def _dir_app():
    """Carpeta de config.json y datos/: junto al código al correr desde el código fuente;
    en %LOCALAPPDATA%\\SupaVic DLNA con el ejecutable (Program Files no se puede escribir).
    SUPAVIC_HOME la fija a mano."""
    if os.environ.get("SUPAVIC_HOME"):
        return Path(os.environ["SUPAVIC_HOME"]).expanduser().resolve()
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) / APP if base else Path.home() / ".supavic-dlna"
    return Path(__file__).resolve().parent


DIR_BASE = _dir_app()
DIR_DATOS = DIR_BASE / "datos"
RUTA_CONFIG = DIR_BASE / "config.json"
RUTA_LOG = DIR_DATOS / "servidor.log"
RUTA_PID = DIR_DATOS / "servidor.pid"
RUTA_EVENTOS = DIR_DATOS / "eventos.jsonl"      # lo lee dashboard.py (proceso independiente)
ES_WINDOWS = os.name == "nt"

SERVER_HDR = f"{'Windows' if ES_WINDOWS else sys.platform}/10 UPnP/1.0 DLNADOC/1.50 SupaVicDLNA/{VERSION}"
RELANZAR = None                 # comando para reiniciar el proceso; lo fija launcher.py (ejecutable / bandeja)
AL_SALIR = None                 # callback antes de reiniciar (launcher.py quita el icono de la bandeja)
log = logging.getLogger("dlna")

CONFIG_DEFECTO = {
    "servidor": {
        "nombre": "",                 # vacío = "SupaVic (<equipo>)"
        "puerto": 8200,
        "ip": "",
        "idioma": "auto",             # auto | es | en (WiiM, panel y dashboard)
        "abrir_navegador": True,      # abrir el panel al iniciar a mano (no al iniciar con Windows)
    },
    "musica": {
        "carpetas": [],
        "ignorar": ["*.part", "*.tmp", ".*", "$RECYCLE.BIN", "System Volume Information"],
        "ordenar_por_tags_hasta": 150,
        "agrupar_carpetas": False,     # True: las carpetas van dentro de una entrada "Carpetas" en la raíz
    },
    "portadas": {
        "tamano": 600,
        "recortar_cuadrado": True,
    },
    "precarga": {
        # Recorre la biblioteca y llena la caché (tags + portadas) cuando no hay navegación.
        "activa": True,
        "inactividad_min": 5,
        "portadas": True,
        "repetir_horas": 12,
    },
    "vistas": {
        # Navegación por etiquetas (estilo MinimServer/MusicBee) además de las carpetas.
        "activas": ["artistas", "albumes", "generos", "decadas", "recientes", "hires", "aleatorio"],
        "agrupar_desde": 200,          # listas más largas se agrupan por letra (0 = nunca)
        "mostrar_conteo": True,        # "Korn (15)": número entre paréntesis en artistas, álbumes, géneros…
        "tipo_conteo": "pistas",       # pistas = canciones; elementos = álbumes de un artista, artistas de un género…
        "mostrar_anio": True,          # "Álbum [1999]": año después del título del álbum
    },
    "dashboard": {
        # Lo usa dashboard.py; aquí solo para que el panel no borre estas claves al guardar.
        "puerto": 8202,
        "renderers": [],          # URLs de description.xml extra si el descubrimiento no encuentra el WiiM
        "con_servidor": True,     # la bandeja lo inicia y lo detiene junto con el servidor
    },
    "cache": {
        # Tags (SQLite) y portadas. Vacío = datos/. En un SSD/NVMe la navegación en frío es más rápida.
        "directorio": "",
    },
}

AUDIO = {
    ".flac": "audio/flac", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".aif": "audio/aiff",
    ".aiff": "audio/aiff", ".dsf": "audio/dsf", ".dff": "audio/dff",
    ".wv": "audio/x-wavpack", ".ape": "audio/x-ape",
}
IMAGENES = {".jpg", ".jpeg", ".png"}
PORTADA_PREF = ["cover", "folder", "front", "album", "albumart", "portada", "frontal"]
DLNA_FLAGS = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
NS_CDS = "urn:schemas-upnp-org:service:ContentDirectory:1"
NS_CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"
DEV_TYPE = "urn:schemas-upnp-org:device:MediaServer:1"
FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM = 0x2, 0x4


def esc(s):
    return _esc(str(s), {'"': "&quot;"})


def protocol_info(mime):
    pn = "DLNA.ORG_PN=MP3;" if mime == "audio/mpeg" else ""
    return f"http-get:*:{mime}:{pn}{DLNA_FLAGS}"


def natkey(s):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s.casefold())]


def _int(s):
    m = re.match(r"\s*(\d+)", str(s or ""))
    return int(m.group(1)) if m else 0


# ═══════════════════════════════════════════════════════════════════════════ configuración

def _merge(defecto, actual):
    out = {}
    for k, v in defecto.items():
        if isinstance(v, dict):
            out[k] = _merge(v, actual.get(k, {}) if isinstance(actual.get(k), dict) else {})
        else:
            out[k] = actual.get(k, v)
    return out


def cargar_config():
    actual = {}
    if RUTA_CONFIG.exists():
        try:
            actual = json.loads(RUTA_CONFIG.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            log.error("Invalid config.json (%s); using defaults", e)
    cfg = _merge(CONFIG_DEFECTO, actual)
    aplicar_idioma(cfg)
    if not str(cfg["servidor"]["nombre"]).strip():
        cfg["servidor"]["nombre"] = T("nombre_defecto", host=socket.gethostname())[:64]
    if cfg != actual:
        guardar_config(cfg)
    return cfg


def aplicar_idioma(cfg):
    i18n.fijar(cfg["servidor"].get("idioma"))
    VISTAS.clear()
    VISTAS.update((k, T("vista." + k)) for k in CLAVES_VISTAS)


def guardar_config(cfg):
    RUTA_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUTA_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, RUTA_CONFIG)


def validar_config(nueva):
    """Devuelve (config_limpia, errores, avisos)."""
    errores, avisos = [], []
    cfg = _merge(CONFIG_DEFECTO, nueva if isinstance(nueva, dict) else {})
    s, m, p = cfg["servidor"], cfg["musica"], cfg["portadas"]
    s["nombre"] = str(s["nombre"]).strip()[:64]
    if not s["nombre"]:
        errores.append(T("e.nombre"))
    try:
        s["puerto"] = int(s["puerto"])
        if not 1024 <= s["puerto"] <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        errores.append(T("e.puerto"))
    s["idioma"] = str(s["idioma"] or "auto").lower()
    if s["idioma"] not in ("auto",) + i18n.IDIOMAS:
        s["idioma"] = "auto"
    s["ip"] = str(s["ip"] or "").strip()
    if s["ip"]:
        try:
            ipaddress.IPv4Address(s["ip"])
        except ValueError:
            errores.append(T("e.ip"))
    carpetas = []
    for c in m["carpetas"] if isinstance(m["carpetas"], list) else []:
        c = str(c).strip().strip('"')
        if not c:
            continue
        c = os.path.normpath(c)
        if ES_WINDOWS and re.fullmatch(r"[A-Za-z]:", c):
            c += "\\"
        if c in carpetas:
            continue
        if not os.path.isdir(c):
            avisos.append(T("e.no_existe", ruta=c))
        carpetas.append(c)
    m["carpetas"] = carpetas
    m["ignorar"] = [str(x).strip() for x in (m["ignorar"] if isinstance(m["ignorar"], list) else []) if str(x).strip()]
    try:
        m["ordenar_por_tags_hasta"] = max(0, int(m["ordenar_por_tags_hasta"]))
    except (TypeError, ValueError):
        errores.append(T("e.orden"))
    try:
        p["tamano"] = int(p["tamano"])
        if p["tamano"] != 0 and not 100 <= p["tamano"] <= 2000:
            raise ValueError
    except (TypeError, ValueError):
        errores.append(T("e.portada"))
    p["recortar_cuadrado"] = bool(p["recortar_cuadrado"])
    s["abrir_navegador"] = bool(s["abrir_navegador"])
    pc = cfg["precarga"]
    pc["activa"], pc["portadas"] = bool(pc["activa"]), bool(pc["portadas"])
    try:
        pc["inactividad_min"] = round(float(pc["inactividad_min"]), 2)
        pc["repetir_horas"] = round(float(pc["repetir_horas"]), 2)
        if pc["inactividad_min"] < 0.1 or pc["repetir_horas"] < 0:
            raise ValueError
    except (TypeError, ValueError):
        errores.append(T("e.precarga"))
    v = cfg["vistas"]
    v["activas"] = [x for x in (v["activas"] if isinstance(v["activas"], list) else []) if x in VISTAS]
    v["mostrar_conteo"] = bool(v["mostrar_conteo"])
    v["mostrar_anio"] = bool(v["mostrar_anio"])
    if v["tipo_conteo"] not in ("pistas", "elementos"):
        v["tipo_conteo"] = "pistas"
    m["agrupar_carpetas"] = bool(m["agrupar_carpetas"])
    cfg["dashboard"]["con_servidor"] = bool(cfg["dashboard"]["con_servidor"])
    try:
        v["agrupar_desde"] = max(0, int(v["agrupar_desde"]))
    except (TypeError, ValueError):
        errores.append(T("e.agrupar"))
    d = str(cfg["cache"]["directorio"] or "").strip().strip('"')
    cfg["cache"]["directorio"] = os.path.normpath(d) if d else ""
    if d:
        try:
            os.makedirs(cfg["cache"]["directorio"], exist_ok=True)
        except OSError as e:
            errores.append(T("e.cache", err=e))
    return cfg, errores, avisos


def dir_cache(cfg):
    d = cfg["cache"]["directorio"]
    return Path(d) if d else DIR_DATOS


# ═══════════════════════════════════════════════════════════════════════════ tags

def _val(v):
    if v is None:
        return ""
    if hasattr(v, "text"):
        v = v.text
    if isinstance(v, (list, tuple)):
        if not v:
            return ""
        v = v[0]
    if isinstance(v, tuple):
        v = v[0]
    return str(v).strip()


def _first(tags, *keys):
    if tags is None:
        return ""
    for k in keys:
        try:
            s = _val(tags.get(k))
        except Exception:
            s = ""
        if s:
            return s
    return ""


def _unir(valores):
    """Varios valores de un tag (ARTIST= repetido, ID3 con varios textos…) -> "A; B" sin duplicados."""
    out = []
    for v in valores:
        v = str(v).strip()
        if v and v not in out:
            out.append(v)
    return "; ".join(out)


def _todos(tags, *keys):
    """Todos los valores del primer tag que exista (para artist, albumartist y genre multivalor)."""
    if tags is None:
        return ""
    for k in keys:
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v is None:
            continue
        if hasattr(v, "genres"):                              # ID3 TCON: resuelve "(17)" -> "Rock"
            v = v.genres
        elif hasattr(v, "text"):
            v = v.text
        if not isinstance(v, (list, tuple)):
            v = [v]
        s = _unir(x[0] if isinstance(x, tuple) else x for x in v)
        if s:
            return s
    return ""


def _bandera(tags, *keys):
    """True si un tag tipo COMPILATION / TCMP / cpil está activo."""
    s = _first(tags, *keys).lower()
    return s in ("1", "true", "yes")


def imagenes_embebidas(f):
    """[(tipo, bytes)] de las portadas embebidas en cualquier formato soportado por mutagen."""
    pics = []
    try:
        if getattr(f, "pictures", None):                       # FLAC
            pics += [(p.type, p.data) for p in f.pictures]
    except Exception:
        pass
    tags = getattr(f, "tags", None)
    if tags is None:
        return pics
    try:
        if hasattr(tags, "getall"):                            # ID3
            pics += [(fr.type, fr.data) for fr in tags.getall("APIC")]
    except Exception:
        pass
    try:
        if "covr" in tags:                                     # MP4
            pics += [(3, bytes(c)) for c in tags["covr"]]
    except Exception:
        pass
    try:
        if "metadata_block_picture" in tags:                   # Ogg / Opus
            for x in tags["metadata_block_picture"]:
                p = Picture(base64.b64decode(x))
                pics.append((p.type, p.data))
    except Exception:
        pass
    for key in ("Cover Art (Front)", "Cover Art (Other)"):    # APEv2
        try:
            if key in tags:
                raw = tags[key].value
                pics.append((3, raw.split(b"\x00", 1)[1] if b"\x00" in raw else raw))
        except Exception:
            pass
    return [p for p in pics if p[1]]


# Sube cuando cambia lo que se extrae de los tags: la caché se vacía una vez y se vuelve a leer.
# 2: todos los valores de artist/albumartist/genre, originaldate primero, albumsort y compilation.
VERSION_LECTOR = 2


def _meta_de_tags(meta, get, todos):
    """get(k) -> primer valor; todos(k) -> todos los valores unidos con "; " (tags Vorbis)."""
    meta["title"] = get("title")
    meta["artist"] = todos("artist")
    meta["album"] = get("album")
    meta["aartist"] = todos("albumartist") or todos("album artist")
    meta["track"] = _int(get("tracknumber"))
    meta["disc"] = _int(get("discnumber"))
    m = re.search(r"\d{4}", get("originaldate") or get("originalyear") or get("date") or get("year") or "")
    meta["year"] = int(m.group(0)) if m else 0
    meta["genre"] = todos("genre")
    meta["asort"] = get("albumsort")
    meta["comp"] = 1 if get("compilation").lower() in ("1", "true", "yes") else 0


def leer_flac_rapido(ruta):
    """Lee solo STREAMINFO + VORBIS_COMMENT y detecta PICTURE SIN cargar la imagen (que puede pesar MB).
    Unos KB por archivo en vez de todo el bloque de metadatos. None si el archivo no es FLAC "limpio"."""
    with open(ruta, "rb") as fh:
        if fh.read(4) != b"fLaC":
            return None                                   # p. ej. ID3 delante: que lo lea mutagen
        info, tags, art = None, {}, False
        while True:
            h = fh.read(4)
            if len(h) < 4:
                return None
            ultimo, tipo, largo = h[0] & 0x80, h[0] & 0x7F, int.from_bytes(h[1:4], "big")
            if tipo == 0 and largo >= 18:                 # STREAMINFO
                d = fh.read(largo)
                sr = (d[10] << 12) | (d[11] << 4) | (d[12] >> 4)
                ch = ((d[12] >> 1) & 0x07) + 1
                bits = (((d[12] & 1) << 4) | (d[13] >> 4)) + 1
                total = ((d[13] & 0x0F) << 32) | int.from_bytes(d[14:18], "big")
                info = (sr, ch, bits, total)
            elif tipo == 4:                               # VORBIS_COMMENT
                d = fh.read(largo)
                p = 4 + int.from_bytes(d[0:4], "little")
                n = int.from_bytes(d[p:p + 4], "little")
                p += 4
                for _ in range(n):
                    l = int.from_bytes(d[p:p + 4], "little")
                    k, _, v = d[p + 4:p + 4 + l].decode("utf-8", "replace").partition("=")
                    tags.setdefault(k.lower(), []).append(v.strip())
                    p += 4 + l
            else:
                if tipo == 6:                             # PICTURE: basta con saber que existe
                    art = True
                fh.seek(largo, 1)
            if ultimo:
                break
        inicio_audio = fh.tell()
        tam = os.fstat(fh.fileno()).st_size
    if not info or not info[0]:
        return None
    sr, ch, bits, total = info
    dur = total / sr if total else 0
    meta = {"dur": round(dur, 3), "sr": sr, "ch": ch, "bits": bits,
            "br": int((tam - inicio_audio) * 8 / dur) if dur else 0}
    _meta_de_tags(meta, lambda k: next((v for v in tags.get(k, ()) if v), ""), lambda k: _unir(tags.get(k, ())))
    meta["art"] = art
    return {k: v for k, v in meta.items() if v}


def leer_tags(ruta):
    if ruta[-5:].lower() == ".flac":
        try:
            m = leer_flac_rapido(ruta)
            if m is not None:
                return m
        except Exception as e:
            log.debug("Fast FLAC reader failed on %s (%s); falling back to mutagen", ruta, e)
    meta = {}
    try:
        f = mutagen.File(ruta)
    except Exception as e:
        log.debug("Could not read %s: %s", ruta, e)
        return meta
    if f is None:
        return meta
    info = getattr(f, "info", None)
    if info is not None:
        meta["dur"] = round(float(getattr(info, "length", 0) or 0), 3)
        meta["br"] = int(getattr(info, "bitrate", 0) or 0)
        meta["sr"] = int(getattr(info, "sample_rate", 0) or 0)
        meta["ch"] = int(getattr(info, "channels", 0) or 0)
        meta["bits"] = int(getattr(info, "bits_per_sample", 0) or 0)
    t = f.tags
    meta["title"] = _first(t, "title", "TIT2", "\xa9nam", "Title")
    meta["artist"] = _todos(t, "artist", "TPE1", "\xa9ART", "Artist")
    meta["album"] = _first(t, "album", "TALB", "\xa9alb", "Album")
    meta["aartist"] = _todos(t, "albumartist", "album artist", "TPE2", "aART", "Album Artist")
    meta["track"] = _int(_first(t, "tracknumber", "TRCK", "trkn", "Track"))
    meta["disc"] = _int(_first(t, "discnumber", "TPOS", "disk", "Disc"))
    m = re.search(r"\d{4}", _first(t, "originaldate", "originalyear", "TDOR", "TXXX:originalyear", "date", "year",
                                     "TDRC", "TYER", "\xa9day", "Year"))
    meta["year"] = int(m.group(0)) if m else 0
    meta["genre"] = _todos(t, "genre", "TCON", "\xa9gen", "Genre")
    meta["asort"] = _first(t, "albumsort", "TSOA", "soal", "ALBUMSORT")
    meta["comp"] = 1 if _bandera(t, "compilation", "TCMP", "cpil", "Compilation") else 0
    meta["art"] = bool(imagenes_embebidas(f))
    return {k: v for k, v in meta.items() if v}


class CacheTags:
    """Tags leídos, persistidos en SQLite y validados por mtime + tamaño.
    Lecturas en paralelo; las que no terminan dentro del presupuesto siguen en segundo plano."""

    def __init__(self, ruta):
        from concurrent.futures import ThreadPoolExecutor
        self.db = sqlite3.connect(str(ruta), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS pistas (ruta TEXT PRIMARY KEY, mtime INTEGER, tam INTEGER, meta TEXT)")
        # identidad del archivo independiente de la ruta: mover un álbum no obliga a releerlo
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_identidad ON pistas (tam, mtime)")
        v = self.db.execute("PRAGMA user_version").fetchone()[0]
        if v < VERSION_LECTOR:
            n = self.db.execute("SELECT COUNT(*) FROM pistas").fetchone()[0]
            if n:
                log.info("Tag reader updated (v%d -> v%d): %d cached tracks will be read again", v, VERSION_LECTOR, n)
                self.db.execute("DELETE FROM pistas")
            self.db.execute(f"PRAGMA user_version={VERSION_LECTOR}")
        self.reubicados = 0
        self.version = 0                  # sube con cada cambio; el índice por etiquetas se reconstruye al cambiar
        self.lock = threading.Lock()
        self.leidos = 0
        self.pool = ThreadPoolExecutor(8, thread_name_prefix="tags")
        self.pool_fondo = ThreadPoolExecutor(1, thread_name_prefix="precarga")
        self.en_vuelo = {}
        self.gen_precarga = 0

    def _guardar(self, ruta, mt, tam, meta):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO pistas VALUES (?,?,?,?)",
                            (ruta, mt, tam, json.dumps(meta, ensure_ascii=False)))
            self.leidos += 1
            self.version += 1

    def _leer(self, ruta, mt, tam):
        try:
            m = leer_tags(ruta)
            self._guardar(ruta, mt, tam, m)
            return m
        finally:
            with self.lock:
                self.en_vuelo.pop(ruta, None)

    def _en_cache(self, archivos):
        """Busca por ruta; si no está (archivo movido/renombrado de carpeta), por identidad:
        mismo nombre de archivo + tamaño + mtime al nanosegundo. Lo encontrado así se re-registra en la ruta nueva."""
        res, faltan, reubicar = {}, [], []
        with self.lock:
            for ruta, mt, tam in archivos:
                row = self.db.execute("SELECT mtime, tam, meta FROM pistas WHERE ruta=?", (ruta,)).fetchone()
                if row and row[0] == mt and row[1] == tam:
                    res[ruta] = json.loads(row[2])
                    continue
                nombre = os.path.basename(ruta).casefold()
                hit = next((m for r, m in self.db.execute("SELECT ruta, meta FROM pistas WHERE tam=? AND mtime=?", (tam, mt))
                            if os.path.basename(r).casefold() == nombre), None)
                if hit is not None:
                    res[ruta] = json.loads(hit)
                    reubicar.append((ruta, mt, tam, hit))
                else:
                    faltan.append((ruta, mt, tam))
            if reubicar:
                self.db.executemany("INSERT OR REPLACE INTO pistas VALUES (?,?,?,?)", reubicar)
                self.reubicados += len(reubicar)
                self.version += 1
        return res, faltan

    def podar(self, vigentes):
        """Borra de la caché las pistas que ya no existen (rutas fuera de 'vigentes'). Devuelve cuántas."""
        with self.lock:
            viejas = [(r,) for (r,) in self.db.execute("SELECT ruta FROM pistas") if r not in vigentes]
            if viejas:
                self.db.execute("BEGIN")
                self.db.executemany("DELETE FROM pistas WHERE ruta=?", viejas)
                self.db.execute("COMMIT")
                self.version += 1
        return len(viejas)

    def obtener(self, archivos, espera=None):
        """archivos: [(ruta, mtime_ns, tam)] -> {ruta: meta}.
        Con 'espera' (s), las pistas que no se leyeron a tiempo NO vienen en el resultado
        (el llamador usa datos de la ruta) y se terminan de leer en segundo plano."""
        from concurrent.futures import wait
        res, faltan = self._en_cache(archivos)
        if not faltan:
            return res
        futs = {}
        with self.lock:
            for ruta, mt, tam in faltan:
                f = self.en_vuelo.get(ruta)
                if f is None or (not f.running() and not f.done() and f.cancel()):
                    f = self.pool.submit(self._leer, ruta, mt, tam)
                    self.en_vuelo[ruta] = f
                futs[ruta] = f
        wait(list(futs.values()), timeout=espera)
        for ruta, f in futs.items():
            if f.done() and not f.cancelled():
                try:
                    res[ruta] = f.result()
                except Exception:
                    res[ruta] = {}
        return res

    def precargar(self, archivos):
        """Lee en segundo plano (un hilo, baja prioridad) lo que falte. Una precarga nueva cancela la anterior."""
        self.gen_precarga += 1
        gen = self.gen_precarga

        def tarea():
            if gen != self.gen_precarga:
                return
            _, faltan = self._en_cache(archivos)
            for ruta, mt, tam in faltan:
                if gen != self.gen_precarga:
                    return
                with self.lock:
                    if ruta in self.en_vuelo:
                        continue
                try:
                    self._guardar(ruta, mt, tam, leer_tags(ruta))
                except Exception:
                    pass
        self.pool_fondo.submit(tarea)

    def total(self):
        c = getattr(self, "_total", None)
        if c and c[1] == self.version:
            return c[0]
        with self.lock:
            n = self.db.execute("SELECT COUNT(*) FROM pistas").fetchone()[0]
        self._total = (n, self.version)
        return n

    def limpiar(self):
        with self.lock:
            self.db.execute("DELETE FROM pistas")
            self.db.execute("VACUUM")
            self.version += 1


# ═══════════════════════════════════════════════════════════════════════════ biblioteca

class Biblioteca:
    """Árbol de carpetas en vivo. IDs de objeto estables y sin estado:
         d<raiz>:<ruta/relativa>   carpeta
         t<raiz>:<ruta/relativa>   pista
       <raiz> = hash corto de la ruta de la carpeta configurada."""

    def __init__(self, app):
        self.app = app
        self.dir_cache = OrderedDict()
        self.dir_lock = threading.Lock()
        self.art_src_cache = OrderedDict()
        self.configurar(app.cfg)

    def configurar(self, cfg):
        self.raices = OrderedDict()
        for c in cfg["musica"]["carpetas"]:
            key = hashlib.sha1(os.path.normcase(os.path.abspath(c)).encode("utf-8")).hexdigest()[:8]
            nombre = os.path.basename(c.rstrip("\\/")) or c.rstrip("\\/") or c
            self.raices[key] = (os.path.abspath(c), nombre)
        self.ignorar = [p.casefold() for p in cfg["musica"]["ignorar"]]
        self.limite_tags = cfg["musica"]["ordenar_por_tags_hasta"]
        with self.dir_lock:
            self.dir_cache.clear()
            self.art_src_cache.clear()

    def raices_disponibles(self):
        return [(k, r, n) for k, (r, n) in self.raices.items() if os.path.isdir(r)]

    # ---- rutas
    def resolver(self, key, rel):
        raiz = self.raices.get(key)
        if raiz is None:
            return None
        partes = [p for p in rel.split("/") if p]
        if any(p in (".", "..") or ":" in p or "\\" in p for p in partes):
            return None
        return os.path.join(raiz[0], *partes) if partes else raiz[0]

    @staticmethod
    def padre_rel(rel):
        return rel.rsplit("/", 1)[0] if "/" in rel else ""

    def _ignorado(self, nombre):
        n = nombre.casefold()
        return any(fnmatch.fnmatchcase(n, p) for p in self.ignorar)

    TTL_LISTADO = 20          # s; en Windows la fecha de la carpeta NO es fiable (mover carpetas adentro no la cambia)
    TTL_PORTADA = 300

    def listar(self, ruta, fresco=False, ttl=TTL_LISTADO, validar=True):
        """(carpetas, pistas[(nombre, mtime_ns, tam)], imagenes).
        fresco=True (la carpeta que se está navegando): siempre se lee del disco.
        Si no, se usa la caché solo si la fecha de la carpeta no cambió Y tiene menos de TTL_LISTADO segundos.
        Si el listado nuevo difiere del cacheado, se avisa a los controles (SystemUpdateID)."""
        if not validar and not fresco:                   # solo para números (childCount/conteos): sin tocar el disco
            with self.dir_lock:
                c = self.dir_cache.get(ruta)
                if c:
                    return c[2]
        try:
            mt = os.stat(ruta).st_mtime_ns
        except OSError:
            return None
        ahora = time.monotonic()
        with self.dir_lock:
            c = self.dir_cache.get(ruta)
            if c and not fresco and c[0] == mt and (ttl is None or ahora - c[1] < ttl):
                self.dir_cache.move_to_end(ruta)
                return c[2]
        dirs, pistas, imgs = [], [], []
        try:
            with os.scandir(ruta) as it:
                for e in it:
                    if self._ignorado(e.name):
                        continue
                    try:
                        st = e.stat()
                        if ES_WINDOWS and getattr(st, "st_file_attributes", 0) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                            continue
                        if e.is_dir():
                            dirs.append(e.name)
                            continue
                        ext = os.path.splitext(e.name)[1].lower()
                        if ext in AUDIO:
                            pistas.append((e.name, st.st_mtime_ns, st.st_size))
                        elif ext in IMAGENES:
                            imgs.append(e.name)
                    except OSError:
                        continue
        except OSError as ex:
            log.warning("Could not list %s: %s", ruta, ex)
            return None
        dirs.sort(key=natkey)
        pistas.sort(key=lambda x: natkey(x[0]))
        res = (dirs, pistas, imgs)
        with self.dir_lock:
            previo = self.dir_cache.get(ruta)
            self.dir_cache[ruta] = (mt, ahora, res)
            self.dir_cache.move_to_end(ruta)
            while len(self.dir_cache) > 8192:
                self.dir_cache.popitem(last=False)
        if previo and (previo[2][0] != dirs or [p[0] for p in previo[2][1]] != [p[0] for p in pistas]):
            log.info("Changes detected in %s", ruta)
            self.app.cambio_detectado()
        return res

    def meta_ruta(self, key, rel):
        """Metadatos deducidos de 'Artista/[Año] Álbum/NN. Título.ext' mientras se leen los tags reales."""
        partes = rel.split("/")
        nombre = os.path.splitext(partes[-1])[0]
        m = {}
        mm = re.match(r"^(?:(\d{1,2})-)?(\d{1,3})\s*[.\-_)]\s*(.+)$", nombre)
        if mm:
            m["disc"] = int(mm.group(1) or 0)
            m["track"] = int(mm.group(2))
            nombre = mm.group(3)
        elif " - " in nombre and len(partes) >= 1:
            art, _, tit = nombre.partition(" - ")
            m["artist"], nombre = art.strip(), tit
        m["title"] = nombre.strip()
        if len(partes) >= 2:
            alb = partes[-2]
            y = re.match(r"^\s*[\[(]?(\d{4})[\])]?\s*[-.]?\s*(.*)$", alb)
            if y and y.group(2):
                m["year"], alb = int(y.group(1)), y.group(2)
            m["album"] = alb
            if len(partes) >= 3 and "artist" not in m:
                m["artist"] = partes[-3]
        return {k: v for k, v in m.items() if v}

    def precargar_subcarpetas(self, ruta, dirs):
        """Al ver un artista, deja listos en caché los tags de sus álbumes (solo un nivel)."""
        archivos = []
        for d in dirs[:60]:
            l = self.listar(os.path.join(ruta, d), ttl=None)
            if l:
                archivos += [(os.path.join(ruta, d, n), mt, sz) for n, mt, sz in l[1]]
            if len(archivos) >= 600:
                break
        if archivos:
            self.app.tags.precargar(archivos[:600])

    def modo_album(self, ruta):
        """True si las pistas de la carpeta son de un solo álbum: entonces comparten la portada de la carpeta
        (una imagen por álbum en vez de una por pista). Carpetas mixtas (playlists) usan la de cada pista."""
        ck = ("album", ruta)                             # decisión cacheada 5 min: sin tocar el disco por cada pista
        with self.dir_lock:
            c = self.art_src_cache.get(ck)
            if c and time.monotonic() - c[0] < self.TTL_PORTADA:
                return c[1]
        l = self.listar(ruta, ttl=None, validar=False)
        if not l or len(l[1]) < 2:
            return True
        muestra = l[1][:60]
        metas, faltan = self.app.tags._en_cache([(os.path.join(ruta, n), m, s) for n, m, s in muestra])
        albums = {m.get("album", "").casefold() for m in metas.values() if m.get("album")}
        res = len(albums) <= 1
        if not faltan:                                   # solo se fija cuando ya se conocen todos los tags
            with self.dir_lock:
                self.art_src_cache[ck] = (time.monotonic(), res)
        return res

    def conteo(self, ruta):
        l = self.listar(ruta, ttl=None, validar=False)   # solo para childCount: un número viejo no hace daño
        return len(l[0]) + len(l[1]) if l else 0

    # ---- navegación
    def hijos(self, oid):
        """Lista de descriptores ('raiz', key) | ('d', key, rel) | ('t', key, rel, mtime, tam) o None si no existe."""
        if oid == "0":
            base = self.hijos_raiz()
            if base and self.app.cfg["musica"]["agrupar_carpetas"]:          # todas las carpetas bajo "Carpetas"
                base = [("v", "v:carpetas", T("carpetas"), len(base), None, "object.container.storageFolder", None, None)]
            return base + self.app.indice.raiz()
        if oid == "v:carpetas":
            return self.hijos_raiz()
        if oid.startswith("v:"):
            return self.app.indice.hijos(oid)
        m = re.fullmatch(r"d([0-9a-f]{8}):(.*)", oid, re.S)
        if not m:
            return None
        key, rel = m.group(1), m.group(2)
        ruta = self.resolver(key, rel)
        l = self.listar(ruta, fresco=True) if ruta else None      # on demand: lo que hay en disco AHORA
        if l is None:
            return None
        pre = f"{rel}/" if rel else ""
        out = [("d", key, pre + d) for d in l[0]]
        pistas = list(l[1])
        if l[0]:
            self.precargar_subcarpetas(ruta, l[0])
        if pistas and len(pistas) <= self.limite_tags:
            metas = self.app.tags.obtener([(os.path.join(ruta, n), mt, sz) for n, mt, sz in pistas], espera=1.5)

            def orden(p):
                mm = metas.get(os.path.join(ruta, p[0])) or self.meta_ruta(key, pre + p[0])
                tr = _int(mm.get("track", 0))
                return (0 if tr else 1, _int(mm.get("disc", 0)), tr, natkey(p[0]))
            try:
                pistas.sort(key=orden)
            except Exception:
                log.exception("Sorting %s by tags failed; using file names", ruta)
        out += [("t", key, pre + n, mt, sz) for n, mt, sz in pistas]
        if l[0] and self.app.indice.asegurar(espera=1.0) is not None:   # carpeta con subcarpetas: "▶ Todas las pistas"
            n = self.app.indice.conteo_carpeta(key, rel)          # el número sale del índice; la lista solo al entrar
            if n:
                out.insert(0, self.app.indice._entrada_todas(f"v:carpeta:{key}:{rel}", n))
        return out

    def hijos_raiz(self):
        """Carpetas configuradas; con una sola, directamente su contenido."""
        raices = self.raices_disponibles()
        if len(raices) == 1:
            return self.hijos(f"d{raices[0][0]}:") or []
        return [("raiz", k) for k, _, _ in raices]

    def padre_raices(self):
        return "v:carpetas" if self.app.cfg["musica"]["agrupar_carpetas"] else "0"

    def metadata(self, oid):
        """(descriptor, parentID) para BrowseMetadata."""
        if oid == "0":
            return ("cero",), "-1"
        if oid == "v:carpetas":
            return ("v", oid, T("carpetas"), len(self.hijos_raiz()), None, "object.container.storageFolder",
                    None, None), "0"
        if oid.startswith("v:"):
            return self.app.indice.metadata(oid)
        m = re.fullmatch(r"([dt])([0-9a-f]{8}):(.*)", oid, re.S)
        if not m:
            return None, None
        tipo, key, rel = m.groups()
        ruta = self.resolver(key, rel)
        if not ruta or not (os.path.isdir(ruta) if tipo == "d" else os.path.isfile(ruta)):
            return None, None
        unica = len(self.raices_disponibles()) == 1
        if tipo == "d" and rel == "":
            return ("raiz", key), self.padre_raices()
        prel = self.padre_rel(rel)
        parent = self.padre_raices() if (prel == "" and unica) else f"d{key}:{prel}"
        if tipo == "d":
            return ("d", key, rel), parent
        st = os.stat(ruta)
        return ("t", key, rel, st.st_mtime_ns, st.st_size), parent

    # ---- DIDL
    def oid(self, desc):
        if desc[0] == "cero":
            return "0"
        if desc[0] == "raiz":
            return f"d{desc[1]}:"
        return f"{desc[0]}{desc[1]}:{desc[2]}"

    def render_lista(self, descs, parent, base):
        """Renderiza varios descriptores; lee los tags de las pistas en un solo lote."""
        pistas = [d for d in descs if d[0] == "t"]
        metas = {}
        if pistas:
            rutas = [(self.resolver(d[1], d[2]), d[3], d[4]) for d in pistas]
            metas = self.app.tags.obtener(rutas, espera=1.5)
        out = []
        for d in descs:
            try:                                         # un archivo o carpeta problemático no tumba la página entera
                if d[0] == "t":
                    mm = metas.get(self.resolver(d[1], d[2])) or self.meta_ruta(d[1], d[2])
                    out.append(self._item(d, parent, base, mm))
                else:
                    out.append(self._contenedor(d, parent, base))
            except Exception:
                log.exception("Skipping %r while rendering %r", d[:3], parent)
        return out

    def _contenedor(self, d, parent, base):
        if d[0] == "v":                                    # ("v", oid, titulo, n, art, clase, artista, anio)
            _, oid, titulo, n, art, clase, artista, anio = d
            if art:
                art = base + art
            return self._xml_cont(oid, parent, titulo, n, art, clase, artista, anio)
        if d[0] == "cero":
            n = len(self.hijos("0") or [])
            return self._xml_cont("0", "-1", "root", n, None)
        key = d[1]
        rel = "" if d[0] == "raiz" else d[2]
        ruta = self.resolver(key, rel)
        titulo = self.raices[key][1] if d[0] == "raiz" else rel.rsplit("/", 1)[-1]
        l = self.listar(ruta, ttl=None, validar=False)   # un solo listado para childCount y el conteo del título
        if self.app.cfg["vistas"]["mostrar_conteo"]:
            n = max(self.app.indice.conteo_carpeta(key, rel), len(l[1]) if l else 0)
            titulo += self.app.indice.suf(n, (len(l[0]) + len(l[1])) if l else 0)
        qrel = quote(rel, safe="/")
        art = f"{base}/art/d/{key}/{qrel}{'/' if qrel else ''}cover.jpg"
        return self._xml_cont(self.oid(d), parent, titulo, (len(l[0]) + len(l[1])) if l else 0, art)

    def _xml_cont(self, oid, parent, titulo, n, art, clase="object.container.storageFolder", artista=None, anio=None):
        s = (f'<container id="{esc(oid)}" parentID="{esc(parent)}" restricted="1" searchable="0" childCount="{n}">'
             f"<dc:title>{esc(titulo)}</dc:title><upnp:class>{clase}</upnp:class>")
        if artista:
            s += f"<upnp:artist>{esc(artista)}</upnp:artist><dc:creator>{esc(artista)}</dc:creator>"
        if anio:
            s += f"<dc:date>{anio}-01-01</dc:date>"
        if art:
            s += f"<upnp:albumArtURI{self.app.art_profile}>{esc(art)}</upnp:albumArtURI>"
        return s + "</container>"

    def _item(self, d, parent, base, mm):
        _, key, rel, _, tam = d
        nombre = rel.rsplit("/", 1)[-1]
        ext = os.path.splitext(nombre)[1].lower()
        mime = AUDIO.get(ext, "application/octet-stream")
        prel = self.padre_rel(rel)
        carpeta = prel.rsplit("/", 1)[-1] if prel else self.raices[key][1]
        titulo = mm.get("title") or os.path.splitext(nombre)[0]
        artista = mm.get("artist") or mm.get("aartist") or ""
        album = mm.get("album") or carpeta
        qrel = quote(rel, safe="/")
        if mm.get("art") and not self.modo_album(os.path.dirname(self.resolver(key, rel))):
            art = f"{base}/art/t/{key}/{qrel}.jpg"
        else:
            qp = quote(prel, safe="/")                          # portada de su carpeta, sin buscar en subcarpetas
            art = f"{base}/art/a/{key}/{qp}{'/' if qp else ''}cover.jpg"
        s = (f'<item id="t{key}:{esc(rel)}" parentID="{esc(parent)}" restricted="1">'
             f"<dc:title>{esc(titulo)}</dc:title><upnp:class>object.item.audioItem.musicTrack</upnp:class>")
        if artista:
            s += f"<dc:creator>{esc(artista)}</dc:creator><upnp:artist>{esc(artista)}</upnp:artist>"
        if mm.get("aartist"):
            s += f'<upnp:artist role="AlbumArtist">{esc(mm["aartist"])}</upnp:artist>'
        s += f"<upnp:album>{esc(album)}</upnp:album>"
        if mm.get("genre"):
            s += f"<upnp:genre>{esc(mm['genre'])}</upnp:genre>"
        if mm.get("track"):
            s += f"<upnp:originalTrackNumber>{mm['track']}</upnp:originalTrackNumber>"
        if mm.get("year"):
            s += f"<dc:date>{mm['year']}-01-01</dc:date>"
        s += f"<upnp:albumArtURI{self.app.art_profile}>{esc(art)}</upnp:albumArtURI>"
        attrs = f' protocolInfo="{protocol_info(mime)}" size="{tam}"'
        if mm.get("dur"):
            dd = mm["dur"]
            attrs += f' duration="{int(dd // 3600)}:{int(dd % 3600 // 60):02d}:{dd % 60:06.3f}"'
        if mm.get("br"):
            attrs += f' bitrate="{mm["br"] // 8}"'
        if mm.get("sr"):
            attrs += f' sampleFrequency="{mm["sr"]}"'
        if mm.get("ch"):
            attrs += f' nrAudioChannels="{mm["ch"]}"'
        if mm.get("bits"):
            attrs += f' bitsPerSample="{mm["bits"]}"'
        s += f'<res{attrs}>{esc(base)}/m/{key}/{esc(qrel)}</res>'
        return s + "</item>"

    # ---- portadas
    def fuente_portada_pista(self, ruta):
        return ("emb", ruta) if os.path.isfile(ruta) else None

    def fuente_portada_carpeta(self, ruta, prof=0, max_prof=2):
        try:
            mt = os.stat(ruta).st_mtime_ns
        except OSError:
            return None
        ck = (ruta, mt, max_prof)
        with self.dir_lock:
            c = self.art_src_cache.get(ck)
            if c and time.monotonic() - c[0] < self.TTL_PORTADA:
                return c[1]
        l = self.listar(ruta, ttl=None)
        src = None
        if l:
            dirs, pistas, imgs = l
            if imgs:
                def pref(n):
                    stem = os.path.splitext(n)[0].strip().casefold()
                    return (PORTADA_PREF.index(stem) if stem in PORTADA_PREF else len(PORTADA_PREF), natkey(n))
                src = ("img", os.path.join(ruta, sorted(imgs, key=pref)[0]))
            if not src and pistas:
                metas = self.app.tags.obtener([(os.path.join(ruta, n), m, s) for n, m, s in pistas[:4]])
                hit = next((r for r, mm in metas.items() if mm.get("art")), None)
                if hit:
                    src = ("emb", hit)
            if not src and prof < max_prof:
                for d in dirs[:6]:
                    src = self.fuente_portada_carpeta(os.path.join(ruta, d), prof + 1, max_prof)
                    if src:
                        break
        with self.dir_lock:
            self.art_src_cache[ck] = (time.monotonic(), src)
            while len(self.art_src_cache) > 8192:
                self.art_src_cache.popitem(last=False)
        return src


def generar_portada(src, tamano, recortar):
    tipo, ruta = src
    if tipo == "img":
        with open(ruta, "rb") as fh:
            data = fh.read()
    else:
        f = mutagen.File(ruta)
        pics = imagenes_embebidas(f) if f is not None else []
        if not pics:
            return None
        data = next((d for t, d in pics if t == 3), pics[0][1])
    if Image is None:
        mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return mime, data
    try:
        im = Image.open(io.BytesIO(data))
        if tamano:
            im.draft("RGB", (tamano * 2, tamano * 2))
        im = im.convert("RGB")
        w, h = im.size
        if recortar and max(w, h) > 1.15 * min(w, h):          # miniaturas 16:9 de YouTube, etc.
            lado = min(w, h)
            im = im.crop(((w - lado) // 2, (h - lado) // 2, (w - lado) // 2 + lado, (h - lado) // 2 + lado))
        if tamano:
            im.thumbnail((tamano, tamano), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=88, optimize=True)
        return "image/jpeg", out.getvalue()
    except Exception as e:
        log.debug("Pillow could not process the cover of %s: %s", ruta, e)
        return None


# ═══════════════════════════════════════════════════════════════════════════ vistas por etiquetas

CLAVES_VISTAS = ("artistas", "interpretes", "albumes", "generos", "decadas", "recientes", "hires", "aleatorio")
VISTAS = OrderedDict((k, k) for k in CLAVES_VISTAS)      # clave -> nombre visible (lo llena aplicar_idioma)
ARTICULOS = ("the ",)          # como MusicBee/MinimServer: "The Cure" en la C; "Los Prisioneros" se queda en la L
C_ALBUM, C_ARTISTA, C_GENERO = "object.container.album.musicAlbum", "object.container.person.musicArtist", "object.container.genre.musicGenre"


def _norm(s):
    return " ".join((s or "").split()).casefold()


def _sin_articulo(s):
    n = _norm(s)
    for a in ARTICULOS:
        if n.startswith(a) and len(n) > len(a):
            return n[len(a):]
    return n


def _orden(s):
    """Orden natural, sin artículo y sin acentos (Ángela junto a Andrés, no después de la Z)."""
    import unicodedata
    n = unicodedata.normalize("NFKD", _sin_articulo(s))
    return natkey("".join(c for c in n if not unicodedata.combining(c)))


def _letra(s):
    import unicodedata
    n = _sin_articulo(s)
    c = unicodedata.normalize("NFD", n[:1])[:1].upper() if n else "#"
    return c if c.isalpha() and c.isascii() else ("0-9" if c.isdigit() else "#")


def _hid(*partes):
    return hashlib.sha1("|".join(_norm(p) for p in partes).encode("utf-8")).hexdigest()[:10]


def _multi(v, seps=";"):
    return [x.strip() for x in re.split("[" + re.escape(seps) + "]", v or "") if x.strip()]


_RE_DISCO = re.compile(r"(?i)^\s*[\[(]?\s*(cd|disc|disk|disco|disque|vol(ume)?\.?)\s*[-_.]?\s*\d+\b")


def _carpeta_album(carpeta):
    """Carpeta que representa al álbum: 'Artista/Álbum/CD2' -> 'Artista/Álbum' (discos en subcarpetas)."""
    if "/" in carpeta:
        padre, ultima = carpeta.rsplit("/", 1)
    else:
        padre, ultima = "", carpeta
    return padre if ultima and _RE_DISCO.match(ultima) else carpeta


class Indice:
    """Índice por etiquetas construido desde la caché de tags (SQLite). No lee archivos de música:
    solo comprueba que sigan existiendo. Se reconstruye en segundo plano cuando la caché cambia (máx. 1/min)."""

    def __init__(self, app):
        self.app = app
        self.d = None
        self.version = -1
        self.t_build = 0.0
        self.construyendo = False
        self.lock = threading.Lock()
        self.listo = threading.Event()

    # ---- construcción
    def asegurar(self, espera=4.0):
        v = self.app.tags.version
        with self.lock:
            lanzar = (not self.construyendo and time.monotonic() - getattr(self, "t_fallo", -1e9) > 60
                      and (self.d is None or (v != self.version and time.monotonic() - self.t_build > 60)))
            if lanzar:
                self.construyendo = True
        if lanzar:
            threading.Thread(target=self._construir, daemon=True, name="indice").start()
        if self.d is None:
            self.listo.wait(espera)
        return self.d

    def _raiz_de(self, ruta, raices):
        rc = os.path.normcase(ruta)
        for key, pref, plen in raices:
            if rc.startswith(pref):
                return key, ruta[plen:].replace("\\", "/")
        return None

    def _construir(self):
        t0 = time.time()
        try:
            v = self.app.tags.version
            with self.app.tags.lock:
                filas = self.app.tags.db.execute("SELECT ruta, mtime, tam, meta FROM pistas").fetchall()
            raices = []
            for key, (r, _) in self.app.bib.raices.items():
                pref = os.path.normcase(r if r.endswith(os.sep) else r + os.sep)
                raices.append((key, pref, len(pref)))
            # Verificar que los archivos sigan ahí con UN listado por carpeta (no un stat por archivo):
            # ~11x menos llamadas al disco. Los valores de scandir son los mismos que guardó listar().
            por_dir = defaultdict(list)
            for fila in filas:
                kr = self._raiz_de(fila[0], raices)
                if kr:
                    por_dir[os.path.dirname(fila[0])].append((fila, kr))
            vigentes = []
            for carpeta_abs, items in por_dir.items():
                nombres = {os.path.basename(f[0]): (f[1], f[2]) for f, _ in items}
                vivos = set()
                try:
                    with os.scandir(carpeta_abs) as it:
                        for e in it:
                            esperado = nombres.get(e.name)
                            if esperado:
                                try:
                                    st = e.stat()
                                except OSError:
                                    continue
                                if (st.st_mtime_ns, st.st_size) == esperado:
                                    vivos.add(e.name)
                except OSError:
                    continue                            # carpeta movida/borrada: nada de ella sigue vigente
                vigentes += [(f, kr) for f, kr in items if os.path.basename(f[0]) in vivos]
            vigentes.sort(key=lambda x: x[0][0])
            pistas, albumes = [], {}
            ids_alb, generos_txt = {}, {}               # memos: un hash por álbum y un split por género, no por pista
            conservar = ("title", "artist", "aartist", "album", "track", "disc", "year", "genre", "bits", "sr",
                         "asort", "comp")
            repetidos = ("artist", "aartist", "album", "genre")      # mismo texto en muchas pistas: una sola copia
            intern = sys.intern
            # 1) pistas + "álbum crudo" (título, artista del álbum). Se anota en qué carpetas aparece cada uno:
            #    el mismo título del mismo artista en carpetas distintas (reediciones, homónimos) son álbumes distintos.
            crudos, carpetas_de = [], defaultdict(set)
            for (ruta, mt, tam, js), kr in vigentes:
                completo = json.loads(js)
                m = {k: completo[k] for k in conservar if k in completo}
                for k in repetidos:
                    if k in m:
                        m[k] = intern(m[k])
                key, rel = kr
                carpeta = rel.rsplit("/", 1)[0] if "/" in rel else ""
                pistas.append({"key": key, "rel": rel, "mt": mt, "tam": tam, "m": m, "dir": intern(carpeta)})
                alb = m.get("album") or (carpeta.rsplit("/", 1)[-1] if carpeta else self.app.bib.raices[key][1])
                aa = m.get("aartist", "")
                crudo = (alb, aa if aa else f"{key}/{carpeta}")
                dalb = f"{key}/{_carpeta_album(carpeta)}"
                crudos.append((crudo, dalb))
                carpetas_de[crudo].add(dalb)
            # 2) álbumes
            for i, p in enumerate(pistas):
                m, (crudo, dalb) = p["m"], crudos[i]
                ck = crudo if len(carpetas_de[crudo]) == 1 else crudo + (dalb,)   # IDs estables si no hay homónimos
                aid = ids_alb.get(ck)
                if aid is None:
                    aid = ids_alb[ck] = _hid(*ck)
                a = albumes.get(aid)
                if a is None:
                    a = albumes[aid] = {"id": aid, "titulo": crudo[0], "aa": m.get("aartist", ""), "artistas": Counter(),
                                        "anio": 0, "pistas": [], "key": p["key"], "dir": p["dir"], "mt": 0,
                                        "hires": False, "generos": set(), "asort": "", "comp": False}
                p["al"] = aid
                a["pistas"].append(i)
                a["artistas"][m.get("artist", "")] += 1
                a["anio"] = max(a["anio"], m.get("year", 0))
                a["mt"] = max(a["mt"], p["mt"])
                a["asort"] = a["asort"] or m.get("asort", "")
                a["comp"] = a["comp"] or bool(m.get("comp"))
                if (m.get("bits") or 0) > 16 or (m.get("sr") or 0) > 48000:
                    a["hires"] = True
                gtxt = m.get("genre")
                if gtxt:
                    gs = generos_txt.get(gtxt)
                    if gs is None:
                        gs = generos_txt[gtxt] = _multi(gtxt, ";/")
                    a["generos"].update(gs)
            artistas, generos, interpretes = {}, {}, {}
            varios = T("varios")
            for a in albumes.values():
                if a["aa"]:
                    a["artista"] = a["aa"]
                else:
                    nombres = [n for n in a["artistas"] if n]
                    a["artista"] = (nombres[0] if len(nombres) == 1 and not a["comp"]
                                    else (varios if nombres else T("desconocido")))
                a["pistas"].sort(key=lambda i: (pistas[i]["m"].get("disc", 0), pistas[i]["m"].get("track", 0) or 999,
                                                pistas[i]["rel"]))       # nombres "NN. Título": desempate simple basta
                for n in _multi(a["artista"]):
                    ar = artistas.setdefault(_hid(n), {"id": _hid(n), "nombre": n, "albumes": []})
                    ar["albumes"].append(a["id"])
                for g in a["generos"]:
                    ge = generos.setdefault(_hid(g), {"id": _hid(g), "nombre": g, "artistas": {}})
                    for n in _multi(a["artista"]):
                        ge["artistas"].setdefault(_hid(n), []).append(a["id"])
                for i in a["pistas"]:                            # artistas de pista (featuring, compilaciones…)
                    for n in _multi(pistas[i]["m"].get("artist", "")):
                        ip = interpretes.setdefault(_hid(n), {"id": _hid(n), "nombre": n, "albumes": {}})
                        ip["albumes"].setdefault(a["id"], []).append(i)
            por_anio = lambda aid: (albumes[aid]["anio"] or 9999, _orden(albumes[aid]["titulo"]))
            por_titulo = lambda aid: (_orden(albumes[aid]["asort"] or albumes[aid]["titulo"]),)
            for ar in artistas.values():                         # compilaciones: por título (o albumsort), no por año
                ar["albumes"].sort(key=por_titulo if ar["nombre"] == varios else por_anio)
            for ge in generos.values():
                for k, l in ge["artistas"].items():
                    l.sort(key=por_titulo if artistas.get(k, {}).get("nombre") == varios else por_anio)
            for ip in interpretes.values():
                ip["orden"] = sorted(ip["albumes"], key=por_anio)
            d = {
                "pistas": pistas, "albumes": albumes, "artistas": artistas, "generos": generos, "interpretes": interpretes,
                "orden_artistas": sorted(artistas, key=lambda k: _orden(artistas[k]["nombre"])),
                "orden_interpretes": sorted(interpretes, key=lambda k: _orden(interpretes[k]["nombre"])),
                "orden_albumes": sorted(albumes, key=lambda k: (_orden(albumes[k]["asort"] or albumes[k]["titulo"]),
                                                               _orden(albumes[k]["artista"]), albumes[k]["anio"])),
                "orden_generos": sorted(generos, key=lambda k: _orden(generos[k]["nombre"])),
                "recientes": sorted(albumes, key=lambda k: -albumes[k]["mt"])[:200],
                "hires": sorted((k for k in albumes if albumes[k]["hires"]),
                                key=lambda k: (_orden(albumes[k]["artista"]),) + por_anio(k)),
                "decadas": {},
                "conteo_dir": Counter(),         # (raiz, carpeta rel) -> pistas debajo (recursivo)
            }
            directas = Counter((p["key"], p["dir"]) for p in pistas)
            for (k, carpeta), n in directas.items():
                d["conteo_dir"][(k, "")] += n
                if carpeta:
                    partes = carpeta.split("/")
                    for j in range(1, len(partes) + 1):
                        d["conteo_dir"][(k, "/".join(partes[:j]))] += n
            for k in sorted(albumes, key=por_anio):
                y = albumes[k]["anio"]
                if y:
                    d["decadas"].setdefault(y // 10 * 10, []).append(k)
            self.d, self.version, self.t_build = d, v, time.monotonic()
            # textos normalizados para la búsqueda: ya con el índice publicado (no retrasa la navegación)
            d["norm"] = [{"title": _norm(p["m"].get("title")), "artist": _norm(p["m"].get("artist") or p["m"].get("aartist")),
                          "album": _norm(p["m"].get("album")), "genre": _norm(p["m"].get("genre"))} for p in pistas]
            log.info("Tag index: %d tracks, %d albums, %d artists, %d genres (%.1f s)",
                     len(pistas), len(albumes), len(artistas), len(generos), time.time() - t0)
        except Exception:
            self.t_fallo = time.monotonic()
            log.exception("Building the tag index (retrying in 60 s)")
        finally:
            self.construyendo = False
            self.listo.set()

    def resumen(self):
        d = self.d
        return {"listo": d is not None, "construyendo": self.construyendo,
                "pistas": len(d["pistas"]) if d else 0, "albumes": len(d["albumes"]) if d else 0,
                "artistas": len(d["artistas"]) if d else 0, "generos": len(d["generos"]) if d else 0}

    # ---- conteos ("Korn (15)")
    def suf(self, pistas, elementos=None):
        """Sufijo de conteo: " (15)". tipo_conteo = "pistas" (canciones) o "elementos" (álbumes de un artista,
        artistas de un género…; los álbumes no llevan número porque sus hijos son las pistas)."""
        v = self.app.cfg["vistas"]
        if not v["mostrar_conteo"]:
            return ""
        n = elementos if v.get("tipo_conteo") == "elementos" else pistas
        return f" ({n:,})" if n else ""

    def con_anio(self, a, titulo=None):
        t = titulo or a["titulo"]
        return f"{t} [{a['anio']}]" if a["anio"] and self.app.cfg["vistas"].get("mostrar_anio") and not titulo else t

    def n_albumes(self, d, aids):
        return sum(len(d["albumes"][a]["pistas"]) for a in set(aids))

    def conteo_carpeta(self, key, rel):
        d = self.d
        return d["conteo_dir"].get((key, rel), 0) if d else 0

    # ---- descriptores
    def _art_album(self, a):
        q = quote(a["dir"], safe="/")
        return f"/art/a/{a['key']}/{q}{'/' if q else ''}cover.jpg"

    def _c_album(self, d, aid, oid=None, titulo=None):
        a = d["albumes"][aid]
        return ("v", oid or f"v:al:{aid}", self.con_anio(a, titulo) + self.suf(len(a["pistas"])), len(a["pistas"]),
                self._art_album(a), C_ALBUM, a["artista"], a["anio"] or None)

    def _c_album_ia(self, d, iid, aid):
        """Álbum dentro de un artista de pista: solo cuenta (y contiene) las pistas de ese artista."""
        a, n = d["albumes"][aid], len(d["interpretes"][iid]["albumes"][aid])
        return ("v", f"v:ia:{iid}:al:{aid}", self.con_anio(a) + self.suf(n), n, self._art_album(a), C_ALBUM,
                a["artista"], a["anio"] or None)

    def _c_interprete(self, d, iid):
        ip = d["interpretes"][iid]
        art = self._art_album(d["albumes"][ip["orden"][0]]) if ip["orden"] else None
        n = sum(len(l) for l in ip["albumes"].values())
        return ("v", f"v:ia:{iid}", ip["nombre"] + self.suf(n, len(ip["orden"])),
                len(ip["orden"]) + 1, art, C_ARTISTA, None, None)

    def _c_artista(self, d, arid, oid=None, albumes=None):
        ar = d["artistas"][arid]
        albs = albumes if albumes is not None else ar["albumes"]
        art = self._art_album(d["albumes"][albs[0]]) if albs else None
        return ("v", oid or f"v:ar:{arid}", ar["nombre"] + self.suf(self.n_albumes(d, albs), len(albs)),
                len(albs) + (1 if len(albs) else 0), art, C_ARTISTA, None, None)

    def _t(self, d, i):
        p = d["pistas"][i]
        return ("t", p["key"], p["rel"], p["mt"], p["tam"])

    def _agrupar(self, oid_base, ids, nombre_de, fabricar, pistas_de=None):
        lim = self.app.cfg["vistas"]["agrupar_desde"]
        if not lim or len(ids) <= lim:
            return [fabricar(k) for k in ids]
        grupos = OrderedDict()
        for k in ids:
            grupos.setdefault(_letra(nombre_de(k)), []).append(k)
        orden = sorted(grupos, key=lambda g: (g == "#", g == "0-9", g))
        return [("v", f"{oid_base}@{g}", g + (self.suf(sum(pistas_de(k) for k in grupos[g]), len(grupos[g]))
                                              if pistas_de else ""),
                 len(grupos[g]), None, "object.container.storageFolder", None, None) for g in orden]

    def raiz(self):
        activas = self.app.cfg["vistas"]["activas"]
        if not activas:
            return []
        d = self.asegurar(espera=0.5)
        out = []
        for k in activas:
            n = len(self.hijos(f"v:{k}") or []) if d else 1
            out.append(("v", f"v:{k}", VISTAS[k], n, None, "object.container.storageFolder", None, None))
        return out

    LIMITE_TODAS = 3000
    SIN_TODAS = {"v:artistas", "v:interpretes", "v:albumes", "v:generos", "v:decadas", "v:aleatorio", "v:indexando"}

    def _entrada_todas(self, base, n):
        tit = (T("todas", n=f"{n:,}") if n <= self.LIMITE_TODAS
               else T("todas.limite", lim=f"{self.LIMITE_TODAS:,}", n=f"{n:,}"))
        return ("v", f"{base}:todas", tit, min(n, self.LIMITE_TODAS), None, "object.container.playlistContainer", None, None)

    def todas(self, base):
        """Todas las pistas bajo un contenedor (vista o carpeta), en orden de navegación y sin duplicados.
        Se cachea por construcción del índice: paginar en el WiiM no la recalcula."""
        d = self.d
        if d is None:
            return []
        if not hasattr(self, "_todas_cache"):
            self._todas_cache = OrderedDict()
        ck = (base, id(d))
        with self.lock:
            if ck in self._todas_cache:
                self._todas_cache.move_to_end(ck)
                return self._todas_cache[ck]
        out = self._calcular_todas(d, base)
        with self.lock:
            self._todas_cache[ck] = out
            while len(self._todas_cache) > 64:
                self._todas_cache.popitem(last=False)
        return out

    def _calcular_todas(self, d, base):
        if base.startswith("v:carpeta:"):
            key, rel = base[10:18], base[19:]
            pref = rel + "/" if rel else ""
            sel = [i for i, p in enumerate(d["pistas"]) if p["key"] == key and p["rel"].startswith(pref)]
            nk = {}                                       # orden natural calculado una vez por carpeta, no por pista
            for i in sel:
                c = d["pistas"][i]["dir"]
                if c not in nk:
                    nk[c] = natkey(c)
            sel.sort(key=lambda i: (nk[d["pistas"][i]["dir"]], d["pistas"][i]["m"].get("disc", 0),
                                    d["pistas"][i]["m"].get("track", 0) or 999, d["pistas"][i]["rel"]))
            return [self._t(d, i) for i in sel]
        out, vistos = [], set()

        def rec(oid, prof):
            for h in self._hijos(oid) or []:
                if h[0] == "t":
                    if (h[1], h[2]) not in vistos:
                        vistos.add((h[1], h[2]))
                        out.append(h)
                elif h[0] == "v" and prof < 5:
                    rec(h[1], prof + 1)
        rec(base, 0)
        return out

    def hijos(self, oid):
        if oid.endswith(":todas"):
            if self.asegurar() is None:
                return []
            return self.todas(oid[:-6])[:self.LIMITE_TODAS]
        res = self._hijos(oid)
        if res and oid not in self.SIN_TODAS and self.d is not None and any(h[0] == "v" for h in res):
            t = self.todas(oid)
            if t:
                res = [self._entrada_todas(oid, len(t))] + res
        return res

    def _hijos(self, oid):
        d = self.asegurar()
        if d is None:
            return [("v", "v:indexando", T("indexando"), 0, None,
                     "object.container.storageFolder", None, None)]
        partes = oid[2:].split(":")
        vista, _, letra = partes[0].partition("@")
        try:
            if vista == "artistas":
                ids = d["orden_artistas"]
                if letra:
                    ids = [k for k in ids if _letra(d["artistas"][k]["nombre"]) == letra]
                    return [self._c_artista(d, k) for k in ids]
                return self._agrupar("v:artistas", ids, lambda k: d["artistas"][k]["nombre"], lambda k: self._c_artista(d, k),
                                     lambda k: self.n_albumes(d, d["artistas"][k]["albumes"]))
            if vista == "interpretes":
                ids = d["orden_interpretes"]
                if letra:
                    return [self._c_interprete(d, k) for k in ids if _letra(d["interpretes"][k]["nombre"]) == letra]
                return self._agrupar("v:interpretes", ids, lambda k: d["interpretes"][k]["nombre"],
                                     lambda k: self._c_interprete(d, k),
                                     lambda k: sum(len(l) for l in d["interpretes"][k]["albumes"].values()))
            if vista == "ia":                               # artista de pista -> sus álbumes -> solo sus pistas
                ip = d["interpretes"][partes[1]]
                if len(partes) > 3 and partes[2] == "al":
                    return [self._t(d, i) for i in ip["albumes"][partes[3]]]
                return [self._c_album_ia(d, partes[1], a) for a in ip["orden"]]
            if vista == "albumes":
                ids = d["orden_albumes"]
                if letra:
                    return [self._c_album(d, k) for k in ids if _letra(d["albumes"][k]["titulo"]) == letra]
                return self._agrupar("v:albumes", ids, lambda k: d["albumes"][k]["titulo"], lambda k: self._c_album(d, k),
                                     lambda k: len(d["albumes"][k]["pistas"]))
            if vista == "generos":
                return [("v", f"v:ge:{g}", d["generos"][g]["nombre"]
                         + self.suf(self.n_albumes(d, [a for l in d["generos"][g]["artistas"].values() for a in l]),
                                    len(d["generos"][g]["artistas"])),
                         len(d["generos"][g]["artistas"]),
                         self._art_album(d["albumes"][next(iter(d["generos"][g]["artistas"].values()))[0]]),
                         C_GENERO, None, None) for g in d["orden_generos"]]
            if vista == "decadas":
                return [("v", f"v:de:{dec}", f"{dec}s" + self.suf(self.n_albumes(d, d["decadas"][dec]), len(d["decadas"][dec])), len(d["decadas"][dec]), self._art_album(d["albumes"][d["decadas"][dec][0]]),
                         "object.container.storageFolder", None, None) for dec in sorted(d["decadas"])]
            if vista == "recientes":
                return [self._c_album(d, k) for k in d["recientes"]]
            if vista == "hires":
                return [self._c_album(d, k) for k in d["hires"]]
            if vista == "aleatorio":
                n = min(100, len(d["pistas"]))
                return [self._t(d, i) for i in random.Random(int(time.time() // 600)).sample(range(len(d["pistas"])), n)]
            if vista == "ar":                               # artista -> álbumes (la entrada "todas" es genérica)
                return [self._c_album(d, a) for a in d["artistas"][partes[1]]["albumes"]]
            if vista == "al":
                return [self._t(d, i) for i in d["albumes"][partes[1]]["pistas"]]
            if vista == "ge":                               # género -> artistas -> álbumes
                ge = d["generos"][partes[1]]
                if len(partes) > 3 and partes[2] == "ar":
                    return [self._c_album(d, a) for a in ge["artistas"][partes[3]]]
                return [self._c_artista(d, arid, oid=f"v:ge:{partes[1]}:ar:{arid}", albumes=ge["artistas"][arid])
                        for arid in sorted(ge["artistas"], key=lambda k: _orden(d["artistas"][k]["nombre"]))]
            if vista == "de":
                return [self._c_album(d, a, titulo=f"{d['albumes'][a]['anio']} · {d['albumes'][a]['titulo']}")
                        for a in d["decadas"][int(partes[1])]]
        except (KeyError, IndexError, ValueError):
            return None
        return None

    def padre(self, oid):
        partes = oid[2:].split(":")
        v = partes[0]
        if v in VISTAS or v.split("@")[0] in VISTAS:
            return "v:" + v.split("@")[0] if "@" in v else "0"
        return {"ar": "v:artistas", "ia": "v:interpretes", "al": "v:albumes", "ge": "v:generos",
                "de": "v:decadas"}.get(v, "0") \
            if len(partes) <= 2 else f"v:{v}:{partes[1]}"

    def metadata(self, oid):
        d = self.asegurar()
        if oid.endswith(":todas"):
            base = oid[:-6]
            t = self.todas(base) if d else []
            if not t:
                return None, None
            padre = f"d{base[10:18]}:{base[19:]}" if base.startswith("v:carpeta:") else base
            return self._entrada_todas(base, len(t)), padre
        partes = oid[2:].split(":")
        v, _, letra = partes[0].partition("@")
        try:
            if v in VISTAS and not letra and len(partes) == 1:
                desc = ("v", oid, VISTAS[v], len(self.hijos(oid) or []), None, "object.container.storageFolder", None, None)
            elif d is None:
                return None, None
            elif letra:
                desc = ("v", oid, letra, len(self.hijos(oid) or []), None, "object.container.storageFolder", None, None)
            elif v == "al":
                desc = self._c_album(d, partes[1])
            elif v == "ar" and len(partes) == 2:
                desc = self._c_artista(d, partes[1])
            elif v == "ge" and len(partes) == 4:
                ge = d["generos"][partes[1]]
                desc = self._c_artista(d, partes[3], oid=oid, albumes=ge["artistas"][partes[3]])
            elif v == "ia" and len(partes) == 2:
                desc = self._c_interprete(d, partes[1])
            elif v == "ia" and len(partes) == 4:
                desc = self._c_album_ia(d, partes[1], partes[3])
            else:
                hijos = self.hijos(oid)
                if hijos is None:
                    return None, None
                desc = ("v", oid, self.titulo(oid), len(hijos), None, "object.container.storageFolder", None, None)
        except (KeyError, IndexError, ValueError):
            return None, None
        return desc, self.padre(oid)

    def titulo(self, oid):
        """Nombre legible para el log/dashboard."""
        if oid.endswith(":todas"):
            base = oid[:-6]
            return (f"{T('t.carpeta')} / {base[19:] or '/'}" if base.startswith("v:carpeta:") else self.titulo(base)) + f" / {T('t.todas')}"
        d = self.d
        partes = oid[2:].split(":")
        v = partes[0].split("@")[0]
        try:
            if v in VISTAS:
                return VISTAS[v] + (f" / {partes[0].split('@')[1]}" if "@" in partes[0] else "")
            if v == "al":
                a = d["albumes"][partes[1]]
                return f"{T('t.album')} / {a['artista']} — {a['titulo']}"
            if v == "ar":
                return f"{T('t.artista')} / {d['artistas'][partes[1]]['nombre']}"
            if v == "ia":
                t = f"{T('t.artista')} / {d['interpretes'][partes[1]]['nombre']}"
                return t + (f" / {d['albumes'][partes[3]]['titulo']}" if len(partes) > 3 else "")
            if v == "ge":
                t = f"{T('t.genero')} / {d['generos'][partes[1]]['nombre']}"
                return t + (f" / {d['artistas'][partes[3]]['nombre']}" if len(partes) > 3 else "")
            if v == "de":
                return f"{T('t.decada')} / {partes[1]}s"
        except (KeyError, IndexError, TypeError):
            pass
        return oid

    # ---- búsqueda UPnP (ContentDirectory:Search)
    def buscar(self, criterio, limite=500):
        d = self.asegurar()
        if d is None:
            return []
        terminos = [( c.lower(), _norm(t.replace('\\"', '"')))
                    for c, t in re.findall(r'(dc:title|dc:creator|upnp:artist|upnp:album|upnp:genre)\s+contains\s+"((?:[^"\\]|\\.)*)"',
                                           criterio or "", re.I) if t.strip()]
        if not terminos:
            return []
        m = re.search(r'upnp:class\s+(?:derivedfrom|=)\s+"([^"]+)"', criterio or "", re.I)
        clase = (m.group(1) if m else "object.item").lower()
        campos = {"dc:title": "title", "dc:creator": "artist", "upnp:artist": "artist", "upnp:album": "album", "upnp:genre": "genre"}
        if "norm" not in d:                              # se prepara una vez por índice, al primer uso
            d["norm"] = [{"title": _norm(p["m"].get("title")), "artist": _norm(p["m"].get("artist") or p["m"].get("aartist")),
                          "album": _norm(p["m"].get("album")), "genre": _norm(p["m"].get("genre"))} for p in d["pistas"]]
        if "album" in clase:
            out = [self._c_album(d, k) for k in d["orden_albumes"]
                   if any(t in _norm(d["albumes"][k]["titulo"] if c in ("dc:title", "upnp:album") else d["albumes"][k]["artista"])
                          for c, t in terminos)]
        elif "person" in clase:
            out = [self._c_artista(d, k) for k in d["orden_artistas"]
                   if any(t in _norm(d["artistas"][k]["nombre"]) for c, t in terminos)]
        else:
            out = [self._t(d, i) for i, n in enumerate(d["norm"]) if any(t in n[campos[c]] for c, t in terminos)]
        return out[:limite]


# ═══════════════════════════════════════════════════════════════════════════ UPnP: XML

def device_xml(nombre, udn):
    def svc(stype, sid, short):
        return (f"<service><serviceType>{stype}</serviceType><serviceId>urn:upnp-org:serviceId:{sid}</serviceId>"
                f"<SCPDURL>/{short}.xml</SCPDURL><controlURL>/ctl/{short}</controlURL>"
                f"<eventSubURL>/evt/{short}</eventSubURL></service>")
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion><device>"
            f"<deviceType>{DEV_TYPE}</deviceType><friendlyName>{esc(nombre)}</friendlyName>"
            "<manufacturer>SupaVic</manufacturer><modelName>SupaVic DLNA</modelName>"
            f"<modelDescription>{esc(T('descripcion'))}</modelDescription><modelNumber>{VERSION}</modelNumber>"
            f"<serialNumber>{udn[-12:]}</serialNumber><UDN>uuid:{udn}</UDN>"
            "<dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC><presentationURL>/</presentationURL><serviceList>"
            + svc(NS_CDS, "ContentDirectory", "cds") + svc(NS_CMS, "ConnectionManager", "cms")
            + "</serviceList></device></root>")


def scpd(acciones, variables):
    a = ""
    for nombre, args in acciones:
        a += f"<action><name>{nombre}</name><argumentList>"
        for an, d, var in args:
            a += (f"<argument><name>{an}</name><direction>{d}</direction>"
                  f"<relatedStateVariable>{var}</relatedStateVariable></argument>")
        a += "</argumentList></action>"
    v = ""
    for nombre, tipo, eventos, permitidos in variables:
        v += f'<stateVariable sendEvents="{"yes" if eventos else "no"}"><name>{nombre}</name><dataType>{tipo}</dataType>'
        if permitidos:
            v += "<allowedValueList>" + "".join(f"<allowedValue>{x}</allowedValue>" for x in permitidos) + "</allowedValueList>"
        v += "</stateVariable>"
    return ('<?xml version="1.0" encoding="utf-8"?><scpd xmlns="urn:schemas-upnp-org:service-1-0">'
            "<specVersion><major>1</major><minor>0</minor></specVersion>"
            f"<actionList>{a}</actionList><serviceStateTable>{v}</serviceStateTable></scpd>")


CDS_SCPD = scpd(
    [("Browse", [("ObjectID", "in", "A_ARG_TYPE_ObjectID"), ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag"),
                 ("Filter", "in", "A_ARG_TYPE_Filter"), ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                 ("RequestedCount", "in", "A_ARG_TYPE_Count"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                 ("Result", "out", "A_ARG_TYPE_Result"), ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                 ("TotalMatches", "out", "A_ARG_TYPE_Count"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID")]),
     ("Search", [("ContainerID", "in", "A_ARG_TYPE_ObjectID"), ("SearchCriteria", "in", "A_ARG_TYPE_SearchCriteria"),
                 ("Filter", "in", "A_ARG_TYPE_Filter"), ("StartingIndex", "in", "A_ARG_TYPE_Index"),
                 ("RequestedCount", "in", "A_ARG_TYPE_Count"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria"),
                 ("Result", "out", "A_ARG_TYPE_Result"), ("NumberReturned", "out", "A_ARG_TYPE_Count"),
                 ("TotalMatches", "out", "A_ARG_TYPE_Count"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID")]),
     ("GetSystemUpdateID", [("Id", "out", "SystemUpdateID")]),
     ("GetSearchCapabilities", [("SearchCaps", "out", "SearchCapabilities")]),
     ("GetSortCapabilities", [("SortCaps", "out", "SortCapabilities")])],
    [("A_ARG_TYPE_ObjectID", "string", False, None), ("A_ARG_TYPE_Result", "string", False, None),
     ("A_ARG_TYPE_BrowseFlag", "string", False, ["BrowseMetadata", "BrowseDirectChildren"]),
     ("A_ARG_TYPE_Filter", "string", False, None), ("A_ARG_TYPE_SortCriteria", "string", False, None),
     ("A_ARG_TYPE_SearchCriteria", "string", False, None),
     ("A_ARG_TYPE_Index", "ui4", False, None), ("A_ARG_TYPE_Count", "ui4", False, None),
     ("A_ARG_TYPE_UpdateID", "ui4", False, None), ("SearchCapabilities", "string", False, None),
     ("SortCapabilities", "string", False, None), ("SystemUpdateID", "ui4", True, None)])

CMS_SCPD = scpd(
    [("GetProtocolInfo", [("Source", "out", "SourceProtocolInfo"), ("Sink", "out", "SinkProtocolInfo")]),
     ("GetCurrentConnectionIDs", [("ConnectionIDs", "out", "CurrentConnectionIDs")]),
     ("GetCurrentConnectionInfo", [
         ("ConnectionID", "in", "A_ARG_TYPE_ConnectionID"), ("RcsID", "out", "A_ARG_TYPE_RcsID"),
         ("AVTransportID", "out", "A_ARG_TYPE_AVTransportID"), ("ProtocolInfo", "out", "A_ARG_TYPE_ProtocolInfo"),
         ("PeerConnectionManager", "out", "A_ARG_TYPE_ConnectionManager"),
         ("PeerConnectionID", "out", "A_ARG_TYPE_ConnectionID"), ("Direction", "out", "A_ARG_TYPE_Direction"),
         ("Status", "out", "A_ARG_TYPE_ConnectionStatus")])],
    [("SourceProtocolInfo", "string", True, None), ("SinkProtocolInfo", "string", True, None),
     ("CurrentConnectionIDs", "string", True, None),
     ("A_ARG_TYPE_ConnectionStatus", "string", False, ["OK", "ContentFormatMismatch", "InsufficientBandwidth", "UnreliableChannel", "Unknown"]),
     ("A_ARG_TYPE_ConnectionManager", "string", False, None),
     ("A_ARG_TYPE_Direction", "string", False, ["Input", "Output"]),
     ("A_ARG_TYPE_ProtocolInfo", "string", False, None), ("A_ARG_TYPE_ConnectionID", "i4", False, None),
     ("A_ARG_TYPE_AVTransportID", "i4", False, None), ("A_ARG_TYPE_RcsID", "i4", False, None)])

SOURCE_PROTOCOLS = ",".join(protocol_info(m) for m in sorted(set(AUDIO.values())))
DIDL_OPEN = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
             'xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">')


def soap_ok(service, accion, args):
    body = "".join(f"<{k}>{esc(v)}</{k}>" for k, v in args)
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{accion}Response xmlns:u="{service}">{body}</u:{accion}Response></s:Body></s:Envelope>')


def soap_fault(code, desc):
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault>'
            "<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>"
            f'<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>{code}</errorCode>'
            f"<errorDescription>{esc(desc)}</errorDescription></UPnPError></detail>"
            "</s:Fault></s:Body></s:Envelope>")


def localname(tag):
    return tag.rsplit("}", 1)[-1]


# ═══════════════════════════════════════════════════════════════════════════ red

def ips_locales():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def ip_hacia(destino):
    """IP local que el sistema usaría para llegar a 'destino' (correcta aunque haya VPN / Hyper-V)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((destino, 1900))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def cliente_permitido(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback or a.is_link_local
    except ValueError:
        return False


# ═══════════════════════════════════════════════════════════════════════════ eventos

class Eventos:
    """Bitácora JSONL de actividad para el dashboard. Rota a 5 MB (conserva 5).
    El lector abre/lee/cierra en cada consulta, así que en Windows el renombrado no choca;
    si chocara, se reintenta en la siguiente escritura."""
    MAX = 5_000_000
    COPIAS = 5

    def __init__(self, ruta):
        self.ruta = ruta
        self.lock = threading.Lock()
        self.ultimo_desc = {}

    def emitir(self, tipo, **datos):
        datos = {"t": round(time.time(), 3), "tipo": tipo, **datos}
        linea = json.dumps(datos, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            try:
                if self.ruta.exists() and self.ruta.stat().st_size > self.MAX:
                    self._rotar()
                with open(self.ruta, "a", encoding="utf-8") as fh:
                    fh.write(linea)
            except OSError as e:
                log.debug("Could not write event: %s", e)

    def _rotar(self):
        try:
            for i in range(self.COPIAS - 1, 0, -1):
                a = self.ruta.with_name(f"{self.ruta.name}.{i}")
                if a.exists():
                    os.replace(a, self.ruta.with_name(f"{self.ruta.name}.{i + 1}"))
            os.replace(self.ruta, self.ruta.with_name(f"{self.ruta.name}.1"))
        except OSError as e:
            log.debug("Event log rotation postponed: %s", e)

    def descubrimiento(self, ip, ua):
        ahora = time.time()
        if ahora - self.ultimo_desc.get(ip, 0) > 300:          # no llenar el log con re-anuncios
            self.ultimo_desc[ip] = ahora
            self.emitir("descubrimiento", ip=ip, ua=(ua or "")[:120])


# ═══════════════════════════════════════════════════════════════════════════ aplicación

class App:
    def __init__(self):
        self.cfg = cargar_config()
        self.inicio = time.time()
        # (el prefijo histórico se conserva: cambiarlo haría que los reproductores vean un servidor nuevo)
        self.udn = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"servidorMusicaDLNA:{socket.gethostname()}:{DIR_BASE}"))
        self.update_id = int(time.time()) % 100000
        self.dir_cache = dir_cache(self.cfg)
        try:
            self.dir_cache.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.error("Could not use the cache at %s (%s); using %s", self.dir_cache, e, DIR_DATOS)
            self.dir_cache = DIR_DATOS
        self.dir_portadas = self.dir_cache / "portadas"
        self.migrar_cache()
        self.tags = CacheTags(self.dir_cache / "cache.db")
        self.aplicar_portadas()
        self.bib = Biblioteca(self)
        self.art_mem = OrderedDict()
        self.art_lock = threading.Lock()
        self.subs = {}
        self.subs_lock = threading.Lock()
        self.clientes = {}
        self.cli_lock = threading.Lock()
        self.httpd = None
        self.ssdp = None
        self.eventos = Eventos(RUTA_EVENTOS)
        self.indice = Indice(self)
        self.ultima_nav = time.time()
        self.ultima_transfer = 0.0
        self.transfers = 0
        self.forzar_precarga = False
        self.precarga = None

    def migrar_cache(self):
        """Si la caché cambió de carpeta (p. ej. de datos\\ al NVMe) y la nueva está vacía, mueve la anterior."""
        if self.dir_cache.resolve() == DIR_DATOS.resolve() or (self.dir_cache / "cache.db").exists():
            return
        if not (DIR_DATOS / "cache.db").exists():
            return
        import shutil
        try:
            for n in ("cache.db", "cache.db-wal", "cache.db-shm"):
                if (DIR_DATOS / n).exists():
                    shutil.move(str(DIR_DATOS / n), str(self.dir_cache / n))
            if (DIR_DATOS / "portadas").is_dir() and not self.dir_portadas.exists():
                shutil.move(str(DIR_DATOS / "portadas"), str(self.dir_portadas))
            log.info("Previous cache moved from %s to %s", DIR_DATOS, self.dir_cache)
        except OSError as e:
            log.warning("Could not move the previous cache (%s); starting a new one", e)

    def dashboard_activo(self):
        """¿Responde el dashboard? (para el enlace del panel; cacheado 10 s)."""
        c = getattr(self, "_dash", None)
        if c and time.monotonic() - c[0] < 10:
            return c[1]
        try:
            with socket.create_connection(("127.0.0.1", self.cfg["dashboard"]["puerto"]), timeout=0.2):
                ok = True
        except OSError:
            ok = False
        self._dash = (time.monotonic(), ok)
        return ok

    def aplicar_portadas(self):
        p = self.cfg["portadas"]
        self.art_profile = ' dlna:profileID="JPEG_SM"' if Image and 0 < p["tamano"] <= 640 else ""

    @property
    def puerto(self):
        return self.cfg["servidor"]["puerto"]

    # ---- actividad de clientes (para el panel)
    def registrar(self, ip, ua, que):
        with self.cli_lock:
            c = self.clientes.setdefault(ip, {"ip": ip, "ua": "", "ultimo": 0, "accion": "", "sonando": ""})
            c["ultimo"] = time.time()
            if ua:
                c["ua"] = ua[:120]
            c["accion"] = que
            if que.startswith("▶ "):
                c["sonando"] = que[2:]

    # ---- portadas
    # ---- actividad (para pausar la precarga)
    def marcar_actividad(self):
        self.ultima_nav = time.time()
        self.forzar_precarga = False

    def inactivo(self):
        if self.transfers > 0 or time.time() - self.ultima_transfer < 10:
            return False
        if self.forzar_precarga:
            return True
        return time.time() - self.ultima_nav >= self.cfg["precarga"]["inactividad_min"] * 60

    def clave_portada(self, src):
        p = self.cfg["portadas"]
        try:
            st = os.stat(src[1])
        except OSError:
            return None
        # sin la carpeta en la clave: mover un álbum conserva sus portadas ya generadas
        nombre = os.path.basename(src[1]).casefold()
        return hashlib.sha1(f"{src[0]}|{nombre}|{st.st_mtime_ns}|{st.st_size}|{p['tamano']}|{p['recortar_cuadrado']}"
                            .encode("utf-8", "replace")).hexdigest()

    def portada_en_disco(self, src):
        clave = self.clave_portada(src)
        return clave is None or (self.dir_portadas / clave[:2] / f"{clave}.jpg").exists()

    def portada(self, src, guardar_en_memoria=True):
        if not src:
            return None
        p = self.cfg["portadas"]
        clave = self.clave_portada(src)
        if clave is None:
            return None
        with self.art_lock:
            if clave in self.art_mem:
                self.art_mem.move_to_end(clave)
                return self.art_mem[clave]
        archivo = self.dir_portadas / clave[:2] / f"{clave}.jpg"
        res = None
        if archivo.exists():
            res = ("image/jpeg", archivo.read_bytes())
        else:
            try:
                res = generar_portada(src, p["tamano"], p["recortar_cuadrado"])
            except Exception as e:
                log.debug("Cover %s: %s", src, e)
            if res and res[0] == "image/jpeg":
                archivo.parent.mkdir(parents=True, exist_ok=True)
                tmp = archivo.with_suffix(".tmp")
                tmp.write_bytes(res[1])
                os.replace(tmp, archivo)
        if guardar_en_memoria:
            with self.art_lock:
                self.art_mem[clave] = res
                while len(self.art_mem) > 256:
                    self.art_mem.popitem(last=False)
        return res

    def limpiar_cache(self):
        n = 0
        if self.dir_portadas.exists():
            for f in self.dir_portadas.rglob("*.jpg"):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        self.tags.limpiar()
        with self.art_lock:
            self.art_mem.clear()
        self.bib.configurar(self.cfg)
        self.contenido_cambio()
        return n

    def estadisticas_portadas(self):
        c = getattr(self, "_est_port", None)
        if c and time.monotonic() - c[0] < 60:
            return c[1]
        r = self._estadisticas_portadas()
        self._est_port = (time.monotonic(), r)
        return r

    def _estadisticas_portadas(self):
        n = tam = 0
        if self.dir_portadas.exists():
            for f in self.dir_portadas.rglob("*.jpg"):
                n += 1
                try:
                    tam += f.stat().st_size
                except OSError:
                    pass
        return n, tam

    # ---- configuración en caliente
    def actualizar_config(self, nueva):
        cfg, errores, avisos = validar_config(nueva)
        if errores:
            return {"ok": False, "errores": errores, "avisos": avisos}
        reiniciar = cfg["servidor"] != self.cfg["servidor"] or cfg["cache"] != self.cfg["cache"]
        cambio_portadas = cfg["portadas"] != self.cfg["portadas"]
        guardar_config(cfg)
        self.cfg = cfg
        self.bib.configurar(cfg)
        if cambio_portadas:
            self.aplicar_portadas()
            with self.art_lock:
                self.art_mem.clear()
        self.contenido_cambio()
        log.info("Settings saved%s", " (restarting)" if reiniciar else "")
        self.eventos.emitir("config", reiniciar=reiniciar)
        if self.precarga:
            self.precarga.ultimo_completo = 0                # ajustes nuevos: permitir otra pasada
        if reiniciar:
            threading.Timer(0.8, self.reiniciar).start()
        return {"ok": True, "avisos": avisos, "reiniciar": reiniciar}

    def cambio_detectado(self):
        """Agrupa cambios detectados en el disco (2 s) en un solo aviso a los controles."""
        with self.subs_lock:
            if getattr(self, "_cambio_pendiente", False):
                return
            self._cambio_pendiente = True

        def aplicar():
            self._cambio_pendiente = False
            self.contenido_cambio()
        threading.Timer(2.0, aplicar).start()

    def contenido_cambio(self):
        self.update_id += 1
        self.notificar_suscriptores()

    def reiniciar(self):
        log.info("Restarting process…")
        flags = 0x00000008 | 0x00000200 if ES_WINDOWS else 0      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:
            if self.ssdp:
                self.ssdp.notificar("ssdp:byebye")
                self.ssdp.sock.close()
            if self.httpd:
                self.httpd.server_close()
        except Exception:
            pass
        try:
            RUTA_PID.unlink()
        except OSError:
            pass
        if callable(AL_SALIR):
            try:
                AL_SALIR()
            except Exception:
                pass
        cmd = RELANZAR or [sys.executable, str(Path(__file__).resolve())]
        subprocess.Popen(cmd, cwd=str(DIR_BASE), creationflags=flags, close_fds=True)
        os._exit(0)

    # ---- GENA (eventos UPnP)
    def suscribir(self, callback, servicio, timeout):
        urls = re.findall(r"<([^>]+)>", callback or "")
        sid = f"uuid:{uuid.uuid4()}"
        with self.subs_lock:
            self.subs[sid] = [urls, servicio, time.time() + timeout, 0]
        threading.Thread(target=self._evento, args=(sid,), daemon=True).start()
        return sid

    def renovar(self, sid, timeout):
        with self.subs_lock:
            if sid not in self.subs:
                return False
            self.subs[sid][2] = time.time() + timeout
            return True

    def desuscribir(self, sid):
        with self.subs_lock:
            return self.subs.pop(sid, None) is not None

    def notificar_suscriptores(self):
        with self.subs_lock:
            sids = list(self.subs)
        for sid in sids:
            threading.Thread(target=self._evento, args=(sid,), daemon=True).start()

    def _evento(self, sid):
        time.sleep(0.2)
        with self.subs_lock:
            sub = self.subs.get(sid)
            if not sub or sub[2] < time.time():
                self.subs.pop(sid, None)
                return
            urls, servicio, _, seq = sub
            sub[3] += 1
        if servicio == "cds":
            props = [("SystemUpdateID", self.update_id), ("ContainerUpdateIDs", "")]
        else:
            props = [("SourceProtocolInfo", SOURCE_PROTOCOLS), ("SinkProtocolInfo", ""), ("CurrentConnectionIDs", "0")]
        body = ('<?xml version="1.0" encoding="utf-8"?><e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
                + "".join(f"<e:property><{k}>{esc(v)}</{k}></e:property>" for k, v in props)
                + "</e:propertyset>").encode()
        for url in urls:
            try:
                u = urlparse(url)
                c = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=5)
                c.request("NOTIFY", u.path or "/", body, {
                    "HOST": f"{u.hostname}:{u.port or 80}", "CONTENT-TYPE": 'text/xml; charset="utf-8"',
                    "NT": "upnp:event", "NTS": "upnp:propchange", "SID": sid, "SEQ": str(seq)})
                c.getresponse().read()
                c.close()
                return
            except Exception as e:
                log.debug("Event to %s failed: %s", url, e)

    def nombre_objeto(self, oid):
        """Ruta legible de un ObjectID para el dashboard: 'Discografia_FLAC/Bill Evans'."""
        if oid == "0":
            return "/"
        if oid == "v:carpetas":
            return T("carpetas")
        if oid.startswith("v:"):
            return self.indice.titulo(oid)
        m = re.fullmatch(r"[dt]([0-9a-f]{8}):(.*)", oid, re.S)
        if not m or m.group(1) not in self.bib.raices:
            return oid
        raiz = self.bib.raices[m.group(1)][1]
        return f"{raiz}/{m.group(2)}" if m.group(2) else raiz

    def meta_archivo(self, ruta):
        try:
            st = os.stat(ruta)
            return self.tags.obtener([(ruta, st.st_mtime_ns, st.st_size)]).get(ruta, {})
        except OSError:
            return {}

    # ---- SOAP
    def control(self, servicio, accion, args, base, ip=""):
        if servicio == "cds":
            if accion == "Browse":
                return self.browse(args, base, ip)
            if accion == "GetSystemUpdateID":
                return soap_ok(NS_CDS, accion, [("Id", self.update_id)])
            if accion == "Search":
                return self.buscar(args, base, ip)
            if accion == "GetSearchCapabilities":
                return soap_ok(NS_CDS, accion, [("SearchCaps", "dc:title,dc:creator,upnp:artist,upnp:album,upnp:genre,upnp:class")])
            if accion == "GetSortCapabilities":
                return soap_ok(NS_CDS, accion, [("SortCaps", "")])
        elif servicio == "cms":
            if accion == "GetProtocolInfo":
                return soap_ok(NS_CMS, accion, [("Source", SOURCE_PROTOCOLS), ("Sink", "")])
            if accion == "GetCurrentConnectionIDs":
                return soap_ok(NS_CMS, accion, [("ConnectionIDs", "0")])
            if accion == "GetCurrentConnectionInfo":
                return soap_ok(NS_CMS, accion, [
                    ("RcsID", -1), ("AVTransportID", -1), ("ProtocolInfo", ""), ("PeerConnectionManager", ""),
                    ("PeerConnectionID", -1), ("Direction", "Output"), ("Status", "OK")])
        return None

    def buscar(self, a, base, ip=""):
        try:
            inicio = max(0, int(a.get("StartingIndex") or 0))
            cuantos = max(0, int(a.get("RequestedCount") or 0))
        except ValueError:
            return soap_fault(402, "Invalid Args")
        t0 = time.time()
        res = self.indice.buscar(a.get("SearchCriteria", ""))
        pagina = res[inicio:inicio + cuantos] if cuantos else res[inicio:]
        items = self.bib.render_lista(pagina, a.get("ContainerID", "0") or "0", base)
        self.eventos.emitir("navegacion", ip=ip, carpeta=f"{T('t.busqueda')}: {a.get('SearchCriteria', '')[:80]}", inicio=inicio,
                            n=len(items), total=len(res), ms=round((time.time() - t0) * 1000, 1))
        return soap_ok(NS_CDS, "Search", [
            ("Result", DIDL_OPEN + "".join(items) + "</DIDL-Lite>"),
            ("NumberReturned", len(items)), ("TotalMatches", len(res)), ("UpdateID", self.update_id)])

    def browse(self, a, base, ip=""):
        oid = a.get("ObjectID", "0") or "0"
        flag = a.get("BrowseFlag", "BrowseDirectChildren")
        try:
            inicio = max(0, int(a.get("StartingIndex") or 0))
            cuantos = max(0, int(a.get("RequestedCount") or 0))
        except ValueError:
            return soap_fault(402, "Invalid Args")
        t0 = time.time()
        bib = self.bib
        if flag == "BrowseMetadata":
            desc, parent = bib.metadata(oid)
            if desc is None:
                return soap_fault(701, "No such object")
            items, total = bib.render_lista([desc], parent, base), 1
        elif flag == "BrowseDirectChildren":
            hijos = bib.hijos(oid)
            if hijos is None:
                return soap_fault(701, "No such object")
            total = len(hijos)
            pagina = hijos[inicio:inicio + cuantos] if cuantos else hijos[inicio:]
            descs = [("raiz", h[1]) if h[0] == "raiz" else h for h in pagina]
            items = bib.render_lista(descs, oid, base)
        else:
            return soap_fault(402, "Invalid Args")
        ms = (time.time() - t0) * 1000
        log.debug("Browse %s %r [%d+%d] -> %d/%d in %.0f ms", flag, oid, inicio, cuantos, len(items), total, ms)
        if flag == "BrowseDirectChildren":
            self.eventos.emitir("navegacion", ip=ip, carpeta=self.nombre_objeto(oid), inicio=inicio,
                                n=len(items), total=total, ms=round(ms, 1))
        return soap_ok(NS_CDS, "Browse", [
            ("Result", DIDL_OPEN + "".join(items) + "</DIDL-Lite>"),
            ("NumberReturned", len(items)), ("TotalMatches", total), ("UpdateID", self.update_id)])


# ═══════════════════════════════════════════════════════════════════════════ precarga en reposo

class Precargador(threading.Thread):
    """Con el servidor inactivo, recorre las carpetas y llena la caché: tags (lector rápido) y
    portadas de cada carpeta (y de cada pista en carpetas mixtas). Se detiene ante cualquier navegación
    y retoma en la siguiente inactividad saltando lo que ya está en caché."""

    def __init__(self, app):
        super().__init__(daemon=True, name="precarga")
        self.app = app
        self.ultimo_completo = 0.0
        self.estado = "esperando"
        self.st = {"carpetas": 0, "total_carpetas": 0, "pistas": 0, "portadas": 0, "actual": "",
                   "inicio": 0, "fin": 0}

    def resumen(self):
        cfg = self.app.cfg["precarga"]
        r = dict(self.st, estado=self.estado, activa=cfg["activa"], ultimo_completo=int(self.ultimo_completo))
        r["espera_seg"] = max(0, int(cfg["inactividad_min"] * 60 - (time.time() - self.app.ultima_nav)))
        return r

    def run(self):
        time.sleep(5)
        while True:
            time.sleep(3)
            cfg = self.app.cfg["precarga"]
            if not cfg["activa"]:
                self.estado = "desactivada"
                continue
            if self.ultimo_completo and time.time() - self.ultimo_completo < cfg["repetir_horas"] * 3600:
                self.estado = "completa"
                continue
            if not self.app.inactivo():
                if self.estado not in ("pausada",):
                    self.estado = "esperando"
                continue
            try:
                self.pasada()
            except Exception:
                log.exception("Prefetch")
                self.estado = "esperando"
                time.sleep(60)

    def _pausa(self):
        self.estado = "pausada"
        log.info("Prefetch paused by activity (%d tracks, %d covers this pass)",
                 self.st["pistas"], self.st["portadas"])
        self.app.eventos.emitir("precarga", accion="pausa", pistas=self.st["pistas"], portadas=self.st["portadas"],
                                carpetas=self.st["carpetas"], total=self.st["total_carpetas"])

    def pasada(self):
        app, bib = self.app, self.app.bib
        self.estado = "escaneando"
        self.st.update(carpetas=0, total_carpetas=0, pistas=0, portadas=0, actual="", inicio=int(time.time()), fin=0)
        log.info("Prefetch: server idle, filling cache…")
        app.eventos.emitir("precarga", accion="inicio")
        # 1) enumerar carpetas (DFS, mismo orden que en el WiiM)
        carpetas, pila = [], [r for r, _ in reversed(list(bib.raices.values())) if os.path.isdir(r)]
        while pila:
            if not app.inactivo():
                return self._pausa()
            ruta = pila.pop()
            l = bib.listar(ruta)
            if not l:
                continue
            carpetas.append(ruta)
            pila.extend(os.path.join(ruta, d) for d in reversed(l[0]))
            self.st["total_carpetas"] = len(carpetas)
        hacer_portadas = app.cfg["precarga"]["portadas"]
        vigentes, claves = set(), set()
        # 2) tags y portadas carpeta por carpeta
        for i, ruta in enumerate(carpetas):
            self.st["carpetas"], self.st["actual"] = i, ruta
            l = bib.listar(ruta)
            if not l:
                continue
            if not l[1]:
                if hacer_portadas:                       # carpeta de artista con su propia imagen
                    src = bib.fuente_portada_carpeta(ruta, max_prof=0)
                    if src:
                        claves.add(app.clave_portada(src))
                        if not app.portada_en_disco(src) and app.inactivo():
                            app.portada(src, guardar_en_memoria=False)
                continue
            archivos = [(os.path.join(ruta, n), m, s) for n, m, s in l[1]]
            vigentes.update(a[0] for a in archivos)
            _, faltan = app.tags._en_cache(archivos)
            for r, m, s in faltan:
                if not app.inactivo():
                    return self._pausa()
                try:
                    app.tags._guardar(r, m, s, leer_tags(r))
                    self.st["pistas"] += 1
                except Exception as e:
                    log.debug("Prefetch tags %s: %s", r, e)
            if not hacer_portadas:
                continue
            fuentes = [bib.fuente_portada_carpeta(ruta, max_prof=0)]
            if not bib.modo_album(ruta):
                metas, _ = app.tags._en_cache(archivos)
                fuentes += [("emb", r) for r, mm in metas.items() if mm.get("art")]
            for src in fuentes:
                if not src:
                    continue
                claves.add(app.clave_portada(src))
                if app.portada_en_disco(src):
                    continue
                if not app.inactivo():
                    return self._pausa()
                try:
                    if app.portada(src, guardar_en_memoria=False):
                        self.st["portadas"] += 1
                except Exception as e:
                    log.debug("Prefetch cover %s: %s", src, e)
        # 3) limpieza: pistas que ya no existen y portadas que nadie usa (solo la caché, nunca la música)
        quitadas = app.tags.podar(vigentes) if vigentes else 0
        borradas = 0
        if hacer_portadas and app.dir_portadas.exists():
            for f in app.dir_portadas.rglob("*.jpg"):
                try:
                    if f.stem not in claves and f.stat().st_mtime < self.st["inicio"]:
                        f.unlink()
                        borradas += 1
                except OSError:
                    pass
        if quitadas or borradas:
            log.info("Cache cleaned: %d missing tracks, %d unused covers", quitadas, borradas)
        self.st.update(carpetas=len(carpetas), actual="", fin=int(time.time()))
        self.ultimo_completo = time.time()
        self.estado = "completa"
        app.forzar_precarga = False
        log.info("Prefetch complete: %d folders, %d tracks read, %d covers generated in %.0f s",
                 len(carpetas), self.st["pistas"], self.st["portadas"], time.time() - self.st["inicio"])
        app.eventos.emitir("precarga", accion="fin", pistas=self.st["pistas"], portadas=self.st["portadas"],
                           carpetas=len(carpetas), seg=int(time.time() - self.st["inicio"]))


# ═══════════════════════════════════════════════════════════════════════════ HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_HDR
    sys_version = ""

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_address[0], fmt % args)

    def base(self):
        ip = self.connection.getsockname()[0]
        return f"http://{ip}:{self.server.server_address[1]}"

    def _send(self, code, body=b"", ctype='text/xml; charset="utf-8"', headers=None, head=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Date", formatdate(usegmt=True))
        self.send_header("EXT", "")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head and body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        url = urlparse(self.path)
        ruta = unquote(url.path)
        app = self.app
        if ruta == "/desc.xml":
            app.registrar(self.client_address[0], self.headers.get("User-Agent"), "descubrimiento")
            app.eventos.descubrimiento(self.client_address[0], self.headers.get("User-Agent"))
            return self._send(200, device_xml(app.cfg["servidor"]["nombre"], app.udn), head=head)
        if ruta == "/cds.xml":
            return self._send(200, CDS_SCPD, head=head)
        if ruta == "/cms.xml":
            return self._send(200, CMS_SCPD, head=head)
        if ruta.startswith("/m/"):
            return self.enviar_audio(ruta[3:], head)
        if ruta.startswith("/art/"):
            return self.enviar_portada(ruta[5:], head)
        if ruta in ("/", "/index.html"):
            return self._send(200, PANEL_HTML.replace("__LANG__", i18n.LANG), "text/html; charset=utf-8",
                              {"Cache-Control": "no-store"}, head=head)
        if ruta in ("/favicon.svg", "/favicon.ico"):
            return self._send(200, FAVICON_SVG, "image/svg+xml", {"Cache-Control": "max-age=86400"}, head=head)
        if ruta.startswith("/api/"):
            return self.api_get(ruta, parse_qs(url.query))
        return self._send(404, "", "text/plain", head=head)

    # ---- streaming
    def enviar_audio(self, resto, head):
        key, _, rel = resto.partition("/")
        app = self.app
        ruta = app.bib.resolver(key, rel)
        if not ruta or os.path.splitext(ruta)[1].lower() not in AUDIO or not os.path.isfile(ruta):
            return self._send(404, "", "text/plain", head=head)
        mime = AUDIO[os.path.splitext(ruta)[1].lower()]
        tam = os.path.getsize(ruta)
        ini, fin, code = 0, tam - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    ini = int(m.group(1))
                    fin = min(int(m.group(2)), tam - 1) if m.group(2) else tam - 1
                else:
                    ini = max(0, tam - int(m.group(2)))
                if ini >= tam or ini > fin:
                    return self._send(416, "", "text/plain", {"Content-Range": f"bytes */{tam}"}, head=head)
                code = 206
        if not head and ini == 0:
            app.registrar(self.client_address[0], self.headers.get("User-Agent"), "▶ " + rel)
        largo = fin - ini + 1
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(largo))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {ini}-{fin}/{tam}")
        self.send_header("Date", formatdate(usegmt=True))
        self.send_header("transferMode.dlna.org", "Streaming")
        self.send_header("contentFeatures.dlna.org", protocol_info(mime).split(":", 3)[3])
        self.end_headers()
        if head:
            return
        sid = uuid.uuid4().hex[:12]
        ip = self.client_address[0]
        meta = app.meta_archivo(ruta)
        meta.pop("art", None)
        base_ev = {"id": sid, "ip": ip, "raiz": app.bib.raices[key][1], "key": key, "rel": rel,
                   "ext": os.path.splitext(ruta)[1].lower().lstrip("."), "tam": tam, "ini": ini, "fin": fin}
        app.eventos.emitir("stream_inicio", ua=(self.headers.get("User-Agent") or "")[:120], meta=meta, **base_ev)
        t0 = time.time()
        enviados, estado = 0, "completo"
        app.transfers += 1
        try:
            with open(ruta, "rb") as fh:
                fh.seek(ini)
                falta = largo
                while falta > 0:
                    chunk = fh.read(min(262144, falta))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    enviados += len(chunk)
                    falta -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
            estado = "cortado"
        except OSError as e:
            log.debug("Stream %s: %s", ruta, e)
            self.close_connection = True
            estado = "error"
        finally:
            app.transfers -= 1
            app.ultima_transfer = time.time()
        app.eventos.emitir("stream", estado=estado, enviados=enviados, seg=round(time.time() - t0, 3), **base_ev)

    def enviar_portada(self, resto, head):
        app = self.app
        app.marcar_actividad()
        m = re.fullmatch(r"([dta])/([0-9a-f]{8})/(.*)", resto, re.S)
        src = None
        if m:
            tipo, key, rel = m.groups()
            if tipo == "t" and rel.endswith(".jpg"):
                ruta = app.bib.resolver(key, rel[:-4])
                src = app.bib.fuente_portada_pista(ruta) if ruta else None
            elif rel.endswith("cover.jpg"):                    # d = carpeta (busca en subcarpetas), a = solo esa carpeta
                ruta = app.bib.resolver(key, rel[:-9].rstrip("/"))
                src = app.bib.fuente_portada_carpeta(ruta, max_prof=2 if tipo == "d" else 0) if ruta else None
        res = app.portada(src)
        if not res:
            return self._send(404, "", "text/plain", head=head)
        mime, data = res
        return self._send(200, data, mime, {
            "Cache-Control": "max-age=604800",
            "contentFeatures.dlna.org": "DLNA.ORG_PN=JPEG_SM;DLNA.ORG_OP=00;DLNA.ORG_FLAGS=00f00000000000000000000000000000"
            if mime == "image/jpeg" else "*",
            "transferMode.dlna.org": "Interactive"}, head=head)

    # ---- API del panel
    def api_get(self, ruta, q):
        app = self.app
        if ruta == "/api/estado":
            ahora = time.time()
            with app.cli_lock:
                clientes = sorted((dict(c) for c in app.clientes.values() if ahora - c["ultimo"] < 86400),
                                  key=lambda c: -c["ultimo"])
            for c in clientes:
                c["hace"] = int(ahora - c["ultimo"])
            n_port, tam_port = app.estadisticas_portadas()
            raices = [{"ruta": r, "nombre": n, "disponible": os.path.isdir(r)} for r, n in app.bib.raices.values()]
            return self._json({
                "nombre": app.cfg["servidor"]["nombre"], "version": VERSION,
                "activo_desde": int(app.inicio), "uptime": int(ahora - app.inicio),
                "ips": app.ssdp.ips if app.ssdp else [], "puerto": app.puerto,
                "carpetas": raices, "pistas_cache": app.tags.total(), "pistas_leidas_sesion": app.tags.leidos,
                "portadas": n_port, "portadas_mb": round(tam_port / 1048576, 1),
                "clientes": clientes, "pillow": Image is not None, "cache": str(app.dir_cache),
                "python": sys.version.split()[0],
                "precarga": app.precarga.resumen() if app.precarga else None,
                "reubicados": app.tags.reubicados,
                "indice": app.indice.resumen(),
                "vistas_disponibles": list(VISTAS.items()),
                "idioma": i18n.LANG, "datos": str(DIR_BASE),
                "dashboard": {"puerto": app.cfg["dashboard"]["puerto"], "activo": app.dashboard_activo()},
            })
        if ruta == "/api/config":
            return self._json(app.cfg)
        if ruta == "/api/log":
            try:
                with open(RUTA_LOG, "r", encoding="utf-8", errors="replace") as fh:
                    lineas = fh.readlines()[-300:]
            except OSError:
                lineas = []
            return self._json({"lineas": [l.rstrip("\n") for l in lineas]})
        if ruta == "/api/explorar":
            if not cliente_permitido(self.client_address[0]):
                return self._json({"error": T("e.no_permitido")}, 403)
            return self._json(explorar((q.get("ruta") or [""])[0]))
        return self._json({"error": T("e.no_encontrado")}, 404)

    def do_POST(self):
        ruta = urlparse(self.path).path
        largo = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(largo) if largo else b""
        if ruta.startswith("/api/"):
            return self.api_post(ruta, body)
        servicio = {"/ctl/cds": "cds", "/ctl/cms": "cms"}.get(ruta)
        if not servicio:
            return self._send(404, "", "text/plain")
        try:
            root = ET.fromstring(body)
            soap_body = next(e for e in root if localname(e.tag) == "Body")
            act = soap_body[0]
            accion = localname(act.tag)
            args = {localname(c.tag): (c.text or "") for c in act}
        except Exception:
            return self._send(500, soap_fault(401, "Invalid Action"))
        if accion == "Browse":
            self.app.marcar_actividad()
            self.app.registrar(self.client_address[0], self.headers.get("User-Agent"), "navegando")
        try:
            resp = self.app.control(servicio, accion, args, self.base(), self.client_address[0])
        except Exception:
            log.exception("Error in %s.%s", servicio, accion)
            return self._send(500, soap_fault(501, "Action Failed"))
        if resp is None:
            return self._send(500, soap_fault(401, "Invalid Action"))
        return self._send(500 if "<s:Fault>" in resp else 200, resp)

    def api_post(self, ruta, body):
        if not cliente_permitido(self.client_address[0]):
            return self._json({"error": T("e.red_local")}, 403)
        app = self.app
        if ruta == "/api/config":
            try:
                datos = json.loads(body.decode("utf-8") or "{}")
            except ValueError:
                return self._json({"ok": False, "errores": [T("e.json")]}, 400)
            return self._json(app.actualizar_config(datos))
        if ruta == "/api/limpiar-cache":
            n = app.limpiar_cache()
            log.info("Cache cleared (%d covers)", n)
            return self._json({"ok": True, "portadas": n})
        if ruta == "/api/precarga-ahora":
            if app.precarga:
                app.precarga.ultimo_completo = 0
            app.forzar_precarga = True
            return self._json({"ok": True})
        if ruta == "/api/reiniciar":
            threading.Timer(0.5, app.reiniciar).start()
            return self._json({"ok": True})
        return self._json({"error": T("e.no_encontrado")}, 404)

    def do_SUBSCRIBE(self):
        svc = {"/evt/cds": "cds", "/evt/cms": "cms"}.get(urlparse(self.path).path)
        if not svc:
            return self._send(404, "", "text/plain")
        m = re.search(r"(\d+)", self.headers.get("TIMEOUT", "") or "")
        timeout = min(int(m.group(1)), 3600) if m else 1800
        sid = self.headers.get("SID")
        if sid:
            if not self.app.renovar(sid, timeout):
                return self._send(412, "", "text/plain")
        else:
            if "upnp:event" not in (self.headers.get("NT") or ""):
                return self._send(412, "", "text/plain")
            sid = self.app.suscribir(self.headers.get("CALLBACK"), svc, timeout)
        self._send(200, "", "text/plain", {"SID": sid, "TIMEOUT": f"Second-{timeout}"})

    def do_UNSUBSCRIBE(self):
        ok = self.app.desuscribir(self.headers.get("SID", ""))
        self._send(200 if ok else 412, "", "text/plain")


def explorar(ruta):
    """Lista subcarpetas para el selector de carpetas del panel."""
    if not ruta:
        if ES_WINDOWS:
            unidades = [f"{l}:\\" for l in string.ascii_uppercase if os.path.exists(f"{l}:\\")]
            return {"ruta": "", "padre": None, "carpetas": [{"nombre": u, "ruta": u} for u in unidades]}
        ruta = "/"
    ruta = os.path.abspath(ruta)
    while not os.path.isdir(ruta):                 # sube hasta la carpeta existente más cercana
        arriba = os.path.dirname(ruta.rstrip("\\/"))
        if not arriba or arriba == ruta:
            return explorar("")
        ruta = arriba
    out = []
    try:
        with os.scandir(ruta) as it:
            for e in it:
                try:
                    if not e.is_dir() or e.name.startswith((".", "$")) or e.name == "System Volume Information":
                        continue
                    if ES_WINDOWS and e.stat().st_file_attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                        continue
                    out.append({"nombre": e.name, "ruta": os.path.join(ruta, e.name)})
                except OSError:
                    continue
    except OSError as ex:
        return {"ruta": ruta, "padre": os.path.dirname(ruta), "carpetas": [], "error": str(ex)}
    out.sort(key=lambda x: natkey(x["nombre"]))
    padre = os.path.dirname(ruta.rstrip("\\/"))
    if ES_WINDOWS and re.fullmatch(r"[A-Za-z]:\\?", ruta):
        padre = ""
    elif padre == ruta:
        padre = None
    return {"ruta": ruta, "padre": padre, "carpetas": out}


class Servidor(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not ES_WINDOWS        # en Windows SO_REUSEADDR permitiría dos instancias en el mismo puerto
    request_queue_size = 64

    def server_bind(self):
        if ES_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ═══════════════════════════════════════════════════════════════════════════ SSDP

class SSDP(threading.Thread):
    GRUPO, PUERTO = "239.255.255.250", 1900

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.uuid = f"uuid:{app.udn}"
        self.targets = ["upnp:rootdevice", self.uuid, DEV_TYPE, NS_CDS, NS_CMS]
        self.ip_fija = app.cfg["servidor"]["ip"]
        self.ips = []
        self.unidas = set()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", self.PUERTO))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        self.sock = s
        self.refrescar_interfaces()

    def refrescar_interfaces(self):
        ips = [self.ip_fija] if self.ip_fija else ips_locales()
        for ip in ips:
            if ip in self.unidas:
                continue
            try:
                mreq = struct.pack("4s4s", socket.inet_aton(self.GRUPO), socket.inet_aton(ip))
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self.unidas.add(ip)
            except OSError as e:
                log.debug("Could not join multicast on %s: %s", ip, e)
        if ips != self.ips:
            log.info("Announcing on: %s", ", ".join(ips) or "(no interface)")
        self.ips = ips

    def location(self, ip):
        return f"http://{ip}:{self.app.puerto}/desc.xml"

    def usn(self, nt):
        return self.uuid if nt == self.uuid else f"{self.uuid}::{nt}"

    def notificar(self, nts):
        for ip in list(self.ips):
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            except OSError:
                continue
            for nt in self.targets:
                msg = ("NOTIFY * HTTP/1.1\r\n"
                       f"HOST: {self.GRUPO}:{self.PUERTO}\r\n"
                       "CACHE-CONTROL: max-age=1800\r\n"
                       f"LOCATION: {self.location(ip)}\r\n"
                       f"NT: {nt}\r\nNTS: {nts}\r\n"
                       f"SERVER: {SERVER_HDR}\r\nUSN: {self.usn(nt)}\r\n\r\n")
                try:
                    self.sock.sendto(msg.encode(), (self.GRUPO, self.PUERTO))
                except OSError as e:
                    log.debug("NOTIFY on %s failed: %s", ip, e)

    def anunciador(self):
        for _ in range(3):
            self.notificar("ssdp:alive")
            time.sleep(1)
        ultimo = time.time()
        while True:
            time.sleep(30)                         # al arrancar con Windows la red puede tardar en estar lista
            antes = list(self.ips)
            self.refrescar_interfaces()
            if self.ips != antes or time.time() - ultimo >= 300:
                self.notificar("ssdp:alive")
                ultimo = time.time()

    def responder(self, st, addr, mx):
        time.sleep(random.uniform(0, min(mx, 3) * 0.4))
        ip = self.ip_fija or ip_hacia(addr[0]) or (self.ips[0] if self.ips else None)
        if not ip:
            return
        for t in (self.targets if st == "ssdp:all" else [st]):
            msg = ("HTTP/1.1 200 OK\r\n"
                   "CACHE-CONTROL: max-age=1800\r\n"
                   f"DATE: {formatdate(usegmt=True)}\r\nEXT:\r\n"
                   f"LOCATION: {self.location(ip)}\r\nSERVER: {SERVER_HDR}\r\n"
                   f"ST: {t}\r\nUSN: {self.usn(t)}\r\nContent-Length: 0\r\n\r\n")
            try:
                self.sock.sendto(msg.encode(), addr)
            except OSError as e:
                log.debug("SSDP reply to %s failed: %s", addr, e)

    def run(self):
        threading.Thread(target=self.anunciador, daemon=True).start()
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                if self.sock.fileno() == -1:
                    return
                continue
            texto = data.decode("utf-8", "replace")
            if not texto.startswith("M-SEARCH"):
                continue
            h = {}
            for linea in texto.split("\r\n")[1:]:
                if ":" in linea:
                    k, v = linea.split(":", 1)
                    h[k.strip().upper()] = v.strip()
            if h.get("MAN", "").strip('"') != "ssdp:discover":
                continue
            st = h.get("ST", "")
            if st != "ssdp:all" and st not in self.targets:
                continue
            threading.Thread(target=self.responder, args=(st, addr, _int(h.get("MX", "1")) or 1), daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════ panel web

PANEL_HTML = r"""<!doctype html>
<html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SupaVic DLNA</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
:root{--bg:#f4f2ee;--card:#fff;--tx:#1d1c1a;--mut:#6d6a64;--line:#e3dfd8;--acc:#c2410c;--acc2:#fff3ea;--ok:#15803d;--bad:#b91c1c;--in:#faf9f7}
@media(prefers-color-scheme:dark){:root{--bg:#141312;--card:#1e1d1b;--tx:#eceae6;--mut:#9b978f;--line:#2f2d2a;--acc:#fb923c;--acc2:#2a1d14;--ok:#4ade80;--bad:#f87171;--in:#181716}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.5 "Segoe UI",system-ui,sans-serif}
header{padding:22px 16px 0;max-width:980px;margin:auto;display:flex;align-items:center;gap:14px;flex-wrap:wrap}
.logo{width:42px;height:42px;flex:none}
h1{font-size:20px;margin:0;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--mut);font-size:13px}
.hd-r{margin-left:auto;display:flex;gap:10px;align-items:center}
.pill{font-size:12px;padding:4px 10px;border-radius:99px;background:var(--acc2);color:var(--acc);font-weight:600}
.lnk{font-size:13px;color:var(--acc);text-decoration:none;font-weight:600}.lnk:hover{text-decoration:underline}
nav{max-width:980px;margin:18px auto 0;padding:0 16px;display:flex;gap:4px;border-bottom:1px solid var(--line);overflow-x:auto}
nav button{background:none;border:0;color:var(--mut);font:inherit;font-weight:600;padding:10px 14px;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
nav button.on{color:var(--tx);border-color:var(--acc)}
nav button:focus-visible,.btn:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
main{max-width:980px;margin:auto;padding:18px 16px 40px}
section{display:none}section.on{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.k{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em}
.v{font-size:22px;font-weight:650;margin-top:2px;font-variant-numeric:tabular-nums}
h2{font-size:15px;margin:24px 0 10px}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600}
.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}
code{font:13px ui-monospace,Consolas,monospace;word-break:break-all}
label{display:block;font-weight:600;font-size:13px;margin:14px 0 5px}
.hint{font-weight:400;color:var(--mut);font-size:12px}
input[type=text],input[type=number],textarea,select{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:var(--in);color:var(--tx);font:inherit}
textarea{min-height:70px;font:13px ui-monospace,Consolas,monospace}
input:focus,textarea:focus,select:focus{outline:2px solid var(--acc);outline-offset:-1px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media(max-width:600px){.row{grid-template-columns:1fr}}
.btn{border:1px solid var(--line);background:var(--card);color:var(--tx);font:inherit;font-weight:600;padding:8px 14px;border-radius:8px;cursor:pointer}
.btn.pri{background:var(--acc);border-color:var(--acc);color:#fff}
.btn.sm{padding:4px 10px;font-size:13px}
.btn:disabled{opacity:.5}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}
.lista{list-style:none;padding:0;margin:0}
.lista li{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}
.lista li code{flex:1}
.msg{margin-top:14px;padding:10px 12px;border-radius:8px;font-size:14px;display:none}
.msg.on{display:block}.msg.ok{background:color-mix(in srgb,var(--ok) 12%,transparent)}.msg.bad{background:color-mix(in srgb,var(--bad) 12%,transparent)}
.bienv{display:none;margin-bottom:16px;border-color:var(--acc);background:var(--acc2)}
.bienv b{display:block;font-size:16px;margin-bottom:4px}
#log{background:var(--in);border:1px solid var(--line);border-radius:10px;padding:12px;font:12px/1.55 ui-monospace,Consolas,monospace;max-height:65vh;overflow:auto;white-space:pre-wrap;word-break:break-all}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--tx);padding:0;width:min(560px,calc(100vw - 32px))}
dialog::backdrop{background:#0007}
.dh{padding:14px 16px;border-bottom:1px solid var(--line);font-weight:650}
.dpath{padding:8px 16px;font:13px ui-monospace,Consolas,monospace;color:var(--mut);word-break:break-all}
.dl{max-height:50vh;overflow:auto;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.dl div{padding:8px 16px;cursor:pointer;display:flex;gap:8px}.dl div:hover{background:var(--acc2)}
.df{padding:12px 16px;display:flex;gap:8px;justify-content:flex-end}
.chk{display:flex;align-items:center;gap:8px;font-weight:600;font-size:13px;margin-top:14px}
.pbar{height:6px;background:var(--line);border-radius:99px;overflow:hidden;margin:10px 0 6px}.pbar i{display:block;height:100%;background:var(--acc);border-radius:99px}
.prow{display:flex;gap:14px;flex-wrap:wrap;align-items:center;justify-content:space-between}
h3.f{font-size:14px;margin:22px 0 0;padding-top:16px;border-top:1px solid var(--line)}
footer{max-width:980px;margin:auto;padding:0 16px 40px;color:var(--mut);font-size:12px}
</style></head><body>
<header>
  <svg class="logo" viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="15" fill="#c2410c"/><g fill="none" stroke="#fff" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round"><path d="M25 45V18l20-4v24"/><circle cx="19.5" cy="45" r="5.5"/><circle cx="39.5" cy="38" r="5.5"/></g><g fill="none" stroke="#ffd7bd" stroke-width="3" stroke-linecap="round"><path d="M50 22a10 10 0 0 1 0 12"/><path d="M54.5 17.5a16 16 0 0 1 0 21"/></g></svg>
  <div><h1 id="nombre">SupaVic DLNA</h1><div class="sub" id="sub" data-i="cargando"></div></div>
  <div class="hd-r"><a class="lnk" id="l-dash" href="#" target="_blank" rel="noopener" style="display:none">Dashboard ↗</a><span class="pill" id="estado">●</span></div>
</header>
<nav><button data-t="estado" class="on" data-i="tab.estado"></button><button data-t="carpetas" data-i="tab.carpetas"></button><button data-t="config" data-i="tab.config"></button><button data-t="log">Log</button></nav>
<main>
<section id="t-estado" class="on">
  <div class="card bienv" id="bienv"><b data-i="bienv.t"></b><span data-i="bienv.d"></span><div class="acts" style="margin-top:12px"><button class="btn pri" id="bienv-btn" data-i="bienv.btn"></button></div></div>
  <div class="grid">
    <div class="card"><div class="k" data-i="k.pistas"></div><div class="v" id="s-pistas">–</div></div>
    <div class="card"><div class="k" data-i="k.portadas"></div><div class="v" id="s-port">–</div></div>
    <div class="card"><div class="k" data-i="k.activo"></div><div class="v" id="s-up">–</div></div>
    <div class="card"><div class="k" data-i="k.carpetas"></div><div class="v" id="s-carp">–</div></div>
  </div>
  <h2 data-i="h.indice"></h2>
  <div class="grid" id="s-idx"></div>
  <h2 data-i="h.precarga"></h2>
  <div class="card" id="s-pre"></div>
  <h2 data-i="h.disp"></h2>
  <div class="card" style="padding:4px 12px"><table><thead><tr><th>IP</th><th data-i="th.ultima"></th><th data-i="th.sonando"></th></tr></thead><tbody id="s-cli"></tbody></table></div>
  <h2 data-i="h.red"></h2>
  <div class="card" id="s-red"></div>
</section>

<section id="t-carpetas">
  <p class="mut" style="margin-top:0" data-i="carp.intro"></p>
  <div class="card"><ul class="lista" id="c-lista"></ul>
    <div class="acts"><button class="btn" id="c-explorar" data-i="btn.agregar"></button><button class="btn pri" id="c-guardar" data-i="btn.guardar"></button></div>
    <div class="msg" id="c-msg"></div></div>
</section>

<section id="t-config">
  <div class="card">
    <div class="row">
      <div><label for="f-nombre" data-i="f.nombre"></label><input type="text" id="f-nombre" maxlength="64"></div>
      <div><label for="f-idioma" data-i="f.idioma"></label><select id="f-idioma"><option value="auto" data-i="f.idioma.auto"></option><option value="es">Español</option><option value="en">English</option></select></div>
    </div>
    <div class="row">
      <div><label for="f-puerto" data-i="f.puerto"></label><input type="number" id="f-puerto" min="1024" max="65535"></div>
      <div><label for="f-ip" data-i="f.ip"></label><input type="text" id="f-ip" data-ip="f.ip.ph"></div>
    </div>
    <div class="row">
      <div><label for="f-tam" data-i="f.tam"></label><input type="number" id="f-tam" min="0" max="2000"></div>
      <div><label for="f-orden" data-i="f.orden"></label><input type="number" id="f-orden" min="0"></div>
    </div>
    <label for="f-cache" data-i="f.cache"></label><input type="text" id="f-cache" data-ip="f.cache.ph">
    <label class="chk"><input type="checkbox" id="f-crop"> <span data-i="f.crop"></span></label>
    <label class="chk"><input type="checkbox" id="f-abrir"> <span data-i="f.abrir"></span></label>
    <label class="chk"><input type="checkbox" id="f-dash"> <span data-i="f.dash"></span></label>
    <h3 class="f" data-i="p.titulo"></h3>
    <label class="chk"><input type="checkbox" id="p-activa"> <span data-i="p.activa"></span></label>
    <div class="row">
      <div><label for="p-min" data-i="p.min"></label><input type="number" id="p-min" min="0.1" step="0.5"></div>
      <div><label for="p-horas" data-i="p.horas"></label><input type="number" id="p-horas" min="0" step="1"></div>
    </div>
    <label class="chk"><input type="checkbox" id="p-port"> <span data-i="p.port"></span></label>
    <h3 class="f" data-i="v.titulo"></h3>
    <div id="v-lista"></div>
    <label class="chk"><input type="checkbox" id="v-anio"> <span data-i="v.anio"></span></label>
    <label class="chk"><input type="checkbox" id="v-carp"> <span data-i="v.carp"></span></label>
    <div class="row">
      <div><label for="v-conteo" data-i="v.conteo"></label><select id="v-conteo"><option value="pistas" data-i="v.conteo.pistas"></option><option value="elementos" data-i="v.conteo.elem"></option><option value="no" data-i="v.conteo.no"></option></select></div>
      <div><label for="v-agr" data-i="v.agr"></label><input type="number" id="v-agr" min="0"></div>
    </div>
    <label for="f-ign" data-i="f.ign"></label><textarea id="f-ign"></textarea>
    <div class="acts"><button class="btn pri" id="f-guardar" data-i="btn.guardarAj"></button></div>
    <div class="msg" id="f-msg"></div>
  </div>
  <h2 data-i="h.mant"></h2>
  <div class="card"><p class="mut" style="margin-top:0" data-i="mant.txt"></p>
    <div class="acts" style="margin-top:0"><button class="btn" id="m-cache" data-i="btn.borrar"></button><button class="btn" id="m-reini" data-i="btn.reiniciar"></button></div>
    <div class="msg" id="m-msg"></div></div>
</section>

<section id="t-log"><div class="acts" style="margin:0 0 10px"><button class="btn sm" id="l-ref" data-i="btn.actualizar"></button></div><div id="log"></div></section>
</main>
<footer id="pie"></footer>

<dialog id="dlg"><div class="dh" data-i="dlg.titulo"></div><div class="dpath" id="d-ruta"></div><div class="dl" id="d-lista"></div>
<div class="df"><button class="btn" id="d-cancel" data-i="dlg.cancelar"></button><button class="btn pri" id="d-ok" data-i="dlg.usar"></button></div></dialog>

<script>
const LANG="__LANG__";
const TXT={
es:{"cargando":"cargando…","enlinea":"● En línea","sinconexion":"● Sin conexión","tab.estado":"Estado","tab.carpetas":"Carpetas","tab.config":"Ajustes",
"bienv.t":"Agrega tu carpeta de música para empezar","bienv.d":"Elige una o varias carpetas; aparecerán en tu reproductor (WiiM Home, BubbleUPnP…) con el nombre de este servidor.","bienv.btn":"Elegir carpetas",
"k.pistas":"Pistas en caché","k.portadas":"Portadas","k.activo":"Activo","k.carpetas":"Carpetas","h.indice":"Biblioteca por etiquetas","h.precarga":"Precarga en reposo","h.disp":"Dispositivos",
"th.ultima":"Última actividad","th.sonando":"Sonando / acción","h.red":"Red",
"carp.intro":"Se muestran tal cual en tu reproductor. No hace falta indexar antes: los tags y portadas se leen al abrir cada carpeta, quedan en caché y el resto se precarga cuando el servidor está inactivo.",
"btn.agregar":"Agregar carpeta…","btn.guardar":"Guardar","f.nombre":"Nombre en la red","f.idioma":"Idioma <span class=\"hint\">(reinicia el servidor)</span>","f.idioma.auto":"Automático (idioma del sistema)",
"f.puerto":"Puerto HTTP <span class=\"hint\">(reinicia el servidor)</span>","f.ip":"IP fija <span class=\"hint\">vacío = todas las interfaces (recomendado)</span>","f.ip.ph":"ej. 192.168.1.50",
"f.tam":"Tamaño de portadas (px) <span class=\"hint\">0 = original</span>","f.orden":"Ordenar por número de pista hasta <span class=\"hint\">archivos por carpeta</span>",
"f.cache":"Carpeta de caché <span class=\"hint\">tags y portadas; mejor en un SSD (reinicia el servidor)</span>","f.cache.ph":"vacío = carpeta de datos",
"f.crop":"Recortar portadas no cuadradas (miniaturas 16:9 de YouTube)","f.abrir":"Abrir este panel al iniciar el servidor a mano","f.dash":"Iniciar el dashboard junto con el servidor (icono de bandeja)",
"p.titulo":"Precarga en reposo","p.activa":"Llenar la caché (tags y portadas) cuando el servidor esté inactivo","p.min":"Minutos sin navegación para empezar","p.horas":"Repetir pasada completa cada (horas) <span class=\"hint\">detecta música nueva</span>","p.port":"Generar también las portadas",
"v.titulo":"Vistas por etiquetas <span class=\"hint\">aparecen en la raíz, después de las carpetas</span>","v.conteo":"Número entre paréntesis — \"Korn (15)\"","v.conteo.pistas":"Canciones","v.conteo.elem":"Elementos (álbumes de un artista, artistas de un género…)","v.conteo.no":"No mostrar","v.anio":"Mostrar el año después del título del álbum — \"Álbum [1999]\"","v.carp":"Agrupar las carpetas dentro de una entrada \"Carpetas\" en la raíz","v.agr":"Agrupar por letra listas de más de <span class=\"hint\">0 = nunca</span>",
"f.ign":"Ignorar <span class=\"hint\">un patrón por línea, sin distinguir mayúsculas (*.part, .*, nombres exactos)</span>","btn.guardarAj":"Guardar ajustes","h.mant":"Mantenimiento",
"mant.txt":"Borrar la caché obliga a releer tags y portadas al navegar (útil si cambiaste tags sin modificar los archivos).","btn.borrar":"Borrar caché","btn.reiniciar":"Reiniciar servidor","btn.actualizar":"Actualizar",
"dlg.titulo":"Elegir carpeta","dlg.cancelar":"Cancelar","dlg.usar":"Usar esta carpeta","dlg.unidades":"Unidades","dlg.vacia":"Sin subcarpetas",
"ahora":"ahora","hace.min":"hace {n} min","hace.h":"hace {n} h","sinPillow":"sin Pillow","reproduciendo":"reproduciendo","navegando":"navegando","descubrimiento":"descubrimiento","ningunDisp":"Ningún dispositivo se ha conectado todavía.",
"i.pistas":"Pistas","i.albumes":"Álbumes","i.artistas":"Artistas","i.generos":"Géneros","i.construyendo":"Construyendo índice…","i.pendiente":"El índice se construye al abrir una vista en el reproductor.",
"pre.esperando":"Esperando {n} min sin navegación","pre.pausada":"Pausada por actividad · retoma en la siguiente inactividad","pre.escaneando":"Escaneando","pre.completa":"Caché completa","pre.desactivada":"Desactivada",
"pre.transfer":" (esperando a que terminen las transferencias)","pre.ya":"Iniciar ahora","pre.prog":"{a} de {b} carpetas · {p} pistas leídas · {c} portadas generadas en esta pasada","pre.ultima":"Última pasada completa: {x}","nunca":"nunca",
"red.anunciado":"Anunciado en","red.ninguna":"Ninguna interfaz","red.cache":"Caché","red.carpetas":"Carpetas","disponible":"disponible","nodisponible":"no disponible",
"sinCarpetas":"Sin carpetas. Agrega al menos una.","quitar":"Quitar","guardado":"Guardado.","reiniciando":"Reiniciando…","error":"Error","cacheBorrada":"Caché borrada ({n} portadas).","vacio":"(vacío)",
"pie":"SupaVic DLNA {v} · Python {py} · datos en {d}"},
en:{"cargando":"loading…","enlinea":"● Online","sinconexion":"● Offline","tab.estado":"Status","tab.carpetas":"Folders","tab.config":"Settings",
"bienv.t":"Add your music folder to get started","bienv.d":"Pick one or more folders; they'll show up on your player (WiiM Home, BubbleUPnP…) under this server's name.","bienv.btn":"Choose folders",
"k.pistas":"Tracks cached","k.portadas":"Covers","k.activo":"Uptime","k.carpetas":"Folders","h.indice":"Tag library","h.precarga":"Idle prefetch","h.disp":"Devices",
"th.ultima":"Last activity","th.sonando":"Playing / action","h.red":"Network",
"carp.intro":"They appear as-is on your player. No up-front indexing: tags and covers are read when each folder is opened, cached, and the rest is prefetched while the server is idle.",
"btn.agregar":"Add folder…","btn.guardar":"Save","f.nombre":"Network name","f.idioma":"Language <span class=\"hint\">(restarts the server)</span>","f.idioma.auto":"Automatic (system language)",
"f.puerto":"HTTP port <span class=\"hint\">(restarts the server)</span>","f.ip":"Fixed IP <span class=\"hint\">empty = all interfaces (recommended)</span>","f.ip.ph":"e.g. 192.168.1.50",
"f.tam":"Cover size (px) <span class=\"hint\">0 = original</span>","f.orden":"Sort by track number up to <span class=\"hint\">files per folder</span>",
"f.cache":"Cache folder <span class=\"hint\">tags and covers; best on an SSD (restarts the server)</span>","f.cache.ph":"empty = data folder",
"f.crop":"Crop non-square covers (16:9 YouTube thumbnails)","f.abrir":"Open this panel when the server is started manually","f.dash":"Start the dashboard with the server (tray icon)",
"p.titulo":"Idle prefetch","p.activa":"Fill the cache (tags and covers) while the server is idle","p.min":"Minutes without browsing before starting","p.horas":"Repeat a full pass every (hours) <span class=\"hint\">picks up new music</span>","p.port":"Generate covers too",
"v.titulo":"Tag views <span class=\"hint\">shown at the root, after the folders</span>","v.conteo":"Count in parentheses — \"Korn (15)\"","v.conteo.pistas":"Songs","v.conteo.elem":"Items (an artist's albums, a genre's artists…)","v.conteo.no":"Don't show","v.anio":"Show the year after the album title — \"Album [1999]\"","v.carp":"Put folders inside a \"Folders\" entry at the root","v.agr":"Group lists longer than this by letter <span class=\"hint\">0 = never</span>",
"f.ign":"Ignore <span class=\"hint\">one pattern per line, case-insensitive (*.part, .*, exact names)</span>","btn.guardarAj":"Save settings","h.mant":"Maintenance",
"mant.txt":"Clearing the cache forces tags and covers to be read again while browsing (useful if you edited tags without changing the files).","btn.borrar":"Clear cache","btn.reiniciar":"Restart server","btn.actualizar":"Refresh",
"dlg.titulo":"Choose folder","dlg.cancelar":"Cancel","dlg.usar":"Use this folder","dlg.unidades":"Drives","dlg.vacia":"No subfolders",
"ahora":"now","hace.min":"{n} min ago","hace.h":"{n} h ago","sinPillow":"no Pillow","reproduciendo":"playing","navegando":"browsing","descubrimiento":"discovery","ningunDisp":"No device has connected yet.",
"i.pistas":"Tracks","i.albumes":"Albums","i.artistas":"Artists","i.generos":"Genres","i.construyendo":"Building index…","i.pendiente":"The index is built when a view is opened on the player.",
"pre.esperando":"Waiting for {n} min without browsing","pre.pausada":"Paused by activity · resumes at the next idle period","pre.escaneando":"Scanning","pre.completa":"Cache complete","pre.desactivada":"Disabled",
"pre.transfer":" (waiting for transfers to finish)","pre.ya":"Start now","pre.prog":"{a} of {b} folders · {p} tracks read · {c} covers generated this pass","pre.ultima":"Last full pass: {x}","nunca":"never",
"red.anunciado":"Announced on","red.ninguna":"No interface","red.cache":"Cache","red.carpetas":"Folders","disponible":"available","nodisponible":"not available",
"sinCarpetas":"No folders yet. Add at least one.","quitar":"Remove","guardado":"Saved.","reiniciando":"Restarting…","error":"Error","cacheBorrada":"Cache cleared ({n} covers).","vacio":"(empty)",
"pie":"SupaVic DLNA {v} · Python {py} · data in {d}"}}[LANG]||{};
const t=(k,o)=>{let s=TXT[k]??k;if(o)for(const x in o)s=s.split("{"+x+"}").join(o[x]);return s};
const LOC=(()=>{const d=LANG==="es"?"es-MX":"en-US",l=navigator.language||"";try{if(l.toLowerCase().startsWith(LANG)){new Intl.NumberFormat(l);return l}}catch(e){}return d})();
const nf=n=>Number(n||0).toLocaleString(LOC);
document.querySelectorAll("[data-i]").forEach(e=>e.innerHTML=t(e.dataset.i));
document.querySelectorAll("[data-ip]").forEach(e=>e.placeholder=t(e.dataset.ip));
const $=s=>document.querySelector(s);
let cfg=null, carpetas=[], dRuta="", vistasDisp=[], vistasPintadas="";
function pintarVistas(){
  if(!cfg||!vistasDisp.length)return;
  const firma=JSON.stringify([vistasDisp,cfg.vistas.activas]);if(firma===vistasPintadas)return;vistasPintadas=firma;
  $("#v-lista").innerHTML=vistasDisp.map(([k,n])=>`<label class="chk" style="margin-top:8px"><input type="checkbox" value="${k}" ${cfg.vistas.activas.includes(k)?"checked":""}> ${h(n)}</label>`).join("");
}
const api=(u,o)=>fetch(u,o).then(r=>r.json());
const post=(u,b)=>api(u,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b||{})});
const h=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function dur(s){const d=Math.floor(s/86400),hh=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${hh}h`:hh?`${hh}h ${m}m`:`${m}m`}
function hace(s){return s<60?t("ahora"):s<3600?t("hace.min",{n:Math.floor(s/60)}):t("hace.h",{n:Math.floor(s/3600)})}
function msg(id,txt,ok){const e=$(id);e.className="msg on "+(ok?"ok":"bad");e.innerHTML=txt}
function irA(tab){document.querySelector(`nav button[data-t="${tab}"]`)?.click()}

document.querySelectorAll("nav button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("nav button,section").forEach(x=>x.classList.remove("on"));
  b.classList.add("on");$("#t-"+b.dataset.t).classList.add("on");
  try{localStorage.setItem("tab",b.dataset.t)}catch(e){}
  if(b.dataset.t==="log")cargarLog();
});
try{const tb=localStorage.getItem("tab");tb&&irA(tb)}catch(e){}
$("#bienv-btn").onclick=()=>{irA("carpetas");$("#c-explorar").click()};

async function estado(){
  try{
    const s=await api("/api/estado");
    $("#nombre").textContent=s.nombre;document.title=`${s.nombre} · SupaVic DLNA`;
    $("#sub").textContent=`SupaVic DLNA ${s.version}${s.pillow?"":" · "+t("sinPillow")}`;
    $("#estado").textContent=t("enlinea");
    $("#pie").textContent=t("pie",{v:s.version,py:s.python,d:s.datos||""});
    const ld=$("#l-dash");if(s.dashboard&&s.dashboard.activo){ld.style.display="";ld.href=`${location.protocol}//${location.hostname}:${s.dashboard.puerto}/`}else ld.style.display="none";
    $("#bienv").style.display=s.carpetas.length?"none":"block";
    $("#s-pistas").textContent=nf(s.pistas_cache);
    $("#s-port").textContent=`${nf(s.portadas)} · ${s.portadas_mb} MB`;
    $("#s-up").textContent=dur(s.uptime);
    const disp=s.carpetas.filter(c=>c.disponible).length;
    $("#s-carp").innerHTML=`${disp}/${s.carpetas.length}`;
    const acc=a=>a.startsWith("▶")?t("reproduciendo"):(a==="navegando"||a==="descubrimiento")?t(a):a;
    $("#s-cli").innerHTML=s.clientes.length?s.clientes.map(c=>`<tr><td><code>${h(c.ip)}</code><div class="mut" style="font-size:12px">${h(c.ua||"")}</div></td><td>${hace(c.hace)}</td><td>${c.sonando?`<code>${h(c.sonando)}</code>`:""}<div class="mut" style="font-size:12px">${h(acc(c.accion))}</div></td></tr>`).join(""):`<tr><td colspan="3" class="mut">${t("ningunDisp")}</td></tr>`;
    vistasDisp=s.vistas_disponibles||vistasDisp;pintarVistas();
    const ix=s.indice||{};
    $("#s-idx").innerHTML=ix.listo?[["i.pistas",ix.pistas],["i.albumes",ix.albumes],["i.artistas",ix.artistas],["i.generos",ix.generos]]
      .map(([k,v])=>`<div class="card"><div class="k">${t(k)}</div><div class="v">${nf(v)}</div></div>`).join("")
      :`<div class="card mut">${ix.construyendo?t("i.construyendo"):t("i.pendiente")}</div>`;
    const pr=s.precarga;
    if(pr){
      const est=pr.estado==="esperando"?t("pre.esperando",{n:Math.ceil(pr.espera_seg/60)}):t("pre."+pr.estado);
      const pct=pr.total_carpetas?Math.round(100*pr.carpetas/pr.total_carpetas):0;
      const ult=pr.ultimo_completo?new Date(pr.ultimo_completo*1000).toLocaleString(LOC,{dateStyle:"short",timeStyle:"short"}):t("nunca");
      $("#s-pre").innerHTML=`<div class="prow"><div><b>${h(est)}</b>${pr.estado==="esperando"&&pr.espera_seg===0?t("pre.transfer"):""}</div>
        <button class="btn sm" id="pre-ya" ${pr.estado==="escaneando"||!pr.activa?"disabled":""}>${t("pre.ya")}</button></div>
        ${pr.total_carpetas?`<div class="pbar"><i style="width:${pr.estado==="completa"?100:pct}%"></i></div>
        <div class="mut" style="font-size:13px">${t("pre.prog",{a:nf(pr.estado==="completa"?pr.total_carpetas:pr.carpetas),b:nf(pr.total_carpetas),p:nf(pr.pistas),c:nf(pr.portadas)})}</div>`:""}
        ${pr.actual&&pr.estado==="escaneando"?`<div style="margin-top:6px"><code style="font-size:12px">${h(pr.actual)}</code></div>`:""}
        <div class="mut" style="font-size:12px;margin-top:6px">${t("pre.ultima",{x:h(ult)})}</div>`;
      const b=$("#pre-ya");if(b)b.onclick=async()=>{b.disabled=true;await post("/api/precarga-ahora");setTimeout(estado,1500)};
    }
    $("#s-red").innerHTML=`<div class="k">${t("red.anunciado")}</div>`+(s.ips.map(ip=>`<div><code>http://${h(ip)}:${s.puerto}/</code></div>`).join("")||`<div class="bad">${t("red.ninguna")}</div>`)
      +`<div class="k" style="margin-top:12px">${t("red.cache")}</div><div><code>${h(s.cache)}</code></div>`
      +`<div class="k" style="margin-top:12px">${t("red.carpetas")}</div>`+s.carpetas.map(c=>`<div><code>${h(c.ruta)}</code> <span class="${c.disponible?"ok":"bad"}">${c.disponible?t("disponible"):t("nodisponible")}</span></div>`).join("");
  }catch(e){$("#estado").textContent=t("sinconexion")}
}
function pintarCarpetas(){
  $("#c-lista").innerHTML=carpetas.length?carpetas.map((c,i)=>`<li><code>${h(c)}</code><button class="btn sm" data-n="${i}">${t("quitar")}</button></li>`).join(""):`<li class="mut">${t("sinCarpetas")}</li>`;
  $("#c-lista").querySelectorAll("button").forEach(b=>b.onclick=()=>{carpetas.splice(+b.dataset.n,1);pintarCarpetas()});
}
async function cargarConfig(){
  cfg=await api("/api/config");carpetas=[...cfg.musica.carpetas];pintarCarpetas();
  $("#f-nombre").value=cfg.servidor.nombre;$("#f-puerto").value=cfg.servidor.puerto;$("#f-ip").value=cfg.servidor.ip;$("#f-idioma").value=cfg.servidor.idioma||"auto";
  $("#f-tam").value=cfg.portadas.tamano;$("#f-crop").checked=cfg.portadas.recortar_cuadrado;
  $("#f-orden").value=cfg.musica.ordenar_por_tags_hasta;$("#f-cache").value=cfg.cache.directorio;
  $("#v-agr").value=cfg.vistas.agrupar_desde;$("#v-conteo").value=cfg.vistas.mostrar_conteo?(cfg.vistas.tipo_conteo||"pistas"):"no";$("#v-anio").checked=cfg.vistas.mostrar_anio;$("#v-carp").checked=cfg.musica.agrupar_carpetas;pintarVistas();
  $("#f-abrir").checked=cfg.servidor.abrir_navegador;$("#f-dash").checked=cfg.dashboard.con_servidor;
  $("#p-activa").checked=cfg.precarga.activa;$("#p-min").value=cfg.precarga.inactividad_min;
  $("#p-horas").value=cfg.precarga.repetir_horas;$("#p-port").checked=cfg.precarga.portadas;$("#f-ign").value=cfg.musica.ignorar.join("\n");
}
async function guardar(nuevo,id){
  const r=await post("/api/config",nuevo);
  if(!r.ok){msg(id,(r.errores||[t("error")]).map(h).join("<br>"),false);return}
  let tx=t("guardado")+(r.avisos?.length?"<br>"+r.avisos.map(h).join("<br>"):"");
  if(r.reiniciar){tx+="<br>"+t("reiniciando");const p=nuevo.servidor.puerto;
    setTimeout(()=>{location.href=`${location.protocol}//${location.hostname}:${p}/`},3500)}
  msg(id,tx,true);cargarConfig();estado();
}
$("#c-guardar").onclick=()=>{const n=structuredClone(cfg);n.musica.carpetas=carpetas;guardar(n,"#c-msg")};
$("#f-guardar").onclick=()=>{const n=structuredClone(cfg);
  n.servidor.nombre=$("#f-nombre").value;n.servidor.puerto=+$("#f-puerto").value;n.servidor.ip=$("#f-ip").value.trim();n.servidor.idioma=$("#f-idioma").value;
  n.portadas.tamano=+$("#f-tam").value;n.portadas.recortar_cuadrado=$("#f-crop").checked;
  n.musica.ordenar_por_tags_hasta=+$("#f-orden").value;n.cache.directorio=$("#f-cache").value.trim();
  if(document.querySelector("#v-lista input"))n.vistas.activas=[...document.querySelectorAll("#v-lista input:checked")].map(x=>x.value);n.vistas.agrupar_desde=+$("#v-agr").value;const vc=$("#v-conteo").value;n.vistas.mostrar_conteo=vc!=="no";if(vc!=="no")n.vistas.tipo_conteo=vc;n.vistas.mostrar_anio=$("#v-anio").checked;n.musica.agrupar_carpetas=$("#v-carp").checked;
  n.servidor.abrir_navegador=$("#f-abrir").checked;n.dashboard.con_servidor=$("#f-dash").checked;
  n.precarga.activa=$("#p-activa").checked;n.precarga.inactividad_min=+$("#p-min").value;
  n.precarga.repetir_horas=+$("#p-horas").value;n.precarga.portadas=$("#p-port").checked;n.musica.ignorar=$("#f-ign").value.split("\n").map(s=>s.trim()).filter(Boolean);
  guardar(n,"#f-msg")};
$("#m-cache").onclick=async()=>{const r=await post("/api/limpiar-cache");msg("#m-msg",r.ok?t("cacheBorrada",{n:nf(r.portadas)}):t("error"),r.ok);estado()};
$("#m-reini").onclick=async()=>{await post("/api/reiniciar");msg("#m-msg",t("reiniciando"),true);setTimeout(()=>location.reload(),3500)};

async function explorarA(ruta){
  const r=await api("/api/explorar?ruta="+encodeURIComponent(ruta));
  dRuta=r.ruta;$("#d-ruta").textContent=r.ruta||t("dlg.unidades");
  let html=r.padre!==null&&r.padre!==undefined?`<div data-r="${h(r.padre)}">↑ ..</div>`:"";
  html+=r.carpetas.map(c=>`<div data-r="${h(c.ruta)}">📁 ${h(c.nombre)}</div>`).join("");
  if(r.error)html+=`<div class="bad">${h(r.error)}</div>`;
  $("#d-lista").innerHTML=html||`<div class="mut">${t("dlg.vacia")}</div>`;
  $("#d-lista").querySelectorAll("[data-r]").forEach(d=>d.onclick=()=>explorarA(d.dataset.r));
  $("#d-ok").disabled=!dRuta;
}
$("#c-explorar").onclick=()=>{$("#dlg").showModal();explorarA(carpetas[carpetas.length-1]||"")};
$("#d-cancel").onclick=()=>$("#dlg").close();
$("#d-ok").onclick=()=>{if(dRuta&&!carpetas.includes(dRuta))carpetas.push(dRuta);pintarCarpetas();$("#dlg").close()};

async function cargarLog(){const r=await api("/api/log");const e=$("#log");e.textContent=r.lineas.join("\n")||t("vacio");e.scrollTop=e.scrollHeight}
$("#l-ref").onclick=cargarLog;
cargarConfig();estado();setInterval(estado,5000);
</script></body></html>"""

FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="15" fill="#c2410c"/>'
               '<g fill="none" stroke="#fff" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round">'
               '<path d="M25 45V18l20-4v24"/><circle cx="19.5" cy="45" r="5.5"/><circle cx="39.5" cy="38" r="5.5"/></g>'
               '<g fill="none" stroke="#ffd7bd" stroke-width="3" stroke-linecap="round">'
               '<path d="M50 22a10 10 0 0 1 0 12"/><path d="M54.5 17.5a16 16 0 0 1 0 21"/></g></svg>')


# ═══════════════════════════════════════════════════════════════════════════ main

def configurar_log(verbose):
    DIR_DATOS.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(RUTA_LOG, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stderr is not None:            # pythonw.exe no tiene consola
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def ping():
    """Imprime el puerto y devuelve 0 si el servidor responde (lo usan los .bat y launcher.py)."""
    import urllib.request
    try:
        cfg = json.loads(RUTA_CONFIG.read_text(encoding="utf-8-sig"))
        puerto = int(cfg.get("servidor", {}).get("puerto", 8200))
        urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/estado", timeout=2).read()
        print(puerto)
        return 0
    except Exception:
        return 1


def abrir_panel():
    """Abre el panel en el navegador si servidor.abrir_navegador es true."""
    try:
        cfg = json.loads(RUTA_CONFIG.read_text(encoding="utf-8-sig")).get("servidor", {})
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("abrir_navegador", True):
        import webbrowser
        webbrowser.open(f"http://localhost:{int(cfg.get('puerto', 8200))}/")
    return 0


class ErrorInicio(Exception):
    """El servidor no pudo arrancar (puerto ocupado, UDP 1900 bloqueado…)."""


def arrancar(verbose=False):
    """Arranca el servidor en hilos y devuelve la App. Lo usan main() y launcher.py (bandeja)."""
    configurar_log(verbose)
    app = App()
    log.info("─── %s %s (Python %s) · data: %s ───", APP, VERSION, sys.version.split()[0], DIR_BASE)
    if Image is None:
        log.warning("Pillow is not installed: covers are sent without resizing")
    if not app.cfg["musica"]["carpetas"]:
        log.warning("No music folders configured yet. Add them at http://127.0.0.1:%d/", app.puerto)

    httpd = None
    for intento in range(40):                     # espera a que suelte el puerto una instancia anterior
        try:
            httpd = Servidor(("0.0.0.0", app.puerto), Handler)
            break
        except OSError as e:
            if intento == 39:
                log.error("Could not open port %d: %s", app.puerto, e)
                raise ErrorInicio(f"Port {app.puerto}: {e}")
            time.sleep(0.5)
    httpd.app = app
    app.httpd = httpd
    try:
        app.ssdp = SSDP(app)
    except OSError as e:
        log.error("Could not open UDP 1900 for discovery (%s). Players won't see the server.", e)
        httpd.server_close()
        raise ErrorInicio(f"UDP 1900: {e}")
    RUTA_PID.write_text(str(os.getpid()), encoding="ascii")
    app.eventos.emitir("inicio", version=VERSION, nombre=app.cfg["servidor"]["nombre"], puerto=app.puerto,
                       pid=os.getpid())
    app.ssdp.start()
    app.precarga = Precargador(app)
    app.precarga.start()
    threading.Timer(3, lambda: app.indice.asegurar(espera=0)).start()   # índice listo antes de la 1ª navegación
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    log.info("'%s' ready · panel: http://%s:%d/", app.cfg["servidor"]["nombre"],
             (app.ssdp.ips or ["127.0.0.1"])[0], app.puerto)
    log.info("Cache: %s", app.dir_cache)
    for k, (r, _) in app.bib.raices.items():
        log.info("  folder %s %s%s", k, r, "" if os.path.isdir(r) else "  (not available)")
    return app


def detener(app):
    """Apagado ordenado: avisa a los controles (ssdp:byebye) y libera los puertos."""
    log.info("Exiting…")
    try:
        app.ssdp.notificar("ssdp:byebye")
        app.httpd.shutdown()
        app.httpd.server_close()
    except Exception:
        pass
    try:
        RUTA_PID.unlink()
    except OSError:
        pass


def main():
    if "--ping" in sys.argv:
        sys.exit(ping())
    if "--abrir-panel" in sys.argv:
        sys.exit(abrir_panel())
    try:
        app = arrancar("-v" in sys.argv or "--verbose" in sys.argv)
    except ErrorInicio:
        sys.exit(1)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        detener(app)


if __name__ == "__main__":
    main()
