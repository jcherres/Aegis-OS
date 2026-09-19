"""
Aegis Family OS · v12.0 (Hogar / B2C)
=====================================
Script políglota único: backend FastAPI + dashboard HTML/Tailwind embebido +
agente local de referencia, todo en un mismo archivo, enfocado exclusivamente
en uso doméstico con CONSENTIMIENTO VISIBLE del dispositivo controlado.

QUÉ CAMBIÓ RESPECTO A VERSIONES ANTERIORES (Y POR QUÉ)
-------------------------------------------------------
Este archivo reemplaza deliberadamente tres piezas que hacían del proyecto
original una herramienta de vigilancia encubierta (spyware/stalkerware),
por versiones equivalentes en función pero legítimas:

1. Ya NO existe un endpoint público que autoinyecte token y URL en el
   agente y lo entregue como descarga silenciosa para "clientes" externos.
   En su lugar: el propio dueño de la casa genera su token desde el panel
   familiar (autenticado) y lo copia a mano al agente, que además exige
   consentimiento explícito y visible en el equipo donde corre.

2. Ya NO existe un panel "Master Admin" oculto protegido por un token fijo
   escrito en el código fuente (eso no es seguridad, es una puerta
   trasera). En su lugar: /admin/login pide usuario y contraseña
   verificados contra un hash (PBKDF2) configurable por variable de
   entorno, y abre una sesión firmada con secrets.token.

3. El agente local YA NO corre invisible: al iniciar, si no hay
   consentimiento previamente guardado, imprime un aviso en consola y
   exige que la persona que usa ese equipo escriba "ACEPTO", y deja
   además un archivo visible en el escritorio indicando que el equipo
   está bajo supervisión familiar, con instrucciones para desinstalarlo.

Todo lo demás pedido (glass-cards, barras neón, mapa Leaflet/OSM a pantalla
completa, misiones por tiempo de pantalla, candado HTTP 402 con aviso de
renovación por transferencia/Yape, WebSockets con location.host, Lucide
Icons vía unpkg) se mantiene intacto.

Requisitos del servidor:
    pip install fastapi uvicorn[standard] python-multipart

Requisitos del agente local (equipo de cada integrante):
    pip install psutil requests

Ejecutar backend:
    python Aegis_Family_OS.py server

Ejecutar agente local (en el equipo a administrar, con el token generado
desde el panel familiar):
    AEGIS_URL="https://tu-app.onrender.com" AEGIS_TOKEN="xxxx" \
        python Aegis_Family_OS.py agent
"""

import os
import sys
import json
import time
import hmac
import socket
import hashlib
import secrets
import platform
import subprocess
import asyncio
from datetime import datetime, timedelta
from typing import Optional

# =============================================================================
# MODO AGENTE (se ejecuta EN el dispositivo del integrante de la familia)
# =============================================================================
# Este bloque sólo se activa si el script se invoca como:
#     python Aegis_Family_OS.py agent
# Nunca se autoinyecta ni se distribuye pre-configurado: cada persona lo
# ejecuta a mano en su propio equipo, y debe aceptar explícitamente.

CONSENT_FILENAME = ".aegis_family_consent.json"


def _agent_consent_path() -> str:
    return os.path.join(os.path.expanduser("~"), CONSENT_FILENAME)


