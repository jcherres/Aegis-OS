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
import base64
import html
import sqlite3
import subprocess
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
    hashlib.sha256(b"FAM123").hexdigest(),
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


class CodigoVinculacionSchema(BaseModel):
    nombre_dispositivo: Optional[str] = ""


class AccionEnergia(BaseModel):
    accion: Literal["ENCENDER", "BLOQUEAR", "APAGAR", "REINICIAR"]


class TelemetriaAgenteSchema(BaseModel):
    cpu: Optional[str] = None
    ram: Optional[str] = None
    disco: Optional[str] = None
    bateria: Optional[str] = None
    procesos: Optional[int] = None


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

  #mapa{width:100%;height:100%;border-radius:20px;filter:hue-rotate(180deg) saturate(1.15) brightness(.85)}
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
      ▶ CONVERSAR
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
            <button id="btnVincular" onclick="generarCodigoVinculacion()" class="hidden btn-glass px-3 py-2 text-[10px] tracking-[0.2em] text-cyan-200 uppercase">+ VINCULAR</button>
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
            <div class="hud-box p-4 text-center"><p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2">DISCO</p><p id="mDisco" class="font-display text-xl font-black txt-cyan">--</p></div>
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div class="hud-box p-4 text-center"><p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2">BATERÍA</p><p id="mBat" class="font-display text-xl font-black txt-cyan">--</p></div>
            <div class="hud-box p-4 text-center"><p class="text-[10px] tracking-[0.25em] text-slate-500 mb-2">PROCESOS</p><p id="mProc" class="font-display text-xl font-black txt-cyan">--</p></div>
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

<!-- ================= MODAL VINCULACIÓN ================= -->
<div id="modalVinculacion" class="hidden fixed inset-0 bg-black/90 backdrop-blur-xl flex items-center justify-center p-4 z-50">
  <div class="glass-card p-7 w-full max-w-lg text-center flex flex-col gap-5">
    <h3 class="font-display text-lg font-black text-white tracking-[0.1em]">🔗 VINCULAR DISPOSITIVO</h3>
    <p class="text-[10px] tracking-[0.2em] text-slate-500">COMPARTE ESTE CÓDIGO DE 6 DÍGITOS (10 MIN)</p>
    <div id="codigoVincBox" class="hud-box p-5 text-4xl tracking-[0.5em] txt-cyan">------</div>
    <p class="text-[10px] tracking-[0.25em] text-amber-400">INGRÉSALO EN: <span id="urlVincular" class="text-cyan-300">/vincular/------</span></p>
    <button onclick="cerrarModal('modalVinculacion')" class="btn-glass py-3 text-[11px] tracking-[0.2em]">CERRAR</button>
  </div>
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
    socketWS.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.event === "LICENCIA_EXPIRADA") {
          mostrarPaywall(data.mensaje || "Tu período de prueba ha terminado.");
        }
        if (data.event === "INTEGRANTES_ACTUALIZADOS") {
          // Repintado en caliente: el Admin añadió/editó/eliminó a un integrante.
          // Refresca dispositivos y la cuadrícula neón de perfiles al instante.
          if (usuarioActual) {
            cargarDispositivos();
            cargarIntegrantes();
          } else if (codigoCasaActual) {
            cargarPerfiles();
          }
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
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19 }).addTo(mapaLeaflet);
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
  const h = { "X-Pin": pinActivo };
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
   BATERÍA REAL DEL NAVEGADOR
   ===================================================================== */
async function obtenerBateriaNavegador() {
  if (!navigator.getBattery) return null;
  try {
    const bat = await navigator.getBattery();
    const pct = Math.round((bat.level || 0) * 100);
    const estado = bat.charging ? "⚡ Cargando" : "🔋 Batería";
    return pct + "% (" + estado + ")";
  } catch (e) { return null; }
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
  cargarDispositivos();
  cargarIntegrantes();
  if (timerTelemetria) clearInterval(timerTelemetria);
  timerTelemetria = setInterval(() => { cargarDispositivos(); cargarIntegrantes(); }, 12000);
}

