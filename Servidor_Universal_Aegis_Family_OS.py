"""
Servidor_Universal_Aegis_Family_OS.py
=====================================
AEGIS FAMILY OS · Servidor Universal Políglota v12.1
---------------------------------------------------
Backend FastAPI + SQLite/PostgreSQL + Dashboard Cyberpunk + Agente Local
+ WebSockets dinámicos + Mercado Pago Perú, todo en UN SOLO archivo.

BLINDAJES v12.1
---------------
· Bypass VIP silencioso: validación por hash SHA-256 en RAM (sin plaintext
  visible en HTML/JS). El frontend nunca sugiere el código real.
· CRUD dinámico de integrantes: cuadrícula de círculos neón, botón flotante
  (+) con Lucide, edición y borrado por perfil.
· Telemetría de batería real del navegador vía navigator.getBattery().
· Comandos remotos bloqueados para la estación local maestra.
· Mapa Leaflet con geolocalización HTML5 + fallback IP + zoom nivel 15.

Ejecutar local:
    pip install fastapi uvicorn psutil pydantic requests
    python Servidor_Universal_Aegis_Family_OS.py

Render.com → Start Command:
    python Servidor_Universal_Aegis_Family_OS.py
"""

import os
import hmac
import json
import sqlite3
import hashlib
import secrets
import socket
import string
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional, Literal, List

import psutil
from fastapi import (
    FastAPI, HTTPException, Header, Depends, Request,
    WebSocket, WebSocketDisconnect,
)
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    import requests
except ImportError:
    requests = None


# ===========================================================================
# 1 · CONFIGURACIÓN POR ENTORNO
# ===========================================================================
DB_NAME = "aegis_family.db"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

PIN_SEMILLA_MASTER = os.environ.get("AEGIS_MASTER_PIN", "9999")
CORS_ORIGIN_REGEX = os.environ.get("AEGIS_CORS_REGEX", r".*")
# 🐛 FIX #3 (CORS): un origin_regex comodín (".*") combinado con allow_credentials=True
# viola la spec fetch/CORS — el navegador descarta silenciosamente la respuesta con
# credenciales. La API autentica por headers (X-Pin/X-Casa), no por cookies, así que
# no necesita credentials=True salvo que el operador lo pida explícitamente.
CORS_CREDENCIALES = os.environ.get("AEGIS_CORS_CREDENCIALES", "FALSE").upper() == "TRUE"

MODO_COMERCIAL = os.environ.get("AEGIS_MODO_COMERCIAL", "FALSE").upper() == "TRUE"

MP_ACCESS_TOKEN = os.environ.get("AEGIS_MP_TOKEN", "").strip()
DIAS_SUSCRIPCION = int(os.environ.get("AEGIS_DIAS_SUSCRIPCION", "30"))