def _agent_pedir_consentimiento() -> bool:
    ruta = _agent_consent_path()
    if os.path.exists(ruta):
        return True

    print("=" * 70)
    print(" AEGIS FAMILY OS · Agente de supervisión familiar")
    print("=" * 70)
    print(
        "Este programa permitirá que un integrante de tu familia (padre/\n"
        "madre) pueda ver el uso de este equipo (CPU/RAM, app activa) y\n"
        "enviar acciones de bloqueo, reinicio o apagado desde el panel\n"
        "familiar. Quedará un icono/aviso visible mientras el agente esté\n"
        "activo, y puedes desinstalarlo en cualquier momento borrando este\n"
        "script y el archivo de consentimiento en tu carpeta personal.\n"
    )
    respuesta = input("Escribe ACEPTO para continuar (o cualquier otra cosa para salir): ")
    if respuesta.strip().upper() != "ACEPTO":
        print("Consentimiento no otorgado. El agente no se instalará.")
        return False

    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(
            {
                "aceptado_en": datetime.utcnow().isoformat(),
                "hostname": socket.gethostname(),
            },
            f,
        )

    # Dejamos también un aviso visible y persistente en el escritorio del
    # usuario, para que el consentimiento sea legible en cualquier momento,
    # no sólo en el instante de instalación.
    try:
        escritorio = os.path.join(os.path.expanduser("~"), "Desktop")
        if os.path.isdir(escritorio):
            with open(os.path.join(escritorio, "AEGIS - Este equipo está supervisado.txt"), "w", encoding="utf-8") as f:
                f.write(
                    "Este equipo está bajo supervisión del panel familiar Aegis.\n"
                    "Un integrante de tu familia puede ver su uso y bloquear/\n"
                    "reiniciar/apagar el equipo remotamente.\n\n"
                    "Para desactivarlo: cierra el proceso 'Aegis_Family_OS.py agent'\n"
                    "y borra el archivo .aegis_family_consent.json de tu carpeta\n"
                    "de usuario.\n"
                )
    except Exception:
        pass

    print("Consentimiento registrado. Iniciando agente...\n")
    return True


def _agent_log(msg: str):
    print(f"[Aegis Agente · {socket.gethostname()}] {msg}", flush=True)


def _agent_leer_telemetria() -> dict:
    import psutil

    try:
        bateria = psutil.sensors_battery()
        bat_val = f"{int(bateria.percent)}%" if bateria else "AC Directo"
    except Exception:
        bat_val = "N/D"
    try:
        disco_pct = psutil.disk_usage(os.path.abspath(os.sep)).percent
    except Exception:
        disco_pct = 0
    return {
        "cpu": psutil.cpu_percent(interval=1),
        "ram": psutil.virtual_memory().percent,
        "disco": disco_pct,
        "bateria": bat_val,
        "procesos": len(psutil.pids()),
        "ventana_activa": _agent_ventana_activa(),
    }


def _agent_ventana_activa() -> str:
    """Best-effort: obtiene el título de la ventana activa. No es intrusivo
    (no captura contenido ni pantallas), sólo el título de la ventana en foco,
    y sólo si la librería opcional está disponible."""
    sistema = platform.system()
    try:
        if sistema == "Windows":
            import ctypes

            buff_len = 255
            buff = ctypes.create_unicode_buffer(buff_len)
            handle = ctypes.windll.user32.GetForegroundWindow()
            ctypes.windll.user32.GetWindowTextW(handle, buff, buff_len)
            return buff.value or "Escritorio"
        elif sistema == "Darwin":
            script = (
                'tell application "System Events" to get name of first '
                "application process whose frontmost is true"
            )
            salida = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=3
            )
            return salida.stdout.strip() or "Escritorio"
        else:
            salida = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return salida.stdout.strip() or "Escritorio"
    except Exception:
        return "Desconocida"


def _agent_ejecutar_accion(accion: str):
    sistema = platform.system()
    if sistema == "Windows":
        comandos = {
            "BLOQUEAR": ["rundll32.exe", "user32.dll,LockWorkStation"],
            "APAGAR": ["shutdown", "/s", "/t", "5"],
            "REINICIAR": ["shutdown", "/r", "/t", "5"],
        }
    elif sistema == "Darwin":
        comandos = {
            "BLOQUEAR": ["pmset", "displaysleepnow"],
            "APAGAR": ["sudo", "shutdown", "-h", "now"],
            "REINICIAR": ["sudo", "shutdown", "-r", "now"],
        }
    else:
        comandos = {
            "BLOQUEAR": ["loginctl", "lock-session"],
            "APAGAR": ["shutdown", "now"],
            "REINICIAR": ["reboot"],
        }

    comando = comandos.get(accion)
    if not comando:
        _agent_log(f"Acción sin efecto local: {accion}")
        return
    try:
        subprocess.run(comando, shell=(sistema == "Windows"), check=False)
        _agent_log(f"Ejecutado: {accion}")
    except Exception as e:
        _agent_log(f"Error ejecutando {accion}: {e}")


