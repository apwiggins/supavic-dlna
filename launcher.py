#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SupaVic DLNA — lanzador con icono en la bandeja del sistema. Es la entrada del ejecutable (SupaVicDLNA.exe)
y la que usan los .bat del código fuente.

  launcher.py                  servidor + icono de bandeja (+ dashboard si dashboard.con_servidor)
  launcher.py --inicio         igual, pero sin abrir el navegador (así lo arranca Windows al iniciar sesión)
  launcher.py --sin-bandeja    sin icono (Linux/macOS o servicio); Ctrl+C para salir
  launcher.py --dashboard      solo el dashboard (lo lanza la bandeja como proceso aparte)
  launcher.py --autostart on|off   activa/desactiva "Iniciar con Windows" y sale
  launcher.py --ping           sale con 0 si el servidor responde (imprime el puerto)

Si el servidor ya está corriendo, abrirlo otra vez solo abre el panel.
"""
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
DIR_CODIGO = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
ES_WINDOWS = os.name == "nt"
CLAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
NOMBRE_RUN = "SupaVic DLNA"
REGLA_FIREWALL = "SupaVic DLNA"
SIN_VENTANA = 0x08000000 if ES_WINDOWS else 0          # CREATE_NO_WINDOW


def interprete():
    """pythonw.exe en Windows (sin consola) si existe junto al python actual."""
    exe = Path(sys.executable)
    if ES_WINDOWS and exe.name.lower() == "python.exe" and (exe.parent / "pythonw.exe").exists():
        return str(exe.parent / "pythonw.exe")
    return str(exe)


def comando(*args):
    if FROZEN:
        return [sys.executable, *args]
    return [interprete(), str(Path(__file__).resolve()), *args]


# ═══════════════════════════════════════════════════════════════════ Windows: autoinicio, firewall, avisos

def _cmdline(partes):
    return subprocess.list2cmdline(partes)


def autostart_activo():
    if not ES_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CLAVE_RUN) as k:
            winreg.QueryValueEx(k, NOMBRE_RUN)
            return True
    except OSError:
        return False


def fijar_autostart(activo):
    if not ES_WINDOWS:
        return False
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CLAVE_RUN) as k:
        if activo:
            winreg.SetValueEx(k, NOMBRE_RUN, 0, winreg.REG_SZ, _cmdline(comando("--inicio")))
        else:
            try:
                winreg.DeleteValue(k, NOMBRE_RUN)
            except OSError:
                pass
    return True


def mensaje(titulo, texto, pregunta=False):
    """MessageBox nativo. Devuelve True si el usuario eligió Sí (o Aceptar)."""
    if not ES_WINDOWS:
        print(f"{titulo}: {texto}", file=sys.stderr)
        return False
    import ctypes
    estilo = (0x04 | 0x20) if pregunta else 0x30          # MB_YESNO|MB_ICONQUESTION  /  MB_ICONWARNING
    return ctypes.windll.user32.MessageBoxW(None, texto, titulo, estilo | 0x10000) in (1, 6)


def regla_firewall_existe():
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={REGLA_FIREWALL}"],
                           capture_output=True, creationflags=SIN_VENTANA, timeout=15)
        return r.returncode == 0
    except Exception:
        return True                                        # si no se puede saber, no molestar


def asegurar_firewall(srv):
    """Solo el ejecutable portable: el instalador y los .bat ya crean la regla. Pregunta una sola vez."""
    if not (ES_WINDOWS and FROZEN):
        return
    marca = srv.DIR_DATOS / "firewall.preguntado"
    if marca.exists() or regla_firewall_existe():
        return
    try:
        marca.write_text("1", encoding="ascii")
    except OSError:
        pass
    from i18n import T
    if not mensaje(T("b.firewall_titulo"), T("b.firewall"), pregunta=True):
        return
    import ctypes
    params = (f'advfirewall firewall add rule name="{REGLA_FIREWALL}" dir=in action=allow enable=yes '
              f'profile=any remoteip=localsubnet program="{sys.executable}"')
    ctypes.windll.shell32.ShellExecuteW(None, "runas", "netsh", params, None, 0)


def abrir_carpeta(ruta):
    try:
        if ES_WINDOWS:
            os.startfile(str(ruta))
        else:
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(ruta)])
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════ dashboard (proceso aparte)

def dashboard_responde(puerto):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", puerto), timeout=0.3):
            return True
    except OSError:
        return False


def iniciar_dashboard(srv, esperar=False):
    puerto = srv_cfg(srv)["dashboard"]["puerto"]
    if dashboard_responde(puerto):
        return True
    subprocess.Popen(comando("--dashboard"), cwd=str(srv.DIR_BASE), creationflags=SIN_VENTANA, close_fds=True)
    if esperar:
        for _ in range(30):
            time.sleep(0.25)
            if dashboard_responde(puerto):
                return True
    return False


def detener_dashboard(srv):
    pid_f = srv.DIR_DATOS / "dashboard.pid"
    try:
        pid = int(pid_f.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return
    try:
        if ES_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=SIN_VENTANA, timeout=10)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    try:
        pid_f.unlink()
    except OSError:
        pass


def quien_usa_puerto(srv):
    """'libre', 'propio' (esta misma instalación ya corre) u 'otro' (otro programa, u otra copia de SupaVic
    con otra carpeta de datos, responde en el puerto)."""
    import json
    import urllib.request
    puerto = srv_cfg(srv)["servidor"]["puerto"]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/api/estado", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        if dashboard_responde(puerto):                   # algo escucha pero no es nuestro panel
            return "otro"
        return "libre"
    try:
        mismo = Path(d.get("datos", "")).resolve() == srv.DIR_BASE.resolve()
    except Exception:
        mismo = False
    return "propio" if mismo else "otro"


# ═══════════════════════════════════════════════════════════════════ bandeja

APP_ACTUAL = None


def srv_cfg(srv):
    return APP_ACTUAL.cfg if APP_ACTUAL else srv.cargar_config()


def url_panel(srv):
    return f"http://localhost:{srv_cfg(srv)['servidor']['puerto']}/"


def imagen_icono():
    from PIL import Image
    for nombre in ("icon.ico", "icon.png"):
        ruta = DIR_CODIGO / "assets" / nombre
        if ruta.exists():
            im = Image.open(ruta)
            return im.convert("RGBA").resize((64, 64))
    return Image.new("RGBA", (64, 64), (194, 65, 12, 255))


def bandeja(srv, app):
    """Bloquea en el hilo principal hasta que el usuario elige Salir. False si no hay bandeja disponible."""
    try:
        import pystray
    except Exception:
        return False
    from i18n import T

    def panel(icon=None, item=None):
        webbrowser.open(url_panel(srv))

    def dashboard(icon, item):
        def tarea():
            iniciar_dashboard(srv, esperar=True)
            webbrowser.open(f"http://localhost:{app.cfg['dashboard']['puerto']}/")
        threading.Thread(target=tarea, daemon=True).start()

    def reiniciar(icon, item):
        threading.Thread(target=app.reiniciar, daemon=True).start()

    def autoinicio(icon, item):
        fijar_autostart(not autostart_activo())

    def salir(icon, item):
        icon.visible = False
        srv.detener(app)
        detener_dashboard(srv)
        icon.stop()
        os._exit(0)

    items = [
        pystray.MenuItem(T("b.panel"), panel, default=True),
        pystray.MenuItem(T("b.dashboard"), dashboard),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(T("b.reiniciar"), reiniciar),
    ]
    if ES_WINDOWS:
        items.append(pystray.MenuItem(T("b.autoinicio"), autoinicio, checked=lambda item: autostart_activo()))
    items += [
        pystray.MenuItem(T("b.datos"), lambda icon, item: abrir_carpeta(srv.DIR_BASE)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(T("b.salir"), salir),
    ]
    icon = pystray.Icon("SupaVicDLNA", imagen_icono(), f"{srv.APP} — {app.cfg['servidor']['nombre']}"[:127],
                        pystray.Menu(*items))

    def al_salir():                                    # al reiniciar: que no quede un icono fantasma
        try:
            icon.visible = False
        except Exception:
            pass
    srv.AL_SALIR = al_salir
    icon.run()
    return True


# ═══════════════════════════════════════════════════════════════════ main

def main():
    global APP_ACTUAL
    args = sys.argv[1:]
    if "--dashboard" in args:
        import dashboard
        sys.argv = [sys.argv[0]] + [a for a in args if a != "--dashboard"]
        return dashboard.main()
    if "--autostart" in args:
        i = args.index("--autostart")
        valor = args[i + 1].lower() if i + 1 < len(args) else "on"
        fijar_autostart(valor in ("on", "1", "true", "si", "sí"))
        return 0

    import main as srv
    if "--ping" in args:
        return srv.ping()
    reinicio = "--reinicio" in args
    al_iniciar_sesion = "--inicio" in args
    sin_bandeja = "--sin-bandeja" in args

    if not reinicio:
        quien = quien_usa_puerto(srv)
        if quien == "propio":                            # ya está corriendo: solo abrir el panel
            if not al_iniciar_sesion:
                webbrowser.open(url_panel(srv))
            return 0
        if quien == "otro":                              # otro programa (u otra copia) ocupa el puerto
            from i18n import T
            srv.cargar_config()
            mensaje(T("b.error_titulo"), T("b.puerto_ocupado", puerto=srv_cfg(srv)["servidor"]["puerto"]))
            return 1

    pasar = [a for a in args if a in ("--sin-bandeja", "-v", "--verbose")]
    srv.RELANZAR = comando("--reinicio", *pasar)
    try:
        app = srv.arrancar("-v" in args or "--verbose" in args)
    except srv.ErrorInicio as e:
        from i18n import T
        mensaje(T("b.error_titulo"), T("b.error_inicio", err=e, log=srv.RUTA_LOG))
        return 1
    APP_ACTUAL = app
    asegurar_firewall(srv)
    if app.cfg["dashboard"]["con_servidor"]:
        threading.Thread(target=iniciar_dashboard, args=(srv,), daemon=True).start()
    sin_carpetas = not app.cfg["musica"]["carpetas"]
    if not reinicio and (sin_carpetas or (not al_iniciar_sesion and app.cfg["servidor"]["abrir_navegador"])):
        threading.Timer(1.0, webbrowser.open, args=(url_panel(srv),)).start()

    if not sin_bandeja and bandeja(srv, app):
        return 0
    try:                                               # systemd / kill: apagado ordenado como con Ctrl+C
        import signal

        def _term(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _term)
    except (ImportError, ValueError, AttributeError):
        pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.detener(app)
        if app.cfg["dashboard"]["con_servidor"]:
            detener_dashboard(srv)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