# ---------------------------------------------------------------------------
# 🔐 BÓVEDA VIP — Hashes SHA-256 de los códigos familiares privilegiados.
# La validación vive SOLO en memoria RAM. El plaintext nunca aparece en el
# HTML/JS ni en respuestas HTTP. Para añadir un nuevo código:
#     python -c "import hashlib; print(hashlib.sha256('TU_CODIGO'.encode()).hexdigest())"
# ---------------------------------------------------------------------------
_VIP_HASHES = frozenset({
    "99c8525b68df419a4e33b669046c76e279fb08eef2f3ff6d750dfbb9e6cdb195",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s · %(levelname)s · %(name)s · %(message)s",
)
log = logging.getLogger("aegis")


# ===========================================================================
# 2 · ROLES Y JERARQUÍA PARENTAL
# ===========================================================================
ROL_MASTER = "MASTER_ADMIN"
ROL_ADMIN_CASA = "ADMIN_CASA"
ROL_GESTOR_CASA = "GESTOR_CASA"
ROL_USUARIO = "USUARIO"
ROLES_VALIDOS = {ROL_MASTER, ROL_ADMIN_CASA, ROL_GESTOR_CASA, ROL_USUARIO}

JERARQUIA = {
    ROL_MASTER: 4,
    ROL_ADMIN_CASA: 3,
    ROL_GESTOR_CASA: 2,
    ROL_USUARIO: 1,
}


# ===========================================================================
# 3 · MODELOS PYDANTIC
# ===========================================================================
TipoDispositivo = Literal["pc", "smartphone", "tablet", "smart_tv", "enchufe"]


class NuevaCasaSchema(BaseModel):
    nombre_casa: str = Field(min_length=1, max_length=80)
    nombre_admin: str = Field(min_length=1, max_length=80)
    pin_admin: str = Field(min_length=4, max_length=32)
    avatar_admin: str = "👨‍💻"


class NuevoPerfilSchema(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    pin: str = Field(min_length=4, max_length=32)
    avatar: str = "👤"


class EditarPerfilSchema(BaseModel):
    nombre: str = Field(min_length=1, max_length=80)
    pin: str = Field(min_length=4, max_length=32)
    avatar: str = "👤"


class CambiarRolSchema(BaseModel):
    rol: Literal["ADMIN_CASA", "GESTOR_CASA", "USUARIO"]


class DispositivoSchema(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    ip: str = ""
    mac: Optional[str] = ""
    tipo: TipoDispositivo = "pc"


class AccionEnergia(BaseModel):
    accion: Literal["ENCENDER", "BLOQUEAR", "APAGAR", "REINICIAR"]


class RegistroNodoSchema(BaseModel):
    """Alta/actualización de un nodo 'agentless': el propio navegador se anuncia."""
    device_uid: str = Field(min_length=8, max_length=128)
    nombre: str = Field(min_length=1, max_length=120)
    tipo: TipoDispositivo = "pc"


class TelemetriaWebSchema(BaseModel):
    """Telemetría capturada 100% con Web APIs nativas del navegador (sin agente)."""
    device_uid: str = Field(min_length=8, max_length=128)
    cpu: Optional[str] = None            # navigator.hardwareConcurrency
    ram: Optional[str] = None            # navigator.deviceMemory (aprox.)
    bateria: Optional[str] = None        # navigator.getBattery()
    ventana_activa: Optional[str] = None # document.title (foco/actividad)
    hilos: Optional[int] = None          # navigator.hardwareConcurrency
    lat: Optional[float] = None
    lng: Optional[float] = None
    ubicacion: Optional[str] = None


# ===========================================================================
# 4 · UTILIDADES
# ===========================================================================
def generar_salt() -> str:
    return secrets.token_hex(8)


def hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 100_000).hex()


def pin_coincide(pin: str, salt: str, hash_esperado: str) -> bool:
    return hmac.compare_digest(hash_pin(pin, salt), hash_esperado)


def _codigo_aleatorio(alfabeto: str, longitud: int) -> str:
    return "".join(secrets.choice(alfabeto) for _ in range(longitud))


def generar_codigo_casa() -> str:
    return _codigo_aleatorio(string.ascii_uppercase + string.digits, 6)


def generar_codigo_vinculacion() -> str:
    return _codigo_aleatorio(string.digits, 6)


def mac_es_valida(mac: str) -> bool:
    limpia = mac.replace(":", "").replace("-", "").strip()
    return len(limpia) == 12 and all(c in string.hexdigits for c in limpia)


def _url_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}".rstrip("/")


def _es_codigo_vip(codigo: str) -> bool:
    """Validación silenciosa en RAM. Nunca revela el plaintext."""
    if not codigo:
        return False
    digest = hashlib.sha256(codigo.strip().upper().encode()).hexdigest()
    return digest in _VIP_HASHES


# ===========================================================================
# 5 · CAPA DE BASE DE DATOS HÍBRIDA (SQLite ⇄ PostgreSQL)
# ===========================================================================
class ConexionDB:
    def __init__(self):
        self.es_postgres = bool(DATABASE_URL)
        self._id_pendiente: Optional[int] = None

        if self.es_postgres:
            try:
                import psycopg2
                import psycopg2.extras
            except ImportError as e:
                raise RuntimeError("DATABASE_URL configurada pero 'psycopg2' no instalado.") from e
            self.conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            self.cursor = self.conn.cursor()
        else:
            self.conn = sqlite3.connect(DB_NAME, timeout=15, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.cursor = self.conn.cursor()
            try:
                self.cursor.execute("PRAGMA journal_mode=WAL;")
                self.cursor.execute("PRAGMA synchronous=NORMAL;")
                self.cursor.execute("PRAGMA foreign_keys=ON;")
            except sqlite3.Error as e:
                log.warning("PRAGMA SQLite falló: %s", e)

    def __enter__(self): return self

    def __exit__(self, exc_type, exc, tb):
        self.cerrar()
        return False

    def ejecutar(self, sql: str, params: tuple = (), es_insert: bool = False):
        sql_final = sql
        if self.es_postgres:
            sql_final = sql_final.replace("?", "%s")
            if es_insert and "RETURNING" not in sql_final.upper():
                sql_final = sql_final.rstrip().rstrip(";") + " RETURNING id"
        self.cursor.execute(sql_final, params)
        self._id_pendiente = None
        if self.es_postgres and es_insert:
            fila = self.cursor.fetchone()
            self._id_pendiente = fila["id"] if fila else None
        return self.cursor

    def uno(self):
        fila = self.cursor.fetchone()
        return dict(fila) if fila is not None else None

    def todos(self):
        return [dict(f) for f in self.cursor.fetchall()]

    def id_insertado(self) -> Optional[int]:
        return self._id_pendiente if self.es_postgres else self.cursor.lastrowid

    def commit(self): self.conn.commit()

    def cerrar(self):
        try: self.conn.close()
        except Exception: pass


def db() -> ConexionDB:
    return ConexionDB()


# ===========================================================================
# 6 · ESQUEMA E INICIALIZACIÓN
# ===========================================================================
def init_db():
    conexion = db()
    pk = "SERIAL PRIMARY KEY" if conexion.es_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS casas (
            id {pk}, nombre TEXT NOT NULL,
            codigo_invitacion TEXT UNIQUE NOT NULL, creado_en TEXT
        )
    """)
    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS usuarios (
            id {pk}, casa_id INTEGER, nombre TEXT NOT NULL,
            pin_hash TEXT NOT NULL, salt TEXT NOT NULL,
            rol TEXT DEFAULT 'USUARIO', avatar TEXT DEFAULT '👤',
            FOREIGN KEY (casa_id) REFERENCES casas (id)
        )
    """)
    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS dispositivos (
            id {pk}, casa_id INTEGER NOT NULL, nombre TEXT NOT NULL,
            tipo TEXT DEFAULT 'pc', ip TEXT NOT NULL, mac TEXT DEFAULT '',
            token TEXT DEFAULT '', ubicacion TEXT DEFAULT 'Ubicación no establecida',
            lat REAL DEFAULT 0.0, lng REAL DEFAULT 0.0, estado TEXT DEFAULT 'ONLINE',
            asignado_a INTEGER, cpu TEXT, ram TEXT, disco TEXT, bateria TEXT,
            procesos INTEGER, actualizado_en TEXT,
            FOREIGN KEY (casa_id) REFERENCES casas (id)
        )
    """)
    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS codigos_vinculacion (
            id {pk}, casa_id INTEGER NOT NULL, codigo TEXT UNIQUE NOT NULL,
            nombre_sugerido TEXT DEFAULT '', tipo_sugerido TEXT DEFAULT 'pc',
            expira_en TEXT NOT NULL, usado INTEGER DEFAULT 0
        )
    """)
    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS comandos (
            id {pk}, dispositivo_id INTEGER NOT NULL,
            accion TEXT NOT NULL, estado TEXT DEFAULT 'PENDIENTE', creado_en TEXT
        )
    """)

    # -----------------------------------------------------------------
    # FIX #1 (concurrencia/duplicados) — red de seguridad a nivel de esquema:
    # índice ÚNICO PARCIAL sobre (casa_id, token) para filas con token no
    # vacío. El endpoint 'registrar-nodo' ya hace SELECT antes de decidir
    # INSERT/UPDATE, pero dos peticiones casi simultáneas de la MISMA
    # pestaña (doble refresco, reconexión de red) podrían pasar ambas el
    # SELECT antes de que la primera termine su INSERT. Este índice hace
    # que la base de datos rechace el segundo INSERT duplicado; el
    # endpoint atrapa ese conflicto y lo convierte en un UPDATE normal.
    # -----------------------------------------------------------------
    try:
        conexion.ejecutar(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_dispositivos_casa_token "
            "ON dispositivos (casa_id, token) WHERE token <> ''"
        )
    except Exception as e:
        log.warning(
            "No se pudo crear índice único de dispositivos (%s). Puede haber "
            "duplicados históricos de una versión anterior pendientes de limpieza.",
            e,
        )

    conexion.ejecutar("SELECT COUNT(*) AS total FROM usuarios WHERE rol = ?", (ROL_MASTER,))
    if conexion.uno()["total"] == 0:
        salt = generar_salt()
        conexion.ejecutar(
            "INSERT INTO usuarios (casa_id, nombre, pin_hash, salt, rol, avatar) "
            "VALUES (NULL, 'Master', ?, ?, ?, '🛡️')",
            (hash_pin(PIN_SEMILLA_MASTER, salt), salt, ROL_MASTER),
        )

    conexion.ejecutar("SELECT COUNT(*) AS total FROM casas")
    if conexion.uno()["total"] == 0:
        conexion.ejecutar(
            "INSERT INTO casas (nombre, codigo_invitacion, creado_en) VALUES (?, ?, ?)",
            ("Búnker Familiar", "FAM123", datetime.utcnow().isoformat()),
            es_insert=True,
        )
        casa_id = conexion.id_insertado()
        for perfil, pin, rol, av in [
            ("Manuel",      "1234", ROL_ADMIN_CASA,  "👨‍💻"),
            ("Abuela Rosa", "5555", ROL_GESTOR_CASA, "👵"),
            ("Jessica",     "1111", ROL_USUARIO,     "🎓"),
            ("Invitado",    "0000", ROL_USUARIO,     "👤"),
        ]:
            salt = generar_salt()
            conexion.ejecutar(
                "INSERT INTO usuarios (casa_id, nombre, pin_hash, salt, rol, avatar) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (casa_id, perfil, hash_pin(pin, salt), salt, rol, av),
            )
        conexion.ejecutar(
            "INSERT INTO dispositivos (casa_id, nombre, tipo, ip, mac, ubicacion, lat, lng, estado) "
            "VALUES (?, 'Equipo Principal', 'pc', '127.0.0.1', '00:11:22:33:44:55', "
            "'Ubicación detectada', -12.0464, -77.0428, 'ONLINE')",
            (casa_id,),
        )

    conexion.commit()
    conexion.cerrar()


# ===========================================================================
# 7 · HTML EMBEBIDO (SPLASH + DASHBOARD + CRUD INTEGRANTES + MAPA)
# ===========================================================================
HTML_DASHBOARD = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#000000">
<title>AEGIS FAMILY OS</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;600;700;900&family=Orbitron:wght@500;700;900&display=swap" rel="stylesheet">
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  *{-webkit-tap-highlight-color:transparent;box-sizing:border-box}
  body{font-family:'JetBrains Mono',monospace;background:#000;color:#94a3b8;margin:0;min-height:100vh;overflow-x:hidden}
  .font-display{font-family:'Orbitron',sans-serif;letter-spacing:.08em}
  .glass-card{
    background:linear-gradient(155deg,rgba(8,20,36,.92) 0%,rgba(2,6,23,.9) 55%,rgba(2,6,23,.96) 100%);
    backdrop-filter:blur(26px);-webkit-backdrop-filter:blur(26px);
    border:1px solid rgba(34,211,238,.18);
    box-shadow:0 0 80px -30px rgba(34,211,238,.45),inset 0 1px 0 rgba(255,255,255,.05);
    border-radius:28px;
  }
  .hud-box{
    background:linear-gradient(155deg,rgba(8,20,36,.85),rgba(2,6,23,.9));
    border:1px solid rgba(34,211,238,.15);border-radius:20px;
    box-shadow:inset 0 0 30px -10px rgba(34,211,238,.2);
  }
  .btn-glass{
    position:relative;overflow:hidden;
    background:linear-gradient(150deg,rgba(255,255,255,.09),rgba(255,255,255,.02));
    border:1px solid rgba(255,255,255,.12);
    transition:all .28s cubic-bezier(.22,1,.36,1);
    letter-spacing:.14em;border-radius:18px;cursor:pointer;color:inherit;
  }
  .btn-glass:hover{border-color:rgba(34,211,238,.9);background:rgba(34,211,238,.16);color:#a5f3fc;box-shadow:0 0 34px -6px rgba(34,211,238,.9)}
  .btn-glass:active{transform:scale(.97)}
  .cyber-input{
    background:rgba(2,6,23,.9);border:1px solid rgba(51,65,85,.9);
    transition:all .25s ease;border-radius:18px;color:#fff;
    padding:.85rem 1.2rem;width:100%;
    font-family:'JetBrains Mono',monospace;font-size:.85rem;
  }
  .cyber-input:focus{outline:none;border-color:rgba(34,211,238,.8);box-shadow:0 0 0 1px rgba(34,211,238,.35),0 0 32px -10px rgba(34,211,238,.9)}
  .rol-badge{font-size:10px;font-weight:800;padding:3px 10px;border-radius:8px;border:1px solid;letter-spacing:.08em;display:inline-block}
  .rol-ADMIN_CASA{color:#22d3ee;background:rgba(8,145,178,.18);border-color:rgba(34,211,238,.55)}
  .rol-GESTOR_CASA{color:#c084fc;background:rgba(147,51,234,.18);border-color:rgba(192,132,252,.55)}
  .rol-USUARIO{color:#a3e635;background:rgba(101,163,13,.18);border-color:rgba(163,230,53,.55)}
  .rol-MASTER_ADMIN{color:#fbbf24;background:rgba(217,119,6,.18);border-color:rgba(251,191,36,.65)}
  .txt-cyan{color:#67e8f9;text-shadow:0 0 14px rgba(34,211,238,.7)}
  .txt-amber{color:#fcd34d;text-shadow:0 0 14px rgba(251,191,36,.7)}
  .txt-rose{color:#fda4af;text-shadow:0 0 14px rgba(244,63,94,.85)}
  .txt-emerald{color:#6ee7b7;text-shadow:0 0 14px rgba(16,185,129,.7)}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.18}}
  .blink{animation:blink 1.3s ease-in-out infinite}
  @keyframes floatY{0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)}}
  .floaty{animation:floatY 4s ease-in-out infinite}
  @keyframes spinSlow{to{transform:rotate(360deg)}}
  .spin-slow{animation:spinSlow 14s linear infinite}
  @keyframes pulseNeon{0%,100%{box-shadow:0 0 30px -4px rgba(34,211,238,.9),0 0 70px -10px rgba(34,211,238,.5);transform:scale(1)}
    50%{box-shadow:0 0 60px 0 rgba(34,211,238,1),0 0 110px -10px rgba(34,211,238,.7);transform:scale(1.04)}}
  .btn-neon{animation:pulseNeon 2.4s ease-in-out infinite}
  @keyframes fadeInUp{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}
  .fade-up{animation:fadeInUp .7s ease both}
  @keyframes gridDrift{0%{background-position:0 0}100%{background-position:70px 70px}}
  .grid-drift{animation:gridDrift 24s linear infinite}

  /* CÍRCULOS DE INTEGRANTES (CRUD visual) */
  .avatar-circle{
    position:relative;
    width:88px;height:88px;border-radius:50%;
    display:grid;place-items:center;font-size:2.1rem;
    background:radial-gradient(circle at 35% 28%,rgba(34,211,238,.28),rgba(2,6,23,.95) 70%);
    border:2px solid rgba(34,211,238,.45);
    box-shadow:0 0 30px -6px rgba(34,211,238,.85),inset 0 0 22px rgba(34,211,238,.18);
    transition:transform .3s cubic-bezier(.22,1,.36,1),border-color .3s,box-shadow .3s;
    cursor:pointer;
  }
  .avatar-circle:hover{
    transform:translateY(-4px) scale(1.07);
    border-color:#22d3ee;
    box-shadow:0 0 44px -4px rgba(34,211,238,1),inset 0 0 30px rgba(34,211,238,.25);
  }
  .avatar-name{font-size:10px;letter-spacing:.1em;margin-top:6px;text-align:center;
    color:#cbd5e1;max-width:88px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .avatar-mini{
    position:absolute;width:24px;height:24px;border-radius:50%;
    display:grid;place-items:center;font-size:10px;font-weight:900;
    border:1px solid;cursor:pointer;transition:all .2s;background:#020617;z-index:3;
  }
  .avatar-mini-edit{top:-4px;right:-4px;color:#67e8f9;border-color:rgba(34,211,238,.7)}
  .avatar-mini-edit:hover{background:rgba(34,211,238,.25);transform:scale(1.15)}
  .avatar-mini-del{bottom:-4px;right:-4px;color:#fda4af;border-color:rgba(251,113,133,.7)}
  .avatar-mini-del:hover{background:rgba(244,63,94,.25);transform:scale(1.15)}

  /* BOTÓN FLOTANTE (+) LUCIDE */
  .btn-add-circle{
    width:56px;height:56px;border-radius:50%;
    display:grid;place-items:center;font-size:26px;font-weight:300;
    color:#67e8f9;border:1.5px solid rgba(34,211,238,.6);background:rgba(34,211,238,.06);
    cursor:pointer;transition:all .25s;
    box-shadow:0 0 30px -6px rgba(34,211,238,.9);
  }
  .btn-add-circle:hover{
    background:rgba(34,211,238,.22);color:#a5f3fc;
    transform:scale(1.08) rotate(90deg);
    box-shadow:0 0 46px -4px rgba(34,211,238,1);
  }

  #mapa{width:100%;height:100%;border-radius:20px;filter:saturate(1.25) contrast(1.08) brightness(.92)}
  .leaflet-container{background:#020617}
  .pin-neon{filter:drop-shadow(0 0 14px rgba(34,211,238,1)) drop-shadow(0 0 26px rgba(34,211,238,.6))}
</style>
</head>
<body>

<!-- ================= SPLASH SCREEN ================= -->
<div id="splashScreen"
     class="fixed inset-0 z-[9999] flex flex-col items-center justify-center bg-black transition-all duration-700 opacity-100">
  <div class="absolute inset-0 pointer-events-none"
       style="background:radial-gradient(circle at 15% 5%,rgba(6,182,212,.22),transparent 42%),
              radial-gradient(circle at 88% 12%,rgba(59,130,246,.18),transparent 45%),
              radial-gradient(circle at 50% 110%,rgba(244,63,94,.12),transparent 50%);"></div>
  <div class="absolute inset-0 opacity-[0.14] grid-drift pointer-events-none"
       style="background-image:linear-gradient(rgba(34,211,238,.35) 1px,transparent 1px),
              linear-gradient(90deg,rgba(34,211,238,.35) 1px,transparent 1px);
              background-size:70px 70px;"></div>

  <div class="relative fade-up flex flex-col items-center gap-10 px-6 text-center">
    <div class="relative w-40 h-40 grid place-items-center">
      <div class="absolute inset-0 rounded-full border-2 border-cyan-400/40 spin-slow"></div>
      <div class="absolute inset-3 rounded-full border border-cyan-300/20"></div>
      <div class="absolute inset-5 rounded-full"
           style="background:radial-gradient(circle at 40% 30%,rgba(34,211,238,.35),rgba(2,6,23,.9));
                  border:1px solid rgba(34,211,238,.5);
                  box-shadow:0 0 70px -8px rgba(34,211,238,1);"></div>
      <svg class="w-20 h-20 text-cyan-300 drop-shadow-[0_0_22px_rgba(34,211,238,.95)] floaty"
           viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6">
        <path d="M12 2L4 6v6c0 5 3.5 9 8 10 4.5-1 8-5 8-10V6l-8-4z"/>
        <path d="M9 12l2 2 4-4"/>
      </svg>
    </div>
    <div>
      <h1 class="font-display text-4xl md:text-6xl font-black tracking-[0.22em] bg-clip-text text-transparent"
          style="background-image:linear-gradient(90deg,#67e8f9,#22d3ee,#3b82f6,#67e8f9);filter:drop-shadow(0 0 30px rgba(34,211,238,.7));">
        AEGIS FAMILY OS
      </h1>
      <p class="mt-4 text-[10px] md:text-xs tracking-[0.45em] text-cyan-400/70">
        NÚCLEO OPERATIVO · v12.1 · FAMILIA SEGURA
      </p>
    </div>
    <div class="flex items-center gap-3 text-[10px] tracking-[0.35em] text-slate-500">
      <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
      ENLACE SATELITAL LISTO · ESPERANDO AUTORIZACIÓN
    </div>
    <button onclick="conversar()"
            class="btn-neon font-display font-black text-base md:text-lg tracking-[0.3em] uppercase text-cyan-100 px-14 py-5 rounded-3xl border border-cyan-400/70 mt-4"
            style="background:linear-gradient(150deg,rgba(34,211,238,.28),rgba(59,130,246,.12));">
      ▶ CONECTAR NÚCLEO
    </button>
  </div>
</div>

<!-- ================= APP PRINCIPAL ================= -->
<div id="appShell" class="opacity-0 pointer-events-none transition-all duration-700 min-h-screen w-full p-4 md:p-6">
  <div class="max-w-[1700px] mx-auto flex flex-col gap-6">

    <header class="glass-card p-6 flex flex-wrap justify-between items-center gap-4">
      <div class="flex items-center gap-4">
        <span id="userAvatar" class="text-4xl w-16 h-16 grid place-items-center rounded-2xl bg-slate-950 border border-cyan-500/30">👨‍💻</span>
        <div>
          <h2 id="userNombre" class="font-display text-lg md:text-2xl font-black text-white tracking-[0.08em]">--</h2>
          <span id="userRol" class="rol-badge rol-USUARIO mt-1.5">--</span>
        </div>
      </div>
      <div class="flex items-center gap-3">
        <span id="casaBadge" class="text-[10px] tracking-[0.3em] text-cyan-400/70">CASA: ---</span>
        <button onclick="cerrarSesion()" class="btn-glass px-5 py-3 text-[11px] tracking-[0.2em] text-rose-300 uppercase">
          ✕ SALIR
        </button>
      </div>
    </header>

    <!-- FASE 1: Acceso -->
    <div id="faseAcceso" class="glass-card p-8 md:p-12 flex flex-col gap-6 items-center text-center">
      <h1 class="font-display text-3xl md:text-5xl font-black text-white tracking-[0.1em]">TERMINAL DE ACCESO</h1>
      <p class="text-[10px] tracking-[0.35em] text-cyan-400/60">INGRESA EL CÓDIGO DE TU CASA O CREA UNA NUEVA</p>
      <div class="w-full max-w-md flex flex-col gap-3 mt-4">
        <input id="inputCodigoCasa" type="text" maxlength="6" placeholder="X7R9Q2" autocomplete="off"
               class="cyber-input text-center text-3xl tracking-[0.5em] uppercase" style="color:#67e8f9">
        <button onclick="entrarACasa()" class="btn-glass py-4 font-display text-xs tracking-[0.25em] text-cyan-200 uppercase">
          ▶ ENTRAR A MI CASA
        </button>
        <button onclick="mostrarCrear()" class="btn-glass py-3 text-[11px] tracking-[0.2em] text-slate-300 uppercase">
          + CREAR CASA NUEVA
        </button>
      </div>
      <div id="crearBox" class="hidden w-full max-w-md flex flex-col gap-3 mt-3">
        <input id="nuevaCasaNombre" type="text" placeholder="Nombre de la casa" class="cyber-input">
        <input id="nuevaCasaAdminNombre" type="text" placeholder="Tu nombre (Padre/Madre)" class="cyber-input">
        <input id="nuevaCasaPin" type="password" placeholder="PIN Admin (mín. 4)" class="cyber-input text-center tracking-[0.4em]">
        <button onclick="crearCasa()" class="btn-glass py-3 font-display text-xs tracking-[0.25em] text-cyan-200 uppercase">⚡ CREAR CASA</button>
      </div>
    </div>

    <!-- FASE 2: Selector de perfiles -->
    <div id="fasePerfiles" class="hidden glass-card p-8 md:p-10 text-center flex flex-col gap-8">
      <div>
        <p class="text-[10px] tracking-[0.4em] text-cyan-400/60 mb-3">◈ TERMINAL DE ACCESO · AEGIS</p>
        <h2 id="nombreCasaActual" class="font-display text-3xl md:text-4xl font-black text-white tracking-[0.08em]">¿Quién está accediendo?</h2>
      </div>
      <div id="gridPerfiles" class="flex flex-wrap justify-center items-center gap-6 py-4"></div>
      <button onclick="volverInicio()" class="text-[10px] tracking-[0.3em] text-slate-500 hover:text-cyan-400">← SALIR DE ESTA CASA</button>
    </div>

    <!-- FASE 3: Dashboard -->
    <div id="faseDashboard" class="hidden grid grid-cols-1 xl:grid-cols-12 gap-6 w-full">

      <!-- Columna izquierda -->
      <div class="xl:col-span-4 flex flex-col gap-6 min-w-0">
        <div class="glass-card p-6 flex flex-col gap-4">
          <div class="flex justify-between items-center flex-wrap gap-2">
            <h3 class="font-display text-sm font-bold text-cyan-300 tracking-[0.15em]">🖥 DISPOSITIVOS</h3>
            <button id="btnVincular" onclick="mostrarCodigoCasa()" class="hidden btn-glass px-3 py-2 text-[10px] tracking-[0.2em] text-cyan-200 uppercase">🔗 COMPARTIR CÓDIGO</button>
          </div>
          <div id="listaDispositivos" class="space-y-3 max-h-[45vh] overflow-y-auto pr-1"></div>
        </div>

        <!-- CRUD INTEGRANTES -->
        <div id="panelIntegrantes" class="glass-card p-6 flex flex-col gap-4">
          <div class="flex justify-between items-center">
            <h3 class="font-display text-sm font-bold text-cyan-300 tracking-[0.15em]">👥 INTEGRANTES</h3>
            <button id="btnAddIntegrante" onclick="abrirModalNuevoIntegrante()"
                    class="btn-add-circle hidden" title="Añadir integrante">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
                <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
              </svg>
            </button>
          </div>
          <div id="gridIntegrantes" class="flex flex-wrap gap-5 justify-center py-2"></div>
        </div>
      </div>

      <!-- Columna derecha -->
      <div class="xl:col-span-8 flex flex-col gap-6 min-w-0">
        <div class="glass-card p-6">
          <div class="flex justify-between items-center mb-4 flex-wrap gap-2">
            <h3 class="font-display text-sm font-bold text-cyan-300 tracking-[0.15em]">📡 TELEMETRÍA</h3>
            <span id="targetNombre" class="text-xs tracking-[0.2em] txt-cyan">SIN SELECCIÓN</span>
          </div>
          <div class="grid grid-cols-3 gap-3 mb-3">
            <div class="hud-box p-4 text-center"><p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2">CPU</p><p id="mCpu" class="font-display text-xl font-black txt-cyan">--</p></div>
            <div class="hud-box p-4 text-center"><p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2">RAM</p><p id="mRam" class="font-display text-xl font-black txt-cyan">--</p></div>
            <div class="hud-box p-4 text-center"><p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2">ENFOQUE</p><p id="mDisco" class="font-display text-sm font-black txt-cyan truncate" title="">--</p></div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div class="hud-box p-4 text-center"><p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2">BATERÍA REAL</p><p id="mBat" class="font-display text-xl font-black txt-cyan">--</p></div>
            <div class="hud-box p-4 text-center"><p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2">HILOS LÓGICOS</p><p id="mProc" class="font-display text-xl font-black txt-cyan">--</p></div>
          </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div class="glass-card p-6 flex flex-col gap-3">
            <h3 class="font-display text-sm font-bold text-rose-300 tracking-[0.15em]">📍 RADAR SATELITAL</h3>
            <div class="w-full h-[280px] rounded-2xl overflow-hidden border border-slate-800">
              <div id="mapa"></div>
            </div>
          </div>

          <div class="glass-card p-6 flex flex-col justify-between gap-4">
            <div>
              <h3 class="font-display text-sm font-bold text-amber-300 tracking-[0.15em] mb-2">⚡ COMANDOS REMOTOS</h3>
              <p class="text-[10px] tracking-[0.15em] text-slate-500">SOLO ADMIN CASA PUEDE EJECUTARLOS</p>
            </div>

            <div id="panelComandos" class="grid grid-cols-2 gap-3">
              <button onclick="ejecutarComando('ENCENDER')"  class="btn-glass py-4 text-[11px] tracking-[0.18em] text-emerald-300">⏻ ENCENDER</button>
              <button onclick="ejecutarComando('BLOQUEAR')"  class="btn-glass py-4 text-[11px] tracking-[0.18em] text-amber-300">🔒 BLOQUEAR</button>
              <button onclick="ejecutarComando('REINICIAR')" class="btn-glass py-4 text-[11px] tracking-[0.18em] text-blue-300">↻ REINICIAR</button>
              <button onclick="ejecutarComando('APAGAR')"    class="btn-glass py-4 text-[11px] tracking-[0.18em] text-rose-300">⏹ APAGAR</button>
            </div>

            <div id="avisoLocal" class="hidden text-[10px] tracking-[0.22em] text-rose-300 text-center leading-relaxed border border-rose-500/40 rounded-2xl p-4 bg-rose-500/5">
              ⛔ ESTACIÓN LOCAL MAESTRA<br>Directivas de energía restringidas
            </div>

            <div id="avisoGestor" class="hidden text-[10px] tracking-[0.2em] text-purple-300 text-center leading-relaxed">
              TU ROL PERMITE SUPERVISIÓN, NO CONTROL DE ENERGÍA.
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ================= MODAL PIN ================= -->
<div id="modalPin" class="hidden fixed inset-0 bg-black/90 backdrop-blur-xl flex items-center justify-center p-4 z-50">
  <div class="glass-card p-8 w-full max-w-md text-center flex flex-col gap-5">
    <div id="pinAvatar" class="text-6xl">👤</div>
    <h3 id="pinNombre" class="font-display text-2xl font-black text-white">--</h3>
    <input id="pinInput" type="password" placeholder="••••" class="cyber-input text-center text-3xl tracking-[0.4em]" style="color:#fcd34d">
    <div class="flex gap-3">
      <button onclick="cerrarModal('modalPin')" class="btn-glass flex-1 py-3 text-[11px] tracking-[0.2em]">CANCELAR</button>
      <button onclick="validarPin()" class="btn-glass flex-1 py-3 text-[11px] tracking-[0.2em] text-cyan-200">▶ INGRESAR</button>
    </div>
  </div>
</div>

<!-- ================= MODAL CÓDIGO DE CASA (AGENTLESS) ================= -->
<div id="modalVinculacion" class="hidden fixed inset-0 bg-black/90 backdrop-blur-xl flex items-center justify-center p-4 z-50">
  <div class="glass-card p-7 w-full max-w-lg text-center flex flex-col gap-5">
    <h3 class="font-display text-lg font-black text-white tracking-[0.1em]">🔗 CÓDIGO ÚNICO DE LA CASA</h3>
    <p class="text-[10px] tracking-[0.2em] text-slate-500">CUALQUIER INTEGRANTE LO INGRESA EN ESTA MISMA WEB, DESDE SU PROPIO DISPOSITIVO — SIN INSTALAR NADA</p>
    <div id="codigoVincBox" class="hud-box p-5 text-4xl tracking-[0.5em] txt-cyan">------</div>
    <p class="text-[10px] tracking-[0.25em] text-amber-400">ABRE ESTA MISMA URL Y ESCRIBE EL CÓDIGO</p>
    <button onclick="cerrarModal('modalVinculacion')" class="btn-glass py-3 text-[11px] tracking-[0.2em]">CERRAR</button>
  </div>
</div>

<!-- ================= OVERLAY DE BLOQUEO REMOTO (COMANDOS DE ENERGÍA) ================= -->
<div id="overlayBloqueoRemoto" class="hidden fixed inset-0 z-[10000] flex-col items-center justify-center text-center px-6"
     style="background:radial-gradient(circle at 50% 30%, rgba(244,63,94,.28), #000 70%)">
  <div class="text-7xl mb-6 txt-rose blink">🔒</div>
  <h1 class="font-display text-3xl md:text-5xl font-black txt-rose tracking-[0.1em] mb-3">SESIÓN BLOQUEADA</h1>
  <p class="text-xs tracking-[0.3em] text-rose-200/80 max-w-md">EL ADMINISTRADOR DE TU CASA CONGELÓ ESTE DISPOSITIVO A DISTANCIA</p>
</div>

<!-- ================= PANTALLA DE DESCONEXIÓN REMOTA ================= -->
<div id="overlayDesconexion" class="hidden fixed inset-0 z-[10000] flex-col items-center justify-center text-center px-6 bg-black">
  <p class="font-display text-2xl txt-rose tracking-[0.1em] mb-3">⏹ DESCONEXIÓN REMOTA</p>
  <p class="text-[11px] tracking-[0.25em] text-slate-500">TU CASA CERRÓ ESTA SESIÓN A DISTANCIA</p>
</div>

<!-- ================= MODAL NUEVO INTEGRANTE ================= -->
<div id="modalNuevoIntegrante" class="hidden fixed inset-0 bg-black/90 backdrop-blur-xl flex items-center justify-center p-4 z-50">
  <div class="glass-card p-7 w-full max-w-lg text-center flex flex-col gap-5">
    <h3 class="font-display text-lg font-black text-white tracking-[0.1em]">➕ AÑADIR INTEGRANTE</h3>
    <p class="text-[10px] tracking-[0.2em] text-slate-500">EL NUEVO PERFIL SERÁ USUARIO ESTÁNDAR</p>
    <input id="nuevoIntegranteNombre" type="text" placeholder="Nombre" class="cyber-input">
    <input id="nuevoIntegrantePin" type="password" placeholder="PIN (mín. 4 dígitos)" class="cyber-input text-center tracking-[0.4em]">
    <div>
      <p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2 text-left">AVATAR</p>
      <div id="pickerNuevoAvatar" class="grid grid-cols-6 gap-2 bg-slate-950 p-3 rounded-2xl border border-slate-800 max-h-40 overflow-y-auto"></div>
    </div>
    <div class="flex gap-3">
      <button onclick="cerrarModal('modalNuevoIntegrante')" class="btn-glass flex-1 py-3 text-[11px] tracking-[0.2em]">CANCELAR</button>
      <button onclick="crearIntegrante()" class="btn-glass flex-1 py-3 text-[11px] tracking-[0.2em] text-cyan-200">+ CREAR</button>
    </div>
  </div>
</div>

<!-- ================= MODAL EDITAR INTEGRANTE ================= -->
<div id="modalEditarIntegrante" class="hidden fixed inset-0 bg-black/90 backdrop-blur-xl flex items-center justify-center p-4 z-50">
  <div class="glass-card p-7 w-full max-w-lg text-center flex flex-col gap-5">
    <h3 class="font-display text-lg font-black text-white tracking-[0.1em]">✎ EDITAR INTEGRANTE</h3>
    <input id="editIntegranteNombre" type="text" placeholder="Nombre" class="cyber-input">
    <input id="editIntegrantePin" type="password" placeholder="Nuevo PIN (mín. 4)" class="cyber-input text-center tracking-[0.4em]">
    <div>
      <p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2 text-left">AVATAR</p>
      <div id="pickerEditAvatar" class="grid grid-cols-6 gap-2 bg-slate-950 p-3 rounded-2xl border border-slate-800 max-h-40 overflow-y-auto"></div>
    </div>
    <div class="flex gap-3">
      <button onclick="cerrarModal('modalEditarIntegrante')" class="btn-glass flex-1 py-3 text-[11px] tracking-[0.2em]">CANCELAR</button>
      <button onclick="guardarEditIntegrante()" class="btn-glass flex-1 py-3 text-[11px] tracking-[0.2em] text-cyan-200">GUARDAR</button>
    </div>
  </div>
</div>

<!-- ================= PAYWALL 402 ================= -->
<div id="modalSuscripcion" class="hidden fixed inset-0 bg-black/95 backdrop-blur-2xl flex items-center justify-center p-4 z-[9999]">
  <div class="glass-card p-10 max-w-2xl text-center flex flex-col items-center gap-6" style="border-color:rgba(251,113,133,.4)">
    <i class="text-6xl txt-rose blink">🔒</i>
    <div>
      <p class="text-[11px] tracking-[0.45em] text-rose-300/80 mb-2">⚠ ACCESO BLOQUEADO · HTTP 402</p>
      <h2 class="font-display text-4xl font-black tracking-[0.08em] txt-rose">SUSCRIPCIÓN VENCIDA</h2>
    </div>
    <div id="mensajeSuscripcion" class="hud-box p-5 text-rose-200 text-sm leading-relaxed max-w-lg"></div>
    <div class="grid grid-cols-3 gap-3 w-full max-w-lg">
      <div class="hud-box p-4 text-center"><p class="text-[9px] tracking-[0.3em] text-slate-500 mb-1">MÉTODO 1</p><p class="font-display txt-rose">YAPE</p></div>
      <div class="hud-box p-4 text-center"><p class="text-[9px] tracking-[0.3em] text-slate-500 mb-1">MÉTODO 2</p><p class="font-display txt-rose">PLIN</p></div>
      <div class="hud-box p-4 text-center"><p class="text-[9px] tracking-[0.3em] text-slate-500 mb-1">MÉTODO 3</p><p class="font-display txt-rose">TRANSFER.</p></div>
    </div>
    <button onclick="location.reload()" class="btn-glass px-8 py-4 text-[11px] tracking-[0.25em] text-rose-200">← VOLVER</button>
  </div>
</div>

<script>
/* =====================================================================
   ESTADO GLOBAL
   ===================================================================== */
const API = "";
let codigoCasaActual = null;
let casaNombreActual = "";
let pinActivo = "";
let usuarioActual = null;
let dispositivos = [];
let integrantes = [];
let idSeleccionado = null;
let mapaLeaflet = null;
let markerDispositivo = null;
let timerTelemetria = null;
let socketWS = null;
let avatarNuevoTemp = "👤";
let avatarEditTemp = "👤";
let editandoIntegranteId = null;
let miDeviceUid = null;
let miDispositivoId = null;
let ultimaLat = null;
let ultimaLng = null;

const AVATARES = ["👨‍💻","👩‍💻","🕵️‍♂️","🤖","👽","👑","🧙‍♂️","🚀","🛡️","🦊","🐱","👤","👵","👴","🎓","🦸","🐼","🐯"];

/* =====================================================================
   SPLASH → DASHBOARD + GEOLOCALIZACIÓN
   ===================================================================== */
function conversar() {
  const splash = document.getElementById("splashScreen");
  const app = document.getElementById("appShell");
  splash.classList.add("opacity-0", "pointer-events-none");
  app.classList.remove("opacity-0", "pointer-events-none");
  setTimeout(() => { splash.style.display = "none"; }, 800);

  inicializarMapa();
  initSocket();
  centrarMapaGeolocalizacion();
}

async function centrarMapaGeolocalizacion() {
  // 1º intento: HTML5 navigator.geolocation
  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(
      (pos) => centrarMapa(pos.coords.latitude, pos.coords.longitude, "Tu ubicación"),
      (err) => { console.warn("Geoloc HTML5 falló:", err.message); centrarPorIP(); },
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 60000 }
    );
  } else {
    centrarPorIP();
  }
}

async function centrarPorIP() {
  try {
    const r = await fetch("https://ipapi.co/json/");
    const d = await r.json();
    if (d.latitude && d.longitude) {
      centrarMapa(d.latitude, d.longitude, d.city || "Tu zona");
      return;
    }
  } catch (e) { console.warn("IP geoloc falló", e); }
  // Fallback: Lima, Perú
  centrarMapa(-12.0464, -77.0428, "Lima (fallback)");
}

function centrarMapa(lat, lng, etiqueta) {
  ultimaLat = lat; ultimaLng = lng;
  if (!mapaLeaflet) return;
  mapaLeaflet.setView([lat, lng], 15);
  const iconoNeon = L.divIcon({
    className: "pin-neon",
    html: '<div style="width:22px;height:22px;border-radius:50%;background:#22d3ee;border:3px solid #a5f3fc;box-shadow:0 0 22px 6px rgba(34,211,238,.85);"></div>',
    iconSize: [22, 22], iconAnchor: [11, 11]
  });
  if (markerDispositivo) {
    markerDispositivo.setLatLng([lat, lng]);
  } else {
    markerDispositivo = L.marker([lat, lng], { icon: iconoNeon }).addTo(mapaLeaflet);
  }
  markerDispositivo.bindPopup("<b>" + (etiqueta || "") + "</b>").openPopup();
}

/* =====================================================================
   WEBSOCKET DINÁMICO
   ===================================================================== */
function initSocket() {
  try {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + "/ws";
    socketWS = new WebSocket(url);
    socketWS.onopen = () => {
      // Reingeniería Agentless: esta misma pestaña se anuncia como nodo ante el
      // backend para poder recibir comandos de energía dirigidos en tiempo real.
      try {
        socketWS.send(JSON.stringify({ tipo: "registro", device_uid: obtenerDeviceUid() }));
      } catch (e) {}
    };
    socketWS.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.event === "LICENCIA_EXPIRADA") {
          mostrarPaywall(data.mensaje || "Tu período de prueba ha terminado.");
        }
        if (data.event === "INTEGRANTES_ACTUALIZADOS") {
          // Sincronización en caliente: en cuanto el Admin añade, edita o
          // elimina a un integrante desde sus modales, TODAS las pantallas
          // familiares conectadas (no solo la del Admin) ejecutan de
          // inmediato cargarDispositivos() + entrarAlBunker() para repintar
          // la cuadrícula neón de círculos sin esperar el timer ni dar lag.
          if (usuarioActual) {
            cargarDispositivos();
            entrarAlBunker();
          } else if (codigoCasaActual) {
            cargarPerfiles();
          }
        }
        if (data.event === "COMANDO_ENERGIA") {
          aplicarComandoRemoto(data.accion);
        }
      } catch (e) {}
    };
    socketWS.onclose = () => setTimeout(initSocket, 5000);
    socketWS.onerror = () => { try { socketWS.close(); } catch(e){} };
  } catch (e) { console.warn("WS no disponible", e); }
}

/* =====================================================================
   MAPA LEAFLET
   ===================================================================== */
function inicializarMapa() {
  if (mapaLeaflet) return;
  const el = document.getElementById("mapa");
  if (!el) return;
  mapaLeaflet = L.map("mapa", { zoomControl: true, attributionControl: false })
    .setView([-12.0464, -77.0428], 15);
  L.tileLayer("https://{s}.google.com/vt/lyrs=y&x={x}&y={y}&z={z}", {
    maxZoom: 20,
    subdomains: ["mt0", "mt1", "mt2", "mt3"],
    attribution: "",
  }).addTo(mapaLeaflet);
}

function actualizarMapa(lat, lng, nombre) {
  if (!mapaLeaflet || !lat || !lng) return;
  if (markerDispositivo) markerDispositivo.setLatLng([lat, lng]);
  else markerDispositivo = L.marker([lat, lng]).addTo(mapaLeaflet);
  markerDispositivo.bindPopup("<b>" + nombre + "</b>");
  mapaLeaflet.setView([lat, lng], 15);
}

/* =====================================================================
   UTILIDADES
   ===================================================================== */
function mostrar(id) {
  ["faseAcceso","fasePerfiles","faseDashboard"].forEach(x => {
    const el = document.getElementById(x);
    if (el) el.classList.add("hidden");
  });
  const el = document.getElementById(id);
  if (el) el.classList.remove("hidden");
}
function mostrarModal(id) { document.getElementById(id).classList.remove("hidden"); }
function cerrarModal(id) { document.getElementById(id).classList.add("hidden"); }

function mostrarPaywall(msg) {
  document.getElementById("mensajeSuscripcion").innerText = msg;
  document.getElementById("modalSuscripcion").classList.remove("hidden");
  if (timerTelemetria) { clearInterval(timerTelemetria); timerTelemetria = null; }
}

function headersAuth() {
  const h = { "X-Pin": pinActivo, "X-Device-Uid": obtenerDeviceUid() };
  if (codigoCasaActual) h["X-Casa"] = codigoCasaActual;
  return h;
}

async function fetchAuth(url, opts = {}) {
  const o = Object.assign({}, opts);
  o.headers = Object.assign({}, headersAuth(), o.headers || {});
  const res = await fetch(url, o);
  if (res.status === 402) {
    let msg = "Tu período de prueba ha terminado.";
    try { const d = await res.json(); msg = d.detail || msg; } catch(e){}
    mostrarPaywall(msg);
    throw new Error("SUSCRIPCION");
  }
  return res;
}

/* =====================================================================
   IDENTIDAD DEL NODO (AGENTLESS) — sin instalación, sin script descargable.
   El propio navegador genera y conserva un identificador persistente y se
   anuncia ante la casa con el ÚNICO código de invitación de 6 caracteres.
   ===================================================================== */
function obtenerDeviceUid() {
  if (miDeviceUid) return miDeviceUid;
  try {
    miDeviceUid = localStorage.getItem("aegis_device_uid");
  } catch (e) { miDeviceUid = null; }
  if (!miDeviceUid) {
    miDeviceUid = (crypto && crypto.randomUUID)
      ? crypto.randomUUID()
      : ("dev-" + Date.now() + "-" + Math.random().toString(16).slice(2));
    try { localStorage.setItem("aegis_device_uid", miDeviceUid); } catch (e) {}
  }
  return miDeviceUid;
}

function detectarTipoDispositivo() {
  const ua = navigator.userAgent || "";
  if (/SmartTV|Tizen|WebOS|HbbTV|GoogleTV|AppleTV/i.test(ua)) return "smart_tv";
  if (/iPad|Tablet(?!.*Mobile)/i.test(ua)) return "tablet";
  if (/Mobi|Android.*Mobile|iPhone/i.test(ua)) return "smartphone";
  return "pc";
}

function nombreNodoSugerido() {
  const etiquetas = { pc: "PC", smartphone: "Smartphone", tablet: "Tablet", smart_tv: "Smart TV" };
  const tipo = detectarTipoDispositivo();
  const quien = (usuarioActual && usuarioActual.nombre) ? usuarioActual.nombre : "Integrante";
  return (etiquetas[tipo] || "Dispositivo") + " de " + quien;
}

/* =====================================================================
   ALTA DEL NODO + TELEMETRÍA 100% WEB APIS NATIVAS (sin agente/root)
   ===================================================================== */
async function registrarNodo() {
  try {
    const res = await fetchAuth(API + "/dispositivos/registrar-nodo", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device_uid: obtenerDeviceUid(),
        nombre: nombreNodoSugerido(),
        tipo: detectarTipoDispositivo(),
      }),
    });
    if (res.ok) {
      const data = await res.json();
      miDispositivoId = data.dispositivo_id;
    }
  } catch (e) {}
}

async function obtenerBateriaTexto() {
  if (!navigator.getBattery) return null;
  try {
    const bat = await navigator.getBattery();
    const pct = Math.round((bat.level || 0) * 100);
    const estado = bat.charging ? "⚡ Cargando" : "🔋 Batería";
    return pct + "% (" + estado + ")";
  } catch (e) { return null; }
}

async function enviarTelemetriaWeb() {
  if (!usuarioActual) return;
  const hilos = navigator.hardwareConcurrency || null;
  const payload = {
    device_uid: obtenerDeviceUid(),
    cpu: hilos ? (hilos + " núcleos lógicos") : "N/D",
    ram: navigator.deviceMemory ? (navigator.deviceMemory + " GB (aprox.)") : "N/D",
    bateria: await obtenerBateriaTexto(),
    ventana_activa: document.title || "Sin título",
    hilos: hilos,
  };
  if (ultimaLat != null && ultimaLng != null) {
    payload.lat = ultimaLat; payload.lng = ultimaLng;
    payload.ubicacion = "Ubicación detectada";
  }
  try {
    await fetchAuth(API + "/dispositivos/telemetria-web", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) {}
}

function mostrarCodigoCasa() {
  document.getElementById("codigoVincBox").innerText = codigoCasaActual || "------";
  mostrarModal("modalVinculacion");
}

/* =====================================================================
   COMANDOS REMOTOS EN LA PESTAÑA DEL INTEGRANTE (Fullscreen + Keyboard Lock)
   Sin agente de sistema operativo con permisos root: las directivas de
   energía actúan sobre la propia pestaña del navegador vía WebSocket.
   ===================================================================== */
function aplicarComandoRemoto(accion) {
  if (accion === "BLOQUEAR") mostrarOverlayBloqueo();
  else if (accion === "DESBLOQUEAR") quitarOverlayBloqueo();
  else if (accion === "APAGAR") ejecutarApagadoRemoto();
  else if (accion === "REINICIAR") location.reload();
}

async function mostrarOverlayBloqueo() {
  const ov = document.getElementById("overlayBloqueoRemoto");
  if (!ov) return;
  ov.classList.remove("hidden");
  ov.classList.add("flex");
  document.body.style.overflow = "hidden";
  try { if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen(); } catch (e) {}
  try { if (navigator.keyboard && navigator.keyboard.lock) await navigator.keyboard.lock(); } catch (e) {}
}

function quitarOverlayBloqueo() {
  const ov = document.getElementById("overlayBloqueoRemoto");
  if (ov) { ov.classList.add("hidden"); ov.classList.remove("flex"); }
  document.body.style.overflow = "";
  try { if (navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock(); } catch (e) {}
  try { if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen(); } catch (e) {}
}

function ejecutarApagadoRemoto() {
  if (timerTelemetria) { clearInterval(timerTelemetria); timerTelemetria = null; }
  try { if (socketWS) { socketWS.onclose = null; socketWS.close(); } } catch (e) {}
  try { window.close(); } catch (e) {}
  setTimeout(() => {
    const ov = document.getElementById("overlayDesconexion");
    if (ov) { ov.classList.remove("hidden"); ov.classList.add("flex"); }
  }, 300);
}

/* =====================================================================
   FASE ACCESO
   ===================================================================== */
function mostrarCrear() {
  document.getElementById("crearBox").classList.toggle("hidden");
}

async function crearCasa() {
  const nombre_casa = document.getElementById("nuevaCasaNombre").value.trim();
  const nombre_admin = document.getElementById("nuevaCasaAdminNombre").value.trim();
  const pin_admin = document.getElementById("nuevaCasaPin").value.trim();
  if (!nombre_casa || !nombre_admin || pin_admin.length < 4) {
    alert("Completa todos los campos (PIN mín. 4 dígitos)"); return;
  }
  try {
    const res = await fetch(API + "/casas/crear", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nombre_casa, nombre_admin, pin_admin })
    });
    if (!res.ok) { alert("No se pudo crear la casa"); return; }
    const data = await res.json();
    alert("¡Casa creada! Código: " + data.codigo_invitacion + "\nGuárdalo y compártelo.");
    document.getElementById("inputCodigoCasa").value = data.codigo_invitacion;
    document.getElementById("crearBox").classList.add("hidden");
  } catch (e) { alert("Error de conexión"); }
}

async function entrarACasa() {
  const codigo = document.getElementById("inputCodigoCasa").value.trim().toUpperCase();
  if (!codigo) { alert("Ingresa un código"); return; }
  try {
    const res = await fetch(API + "/casas/" + encodeURIComponent(codigo));
    if (!res.ok) { alert("Código inválido"); return; }
    const casa = await res.json();
    codigoCasaActual = casa.codigo_invitacion;
    casaNombreActual = casa.nombre;
    document.getElementById("nombreCasaActual").innerText = casa.nombre;
    document.getElementById("casaBadge").innerText = "CASA: " + casa.codigo_invitacion;
    mostrar("fasePerfiles");
    await cargarPerfiles();
  } catch (e) { alert("Error de conexión"); }
}

function volverInicio() {
  codigoCasaActual = null; pinActivo = ""; usuarioActual = null;
  mostrar("faseAcceso");
}

/* =====================================================================
   FASE PERFILES
   ===================================================================== */
async function cargarPerfiles() {
  try {
    const res = await fetch(API + "/casas/" + encodeURIComponent(codigoCasaActual) + "/usuarios");
    if (!res.ok) { alert("No se pudieron cargar los perfiles"); return; }
    const users = await res.json();
    const cont = document.getElementById("gridPerfiles");
    cont.innerHTML = "";
    users.forEach(u => {
      const btn = document.createElement("button");
      btn.className = "flex flex-col items-center gap-3 p-4 rounded-3xl bg-slate-950/60 border border-cyan-500/25 hover:border-cyan-400 transition";
      btn.innerHTML =
        '<span class="text-6xl">' + (u.avatar || "👤") + "</span>" +
        '<span class="font-display text-sm font-bold text-white tracking-[0.08em]">' + u.nombre + "</span>" +
        '<span class="rol-badge rol-' + u.rol + '">' + u.rol + "</span>";
      btn.onclick = () => abrirPin(u);
      cont.appendChild(btn);
    });
  } catch (e) { alert("Error al cargar perfiles"); }
}

function abrirPin(usuario) {
  document.getElementById("pinAvatar").innerText = usuario.avatar || "👤";
  document.getElementById("pinNombre").innerText = usuario.nombre;
  document.getElementById("pinInput").value = "";
  document.getElementById("pinInput").dataset.userId = usuario.id;
  mostrarModal("modalPin");
  setTimeout(() => document.getElementById("pinInput").focus(), 200);
}

async function validarPin() {
  const pin = document.getElementById("pinInput").value.trim();
  if (!pin) { alert("Ingresa tu PIN"); return; }
  pinActivo = pin;
  try {
    const res = await fetch(API + "/perfil/me", { headers: headersAuth() });
    if (res.status === 402) {
      let msg = "Suscripción vencida.";
      try { const d = await res.json(); msg = d.detail || msg; } catch(e){}
      mostrarPaywall(msg); return;
    }
    if (!res.ok) { alert("PIN incorrecto"); pinActivo = ""; return; }
    usuarioActual = await res.json();
    cerrarModal("modalPin");
    entrarAlDashboard();
  } catch (e) { alert("Error de autenticación"); pinActivo = ""; }
}

/* =====================================================================
   DASHBOARD
   ===================================================================== */
function entrarAlDashboard() {
  document.getElementById("userAvatar").innerText = usuarioActual.avatar || "👤";
  document.getElementById("userNombre").innerText = usuarioActual.nombre;
  const rolEl = document.getElementById("userRol");
  rolEl.innerText = usuarioActual.rol;
  rolEl.className = "rol-badge rol-" + usuarioActual.rol + " mt-1.5";

  const btnVinc = document.getElementById("btnVincular");
  const btnAdd  = document.getElementById("btnAddIntegrante");
  if (usuarioActual.rol === "ADMIN_CASA") {
    btnVinc.classList.remove("hidden");
    btnAdd.classList.remove("hidden");
  } else {
    btnVinc.classList.add("hidden");
    btnAdd.classList.add("hidden");
  }

  document.getElementById("avisoGestor").classList.toggle("hidden", usuarioActual.rol !== "GESTOR_CASA");

  mostrar("faseDashboard");

  // Reingeniería Agentless: esta pestaña se registra como nodo controlado de
  // la casa y empieza a reportar su propia telemetría con Web APIs nativas.
  registrarNodo().then(() => { cargarDispositivos(); enviarTelemetriaWeb(); });
  cargarIntegrantes();
  if (timerTelemetria) clearInterval(timerTelemetria);
  timerTelemetria = setInterval(() => {
    cargarDispositivos(); cargarIntegrantes(); enviarTelemetriaWeb();
  }, 12000);
}

async function cargarDispositivos() {
  try {
    const res = await fetchAuth(API + "/dispositivos");
    if (!res.ok) return;
    dispositivos = await res.json();
    pintarDispositivos();
    if (idSeleccionado) {
      const d = dispositivos.find(x => x.id === idSeleccionado);
      if (d) refrescarTelemetriaUI(d);
    }
  } catch (e) {}
}

const ICONOS = { pc: "💻", smartphone: "📱", tablet: "📟", smart_tv: "📺", enchufe: "🔌" };

function pintarDispositivos() {
  const cont = document.getElementById("listaDispositivos");
  cont.innerHTML = "";
  if (!dispositivos.length) {
    cont.innerHTML = '<p class="text-xs text-slate-500 text-center py-6">Sin dispositivos. Vincula uno.</p>';
    return;
  }
  dispositivos.forEach(d => {
    const el = document.createElement("button");
    const activo = idSeleccionado === d.id;
    el.className = "w-full text-left hud-box p-4 transition " + (activo ? "border-cyan-400" : "hover:border-cyan-500/60");
    el.innerHTML =
      '<div class="flex items-center gap-3">' +
        '<span class="text-2xl">' + (ICONOS[d.tipo] || "🖥") + "</span>" +
        '<div class="flex-1 min-w-0">' +
          '<p class="font-display text-sm font-bold text-white truncate">' + d.nombre + "</p>" +
          '<p class="text-[10px] tracking-[0.15em] text-slate-500 truncate">' + d.ip + " · " + (d.estado || "OFFLINE") + "</p>" +
        "</div>" +
        (d.es_local ? '<span class="rol-badge rol-ADMIN_CASA">LOCAL</span>' : "") +
      "</div>";
    el.onclick = () => seleccionarDispositivo(d);
    cont.appendChild(el);
  });
}

function refrescarTelemetriaUI(d) {
  const s = d.specs || {};
  document.getElementById("mCpu").innerText   = s.cpu     || "--";
  document.getElementById("mRam").innerText   = s.ram     || "--";
  document.getElementById("mDisco").innerText = s.disco   || "--";
  document.getElementById("mBat").innerText   = s.bateria || "--";
  document.getElementById("mProc").innerText  = (s.procesos != null ? s.procesos : "--");
}

function seleccionarDispositivo(d) {
  idSeleccionado = d.id;
  document.getElementById("targetNombre").innerText = d.nombre.toUpperCase();
  refrescarTelemetriaUI(d);
  if (d.lat && d.lng) actualizarMapa(d.lat, d.lng, d.nombre);

  // --- REGLA: Deshabilitar comandos en estación local ---
  const panelComandos = document.getElementById("panelComandos");
  const avisoLocal = document.getElementById("avisoLocal");
  if (d.es_local) {
    panelComandos.style.opacity = "0.25";
    panelComandos.style.pointerEvents = "none";
    avisoLocal.classList.remove("hidden");
  } else {
    avisoLocal.classList.add("hidden");
    if (usuarioActual && usuarioActual.rol === "ADMIN_CASA") {
      panelComandos.style.opacity = "1";
      panelComandos.style.pointerEvents = "auto";
    } else {
      panelComandos.style.opacity = "0.35";
      panelComandos.style.pointerEvents = "none";
    }
  }
  pintarDispositivos();
}

/* =====================================================================
   COMANDOS DE ENERGÍA
   ===================================================================== */
async function ejecutarComando(accion) {
  if (!idSeleccionado) { alert("Selecciona un dispositivo"); return; }
  if (!usuarioActual || usuarioActual.rol !== "ADMIN_CASA") {
    alert("Tu rol no permite enviar comandos de energía (HTTP 403)"); return;
  }
  const d = dispositivos.find(x => x.id === idSeleccionado);
  if (d && d.es_local) {
    alert("⛔ Estación local maestra — directivas de energía restringidas."); return;
  }
  try {
    const res = await fetchAuth(API + "/dispositivos/" + idSeleccionado + "/energia", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accion })
    });
    if (res.status === 403) {
      const dd = await res.json().catch(() => ({}));
      alert("⛔ " + (dd.detail || "Permiso denegado")); return;
    }
    if (!res.ok) { alert("No se pudo enviar el comando"); return; }
    const data = await res.json();
    alert("✔ " + (data.mensaje || "Comando enviado"));
  } catch (e) {}
}

/* =====================================================================
   CRUD DE INTEGRANTES (círculos neón)
   ===================================================================== */
async function cargarIntegrantes() {
  try {
    const res = await fetch(API + "/casas/" + encodeURIComponent(codigoCasaActual) + "/usuarios");
    if (!res.ok) return;
    integrantes = await res.json();
    renderPerfilesCircles(integrantes);
  } catch (e) {}
}

// Alias semántico: "vuelve a entrar al búnker" = refresca la lista de
// integrantes desde el backend y repinta al instante la cuadrícula neón de
// círculos (renderPerfilesCircles). Se usa tanto tras crear/editar/eliminar
// un perfil localmente como al recibir INTEGRANTES_ACTUALIZADOS por WS.
async function entrarAlBunker() {
  await cargarIntegrantes();
}

/* CRUD dinámico de integrantes: cuadrícula de círculos neón. Cuando el rol
   logueado es ADMIN_CASA, cada círculo renderiza dos botones miniatura
   tácticos flotantes (EDITAR/ELIMINAR) sobre el propio avatar. */
function renderPerfilesCircles(miembros) {
  miembros = miembros || integrantes;
  const cont = document.getElementById("gridIntegrantes");
  cont.innerHTML = "";
  if (!miembros.length) {
    cont.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">Aún no hay integrantes.</p>';
    return;
  }
  const esAdmin = usuarioActual && usuarioActual.rol === "ADMIN_CASA";

  miembros.forEach(m => {
    const wrap = document.createElement("div");
    wrap.className = "flex flex-col items-center";
    wrap.style.position = "relative";

    const circle = document.createElement("div");
    circle.className = "avatar-circle";
    circle.innerHTML = '<span>' + (m.avatar || "👤") + '</span>';

    if (esAdmin && m.rol !== "MASTER_ADMIN" && (!usuarioActual || m.id !== usuarioActual.id)) {
      const btnEdit = document.createElement("button");
      btnEdit.className = "avatar-mini avatar-mini-edit";
      btnEdit.title = "Editar";
      btnEdit.innerText = "✎";
      btnEdit.onclick = (ev) => { ev.stopPropagation(); abrirModalEditarIntegrante(m); };
      circle.appendChild(btnEdit);

      const btnDel = document.createElement("button");
      btnDel.className = "avatar-mini avatar-mini-del";
      btnDel.title = "Eliminar";
      btnDel.innerText = "✕";
      btnDel.onclick = (ev) => { ev.stopPropagation(); eliminarIntegrante(m.id); };
      circle.appendChild(btnDel);
    }

    const name = document.createElement("p");
    name.className = "avatar-name";
    name.innerText = m.nombre;

    const badge = document.createElement("span");
    badge.className = "rol-badge rol-" + m.rol + " mt-1";
    badge.innerText = m.rol;

    wrap.appendChild(circle);
    wrap.appendChild(name);
    wrap.appendChild(badge);
    cont.appendChild(wrap);
  });
}

function construirPicker(containerId, seleccionar) {
  const cont = document.getElementById(containerId);
  cont.innerHTML = "";
  AVATARES.forEach(av => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "text-2xl p-2 rounded-lg border border-transparent hover:border-cyan-500/60 hover:bg-cyan-500/10 transition";
    b.innerText = av;
    b.onclick = () => seleccionar(av);
    cont.appendChild(b);
  });
}

function abrirModalNuevoIntegrante() {
  document.getElementById("nuevoIntegranteNombre").value = "";
  document.getElementById("nuevoIntegrantePin").value = "";
  avatarNuevoTemp = "👤";
  construirPicker("pickerNuevoAvatar", (av) => { avatarNuevoTemp = av; });
  mostrarModal("modalNuevoIntegrante");
}

async function crearIntegrante() {
  const nombre = document.getElementById("nuevoIntegranteNombre").value.trim();
  const pin = document.getElementById("nuevoIntegrantePin").value.trim();
  if (!nombre || pin.length < 4) { alert("Nombre y PIN (mín. 4 dígitos) requeridos"); return; }
  try {
    // Requiere PIN de ADMIN_CASA (ver fix de seguridad en el backend): antes
    // esta llamada solo necesitaba el código de casa, que se comparte
    // libremente, y cualquiera podía crear cuentas sin ser Admin.
    const res = await fetchAuth(API + "/casas/" + encodeURIComponent(codigoCasaActual) + "/usuarios/crear", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nombre, pin, avatar: avatarNuevoTemp })
    });
    if (!res.ok) { alert("No se pudo crear el integrante"); return; }
    cerrarModal("modalNuevoIntegrante");
    await cargarIntegrantes();
  } catch (e) { alert("Error de conexión"); }
}

function abrirModalEditarIntegrante(u) {
  editandoIntegranteId = u.id;
  document.getElementById("editIntegranteNombre").value = u.nombre;
  document.getElementById("editIntegrantePin").value = "";
  avatarEditTemp = u.avatar || "👤";
  construirPicker("pickerEditAvatar", (av) => { avatarEditTemp = av; });
  mostrarModal("modalEditarIntegrante");
}

async function guardarEditIntegrante() {
  const nombre = document.getElementById("editIntegranteNombre").value.trim();
  const pin = document.getElementById("editIntegrantePin").value.trim();
  if (!nombre || pin.length < 4) { alert("Nombre y PIN (mín. 4 dígitos) requeridos"); return; }
  try {
    const res = await fetchAuth(API + "/usuarios/" + editandoIntegranteId, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nombre, pin, avatar: avatarEditTemp })
    });
    if (!res.ok) { alert("No se pudo actualizar"); return; }
    cerrarModal("modalEditarIntegrante");
    await cargarIntegrantes();
  } catch (e) {}
}

async function eliminarIntegrante(uid) {
  const m = integrantes.find(x => x.id === uid);
  if (!confirm("¿Eliminar a " + (m ? m.nombre : "este integrante") + "?")) return;
  try {
    const res = await fetchAuth(API + "/usuarios/" + uid, { method: "DELETE" });
    if (!res.ok) { alert("No se pudo eliminar"); return; }
    await cargarIntegrantes();
  } catch (e) {}
}

/* =====================================================================
   CERRAR SESIÓN
   ===================================================================== */
function cerrarSesion() {
  if (timerTelemetria) { clearInterval(timerTelemetria); timerTelemetria = null; }
  if (socketWS) { try { socketWS.onclose = null; socketWS.close(); } catch(e){} }

  // Borrado físico de la cookie de sesión (de raíz, no solo variables en memoria)
  document.cookie = "aegis_session=; expires=Thu, 01 Jan 1970 00:00:00 UTC; path=/;";

  pinActivo = ""; usuarioActual = null; idSeleccionado = null;
  codigoCasaActual = null; casaNombreActual = "";

  location.reload();
}

/* =====================================================================
   LISTENERS
   ===================================================================== */
document.addEventListener("DOMContentLoaded", () => {
  const inp = document.getElementById("pinInput");
  if (inp) inp.addEventListener("keydown", e => { if (e.key === "Enter") validarPin(); });
  const inp2 = document.getElementById("inputCodigoCasa");
  if (inp2) inp2.addEventListener("keydown", e => { if (e.key === "Enter") entrarACasa(); });
});

// Enfoque Hunter: cuando la pestaña vuelve a primer plano o cambia el título,
// se reporta la nueva ventana activa casi al instante (sin esperar el timer).
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && usuarioActual) enviarTelemetriaWeb();
});
window.addEventListener("focus", () => { if (usuarioActual) enviarTelemetriaWeb(); });
</script>
</body>
</html>
"""