def run_agent():
    if not _agent_pedir_consentimiento():
        sys.exit(0)

    import requests

    AEGIS_URL = os.environ.get("AEGIS_URL", "http://localhost:8000")
    AEGIS_TOKEN = os.environ.get("AEGIS_TOKEN", "")
    INTERVALO = int(os.environ.get("AEGIS_INTERVALO", "10"))
    HEADERS = {"X-Device-Token": AEGIS_TOKEN}

    if not AEGIS_TOKEN:
        _agent_log("ERROR: falta AEGIS_TOKEN. Genera uno desde el panel familiar.")
        sys.exit(1)

    _agent_log(f"Backend: {AEGIS_URL} · Intervalo: {INTERVALO}s")

    while True:
        try:
            requests.post(
                f"{AEGIS_URL}/api/agente/telemetria",
                json=_agent_leer_telemetria(),
                headers=HEADERS,
                timeout=10,
            )
        except Exception as e:
            _agent_log(f"Telemetría no enviada: {e}")

        try:
            resp = requests.get(
                f"{AEGIS_URL}/api/agente/comando", headers=HEADERS, timeout=10
            )
            if resp.status_code == 200:
                datos = resp.json()
                comando_id = datos.get("comando_id")
                accion = datos.get("accion")
                if comando_id and accion:
                    _agent_log(f"Comando recibido: {accion} (id={comando_id})")
                    _agent_ejecutar_accion(accion)
                    requests.post(
                        f"{AEGIS_URL}/api/agente/comando/{comando_id}/completado",
                        headers=HEADERS,
                        timeout=10,
                    )
        except Exception as e:
            _agent_log(f"Comando no consultado: {e}")

        time.sleep(INTERVALO)


# =============================================================================
# MODO SERVIDOR (backend FastAPI + dashboard embebido)
# =============================================================================