async function cargarDispositivos() {
  try {
    const res = await fetchAuth(API + "/dispositivos");
    if (!res.ok) return;
    dispositivos = await res.json();

    // Inyectar batería REAL del navegador para el dispositivo local
    const batNavegador = await obtenerBateriaNavegador();
    if (batNavegador) {
      dispositivos.forEach(d => {
        if (d.es_local && d.specs) d.specs.bateria = batNavegador;
      });
    }
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
    pintarIntegrantes();
  } catch (e) {}
}

function pintarIntegrantes() {
  const cont = document.getElementById("gridIntegrantes");
  cont.innerHTML = "";
  if (!integrantes.length) {
    cont.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">Aún no hay integrantes.</p>';
    return;
  }
  const esAdmin = usuarioActual && usuarioActual.rol === "ADMIN_CASA";

  integrantes.forEach(u => {
    const wrap = document.createElement("div");
    wrap.className = "flex flex-col items-center";
    wrap.style.position = "relative";

    const circle = document.createElement("div");
    circle.className = "avatar-circle";
    circle.innerHTML = '<span>' + (u.avatar || "👤") + '</span>';

    if (esAdmin && u.rol !== "MASTER_ADMIN" && (!usuarioActual || u.id !== usuarioActual.id)) {
      const btnEdit = document.createElement("button");
      btnEdit.className = "avatar-mini avatar-mini-edit";
      btnEdit.title = "Editar";
      btnEdit.innerText = "✎";
      btnEdit.onclick = (ev) => { ev.stopPropagation(); abrirModalEditarIntegrante(u); };
      circle.appendChild(btnEdit);

      const btnDel = document.createElement("button");
      btnDel.className = "avatar-mini avatar-mini-del";
      btnDel.title = "Eliminar";
      btnDel.innerText = "✕";
      btnDel.onclick = (ev) => { ev.stopPropagation(); eliminarIntegrante(u); };
      circle.appendChild(btnDel);
    }

    const name = document.createElement("p");
    name.className = "avatar-name";
    name.innerText = u.nombre;

    const badge = document.createElement("span");
    badge.className = "rol-badge rol-" + u.rol + " mt-1";
    badge.innerText = u.rol;

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
    const res = await fetch(API + "/casas/" + encodeURIComponent(codigoCasaActual) + "/usuarios/crear", {
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

async function eliminarIntegrante(u) {
  if (!confirm("¿Eliminar a " + u.nombre + "?")) return;
  try {
    const res = await fetchAuth(API + "/usuarios/" + u.id, { method: "DELETE" });
    if (!res.ok) { alert("No se pudo eliminar"); return; }
    await cargarIntegrantes();
  } catch (e) {}
}

/* =====================================================================
   VINCULACIÓN
   ===================================================================== */
async function generarCodigoVinculacion() {
  try {
    const res = await fetchAuth(API + "/dispositivos/generar-codigo", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nombre_dispositivo: "" })
    });
    if (!res.ok) { alert("No se pudo generar el código"); return; }
    const data = await res.json();
    document.getElementById("codigoVincBox").innerText = data.codigo;
    document.getElementById("urlVincular").innerText = location.origin + "/vincular/" + data.codigo;
    mostrarModal("modalVinculacion");
  } catch (e) {}
}

/* =====================================================================
   CERRAR SESIÓN
   ===================================================================== */
function cerrarSesion() {
  if (timerTelemetria) { clearInterval(timerTelemetria); timerTelemetria = null; }
  pinActivo = ""; usuarioActual = null; idSeleccionado = null;
  mostrar("faseAcceso");
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
</script>
</body>
</html>
"""


# ===========================================================================
# 8 · AGENTE LOCAL (PLANTILLA AUTOINYECTABLE)
# ===========================================================================
AGENTE_TEMPLATE = r'''"""
Aegis Family OS · Agente Local
Requiere: pip install psutil requests
"""
import os, sys, time, platform, subprocess, socket
import psutil, requests

AEGIS_URL = "__AEGIS_URL__"
AEGIS_TOKEN = "__AEGIS_TOKEN__"

if AEGIS_URL == "__AEGIS_URL__":
    AEGIS_URL = os.environ.get("AEGIS_URL", "http://localhost:8000")
if AEGIS_TOKEN == "__AEGIS_TOKEN__":
    AEGIS_TOKEN = os.environ.get("AEGIS_TOKEN", "")

INTERVALO = int(os.environ.get("AEGIS_INTERVALO", "10"))
HEADERS = {"X-Device-Token": AEGIS_TOKEN}
NOMBRE_HOST = socket.gethostname()

def _log(m): print("[Aegis " + NOMBRE_HOST + "] " + m, flush=True)

def telemetria():
    try:
        b = psutil.sensors_battery()
        bat = (str(int(b.percent)) + "%") if b else "AC Directo"
    except Exception:
        bat = "N/D"
    try:
        d = str(int(psutil.disk_usage(os.path.abspath(os.sep)).percent)) + "%"
    except Exception:
        d = "0%"
    return {
        "cpu": str(int(psutil.cpu_percent(interval=1))) + "%",
        "ram": str(int(psutil.virtual_memory().percent)) + "%",
        "disco": d, "bateria": bat, "procesos": len(psutil.pids()),
    }

def ejecutar(accion):
    sis = platform.system()
    if sis == "Windows":
        cmds = {"BLOQUEAR": ["rundll32.exe", "user32.dll,LockWorkStation"],
                "APAGAR": ["shutdown", "/s", "/t", "5"],
                "REINICIAR": ["shutdown", "/r", "/t", "5"]}
    elif sis == "Darwin":
        cmds = {"BLOQUEAR": ["pmset", "displaysleepnow"],
                "APAGAR": ["sudo", "shutdown", "-h", "now"],
                "REINICIAR": ["sudo", "shutdown", "-r", "now"]}
    else:
        cmds = {"BLOQUEAR": ["loginctl", "lock-session"],
                "APAGAR": ["shutdown", "now"],
                "REINICIAR": ["reboot"]}
    c = cmds.get(accion)
    if not c: return
    try:
        subprocess.run(c, shell=(sis == "Windows"), check=False)
        _log("Ejecutado: " + accion)
    except Exception as e:
        _log("Error " + accion + ": " + str(e))

def loop():
    if not AEGIS_TOKEN:
        _log("ERROR: sin token"); sys.exit(1)
    _log("Agente arrancado -> " + AEGIS_URL)
    while True:
        try:
            requests.post(AEGIS_URL + "/agente/telemetria", json=telemetria(), headers=HEADERS, timeout=10)
        except Exception as e:
            _log("Telemetria fallo: " + str(e))
        try:
            r = requests.get(AEGIS_URL + "/agente/comando", headers=HEADERS, timeout=10)
            if r.status_code == 200:
                d = r.json()
                if d.get("comando_id") and d.get("accion"):
                    ejecutar(d["accion"])
                    requests.post(AEGIS_URL + "/agente/comando/" + str(d["comando_id"]) + "/completado",
                                  headers=HEADERS, timeout=10)
        except Exception as e:
            _log("Comando fallo: " + str(e))
        time.sleep(INTERVALO)

if __name__ == "__main__":
    try: loop()
    except KeyboardInterrupt: _log("Detenido.")
'''


# ===========================================================================
# 9 · WEBSOCKET MANAGER
# ===========================================================================
class GestorWebSockets:
    def __init__(self):
        self.conexiones: List[WebSocket] = []

    async def conectar(self, ws: WebSocket):
        await ws.accept()
        self.conexiones.append(ws)

    def desconectar(self, ws: WebSocket):
        if ws in self.conexiones:
            self.conexiones.remove(ws)

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
    allow_credentials=True,
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

    if x_casa:
        casa = buscar_casa_por_codigo(x_casa)
        if casa:
            usuario = buscar_usuario_por_pin(x_pin, casa_id=casa["id"])

    if usuario is None:
        usuario = buscar_usuario_por_pin(x_pin)

    if not usuario:
        raise HTTPException(status_code=403, detail="PIN no válido")

    if usuario["rol"] != ROL_MASTER:
        if not x_casa:
            raise HTTPException(status_code=400, detail="Falta el código de casa (X-Casa)")
        if casa is None:
            casa = buscar_casa_por_codigo(x_casa)
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


def puede_controlar_energia(usuario: dict, dispositivo: dict) -> bool:
    if usuario["rol"] == ROL_ADMIN_CASA: return True
    if usuario["id"] == dispositivo["asignado_a"]: return True
    return False


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


def es_equipo_local(dispositivo: dict) -> bool:
    ip = (dispositivo.get("ip") or "").strip()
    return ip in ("127.0.0.1", "localhost", "::1", "")


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


def ejecutar_comando_local(accion: str) -> dict:
    try:
        if os.name == "nt":
            if accion == "BLOQUEAR":
                subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], shell=True)
                return {"mensaje": "Equipo bloqueado"}
            if accion == "APAGAR":
                subprocess.run(["shutdown", "/s", "/t", "5"], shell=True)
                return {"mensaje": "Apagando en 5 segundos..."}
            if accion == "REINICIAR":
                subprocess.run(["shutdown", "/r", "/t", "5"], shell=True)
                return {"mensaje": "Reiniciando en 5 segundos..."}
        return {"mensaje": f"Comando '{accion}' registrado (modo nube)"}
    except Exception as e:
        log.exception("Fallo comando local %s", accion)
        raise HTTPException(status_code=500, detail=str(e))


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


@app.post("/casas/{codigo}/usuarios/crear")
async def crear_usuario_en_casa(codigo: str, data: NuevoPerfilSchema):
    casa = buscar_casa_por_codigo(codigo)
    if not casa:
        raise HTTPException(status_code=404, detail="No existe ninguna casa con ese código")
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
# 16 · DISPOSITIVOS
# ===========================================================================
@app.get("/dispositivos")
async def listar_dispositivos(usuario: dict = Depends(verificar_pin)):
    if usuario["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="Master usa /master/estadisticas")
    with db() as conexion:
        conexion.ejecutar("SELECT * FROM dispositivos WHERE casa_id = ?", (usuario["casa_id"],))
        filas = conexion.todos()

    resultado = []
    for d in filas:
        if usuario["rol"] == ROL_USUARIO and d["asignado_a"] != usuario["id"]:
            continue
        if es_equipo_local(d):
            specs = telemetria_del_servidor_local()
        else:
            specs = {
                "cpu": d["cpu"] or "Sin datos", "ram": d["ram"] or "Sin datos",
                "disco": d["disco"] or "Sin datos", "bateria": d["bateria"] or "Sin datos",
                "procesos": d["procesos"] or 0,
            }
        resultado.append({
            "id": d["id"], "nombre": d["nombre"], "tipo": d["tipo"],
            "ip": d["ip"], "mac": d["mac"], "ubicacion": d["ubicacion"],
            "lat": d["lat"], "lng": d["lng"], "estado": d["estado"],
            "asignado_a": d["asignado_a"], "es_local": es_equipo_local(d), "specs": specs,
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


@app.post("/dispositivos/generar-codigo")
async def generar_codigo(data: CodigoVinculacionSchema, usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA))):
    with db() as conexion:
        codigo = generar_codigo_vinculacion()
        conexion.ejecutar("SELECT 1 FROM codigos_vinculacion WHERE codigo = ?", (codigo,))
        while conexion.uno():
            codigo = generar_codigo_vinculacion()
            conexion.ejecutar("SELECT 1 FROM codigos_vinculacion WHERE codigo = ?", (codigo,))
        expira_en = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
        conexion.ejecutar(
            "INSERT INTO codigos_vinculacion (casa_id, codigo, nombre_sugerido, tipo_sugerido, expira_en, usado) "
            "VALUES (?, ?, ?, 'pc', ?, 0)",
            (usuario["casa_id"], codigo, data.nombre_dispositivo or "", expira_en),
        )
        conexion.commit()
    return {"codigo": codigo, "expira_en": expira_en, "valido_minutos": 10}


# ===========================================================================
# 17 · ENERGÍA (bloqueo parental estricto)
# ===========================================================================
@app.post("/dispositivos/{dev_id}/energia")
async def controlar_energia(dev_id: int, data: AccionEnergia, usuario: dict = Depends(verificar_pin)):
    if usuario["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="Master no controla dispositivos de casas")
    if usuario["rol"] == ROL_GESTOR_CASA:
        raise HTTPException(status_code=403, detail="Tu rol permite supervisión, no control de energía")
    if usuario["rol"] == ROL_USUARIO:
        raise HTTPException(status_code=403, detail="Tu rol (Hijo/Hija) NO puede ejecutar comandos de energía")

    dispositivo = obtener_dispositivo_de_la_casa(dev_id, usuario)
    if not puede_controlar_energia(usuario, dispositivo):
        raise HTTPException(status_code=403, detail="No tienes permiso sobre este dispositivo")

    accion = data.accion.upper()

    if es_equipo_local(dispositivo):
        return ejecutar_comando_local(accion)

    if accion == "ENCENDER":
        if not dispositivo["mac"]:
            raise HTTPException(status_code=400, detail="Este dispositivo no tiene MAC registrada para Wake-on-LAN")
        try:
            enviar_wol(dispositivo["mac"])
            return {"mensaje": "Paquete Wake-on-LAN enviado a la red local"}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    encolar_comando(dev_id, accion)
    return {"mensaje": f"Comando '{accion}' encolado. Se ejecutará cuando el Agente lo reciba."}


# ===========================================================================
# 18 · VINCULACIÓN AUTÓNOMA
# ===========================================================================
def _construir_script_agente(url_base: str, token: str) -> str:
    return AGENTE_TEMPLATE.replace("__AEGIS_URL__", url_base).replace("__AEGIS_TOKEN__", token)


def _pagina_error(titulo: str, mensaje: str) -> str:
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<title>Aegis OS — Error</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#000;color:#94a3b8;font-family:system-ui,sans-serif;padding:24px}}
.card{{background:linear-gradient(155deg,rgba(8,20,36,.95),rgba(2,6,23,.98));
border:1px solid rgba(251,113,133,.35);border-radius:28px;padding:48px 36px;max-width:520px;
text-align:center;box-shadow:0 0 90px -30px rgba(244,63,94,.6)}}
h1{{font-family:'Orbitron',sans-serif;color:#fda4af;font-size:26px;margin:16px 0}}
.icon{{font-size:56px}}
</style></head><body><div class="card">
<div class="icon">⚠</div><h1>{html.escape(titulo)}</h1><p>{html.escape(mensaje)}</p>
</div></body></html>"""


def _pagina_vincular(nombre: str, token: str, codigo: str, script: str) -> str:
    script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    data_url = f"data:text/x-python;base64,{script_b64}"
    return f"""<!DOCTYPE html><html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AEGIS FAMILY OS — Vinculación</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
body{{margin:0;background:#000;color:#94a3b8;font-family:system-ui,sans-serif;padding:32px 20px;
background-image:radial-gradient(circle at 15% 5%,rgba(6,182,212,.20),transparent 42%),
radial-gradient(circle at 88% 12%,rgba(59,130,246,.16),transparent 45%);}}
.wrap{{max-width:920px;margin:0 auto}}
.card{{background:linear-gradient(155deg,rgba(8,20,36,.95),rgba(2,6,23,.98));
border:1px solid rgba(34,211,238,.2);border-radius:32px;padding:40px 32px;
box-shadow:0 0 110px -30px rgba(34,211,238,.6);margin-bottom:24px}}
h1{{font-family:'Orbitron',sans-serif;font-size:26px;color:#67e8f9;letter-spacing:.12em;margin:0 0 8px}}
.sub{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.35em;color:rgba(103,232,249,.6);margin:0 0 28px}}
.row{{background:rgba(2,6,23,.7);border:1px solid rgba(34,211,238,.15);border-radius:18px;padding:18px;margin-bottom:14px}}
.row-label{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.3em;color:#64748b;margin-bottom:6px}}
.row-value{{font-family:'JetBrains Mono',monospace;font-size:14px;color:#67e8f9;word-break:break-all}}
.token{{color:#fcd34d}}
.btn{{display:inline-flex;width:100%;padding:22px;border-radius:20px;
font-family:'Orbitron',sans-serif;font-weight:900;font-size:15px;letter-spacing:.18em;
text-transform:uppercase;text-decoration:none;color:#a5f3fc;text-align:center;justify-content:center;
border:1px solid rgba(34,211,238,.6);
background:linear-gradient(150deg,rgba(34,211,238,.18),rgba(59,130,246,.08));
box-shadow:0 0 44px -8px rgba(34,211,238,.9)}}
pre{{background:#020617;border:1px solid rgba(34,211,238,.15);border-radius:16px;
padding:18px;color:#67e8f9;font-size:11px;line-height:1.7;max-height:300px;overflow:auto}}
ul{{font-family:'JetBrains Mono',monospace;font-size:11px;line-height:2;color:#94a3b8;padding-left:0;list-style:none}}
ul li::before{{content:'▸ ';color:#22d3ee}}
h2{{font-family:'Orbitron',sans-serif;font-size:13px;letter-spacing:.18em;color:#c084fc;margin:24px 0 10px}}
</style></head><body>
<div class="wrap"><div class="card">
<h1>◆ DISPOSITIVO VINCULADO</h1>
<p class="sub">AEGIS FAMILY OS · SISTEMA AUTÓNOMO</p>
<div class="row"><div class="row-label">Nombre</div><div class="row-value">{html.escape(nombre)}</div></div>
<div class="row"><div class="row-label">Código usado</div><div class="row-value">{html.escape(codigo)}</div></div>
<div class="row"><div class="row-label">Token del agente</div><div class="row-value token">{html.escape(token)}</div></div>
<h2>▶ Instalación automática</h2>
<a class="btn" download="aegis_agente.py" href="{data_url}">📥 Descargar Agente e Instalar</a>
<h2>▶ Instrucciones</h2>
<ul>
<li><b>Windows:</b> doble clic sobre aegis_agente.py (Python 3.10+).</li>
<li><b>macOS / Linux:</b> <code>python3 aegis_agente.py</code></li>
<li><b>Smart TV / Enchufe:</b> usar el agente o webhook compatible.</li>
</ul>
<h2>▶ Código fuente</h2>
<pre>{html.escape(script)}</pre>
</div></div></body></html>"""


@app.get("/vincular/{codigo}", response_class=HTMLResponse)
def vincular_autonomo(codigo: str, request: Request):
    codigo = codigo.strip()
    if not codigo or len(codigo) != 6 or not codigo.isdigit():
        return HTMLResponse(_pagina_error("Código inválido", "El código debe tener 6 dígitos."), status_code=400)

    with db() as conexion:
        conexion.ejecutar("SELECT * FROM codigos_vinculacion WHERE codigo = ?", (codigo,))
        fila = conexion.uno()
        if not fila:
            return HTMLResponse(_pagina_error("Código no encontrado", "Verifica el código."), status_code=404)
        if fila["usado"]:
            return HTMLResponse(_pagina_error("Código ya utilizado", "Pide uno nuevo."), status_code=400)
        try:
            expira = datetime.fromisoformat(fila["expira_en"])
        except (ValueError, TypeError):
            expira = datetime.utcnow() - timedelta(seconds=1)
        if expira < datetime.utcnow():
            return HTMLResponse(_pagina_error("Código expirado", "Caducó a los 10 minutos."), status_code=400)

        nombre_auto = (fila.get("nombre_sugerido") or "").strip() or f"Equipo-{secrets.token_hex(2).upper()}"
        tipo_auto = fila.get("tipo_sugerido") or "pc"
        ip_visitante = request.client.host if request.client else "desconocida"
        token = secrets.token_hex(16)

        conexion.ejecutar(
            "INSERT INTO dispositivos (casa_id, nombre, tipo, ip, mac, token, ubicacion, estado) "
            "VALUES (?, ?, ?, ?, '', ?, 'Pendiente de ubicación', 'ONLINE')",
            (fila["casa_id"], nombre_auto, tipo_auto, ip_visitante, token),
            es_insert=True,
        )
        conexion.ejecutar("UPDATE codigos_vinculacion SET usado = 1 WHERE id = ?", (fila["id"],))
        conexion.commit()

    base = _url_base(request)
    script = _construir_script_agente(base, token)
    return HTMLResponse(_pagina_vincular(nombre_auto, token, codigo, script))


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


# ===========================================================================
# 20 · AGENTE LOCAL
# ===========================================================================
def _agente_autenticado(x_device_token: Optional[str]) -> dict:
    if not x_device_token:
        raise HTTPException(status_code=401, detail="Falta X-Device-Token")
    with db() as conexion:
        conexion.ejecutar("SELECT * FROM dispositivos WHERE token = ?", (x_device_token,))
        dispositivo = conexion.uno()
    if not dispositivo:
        raise HTTPException(status_code=403, detail="Token de dispositivo inválido")
    return dispositivo


@app.get("/agente/comando")
def agente_obtener_comando(x_device_token: str = Header(None)):
    dispositivo = _agente_autenticado(x_device_token)
    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM comandos WHERE dispositivo_id = ? AND estado = 'PENDIENTE' "
            "ORDER BY creado_en ASC LIMIT 1",
            (dispositivo["id"],),
        )
        comando = conexion.uno()
        if comando:
            conexion.ejecutar("UPDATE comandos SET estado = 'ENVIADO' WHERE id = ?", (comando["id"],))
            conexion.commit()
    if not comando:
        return {"comando_id": None, "accion": None}
    return {"comando_id": comando["id"], "accion": comando["accion"]}


@app.post("/agente/comando/{comando_id}/completado")
def agente_marcar_completado(comando_id: int, x_device_token: str = Header(None)):
    dispositivo = _agente_autenticado(x_device_token)
    with db() as conexion:
        conexion.ejecutar(
            "UPDATE comandos SET estado = 'EJECUTADO' WHERE id = ? AND dispositivo_id = ?",
            (comando_id, dispositivo["id"]),
        )
        conexion.commit()
    return {"mensaje": "Comando marcado como ejecutado"}


@app.post("/agente/telemetria")
def agente_reportar_telemetria(
    data: TelemetriaAgenteSchema,
    request: Request,
    x_device_token: str = Header(None),
):
    dispositivo = _agente_autenticado(x_device_token)
    ip_origen = request.client.host if request.client else None
    with db() as conexion:
        if ip_origen:
            conexion.ejecutar(
                "UPDATE dispositivos SET cpu = ?, ram = ?, disco = ?, bateria = ?, "
                "procesos = ?, actualizado_en = ?, estado = 'ONLINE', ip = ? WHERE id = ?",
                (data.cpu, data.ram, data.disco, data.bateria, data.procesos,
                 datetime.utcnow().isoformat(), ip_origen, dispositivo["id"]),
            )
        else:
            conexion.ejecutar(
                "UPDATE dispositivos SET cpu = ?, ram = ?, disco = ?, bateria = ?, "
                "procesos = ?, actualizado_en = ?, estado = 'ONLINE' WHERE id = ?",
                (data.cpu, data.ram, data.disco, data.bateria, data.procesos,
                 datetime.utcnow().isoformat(), dispositivo["id"]),
            )
        conexion.commit()
    return {"mensaje": "Telemetría recibida", "ip_registrada": ip_origen}


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