# ===========================================================================
# 8 · (REINGENIERÍA AGENTLESS) — Ya no existe agente local descargable.
# La sección 8 original generaba y ofrecía para descarga un script Python
# ('AGENTE_TEMPLATE') que el usuario debía instalar y ejecutar en su equipo.
# Esto se elimina por completo: ningún integrante instala nada. El propio
# navegador, con Web APIs nativas, es el único "nodo" (ver secciones 18-20).
# ===========================================================================


# ===========================================================================
# 9 · WEBSOCKET MANAGER
# ===========================================================================
class GestorWebSockets:
    """
    Arquitectura Agentless: cada pestaña de navegador que entra con el código de
    casa se anuncia sobre este mismo WebSocket con su device_uid. Esto permite
    empujar comandos dirigidos (bloquear/apagar/reiniciar) a UN dispositivo
    concreto, sin necesidad de un agente de sistema operativo instalado.
    """
    def __init__(self):
        self.conexiones: List[WebSocket] = []
        self.por_dispositivo: dict = {}  # device_uid -> WebSocket

    async def conectar(self, ws: WebSocket):
        await ws.accept()
        self.conexiones.append(ws)

    def desconectar(self, ws: WebSocket):
        if ws in self.conexiones:
            self.conexiones.remove(ws)
        for uid, sock in list(self.por_dispositivo.items()):
            if sock is ws:
                self.por_dispositivo.pop(uid, None)

    def registrar_dispositivo(self, device_uid: str, ws: WebSocket):
        if device_uid:
            self.por_dispositivo[device_uid] = ws

    def esta_conectado(self, device_uid: str) -> bool:
        return bool(device_uid) and device_uid in self.por_dispositivo

    async def enviar_a_dispositivo(self, device_uid: str, payload: dict) -> bool:
        ws = self.por_dispositivo.get(device_uid)
        if not ws:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.desconectar(ws)
            return False

    async def broadcast(self, payload: dict):
        muertos = []
        for ws in list(self.conexiones):
            try:
                await ws.send_json(payload)
            except Exception:
                muertos.append(ws)
        for ws in muertos:
            self.desconectar(ws)