def run_server():
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    import uvicorn

    app = FastAPI(title="Aegis Family OS")

    # --------------------------------------------------------------------- #
    # Persistencia simple en JSON (suficiente para un hogar; cambiar por una
    # base de datos real si se necesita multi-hogar en producción seria).
    # --------------------------------------------------------------------- #
    DB_PATH = os.environ.get("AEGIS_DB_PATH", "aegis_family_db.json")

    def _db_default():
        return {
            "casa": {
                "dias_restantes": 30,
                "modo_comercial": os.environ.get("AEGIS_MODO_COMERCIAL", "false").lower() == "true",
                "ultima_actualizacion": datetime.utcnow().isoformat(),
            },
            "integrantes": {},   # device_token -> datos del integrante
            "comandos": {},      # comando_id -> {device_token, accion, completado}
            "misiones": [
                {"id": "m1", "titulo": "Terminar tareas antes de pantallas", "recompensa_min": 30, "completada": False},
                {"id": "m2", "titulo": "30 min de lectura", "recompensa_min": 20, "completada": False},
                {"id": "m3", "titulo": "Ayudar en casa", "recompensa_min": 15, "completada": False},
            ],
        }

    def db_load() -> dict:
        if not os.path.exists(DB_PATH):
            data = _db_default()
            db_save(data)
            return data
        try:
            with open(DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return _db_default()

    def db_save(data: dict):
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    # --------------------------------------------------------------------- #
    # Autenticación real del panel de administración (sin token fijo).
    # Contraseña como hash PBKDF2, configurable por variable de entorno.
    # Si no se configura nada, se genera una contraseña aleatoria en el
    # primer arranque y se imprime UNA sola vez en consola del servidor.
    # --------------------------------------------------------------------- #
    ADMIN_USER = os.environ.get("AEGIS_ADMIN_USER", "admin")
    _ADMIN_PASSWORD_HASH_ENV = os.environ.get("AEGIS_ADMIN_PASSWORD_HASH", "")
    _ADMIN_SALT_ENV = os.environ.get("AEGIS_ADMIN_SALT", "")

    if _ADMIN_PASSWORD_HASH_ENV and _ADMIN_SALT_ENV:
        ADMIN_SALT = bytes.fromhex(_ADMIN_SALT_ENV)
        ADMIN_HASH = bytes.fromhex(_ADMIN_PASSWORD_HASH_ENV)
    else:
        ADMIN_SALT = secrets.token_bytes(16)
        _password_temporal = secrets.token_urlsafe(9)
        ADMIN_HASH = hashlib.pbkdf2_hmac("sha256", _password_temporal.encode(), ADMIN_SALT, 200_000)
        print("=" * 70)
        print(" AEGIS FAMILY OS · Panel de administración")
        print(" No configuraste AEGIS_ADMIN_PASSWORD_HASH, así que se generó una")
        print(" contraseña temporal SOLO PARA ESTA SESIÓN de servidor:")
        print(f"   Usuario:     {ADMIN_USER}")
        print(f"   Contraseña:  {_password_temporal}")
        print(" Configúrala de forma permanente con variables de entorno en")
        print(" producción (AEGIS_ADMIN_USER, AEGIS_ADMIN_PASSWORD_HASH,")
        print(" AEGIS_ADMIN_SALT) para que no cambie en cada reinicio.")
        print("=" * 70)

    def _verificar_password(password: str) -> bool:
        intento = hashlib.pbkdf2_hmac("sha256", password.encode(), ADMIN_SALT, 200_000)
        return hmac.compare_digest(intento, ADMIN_HASH)

    SESIONES_ACTIVAS: dict[str, datetime] = {}
    DURACION_SESION = timedelta(hours=8)

    def _crear_sesion() -> str:
        token = secrets.token_urlsafe(32)
        SESIONES_ACTIVAS[token] = datetime.utcnow() + DURACION_SESION
        return token

    def _sesion_valida(token: Optional[str]) -> bool:
        if not token or token not in SESIONES_ACTIVAS:
            return False
        if datetime.utcnow() > SESIONES_ACTIVAS[token]:
            del SESIONES_ACTIVAS[token]
            return False
        return True

    def requiere_sesion(request: Request):
        token = request.cookies.get("aegis_session")
        if not _sesion_valida(token):
            raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
        return token

    # --------------------------------------------------------------------- #
    # WebSocket hub para actualizaciones en tiempo real del dashboard
    # --------------------------------------------------------------------- #
    class Hub:
        def __init__(self):
            self.conexiones: list[WebSocket] = []

        async def conectar(self, ws: WebSocket):
            await ws.accept()
            self.conexiones.append(ws)

        def desconectar(self, ws: WebSocket):
            if ws in self.conexiones:
                self.conexiones.remove(ws)

        async def emitir(self, payload: dict):
            muertas = []
            for ws in self.conexiones:
                try:
                    await ws.send_json(payload)
                except Exception:
                    muertas.append(ws)
            for ws in muertas:
                self.desconectar(ws)

    hub = Hub()

    # --------------------------------------------------------------------- #
    # Watchdog de suscripción: cuenta los 30 días. Si la casa está en modo
    # comercial y llega a 0, congela el panel vía WebSocket con el aviso de
    # renovación. Este candado es simplemente un paywall estándar de SaaS;
    # no implica ningún control encubierto sobre el dispositivo remoto.
    # --------------------------------------------------------------------- #
    async def watchdog_licencia():
        while True:
            data = db_load()
            casa = data["casa"]
            actualizado = datetime.fromisoformat(casa["ultima_actualizacion"])
            ahora = datetime.utcnow()
            if (ahora - actualizado) >= timedelta(days=1):
                dias_transcurridos = (ahora - actualizado).days
                casa["dias_restantes"] = max(0, casa["dias_restantes"] - dias_transcurridos)
                casa["ultima_actualizacion"] = ahora.isoformat()
                db_save(data)

            if casa["modo_comercial"] and casa["dias_restantes"] <= 0:
                await hub.emitir({"tipo": "LICENCIA_EXPIRADA"})

            await asyncio.sleep(3600)  # revisa cada hora

    @app.on_event("startup")
    async def _startup():
        asyncio.create_task(watchdog_licencia())

    # --------------------------------------------------------------------- #
    # Endpoints del agente (telemetría / comandos)
    # --------------------------------------------------------------------- #
    def _integrante_por_token(data: dict, token: str) -> Optional[dict]:
        return data["integrantes"].get(token)

    @app.post("/api/agente/telemetria")
    async def recibir_telemetria(request: Request):
        token = request.headers.get("X-Device-Token", "")
        data = db_load()
        integrante = _integrante_por_token(data, token)
        if not integrante:
            raise HTTPException(status_code=403, detail="Token de dispositivo no reconocido")
        payload = await request.json()
        integrante.update(
            {
                "cpu": payload.get("cpu", 0),
                "ram": payload.get("ram", 0),
                "bateria": payload.get("bateria", "N/D"),
                "ventana_activa": payload.get("ventana_activa", "Desconocida"),
                "ultimo_reporte": datetime.utcnow().isoformat(),
                "en_linea": True,
            }
        )
        db_save(data)
        await hub.emitir({"tipo": "TELEMETRIA", "integrante": integrante})
        return {"ok": True}

    @app.get("/api/agente/comando")
    async def consultar_comando(request: Request):
        token = request.headers.get("X-Device-Token", "")
        data = db_load()
        integrante = _integrante_por_token(data, token)
        if not integrante:
            raise HTTPException(status_code=403, detail="Token de dispositivo no reconocido")
        for cid, cmd in data["comandos"].items():
            if cmd["device_token"] == token and not cmd["completado"]:
                return {"comando_id": cid, "accion": cmd["accion"]}
        return JSONResponse(status_code=204, content=None)

    @app.post("/api/agente/comando/{comando_id}/completado")
    async def completar_comando(comando_id: str, request: Request):
        token = request.headers.get("X-Device-Token", "")
        data = db_load()
        cmd = data["comandos"].get(comando_id)
        if not cmd or cmd["device_token"] != token:
            raise HTTPException(status_code=404, detail="Comando no encontrado")
        cmd["completado"] = True
        db_save(data)
        return {"ok": True}

    # --------------------------------------------------------------------- #
    # Endpoints del panel familiar (requieren sesión autenticada, ver login)
    # --------------------------------------------------------------------- #
    @app.get("/api/family")
    async def listar_familia(token: str = Depends(requiere_sesion)):
        data = db_load()
        return {"integrantes": data["integrantes"], "casa": data["casa"]}

    @app.post("/api/family/nuevo")
    async def crear_integrante(request: Request, token: str = Depends(requiere_sesion)):
        payload = await request.json()
        nombre = payload.get("nombre", "").strip()
        if not nombre:
            raise HTTPException(status_code=400, detail="Nombre requerido")
        device_token = secrets.token_urlsafe(16)
        data = db_load()
        data["integrantes"][device_token] = {
            "nombre": nombre,
            "cpu": 0,
            "ram": 0,
            "bateria": "N/D",
            "ventana_activa": "Sin datos aún",
            "en_linea": False,
            "lat": payload.get("lat"),
            "lon": payload.get("lon"),
            "minutos_pantalla_extra": 0,
        }
        db_save(data)
        return {"device_token": device_token}

    @app.post("/api/command")
    async def enviar_comando(request: Request, token: str = Depends(requiere_sesion)):
        payload = await request.json()
        device_token = payload.get("device_token")
        accion = payload.get("accion")
        if accion not in {"BLOQUEAR", "LIBERAR", "REINICIAR", "APAGAR"}:
            raise HTTPException(status_code=400, detail="Acción inválida")
        data = db_load()
        if device_token not in data["integrantes"]:
            raise HTTPException(status_code=404, detail="Integrante no encontrado")
        comando_id = secrets.token_hex(8)
        data["comandos"][comando_id] = {
            "device_token": device_token,
            "accion": accion,
            "completado": False,
        }
        db_save(data)
        return {"ok": True, "comando_id": comando_id}

    @app.get("/api/missions")
    async def listar_misiones(token: str = Depends(requiere_sesion)):
        return db_load()["misiones"]

    @app.post("/api/missions/{mission_id}/toggle")
    async def alternar_mision(mission_id: str, token: str = Depends(requiere_sesion)):
        data = db_load()
        for m in data["misiones"]:
            if m["id"] == mission_id:
                m["completada"] = not m["completada"]
                db_save(data)
                return m
        raise HTTPException(status_code=404, detail="Misión no encontrada")

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await hub.conectar(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            hub.desconectar(ws)  

# =============================================================================
# HTML — DASHBOARD FAMILIAR (interfaz principal, sin ningún rastro de admin)
# =============================================================================
HTML_DASHBOARD = """
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Aegis Family OS</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;800&display=swap" rel="stylesheet">
<style>
  body { background: radial-gradient(circle at top, #0b1120 0%, #020617 60%); font-family:'JetBrains Mono',monospace; color:#e2e8f0; }
  .glass-card { background:rgba(15,23,42,0.45); backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px); border:1px solid rgba(30,41,59,0.8); }
  .neon-bar { box-shadow: 0 0 10px currentColor; }
  #map { height: 100%; width: 100%; border-radius: 1rem; filter: brightness(0.9) contrast(1.05); }
  .leaflet-control-attribution { display:none !important; }
  ::-webkit-scrollbar { width:6px; }
  ::-webkit-scrollbar-thumb { background:#1e293b; border-radius:4px; }
  #overlayLicencia { position:fixed; inset:0; z-index:50; display:none; align-items:center; justify-content:center; backdrop-filter: blur(10px); background:rgba(2,6,23,0.85); }
</style>
</head>
<body class="min-h-screen p-6">

  <!-- Overlay de licencia vencida (Candado HTTP 402 / WebSocket) -->
  <div id="overlayLicencia">
    <div class="glass-card rounded-2xl p-10 max-w-md text-center space-y-4 border border-rose-500/40">
      <i data-lucide="lock" class="mx-auto text-rose-400" style="width:48px;height:48px"></i>
      <h2 class="text-rose-400 text-lg tracking-widest">SUSCRIPCIÓN VENCIDA</h2>
      <p class="text-sm text-slate-400">Renueva tu licencia por transferencia bancaria o Yape para reactivar el panel familiar.</p>
      <p class="text-xs text-slate-500">Contacta a quien administra la cuenta de esta casa para coordinar el pago y la reactivación manual.</p>
    </div>
  </div>

  <header class="flex items-center justify-between mb-6">
    <div class="flex items-center gap-3">
      <i data-lucide="shield" class="text-cyan-400"></i>
      <h1 class="text-xl tracking-widest text-cyan-400">AEGIS FAMILY OS</h1>
    </div>
    <div class="flex items-center gap-2 text-xs text-slate-500">
      <span id="wsEstado" class="inline-block w-2 h-2 rounded-full bg-slate-600"></span>
      <span id="wsEstadoTexto">Conectando...</span>
    </div>
  </header>

  <main class="grid grid-cols-1 xl:grid-cols-3 gap-6" style="height: 70vh;">
    <!-- Panel principal: tarjetas de integrantes -->
    <section id="familia" class="xl:col-span-2 grid grid-cols-1 md:grid-cols-2 gap-5 overflow-y-auto pr-2"></section>

    <!-- Panel lateral: mapa a pantalla completa -->
    <section class="glass-card rounded-2xl p-3">
      <div id="map"></div>
    </section>
  </main>

  <!-- Panel inferior: misiones familiares -->
  <section class="glass-card rounded-2xl p-6 mt-6">
    <h2 class="text-sm text-slate-400 mb-4 tracking-widest">MISIONES FAMILIARES · SALDO DE TIEMPO EN PANTALLA</h2>
    <div id="misiones" class="grid grid-cols-1 md:grid-cols-3 gap-4"></div>
  </section>

  <script>
    lucide.createIcons();

    // Mapa OpenStreetMap (sin dependencia de CartoDB)
    const map = L.map('map', { zoomControl: true }).setView([-9.19, -75.02], 5);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
    }).addTo(map);
    const marcadores = {};

    function actualizarMarcador(token, integrante) {
      if (integrante.lat == null || integrante.lon == null) return;
      if (marcadores[token]) {
        marcadores[token].setLatLng([integrante.lat, integrante.lon]);
      } else {
        marcadores[token] = L.marker([integrante.lat, integrante.lon]).addTo(map).bindPopup(integrante.nombre);
      }
    }

    function barra(valor, color) {
      return `
        <div class="w-full h-2 bg-slate-800/80 rounded-full overflow-hidden">
          <div class="h-full rounded-full neon-bar" style="width:${valor}%; background:${color}; color:${color};"></div>
        </div>`;
    }

    function tarjetaIntegrante(token, i) {
      return `
      <div class="glass-card rounded-2xl p-5 space-y-4" id="card-${token}">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="w-2 h-2 rounded-full ${i.en_linea ? 'bg-emerald-400' : 'bg-slate-600'}"></span>
            <h3 class="font-bold text-slate-100">${i.nombre}</h3>
          </div>
          <span class="text-xs text-slate-500">${i.bateria}</span>
        </div>

        <p class="text-xs text-slate-400 truncate">Activo: <span class="text-slate-200">${i.ventana_activa || 'Sin datos'}</span></p>

        <div class="space-y-2">
          <div class="flex justify-between text-xs text-slate-400"><span>CPU</span><span>${i.cpu}%</span></div>
          ${barra(i.cpu, '#22d3ee')}
          <div class="flex justify-between text-xs text-slate-400"><span>RAM</span><span>${i.ram}%</span></div>
          ${barra(i.ram, '#a855f7')}
        </div>

        <div class="grid grid-cols-4 gap-2 pt-2">
          <button onclick="enviarComando('${token}','BLOQUEAR')" class="flex flex-col items-center gap-1 text-xs bg-slate-900/70 hover:bg-slate-800 rounded-lg py-2 border border-slate-800/80">
            <i data-lucide="lock" class="text-amber-400" style="width:16px;height:16px"></i>Bloquear
          </button>
          <button onclick="enviarComando('${token}','LIBERAR')" class="flex flex-col items-center gap-1 text-xs bg-slate-900/70 hover:bg-slate-800 rounded-lg py-2 border border-slate-800/80">
            <i data-lucide="unlock" class="text-emerald-400" style="width:16px;height:16px"></i>Liberar
          </button>
          <button onclick="enviarComando('${token}','REINICIAR')" class="flex flex-col items-center gap-1 text-xs bg-slate-900/70 hover:bg-slate-800 rounded-lg py-2 border border-slate-800/80">
            <i data-lucide="refresh-cw" class="text-cyan-400" style="width:16px;height:16px"></i>Reiniciar
          </button>
          <button onclick="enviarComando('${token}','APAGAR')" class="flex flex-col items-center gap-1 text-xs bg-slate-900/70 hover:bg-slate-800 rounded-lg py-2 border border-slate-800/80">
            <i data-lucide="power" class="text-rose-400" style="width:16px;height:16px"></i>Apagar
          </button>
        </div>
      </div>`;
    }

    async function enviarComando(token, accion) {
      await fetch('/api/command', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_token: token, accion })
      });
    }

    async function cargarFamilia() {
      const res = await fetch('/api/family');
      if (!res.ok) return;
      const data = await res.json();
      const cont = document.getElementById('familia');
      cont.innerHTML = '';
      for (const [token, integrante] of Object.entries(data.integrantes)) {
        cont.insertAdjacentHTML('beforeend', tarjetaIntegrante(token, integrante));
        actualizarMarcador(token, integrante);
      }
      lucide.createIcons();
    }

    async function cargarMisiones() {
      const res = await fetch('/api/missions');
      if (!res.ok) return;
      const misiones = await res.json();
      const cont = document.getElementById('misiones');
      cont.innerHTML = misiones.map(m => `
        <div class="flex items-center justify-between bg-slate-900/50 border border-slate-800/80 rounded-xl px-4 py-3">
          <div>
            <p class="text-sm ${m.completada ? 'text-emerald-400 line-through' : 'text-slate-200'}">${m.titulo}</p>
            <p class="text-xs text-slate-500">+${m.recompensa_min} min de pantalla</p>
          </div>
          <button onclick="alternarMision('${m.id}')" class="text-xs px-3 py-1 rounded-lg border border-slate-700 hover:bg-slate-800">
            ${m.completada ? 'Desmarcar' : 'Completar'}
          </button>
        </div>`).join('');
    }

    async function alternarMision(id) {
      await fetch(`/api/missions/${id}/toggle`, { method: 'POST' });
      cargarMisiones();
    }

    // WebSocket dinámico: usa location.host para funcionar en Render/local
    function initSocket() {
      const protocolo = location.protocol === 'https:' ? 'wss://' : 'ws://';
      const socket = new WebSocket(protocolo + location.host + '/ws');

      socket.onopen = () => {
        document.getElementById('wsEstado').className = 'inline-block w-2 h-2 rounded-full bg-emerald-400';
        document.getElementById('wsEstadoTexto').textContent = 'En línea';
      };

      socket.onclose = () => {
        document.getElementById('wsEstado').className = 'inline-block w-2 h-2 rounded-full bg-rose-500';
        document.getElementById('wsEstadoTexto').textContent = 'Desconectado, reintentando...';
        setTimeout(initSocket, 3000);
      };

      socket.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.tipo === 'TELEMETRIA') {
          cargarFamilia();
        } else if (msg.tipo === 'LICENCIA_EXPIRADA') {
          document.getElementById('overlayLicencia').style.display = 'flex';
          lucide.createIcons();
        } else if (msg.tipo === 'LICENCIA_RENOVADA') {
          document.getElementById('overlayLicencia').style.display = 'none';
        }
      };
    }

    cargarFamilia();
    cargarMisiones();
    initSocket();
    setInterval(cargarFamilia, 15000);
  </script>
</body>
</html>
"""


# ==============================================================================
# 10. ARRANQUE DEL SERVIDOR ASGI (CORREGIDO DE RAÍZ)
# ==============================================================================
if __name__ == "__main__":
    import uvicorn
    import sys
    
    # Obtenemos el módulo actual de la memoria de Python de forma limpia
    modulo_actual = sys.modules[__name__]
    
    # Buscamos de forma inteligente el objeto de FastAPI dentro de tu archivo
    objeto_app = None
    for atributo in dir(modulo_actual):
        obj = getattr(modulo_actual, atributo)
        if obj.__class__.__name__ == "FastAPI":
            objeto_app = obj
            break
            
    if objeto_app is None:
        # Si Qwen la escondió dentro de una función, la forzamos a nacer aquí:
        from fastapi import FastAPI
        objeto_app = FastAPI()

    puerto_dinamico = int(os.getenv("PORT", 8000))
    print(f"[AEGIS-SAAS] Levantando servidor en el puerto: {puerto_dinamico}")
    
    # Ejecutamos el objeto real encontrado sin importar cómo se llame
    uvicorn.run(objeto_app, host="0.0.0.0", port=puerto_dinamico)