gestor_ws = GestorWebSockets()


# ===========================================================================
# 10 · APP FASTAPI
# ===========================================================================
@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("AEGIS FAMILY OS v12.1 · MODO_COMERCIAL=%s", MODO_COMERCIAL)
    init_db()
    log.info("Esquema listo. Servidor escuchando.")
    yield
    log.info("AEGIS detenido.")


app = FastAPI(title="Aegis Family OS — Servidor Universal", version="12.1", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=CORS_CREDENCIALES,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ===========================================================================
# 11 · AUTENTICACIÓN
# ===========================================================================
def buscar_casa_por_codigo(codigo: str) -> Optional[dict]:
    with db() as conexion:
        conexion.ejecutar("SELECT * FROM casas WHERE codigo_invitacion = ?", (codigo,))
        return conexion.uno()


def buscar_casa_por_id(casa_id: int) -> Optional[dict]:
    with db() as conexion:
        conexion.ejecutar("SELECT * FROM casas WHERE id = ?", (casa_id,))
        return conexion.uno()


def buscar_usuario_por_pin(pin: str, casa_id: Optional[int] = None) -> Optional[dict]:
    with db() as conexion:
        if casa_id is not None:
            conexion.ejecutar("SELECT * FROM usuarios WHERE casa_id = ?", (casa_id,))
        else:
            conexion.ejecutar("SELECT * FROM usuarios")
        candidatos = conexion.todos()
    for u in candidatos:
        if pin_coincide(pin, u["salt"], u["pin_hash"]):
            return u
    return None


def _dias_desde(iso_fecha: Optional[str]) -> Optional[int]:
    if not iso_fecha: return None
    try:
        creado = datetime.fromisoformat(iso_fecha)
    except (ValueError, TypeError):
        return None
    return (datetime.utcnow() - creado).days


async def _verificar_suscripcion(casa: Optional[dict], usuario: dict):
    if not MODO_COMERCIAL: return
    if usuario["rol"] == ROL_MASTER: return
    if not casa or casa.get("creado_en") is None: return

    # 🔐 BYPASS VIP SILENCIOSO — validación por hash en RAM
    if _es_codigo_vip(casa.get("codigo_invitacion") or ""):
        return

    dias = _dias_desde(casa["creado_en"])
    if dias is not None and dias > DIAS_SUSCRIPCION:
        mensaje = (
            f"Suscripción vencida hace {dias - DIAS_SUSCRIPCION} día(s). "
            "Renueva con Yape / Plin para reactivar el panel."
        )
        try:
            await gestor_ws.broadcast({"event": "LICENCIA_EXPIRADA", "mensaje": mensaje})
        except Exception:
            pass
        raise HTTPException(status_code=402, detail=mensaje)


async def verificar_pin(x_pin: str = Header(None), x_casa: str = Header(None)) -> dict:
    if not x_pin:
        raise HTTPException(status_code=401, detail="Falta el PIN de autorización")

    usuario: Optional[dict] = None
    casa: Optional[dict] = None

    # 🐛 FIX #5 (seguridad/lógica): cuando el cliente ya envía X-Casa, la búsqueda del
    # PIN queda ESTRICTAMENTE acotada a esa casa. Antes, si el PIN no coincidía dentro
    # de la casa indicada, el código caía a una búsqueda global del PIN en TODAS las
    # casas del servidor — un escaneo cruzado de credenciales innecesario (ruido y
    # posible fuga por tiempo de respuesta) que además nunca cambiaba el resultado
    # final, porque el PIN encontrado en otra casa igual era rechazado más abajo.
    if x_casa:
        casa = buscar_casa_por_codigo(x_casa)
        if casa:
            usuario = buscar_usuario_por_pin(x_pin, casa_id=casa["id"])
    else:
        # Sin X-Casa solo tiene sentido el PIN del Master (validado globalmente).
        usuario = buscar_usuario_por_pin(x_pin)

    if not usuario:
        raise HTTPException(status_code=403, detail="PIN no válido")

    if usuario["rol"] != ROL_MASTER:
        if not x_casa:
            raise HTTPException(status_code=400, detail="Falta el código de casa (X-Casa)")
        if not casa or casa["id"] != usuario["casa_id"]:
            raise HTTPException(status_code=403, detail="Este PIN no pertenece a esta casa")
        await _verificar_suscripcion(casa, usuario)

    return usuario


def requerir_rol(*roles_permitidos):
    async def dep(usuario: dict = Depends(verificar_pin)):
        if usuario["rol"] not in roles_permitidos:
            raise HTTPException(status_code=403, detail="Tu rol no tiene permiso para esta acción")
        return usuario
    return dep


# 🐛 FIX #2 (lógica muerta/contradictoria): la ruta /dispositivos/{id}/energia ya
# rechaza con 403 a todo usuario con rol USUARIO antes de llegar aquí ("Tu rol
# (Hijo/Hija) NO puede ejecutar comandos de energía"), así que la rama que permitía
# a un USUARIO controlar el dispositivo que tuviera asignado nunca podía ejecutarse
# y contradecía el mensaje de error mostrado al Hijo/Hija. Se deja una única regla
# consistente: solo ADMIN_CASA controla energía.
def puede_controlar_energia(usuario: dict, dispositivo: dict) -> bool:
    return usuario["rol"] == ROL_ADMIN_CASA


def obtener_dispositivo_de_la_casa(dev_id: int, usuario: dict) -> dict:
    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM dispositivos WHERE id = ? AND casa_id = ?",
            (dev_id, usuario["casa_id"]),
        )
        fila = conexion.uno()
    if not fila:
        raise HTTPException(status_code=404, detail="Dispositivo no encontrado en tu casa")
    return fila


# 🐛 FIX #1 (crítico, lógico): "es local" se decidía antes por la IP del dispositivo
# (127.0.0.1/localhost). Eso etiquetaba como "estación local maestra" a CUALQUIER
# fila cuya IP coincidiera, y los comandos de energía sobre ella se ejecutaban con
# subprocess/shutdown directamente en el proceso del SERVIDOR (ver el antiguo
# ejecutar_comando_local) — es decir, el botón "APAGAR" del panel podía apagar la
# máquina que aloja el propio backend, no la laptop/celular del integrante. En el
# modelo agentless, "local" pasa a significar "esta pestaña de navegador es la que
# ejecuta la acción sobre sí misma", identificada por su device_uid propio (columna
# 'token'), nunca por IP ni por ejecución de comandos del sistema operativo.
def es_nodo_propio(dispositivo: dict, device_uid_actual: Optional[str]) -> bool:
    token = (dispositivo.get("token") or "").strip()
    return bool(token) and bool(device_uid_actual) and token == device_uid_actual


def telemetria_del_servidor_local() -> dict:
    try: cpu_val = f"{psutil.cpu_percent(interval=None)}%"
    except Exception: cpu_val = "N/D"
    try: ram_val = f"{psutil.virtual_memory().percent}%"
    except Exception: ram_val = "N/D"
    try: disco_val = f"{psutil.disk_usage(os.path.abspath(os.sep)).percent}%"
    except Exception: disco_val = "N/D"
    try:
        bateria = psutil.sensors_battery()
        bat_val = f"{int(bateria.percent)}%" if bateria else "AC Directo"
    except Exception:
        bat_val = "AC Directo"
    return {"cpu": cpu_val, "ram": ram_val, "disco": disco_val, "bateria": bat_val, "procesos": len(psutil.pids())}


def enviar_wol(mac: str):
    mac_limpia = mac.replace(":", "").replace("-", "").strip()
    if not mac_es_valida(mac_limpia):
        raise ValueError("La dirección MAC no es válida")
    paquete = bytes.fromhex("FF" * 6 + mac_limpia * 16)
    destinos = [("255.255.255.255", 9), ("192.168.1.255", 9), ("192.168.0.255", 9)]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for destino in destinos:
            try: s.sendto(paquete, destino)
            except Exception as e: log.debug("WOL a %s falló: %s", destino, e)


def encolar_comando(dispositivo_id: int, accion: str):
    with db() as conexion:
        conexion.ejecutar(
            "INSERT INTO comandos (dispositivo_id, accion, estado, creado_en) VALUES (?, ?, 'PENDIENTE', ?)",
            (dispositivo_id, accion, datetime.utcnow().isoformat()),
        )
        conexion.commit()


# ===========================================================================
# 12 · RUTAS PÚBLICAS
# ===========================================================================
@app.get("/health")
def health():
    try:
        with db() as conexion:
            conexion.ejecutar("SELECT 1"); conexion.uno()
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded", "version": "12.1",
        "db": "up" if db_ok else "down",
        "motor": "postgres" if DATABASE_URL else "sqlite",
        "modo_comercial": MODO_COMERCIAL, "dias_suscripcion": DIAS_SUSCRIPCION,
    }


@app.get("/", response_class=HTMLResponse)
def cargar_interfaz():
    return HTMLResponse(HTML_DASHBOARD)


# ===========================================================================
# 13 · CASAS
# ===========================================================================
@app.post("/casas/crear")
def crear_casa(data: NuevaCasaSchema):
    if len(data.pin_admin) < 4:
        raise HTTPException(status_code=400, detail="El PIN debe tener al menos 4 dígitos")
    with db() as conexion:
        codigo = generar_codigo_casa()
        conexion.ejecutar("SELECT 1 FROM casas WHERE codigo_invitacion = ?", (codigo,))
        while conexion.uno():
            codigo = generar_codigo_casa()
            conexion.ejecutar("SELECT 1 FROM casas WHERE codigo_invitacion = ?", (codigo,))
        conexion.ejecutar(
            "INSERT INTO casas (nombre, codigo_invitacion, creado_en) VALUES (?, ?, ?)",
            (data.nombre_casa, codigo, datetime.utcnow().isoformat()),
            es_insert=True,
        )
        casa_id = conexion.id_insertado()
        salt = generar_salt()
        conexion.ejecutar(
            "INSERT INTO usuarios (casa_id, nombre, pin_hash, salt, rol, avatar) VALUES (?, ?, ?, ?, ?, ?)",
            (casa_id, data.nombre_admin, hash_pin(data.pin_admin, salt), salt, ROL_ADMIN_CASA, data.avatar_admin),
        )
        conexion.commit()
    return {"mensaje": "Casa creada", "codigo_invitacion": codigo}


@app.get("/casas/{codigo}")
def obtener_casa(codigo: str):
    casa = buscar_casa_por_codigo(codigo)
    if not casa:
        raise HTTPException(status_code=404, detail="No existe ninguna casa con ese código")
    return {"id": casa["id"], "nombre": casa["nombre"], "codigo_invitacion": casa["codigo_invitacion"]}


@app.get("/casas/{codigo}/usuarios")
def usuarios_de_casa(codigo: str):
    casa = buscar_casa_por_codigo(codigo)
    if not casa:
        raise HTTPException(status_code=404, detail="No existe ninguna casa con ese código")
    with db() as conexion:
        conexion.ejecutar("SELECT id, nombre, avatar, rol FROM usuarios WHERE casa_id = ?", (casa["id"],))
        return conexion.todos()


# 🐛 FIX #4 (bypass de autenticación): esta ruta creaba integrantes nuevos sin exigir
# NINGÚN PIN — solo el código de casa, que se comparte libremente con toda la
# familia (se muestra en pantalla, se pasa por chat, etc.). Cualquiera con el código
# podía crear cuentas sin ser Admin. Ahora exige PIN válido de ADMIN_CASA de esa
# misma casa, igual que editar_usuario_admin y eliminar_usuario.
@app.post("/casas/{codigo}/usuarios/crear")
async def crear_usuario_en_casa(
    codigo: str,
    data: NuevoPerfilSchema,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    casa = buscar_casa_por_codigo(codigo)
    if not casa:
        raise HTTPException(status_code=404, detail="No existe ninguna casa con ese código")
    if casa["id"] != usuario["casa_id"]:
        raise HTTPException(status_code=403, detail="No puedes crear integrantes en otra casa")
    if len(data.pin) < 4:
        raise HTTPException(status_code=400, detail="El PIN debe tener al menos 4 dígitos")
    with db() as conexion:
        salt = generar_salt()
        conexion.ejecutar(
            "INSERT INTO usuarios (casa_id, nombre, pin_hash, salt, rol, avatar) VALUES (?, ?, ?, ?, ?, ?)",
            (casa["id"], data.nombre, hash_pin(data.pin, salt), salt, ROL_USUARIO, data.avatar),
        )
        conexion.commit()
    try:
        await gestor_ws.broadcast({"event": "INTEGRANTES_ACTUALIZADOS", "casa": casa["codigo_invitacion"]})
    except Exception:
        pass
    return {"mensaje": "Perfil creado exitosamente"}


# ===========================================================================
# 14 · PERFIL Y USUARIOS
# ===========================================================================
@app.get("/perfil/me")
async def mi_perfil(usuario: dict = Depends(verificar_pin)):
    return {
        "id": usuario["id"], "nombre": usuario["nombre"],
        "avatar": usuario["avatar"], "rol": usuario["rol"], "casa_id": usuario["casa_id"],
    }


@app.put("/perfil")
async def editar_perfil(data: EditarPerfilSchema, usuario: dict = Depends(verificar_pin)):
    if len(data.pin) < 4:
        raise HTTPException(status_code=400, detail="El PIN debe tener al menos 4 dígitos")
    salt = generar_salt()
    with db() as conexion:
        conexion.ejecutar(
            "UPDATE usuarios SET nombre = ?, pin_hash = ?, salt = ?, avatar = ? WHERE id = ?",
            (data.nombre, hash_pin(data.pin, salt), salt, data.avatar, usuario["id"]),
        )
        conexion.commit()
    return {"mensaje": "Perfil actualizado", "nombre": data.nombre, "avatar": data.avatar}


@app.put("/usuarios/{user_id}")
async def editar_usuario_admin(
    user_id: int,
    data: EditarPerfilSchema,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    if len(data.pin) < 4:
        raise HTTPException(status_code=400, detail="El PIN debe tener al menos 4 dígitos")
    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM usuarios WHERE id = ? AND casa_id = ?",
            (user_id, usuario["casa_id"]),
        )
        objetivo = conexion.uno()
    if not objetivo:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if objetivo["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="No puedes modificar al Master")

    salt = generar_salt()
    with db() as conexion:
        conexion.ejecutar(
            "UPDATE usuarios SET nombre = ?, pin_hash = ?, salt = ?, avatar = ? WHERE id = ?",
            (data.nombre, hash_pin(data.pin, salt), salt, data.avatar, user_id),
        )
        conexion.commit()
    try:
        await gestor_ws.broadcast({"event": "INTEGRANTES_ACTUALIZADOS", "casa_id": usuario["casa_id"]})
    except Exception:
        pass
    return {"mensaje": "Usuario actualizado"}


@app.put("/usuarios/{user_id}/rol")
async def cambiar_rol_usuario(
    user_id: int,
    data: CambiarRolSchema,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    if user_id == usuario["id"]:
        raise HTTPException(status_code=400, detail="No puedes cambiar tu propio rol")
    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM usuarios WHERE id = ? AND casa_id = ?",
            (user_id, usuario["casa_id"]),
        )
        objetivo = conexion.uno()
    if not objetivo:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if objetivo["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="No puedes modificar al Master")
    with db() as conexion:
        conexion.ejecutar("UPDATE usuarios SET rol = ? WHERE id = ?", (data.rol, user_id))
        conexion.commit()
    try:
        await gestor_ws.broadcast({"event": "INTEGRANTES_ACTUALIZADOS", "casa_id": usuario["casa_id"]})
    except Exception:
        pass
    return {"mensaje": f"Rol de '{objetivo['nombre']}' actualizado a {data.rol}"}


@app.delete("/usuarios/{user_id}")
async def eliminar_usuario(
    user_id: int,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    if user_id == usuario["id"]:
        raise HTTPException(status_code=400, detail="No puedes eliminarte a ti mismo")
    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM usuarios WHERE id = ? AND casa_id = ?",
            (user_id, usuario["casa_id"]),
        )
        objetivo = conexion.uno()
    if not objetivo:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if objetivo["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="No puedes eliminar al Master")
    with db() as conexion:
        conexion.ejecutar("DELETE FROM usuarios WHERE id = ?", (user_id,))
        conexion.commit()
    try:
        await gestor_ws.broadcast({"event": "INTEGRANTES_ACTUALIZADOS", "casa_id": usuario["casa_id"]})
    except Exception:
        pass
    return {"mensaje": f"Usuario '{objetivo['nombre']}' eliminado"}


# ===========================================================================
# 15 · MASTER ADMIN
# ===========================================================================
@app.get("/master/estadisticas")
async def estadisticas_globales(usuario: dict = Depends(requerir_rol(ROL_MASTER))):
    with db() as conexion:
        conexion.ejecutar("SELECT COUNT(*) AS total FROM casas"); total_casas = conexion.uno()["total"]
        conexion.ejecutar("SELECT COUNT(*) AS total FROM usuarios"); total_usuarios = conexion.uno()["total"]
        conexion.ejecutar("SELECT COUNT(*) AS total FROM dispositivos"); total_dispositivos = conexion.uno()["total"]
        conexion.ejecutar("""
            SELECT c.id, c.nombre, c.codigo_invitacion, c.creado_en,
                   COUNT(DISTINCT u.id) AS usuarios,
                   COUNT(DISTINCT d.id) AS dispositivos
            FROM casas c
            LEFT JOIN usuarios u ON u.casa_id = c.id
            LEFT JOIN dispositivos d ON d.casa_id = c.id
            GROUP BY c.id, c.nombre, c.codigo_invitacion, c.creado_en
            ORDER BY c.creado_en DESC
        """)
        casas = conexion.todos()
    return {
        "resumen": {"total_casas": total_casas, "total_usuarios": total_usuarios, "total_dispositivos": total_dispositivos},
        "salud_servidor": telemetria_del_servidor_local(),
        "casas": casas, "modo_comercial": MODO_COMERCIAL,
    }


# ===========================================================================
# 16 · DISPOSITIVOS (AGENTLESS: cada nodo es una pestaña de navegador)
# ===========================================================================
@app.get("/dispositivos")
async def listar_dispositivos(
    usuario: dict = Depends(verificar_pin),
    x_device_uid: Optional[str] = Header(None, alias="X-Device-Uid"),
):
    if usuario["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="Master usa /master/estadisticas")
    with db() as conexion:
        conexion.ejecutar("SELECT * FROM dispositivos WHERE casa_id = ?", (usuario["casa_id"],))
        filas = conexion.todos()

    resultado = []
    for d in filas:
        if usuario["rol"] == ROL_USUARIO and d["asignado_a"] != usuario["id"]:
            continue
        specs = {
            "cpu": d["cpu"] or "Sin datos", "ram": d["ram"] or "Sin datos",
            "disco": d["disco"] or "Sin foco registrado", "bateria": d["bateria"] or "Sin datos",
            "procesos": d["procesos"] or 0,
        }
        resultado.append({
            "id": d["id"], "nombre": d["nombre"], "tipo": d["tipo"],
            "ip": d["ip"], "mac": d["mac"], "ubicacion": d["ubicacion"],
            "lat": d["lat"], "lng": d["lng"], "estado": d["estado"],
            "asignado_a": d["asignado_a"],
            "es_local": es_nodo_propio(d, x_device_uid),
            "conectado": gestor_ws.esta_conectado(d.get("token") or ""),
            "specs": specs,
        })
    return resultado


@app.post("/dispositivos/agregar")
async def agregar_dispositivo(data: DispositivoSchema, usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA))):
    if data.mac and not mac_es_valida(data.mac):
        raise HTTPException(status_code=400, detail="MAC inválida")
    with db() as conexion:
        conexion.ejecutar(
            "INSERT INTO dispositivos (casa_id, nombre, tipo, ip, mac, ubicacion, estado) "
            "VALUES (?, ?, ?, ?, ?, 'Dispositivo de red', 'ONLINE')",
            (usuario["casa_id"], data.nombre, data.tipo, data.ip, data.mac or ""),
        )
        conexion.commit()
    return {"mensaje": "Dispositivo agregado"}


@app.delete("/dispositivos/{dev_id}")
async def eliminar_dispositivo(dev_id: int, usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA))):
    dispositivo = obtener_dispositivo_de_la_casa(dev_id, usuario)
    with db() as conexion:
        conexion.ejecutar("DELETE FROM comandos WHERE dispositivo_id = ?", (dev_id,))
        conexion.ejecutar("DELETE FROM dispositivos WHERE id = ?", (dev_id,))
        conexion.commit()
    return {"mensaje": f"Dispositivo '{dispositivo['nombre']}' eliminado"}


@app.post("/dispositivos/registrar-nodo")
async def registrar_nodo(
    data: RegistroNodoSchema,
    request: Request,
    usuario: dict = Depends(verificar_pin),
):
    """
    Reingeniería Agentless (punto 1): no hay código de vinculación aparte ni
    script para descargar. En cuanto el integrante entra con el ÚNICO código
    de la casa y su PIN, su propia pestaña se anuncia aquí y queda enlazada
    como un nodo controlado — sin instalar nada.
    """
    if usuario["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="Master no administra nodos de una casa")
    ip_origen = request.client.host if request.client else ""
    ahora = datetime.utcnow().isoformat()

    def _actualizar_existente(conexion: ConexionDB, fila_id: int) -> int:
        conexion.ejecutar(
            "UPDATE dispositivos SET nombre = ?, tipo = ?, ip = ?, estado = 'ONLINE', "
            "asignado_a = ?, actualizado_en = ? WHERE id = ?",
            (data.nombre, data.tipo, ip_origen, usuario["id"], ahora, fila_id),
        )
        return fila_id

    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM dispositivos WHERE casa_id = ? AND token = ?",
            (usuario["casa_id"], data.device_uid),
        )
        existente = conexion.uno()
        if existente:
            dev_id = _actualizar_existente(conexion, existente["id"])
        else:
            try:
                conexion.ejecutar(
                    "INSERT INTO dispositivos (casa_id, nombre, tipo, ip, mac, token, ubicacion, "
                    "estado, asignado_a, actualizado_en) "
                    "VALUES (?, ?, ?, ?, '', ?, 'Ubicación no establecida', 'ONLINE', ?, ?)",
                    (usuario["casa_id"], data.nombre, data.tipo, ip_origen, data.device_uid, usuario["id"], ahora),
                    es_insert=True,
                )
                dev_id = conexion.id_insertado()
            except Exception as e:
                # Condición de carrera: otra petición concurrente de la misma
                # pestaña ya insertó esta fila (casa_id, token) y chocó contra
                # el índice único 'ux_dispositivos_casa_token'. Se recupera esa
                # fila y esta llamada se convierte en UPDATE — nunca se deja
                # una segunda fila duplicada para el mismo nodo físico.
                log.warning(
                    "Conflicto de inserción en registrar-nodo (%s); "
                    "recuperando fila existente y aplicando UPDATE.", e,
                )
                conexion.ejecutar(
                    "SELECT * FROM dispositivos WHERE casa_id = ? AND token = ?",
                    (usuario["casa_id"], data.device_uid),
                )
                fila_recuperada = conexion.uno()
                if not fila_recuperada:
                    raise HTTPException(
                        status_code=500,
                        detail="No se pudo registrar ni recuperar el nodo tras el conflicto de concurrencia.",
                    )
                dev_id = _actualizar_existente(conexion, fila_recuperada["id"])
        conexion.commit()
    try:
        await gestor_ws.broadcast({"event": "INTEGRANTES_ACTUALIZADOS", "casa_id": usuario["casa_id"]})
    except Exception:
        pass
    return {"mensaje": "Nodo enlazado a la casa", "dispositivo_id": dev_id}


@app.post("/dispositivos/telemetria-web")
async def telemetria_web(data: TelemetriaWebSchema, usuario: dict = Depends(verificar_pin)):
    """
    Reingeniería Agentless (punto 2): telemetría capturada 100% con Web APIs
    nativas del navegador del integrante (batería real, hilos lógicos, memoria
    aproximada, título de la pestaña activa) y transmitida por este endpoint.
    """
    if usuario["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="No aplica a Master")
    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM dispositivos WHERE casa_id = ? AND token = ?",
            (usuario["casa_id"], data.device_uid),
        )
        dispositivo = conexion.uno()
        if not dispositivo:
            raise HTTPException(status_code=404, detail="Nodo no registrado. Reingresa con el código de tu casa.")

        campos, valores = ["actualizado_en = ?", "estado = 'ONLINE'"], [datetime.utcnow().isoformat()]
        if data.cpu is not None:
            campos.append("cpu = ?"); valores.append(data.cpu)
        if data.ram is not None:
            campos.append("ram = ?"); valores.append(data.ram)
        if data.bateria is not None:
            campos.append("bateria = ?"); valores.append(data.bateria)
        if data.ventana_activa is not None:
            campos.append("disco = ?"); valores.append(f"🪟 {data.ventana_activa}"[:250])
        if data.hilos is not None:
            campos.append("procesos = ?"); valores.append(data.hilos)
        if data.lat is not None and data.lng is not None:
            campos.append("lat = ?"); valores.append(data.lat)
            campos.append("lng = ?"); valores.append(data.lng)
            campos.append("ubicacion = ?"); valores.append(data.ubicacion or "Ubicación detectada")
        valores.append(dispositivo["id"])
        conexion.ejecutar(f"UPDATE dispositivos SET {', '.join(campos)} WHERE id = ?", tuple(valores))
        conexion.commit()
    return {"mensaje": "Telemetría recibida"}


# ===========================================================================
# 17 · ENERGÍA — DIRECTIVAS COMPATIBLES CON ENTORNOS WEB (sin agente/root)
# ===========================================================================
@app.post("/dispositivos/{dev_id}/energia")
async def controlar_energia(
    dev_id: int,
    data: AccionEnergia,
    usuario: dict = Depends(verificar_pin),
    x_device_uid: Optional[str] = Header(None, alias="X-Device-Uid"),
):
    if usuario["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="Master no controla dispositivos de casas")
    if usuario["rol"] == ROL_GESTOR_CASA:
        raise HTTPException(status_code=403, detail="Tu rol permite supervisión, no control de energía")
    if usuario["rol"] == ROL_USUARIO:
        raise HTTPException(status_code=403, detail="Tu rol (Hijo/Hija) NO puede ejecutar comandos de energía")

    dispositivo = obtener_dispositivo_de_la_casa(dev_id, usuario)
    if not puede_controlar_energia(usuario, dispositivo):
        raise HTTPException(status_code=403, detail="No tienes permiso sobre este dispositivo")
    if es_nodo_propio(dispositivo, x_device_uid):
        raise HTTPException(status_code=400, detail="⛔ Estación local maestra — no puedes controlar tu propia sesión")

    accion = data.accion.upper()
    device_uid_objetivo = (dispositivo.get("token") or "").strip()
    conectado = gestor_ws.esta_conectado(device_uid_objetivo)

    if accion == "ENCENDER":
        # -----------------------------------------------------------------
        # FIX #3 — ENCENDER consulta explícitamente el campo 'estado' que
        # guarda la base de datos:
        #   · estado == 'OFFLINE' → el nodo no tiene sesión web activa; se
        #     emite un paquete Wake-on-LAN real por broadcast en la red local
        #     usando la MAC registrada (enviar_wol).
        #   · en cualquier otro caso con WebSocket conectado → el nodo ya
        #     está "encendido" (tiene su pestaña abierta); ENCENDER se
        #     traduce en deshacer un BLOQUEAR/CONGELAR previo (DESBLOQUEAR).
        # -----------------------------------------------------------------
        if dispositivo.get("estado") == "OFFLINE":
            if not dispositivo["mac"]:
                raise HTTPException(
                    status_code=400,
                    detail="El dispositivo está OFFLINE y no tiene MAC registrada para Wake-on-LAN",
                )
            try:
                enviar_wol(dispositivo["mac"])
                encolar_comando(dev_id, "ENCENDER (WOL)")
                return {"mensaje": f"Paquete Wake-on-LAN enviado a la red local para '{dispositivo['nombre']}'."}
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        if conectado:
            entregado = await gestor_ws.enviar_a_dispositivo(
                device_uid_objetivo, {"event": "COMANDO_ENERGIA", "accion": "DESBLOQUEAR"}
            )
            encolar_comando(dev_id, "DESBLOQUEAR")
            if entregado:
                return {"mensaje": "Pantalla desbloqueada en tiempo real."}
            raise HTTPException(status_code=502, detail="No se pudo entregar el desbloqueo; intenta de nuevo.")

        # Estado ONLINE en base de datos pero sin WebSocket activo en este
        # instante (p. ej. la pestaña se cerró hace poco y aún no se marcó
        # OFFLINE): se intenta WOL igualmente si hay MAC disponible.
        if dispositivo["mac"]:
            try:
                enviar_wol(dispositivo["mac"])
                encolar_comando(dev_id, "ENCENDER (WOL)")
                return {"mensaje": "Paquete Wake-on-LAN enviado a la red local"}
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(status_code=400, detail="El dispositivo está desconectado y no tiene MAC para Wake-on-LAN")

    # BLOQUEAR / APAGAR / REINICIAR: se empujan por WebSocket a la pestaña del
    # integrante, que reacciona con el overlay Cyberpunk a pantalla completa,
    # cierra la pestaña o recarga la sesión — sin ejecutar nada en el servidor.
    encolar_comando(dev_id, accion)
    if not conectado:
        return {
            "mensaje": f"'{dispositivo['nombre']}' está desconectado ahora mismo; el comando quedó registrado.",
            "entregado": False,
        }
    entregado = await gestor_ws.enviar_a_dispositivo(device_uid_objetivo, {"event": "COMANDO_ENERGIA", "accion": accion})
    if entregado:
        return {"mensaje": f"Comando '{accion}' enviado en tiempo real al navegador de '{dispositivo['nombre']}'."}
    return {"mensaje": f"'{dispositivo['nombre']}' se desconectó justo ahora; el comando quedó registrado.", "entregado": False}


# ===========================================================================
# 18 · (antes: VINCULACIÓN AUTÓNOMA por script descargable — eliminada)
# ===========================================================================
# (El antiguo flujo de /vincular/{codigo} con descarga de script Python fue
# eliminado por completo — ver el registro de nodo agentless en la sección 16.)


# ===========================================================================
# 19 · MERCADO PAGO PERÚ — WEBHOOK (match por codigo_invitacion)
# ===========================================================================
@app.post("/api/v1/pagos/mercado-pago/webhook")
async def webhook_mercado_pago(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    tipo = (payload.get("type") or payload.get("topic") or "").lower()
    accion = (payload.get("action") or "").lower()
    if "payment" not in tipo and "payment" not in accion:
        return {"ok": True, "ignored": tipo or accion or "unknown"}

    data = payload.get("data") or {}
    payment_id = data.get("id") or payload.get("id") or payload.get("resource")
    external_ref = str(payload.get("external_reference") or data.get("external_reference") or "").strip().upper()

    casa: Optional[dict] = None
    if external_ref:
        casa = buscar_casa_por_codigo(external_ref)

    if casa is None and MP_ACCESS_TOKEN and requests and payment_id:
        try:
            r = requests.get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                timeout=10,
            )
            if r.ok:
                info = r.json()
                ref = str(info.get("external_reference") or "").strip().upper()
                estado_pago = (info.get("status") or "").lower()
                if ref and estado_pago in ("approved", "authorized"):
                    casa = buscar_casa_por_codigo(ref)
                    external_ref = ref
        except Exception as e:
            log.warning("No se pudo consultar pago MP %s: %s", payment_id, e)

    if casa is None:
        raise HTTPException(
            status_code=400,
            detail="No se pudo localizar la casa por external_reference (código de invitación)",
        )

    ahora = datetime.utcnow().isoformat()
    with db() as conexion:
        conexion.ejecutar("UPDATE casas SET creado_en = ? WHERE id = ?", (ahora, casa["id"]))
        conexion.commit()

    return {
        "ok": True, "casa_id": casa["id"],
        "codigo_invitacion": casa["codigo_invitacion"],
        "renovado_en": ahora,
        "mensaje": f"Suscripción renovada por {DIAS_SUSCRIPCION} días",
    }


# (El antiguo "20 · AGENTE LOCAL" con endpoints /agente/comando y
# /agente/telemetria para un proceso Python externo fue eliminado: en el
# modelo agentless esas funciones las cubren /dispositivos/registrar-nodo,
# /dispositivos/telemetria-web y el push en tiempo real por WebSocket.)


# ===========================================================================
# 21 · WEBSOCKET
# ===========================================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await gestor_ws.conectar(websocket)
    try:
        await websocket.send_json({"event": "BIENVENIDA", "version": "12.1"})
        while True:
            try:
                msg = await websocket.receive_text()
                if msg == "ping":
                    await websocket.send_json({"event": "pong"})
                    continue
                try:
                    data = json.loads(msg)
                except (ValueError, TypeError):
                    continue
                if data.get("tipo") == "registro" and data.get("device_uid"):
                    gestor_ws.registrar_dispositivo(str(data["device_uid"])[:128], websocket)
                    await websocket.send_json({"event": "NODO_ENLAZADO"})
            except WebSocketDisconnect:
                break
    except Exception:
        pass
    finally:
        gestor_ws.desconectar(websocket)


# ===========================================================================
# 22 · ENTRYPOINT
# ===========================================================================
if __name__ == "__main__":
    import uvicorn
    puerto_dinamico = int(os.getenv("PORT", 8000))
    uvicorn.run(
        "Servidor_Universal_Aegis_Family_OS:app",
        host="0.0.0.0",
        port=puerto_dinamico,
        reload=False,
    )
