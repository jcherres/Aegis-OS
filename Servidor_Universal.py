"""
Aegis OS — Servidor Universal (FastAPI)  ·  v11.2
==================================================
Panel de control familiar multi-casa con roles jerárquicos.

CAMBIOS EN ESTA VERSIÓN (v11.2)
--------------------------------
1.  INTERRUPTOR DE NEGOCIO: variable de entorno `AEGIS_MODO_COMERCIAL`.
    - "FALSE" (default) → uso personal doméstico, suscripción infinita.
    - "TRUE" → modo SaaS comercial: cada casa tiene 30 días de prueba
      desde su `creado_en`; al vencer, todo endpoint autenticado devuelve
      402 Payment Required.

2.  CANDADO INTELIGENTE DE SUSCRIPCIÓN:
    `verificar_pin()` consulta la fecha `creado_en` de la casa del usuario
    y bloquea la sesión si ya pasaron más de 30 días (solo en modo
    comercial y salvo para el Master Admin). El frontend captura el 402
    y despliega el cartel Cyberpunk de renovación por Yape/Plin.

3.  WEBHOOK INTEGRADO DE MERCADO PAGO PERÚ:
    POST /api/v1/pagos/mercado-pago/webhook
    Acepta notificaciones `payment.created`, resuelve la casa desde
    `external_reference` (o consultando la API de MP si solo llega el
    `payment_id`) y actualiza `creado_en = utcnow()` para renovar el
    período de 30 días.

4.  INSTALACIÓN AUTÓNOMA SIN USBs:
    GET /vincular/{codigo_seis_digitos}  (público)
    Registra el dispositivo en la casa correspondiente, genera su
    `token_agente`, y devuelve una página Cyberpunk que ofrece:
      · Descarga del script `agente_local.py` con AEGIS_URL y AEGIS_TOKEN
        ya inyectados (Data URL base64, sin segundo request).
      · Vista de código para copy-paste manual.
      · Instrucciones para Windows / macOS / Linux.

5.  ESTABILIDAD LOCAL:
    - SQLite abre con `PRAGMA journal_mode=WAL` y `synchronous=NORMAL`.
    - Wake-on-LAN dispara a 255.255.255.255, 192.168.1.255 y 192.168.0.255.
    - `/agente/telemetria` captura `request.client.host` y lo persiste
      como la IP real del equipo remoto.

6.  CORS: `allow_origin_regex` en vez de `allow_origins=["*"]` para no
    chocar con `allow_credentials=True`.
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
from typing import Optional, Literal

import psutil
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s · %(levelname)s · %(name)s · %(message)s",
)
log = logging.getLogger("aegis")


# ---------------------------------------------------------------------------
# CONFIGURACIÓN POR ENTORNO
# ---------------------------------------------------------------------------
DB_NAME = "aegis_control.db"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

PIN_SEMILLA_MASTER = os.environ.get("AEGIS_MASTER_PIN", "9999")
CORS_ORIGIN_REGEX = os.environ.get("AEGIS_CORS_REGEX", r".*")

MODO_COMERCIAL = os.environ.get("AEGIS_MODO_COMERCIAL", "FALSE").upper() == "TRUE"

MP_ACCESS_TOKEN = os.environ.get("AEGIS_MP_TOKEN", "").strip()

DIAS_SUSCRIPCION = int(os.environ.get("AEGIS_DIAS_SUSCRIPCION", "30"))


# ---------------------------------------------------------------------------
# ROLES
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# MODELOS (Pydantic)
# ---------------------------------------------------------------------------
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
    avatar: str


class CambiarRolSchema(BaseModel):
    rol: Literal["ADMIN_CASA", "GESTOR_CASA", "USUARIO"]


class DispositivoSchema(BaseModel):
    nombre: str = Field(min_length=1, max_length=120)
    ip: str
    mac: Optional[str] = ""
    tipo: TipoDispositivo = "pc"


class CodigoVinculacionSchema(BaseModel):
    nombre_dispositivo: Optional[str] = ""


class VincularConCodigoSchema(BaseModel):
    codigo: str = Field(min_length=4, max_length=12)
    nombre: str = Field(min_length=1, max_length=120)
    ip: str
    mac: Optional[str] = ""
    tipo: TipoDispositivo = "pc"


class UbicacionUpdateSchema(BaseModel):
    lat: float
    lng: float


class AccionEnergia(BaseModel):
    accion: Literal["ENCENDER", "BLOQUEAR", "APAGAR", "REINICIAR"]


class AccionEnchufeSchema(BaseModel):
    accion: Literal["ON", "OFF"]


class TelemetriaAgenteSchema(BaseModel):
    cpu: Optional[str] = None
    ram: Optional[str] = None
    disco: Optional[str] = None
    bateria: Optional[str] = None
    procesos: Optional[int] = None


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# CAPA DE BASE DE DATOS HÍBRIDA (SQLite ⇄ PostgreSQL)
# ---------------------------------------------------------------------------
class ConexionDB:
    def __init__(self):
        self.es_postgres = bool(DATABASE_URL)
        self._id_pendiente: Optional[int] = None

        if self.es_postgres:
            try:
                import psycopg2
                import psycopg2.extras
            except ImportError as e:
                raise RuntimeError(
                    "DATABASE_URL está configurada pero 'psycopg2' no está "
                    "instalado. Agrega 'psycopg2-binary' a requirements.txt."
                ) from e
            self.conn = psycopg2.connect(
                DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
            )
        else:
            self.conn = sqlite3.connect(DB_NAME, timeout=15)
            self.conn.row_factory = sqlite3.Row
            self.cursor = self.conn.cursor()
            try:
                self.cursor.execute("PRAGMA journal_mode=WAL;")
                self.cursor.execute("PRAGMA synchronous=NORMAL;")
                self.cursor.execute("PRAGMA foreign_keys=ON;")
            except sqlite3.Error as e:
                log.warning("No se pudieron aplicar PRAGMAs de SQLite: %s", e)

        self.cursor = self.conn.cursor()

    def __enter__(self):
        return self

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

    def uno(self) -> Optional[dict]:
        fila = self.cursor.fetchone()
        return dict(fila) if fila is not None else None

    def todos(self) -> list:
        return [dict(f) for f in self.cursor.fetchall()]

    def id_insertado(self) -> Optional[int]:
        if self.es_postgres:
            return self._id_pendiente
        return self.cursor.lastrowid

    def commit(self):
        self.conn.commit()

    def cerrar(self):
        try:
            self.conn.close()
        except Exception:
            pass


def db() -> ConexionDB:
    return ConexionDB()


# ---------------------------------------------------------------------------
# ESQUEMA E INICIALIZACIÓN
# ---------------------------------------------------------------------------
def init_db():
    conexion = db()
    pk = "SERIAL PRIMARY KEY" if conexion.es_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"

    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS casas (
            id {pk},
            nombre TEXT NOT NULL,
            codigo_invitacion TEXT UNIQUE NOT NULL,
            creado_en TEXT
        )
    """)

    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS usuarios (
            id {pk},
            casa_id INTEGER,
            nombre TEXT NOT NULL,
            pin_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            rol TEXT DEFAULT 'USUARIO',
            avatar TEXT DEFAULT '👤',
            FOREIGN KEY (casa_id) REFERENCES casas (id)
        )
    """)

    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS dispositivos (
            id {pk},
            casa_id INTEGER NOT NULL,
            nombre TEXT NOT NULL,
            tipo TEXT DEFAULT 'pc',
            ip TEXT NOT NULL,
            mac TEXT DEFAULT '',
            token TEXT DEFAULT '',
            webhook_url TEXT DEFAULT '',
            ubicacion TEXT DEFAULT 'Ubicación no establecida',
            lat REAL DEFAULT 0.0,
            lng REAL DEFAULT 0.0,
            estado TEXT DEFAULT 'ONLINE',
            asignado_a INTEGER,
            cpu TEXT,
            ram TEXT,
            disco TEXT,
            bateria TEXT,
            procesos INTEGER,
            actualizado_en TEXT,
            FOREIGN KEY (casa_id) REFERENCES casas (id),
            FOREIGN KEY (asignado_a) REFERENCES usuarios (id)
        )
    """)

    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS codigos_vinculacion (
            id {pk},
            casa_id INTEGER NOT NULL,
            codigo TEXT UNIQUE NOT NULL,
            nombre_sugerido TEXT DEFAULT '',
            expira_en TEXT NOT NULL,
            usado INTEGER DEFAULT 0
        )
    """)

    conexion.ejecutar(f"""
        CREATE TABLE IF NOT EXISTS comandos (
            id {pk},
            dispositivo_id INTEGER NOT NULL,
            accion TEXT NOT NULL,
            estado TEXT DEFAULT 'PENDIENTE',
            creado_en TEXT
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
        if PIN_SEMILLA_MASTER == "9999":
            log.warning(
                "⚠  Master PIN es la semilla por defecto ('9999'). "
                "Configura AEGIS_MASTER_PIN y/o cámbialo tras el primer login."
            )

    conexion.ejecutar("SELECT COUNT(*) AS total FROM casas")
    if conexion.uno()["total"] == 0:
        codigo = generar_codigo_casa()
        conexion.ejecutar(
            "INSERT INTO casas (nombre, codigo_invitacion, creado_en) VALUES (?, ?, ?)",
            ("Casa de Manuel", codigo, datetime.utcnow().isoformat()),
            es_insert=True,
        )
        casa_id = conexion.id_insertado()
        log.info("Casa semilla creada con código %s", codigo)

        for nombre, pin, rol, avatar in [
            ("Manuel", "1234", ROL_ADMIN_CASA, "👨‍💻"),
            ("Abuela Rosa", "5555", ROL_GESTOR_CASA, "👵"),
            ("Jessica", "1111", ROL_USUARIO, "🎓"),
            ("Invitado", "0000", ROL_USUARIO, "👤"),
        ]:
            salt = generar_salt()
            conexion.ejecutar(
                "INSERT INTO usuarios (casa_id, nombre, pin_hash, salt, rol, avatar) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (casa_id, nombre, hash_pin(pin, salt), salt, rol, avatar),
            )

        conexion.ejecutar(
            "INSERT INTO dispositivos (casa_id, nombre, tipo, ip, mac, ubicacion, lat, lng, estado) "
            "VALUES (?, 'Equipo Principal', 'pc', '127.0.0.1', '00:11:22:33:44:55', "
            "'Ubicación detectada', -12.0464, -77.0428, 'ONLINE')",
            (casa_id,),
        )

    conexion.commit()
    conexion.cerrar()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Aegis OS v11.2 arrancando… MODO_COMERCIAL=%s", MODO_COMERCIAL)
    init_db()
    log.info("Esquema listo. Servidor escuchando.")
    yield
    log.info("Aegis OS detenido.")


# ---------------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Aegis OS — Servidor Universal",
    version="11.2",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ---------------------------------------------------------------------------
# AUTENTICACIÓN Y PERMISOS
# ---------------------------------------------------------------------------
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
    if not iso_fecha:
        return None
    try:
        creado = datetime.fromisoformat(iso_fecha)
    except (ValueError, TypeError):
        return None
    return (datetime.utcnow() - creado).days


def _verificar_suscripcion(casa: Optional[dict], usuario: dict):
    if not MODO_COMERCIAL:
        return
    if usuario["rol"] == ROL_MASTER:
        return
    if not casa or casa.get("creado_en") is None:
        return
    dias = _dias_desde(casa["creado_en"])
    if dias is not None and dias > DIAS_SUSCRIPCION:
        raise HTTPException(
            status_code=402,
            detail=(
                f"Suscripción vencida hace {dias - DIAS_SUSCRIPCION} día(s). "
                "Renueva con Yape/Plin para reactivar el panel."
            ),
        )


def verificar_pin(x_pin: str = Header(None), x_casa: str = Header(None)) -> dict:
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
        _verificar_suscripcion(casa, usuario)

    return usuario


def requerir_rol(*roles_permitidos):
    def dependencia(usuario: dict = Depends(verificar_pin)):
        if usuario["rol"] not in roles_permitidos:
            raise HTTPException(
                status_code=403,
                detail="Tu rol no tiene permiso para realizar esta acción",
            )
        return usuario
    return dependencia


def puede_controlar_energia(usuario: dict, dispositivo: dict) -> bool:
    if usuario["rol"] == ROL_ADMIN_CASA:
        return True
    if usuario["id"] == dispositivo["asignado_a"]:
        return True
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


def obtener_usuario_de_la_casa(user_id: int, usuario: dict) -> dict:
    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM usuarios WHERE id = ? AND casa_id = ?",
            (user_id, usuario["casa_id"]),
        )
        fila = conexion.uno()
    if not fila:
        raise HTTPException(status_code=404, detail="Usuario no encontrado en tu casa")
    return fila


def es_equipo_local(dispositivo: dict) -> bool:
    ip = (dispositivo.get("ip") or "").strip()
    return ip in ("127.0.0.1", "localhost", "::1", "")


def telemetria_del_servidor_local() -> dict:
    cpu_val = f"{psutil.cpu_percent(interval=None)}%"
    ram_val = f"{psutil.virtual_memory().percent}%"
    try:
        disco_val = f"{psutil.disk_usage('/').percent}%"
    except Exception:
        disco_val = "N/D"
    try:
        bateria = psutil.sensors_battery()
        bat_val = f"{int(bateria.percent)}%" if bateria else "AC Directo"
    except Exception:
        bat_val = "Servidor Nube"
    return {
        "cpu": cpu_val, "ram": ram_val, "disco": disco_val,
        "bateria": bat_val, "procesos": len(psutil.pids()),
    }


def enviar_wol(mac: str):
    mac_limpia = mac.replace(":", "").replace("-", "").strip()
    if not mac_es_valida(mac_limpia):
        raise ValueError("La dirección MAC no es válida")
    paquete = bytes.fromhex("FF" * 6 + mac_limpia * 16)

    destinos = [
        ("255.255.255.255", 9),
        ("192.168.1.255", 9),
        ("192.168.0.255", 9),
    ]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for destino in destinos:
            try:
                s.sendto(paquete, destino)
            except Exception as e:
                log.debug("WOL a %s falló: %s", destino, e)


def encolar_comando(dispositivo_id: int, accion: str):
    with db() as conexion:
        conexion.ejecutar(
            "INSERT INTO comandos (dispositivo_id, accion, estado, creado_en) "
            "VALUES (?, ?, 'PENDIENTE', ?)",
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
        return {"mensaje": f"Comando '{accion}' registrado (modo nube / SO no soportado)"}
    except Exception as e:
        log.exception("Fallo ejecutando comando local %s", accion)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# PLANTILLA DEL AGENTE (autoinyección)
# ---------------------------------------------------------------------------
def _ruta_agente() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "agente_local.py")


def _construir_script_agente(url_base: str, token: str) -> str:
    ruta = _ruta_agente()
    if not os.path.exists(ruta):
        raise HTTPException(
            status_code=500,
            detail="Falta el archivo agente_local.py en el servidor",
        )
    with open(ruta, "r", encoding="utf-8") as f:
        plantilla = f.read()
    return (
        plantilla
        .replace("__AEGIS_URL__", url_base)
        .replace("__AEGIS_TOKEN__", token)
    )


def _pagina_error(titulo: str, mensaje: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aegis OS — Error</title>
<style>
body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#000;color:#94a3b8;font-family:system-ui,-apple-system,sans-serif;padding:24px}}
.card{{background:linear-gradient(155deg,rgba(8,20,36,.95),rgba(2,6,23,.98));
border:1px solid rgba(251,113,133,.35);border-radius:28px;padding:48px 36px;max-width:520px;
text-align:center;box-shadow:0 0 90px -30px rgba(244,63,94,.6)}}
h1{{font-family:'Orbitron',sans-serif;color:#fda4af;font-size:28px;margin:16px 0;letter-spacing:.1em}}
p{{color:#94a3b8;font-size:14px;line-height:1.6}}
.icon{{font-size:56px;color:#fb7185;text-shadow:0 0 30px rgba(244,63,94,.9)}}
</style></head><body>
<div class="card">
  <div class="icon">⚠</div>
  <h1>{html.escape(titulo)}</h1>
  <p>{html.escape(mensaje)}</p>
</div></body></html>"""


def _pagina_vincular(nombre: str, token: str, codigo: str, script: str) -> str:
    script_b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    data_url = f"data:text/x-python;base64,{script_b64}"
    script_html = html.escape(script)
    nombre_html = html.escape(nombre)
    token_html = html.escape(token)
    codigo_html = html.escape(codigo)

    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Aegis OS — Vincular Dispositivo</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;-webkit-tap-highlight-color:transparent}}
body{{margin:0;min-height:100vh;background:#000;color:#94a3b8;
font-family:system-ui,-apple-system,sans-serif;padding:32px 20px;
background-image:
 radial-gradient(circle at 15% 5%,rgba(6,182,212,.20),transparent 42%),
 radial-gradient(circle at 88% 12%,rgba(59,130,246,.16),transparent 45%),
 radial-gradient(circle at 50% 110%,rgba(244,63,94,.10),transparent 50%);}}
.wrap{{max-width:920px;margin:0 auto}}
.card{{position:relative;background:linear-gradient(155deg,rgba(8,20,36,.95),rgba(2,6,23,.98));
border:1px solid rgba(34,211,238,.2);border-radius:32px;padding:40px 32px;
box-shadow:0 0 110px -30px rgba(34,211,238,.6);overflow:hidden;margin-bottom:24px}}
.card::before,.card::after{{content:'';position:absolute;width:28px;height:28px;
border:2px solid rgba(34,211,238,.6);filter:drop-shadow(0 0 8px rgba(34,211,238,.8))}}
.card::before{{top:14px;left:14px;border-right:0;border-bottom:0;border-radius:12px 0 0 0}}
.card::after{{bottom:14px;right:14px;border-left:0;border-top:0;border-radius:0 0 12px 0}}
h1{{font-family:'Orbitron',sans-serif;font-size:30px;font-weight:900;color:#67e8f9;
letter-spacing:.12em;margin:0 0 8px;text-shadow:0 0 24px rgba(34,211,238,.8)}}
.sub{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.35em;
color:rgba(103,232,249,.6);margin:0 0 28px}}
.row{{background:rgba(2,6,23,.7);border:1px solid rgba(34,211,238,.15);
border-radius:18px;padding:20px;margin-bottom:16px}}
.row-label{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.3em;
color:#64748b;margin-bottom:8px;text-transform:uppercase}}
.row-value{{font-family:'JetBrains Mono',monospace;font-size:16px;color:#67e8f9;
word-break:break-all;text-shadow:0 0 12px rgba(34,211,238,.6)}}
.token{{font-size:13px;color:#fcd34d;text-shadow:0 0 12px rgba(251,191,36,.6)}}
.btn{{display:inline-flex;align-items:center;justify-content:center;gap:12px;
width:100%;padding:22px;border-radius:20px;font-family:'Orbitron',sans-serif;
font-weight:900;font-size:15px;letter-spacing:.18em;text-transform:uppercase;
text-decoration:none;color:#a5f3fc;cursor:pointer;border:1px solid rgba(34,211,238,.6);
background:linear-gradient(150deg,rgba(34,211,238,.18),rgba(59,130,246,.08));
box-shadow:0 0 44px -8px rgba(34,211,238,.9),inset 0 0 24px rgba(34,211,238,.15);
transition:all .3s ease;margin-bottom:14px}}
.btn:hover{{transform:translateY(-2px);
box-shadow:0 0 60px -6px rgba(34,211,238,1),inset 0 0 32px rgba(34,211,238,.25)}}
.btn:active{{transform:scale(.98)}}
.manual{{font-family:'JetBrains Mono',monospace;font-size:11px;color:#94a3b8;
line-height:1.9;padding-left:0;margin:12px 0 0;list-style:none}}
.manual li::before{{content:'▸ ';color:#22d3ee}}
pre{{background:#020617;border:1px solid rgba(34,211,238,.15);border-radius:16px;
padding:20px;color:#67e8f9;font-family:'JetBrains Mono',monospace;font-size:11px;
line-height:1.7;max-height:320px;overflow:auto;margin:0}}
.note{{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.15em;
color:#fbbf24;text-align:center;margin-top:8px}}
h2{{font-family:'Orbitron',sans-serif;font-size:14px;font-weight:700;
letter-spacing:.18em;color:#c084fc;margin:24px 0 12px;text-transform:uppercase}}
</style></head><body>
<div class="wrap">
  <div class="card">
    <h1>◆ DISPOSITIVO VINCULADO</h1>
    <p class="sub">SISTEMA AUTÓNOMO · AEGIS OS V11.2</p>

    <div class="row">
      <div class="row-label">Nombre asignado</div>
      <div class="row-value">{nombre_html}</div>
    </div>
    <div class="row">
      <div class="row-label">Código de vinculación (ya consumido)</div>
      <div class="row-value">{codigo_html}</div>
    </div>
    <div class="row">
      <div class="row-label">Token del agente (guárdalo como respaldo)</div>
      <div class="row-value token">{token_html}</div>
    </div>

    <h2>▶ Instalación automática</h2>
    <a class="btn" download="aegis_agente.py" href="{data_url}">
      📥 Descargar Agente e Instalar
    </a>
    <p class="note">Haz clic en el botón y ejecuta el archivo descargado.</p>

    <h2>▶ Instrucciones por sistema</h2>
    <ul class="manual">
      <li><b>Windows:</b> doble clic sobre <code>aegis_agente.py</code> (requiere Python 3.10+).</li>
      <li><b>macOS / Linux:</b> <code>python3 aegis_agente.py</code> en la terminal.</li>
      <li><b>Modo silencioso Windows:</b> <code>pythonw aegis_agente.py</code></li>
      <li><b>Autostart Linux:</b> añade <code>@reboot python3 /ruta/aegis_agente.py</code> con <code>crontab -e</code></li>
    </ul>

    <h2>▶ Código fuente (copy-paste manual)</h2>
    <pre>{script_html}</pre>
  </div>
</div>
</body></html>"""


# ---------------------------------------------------------------------------
# HEALTH & INTERFAZ WEB
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    try:
        with db() as conexion:
            conexion.ejecutar("SELECT 1")
            conexion.uno()
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "version": "11.2",
        "db": "up" if db_ok else "down",
        "motor": "postgres" if DATABASE_URL else "sqlite",
        "modo_comercial": MODO_COMERCIAL,
        "dias_suscripcion": DIAS_SUSCRIPCION,
    }


@app.get("/")
def cargar_interfaz():
    if not os.path.exists("index.html"):
        raise HTTPException(status_code=404, detail="No se encontró index.html")
    return FileResponse("index.html")


# ---------------------------------------------------------------------------
# CASAS
# ---------------------------------------------------------------------------
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
            "INSERT INTO usuarios (casa_id, nombre, pin_hash, salt, rol, avatar) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (casa_id, data.nombre_admin, hash_pin(data.pin_admin, salt), salt,
             ROL_ADMIN_CASA, data.avatar_admin),
        )
        conexion.commit()

    log.info("Casa '%s' creada con código %s", data.nombre_casa, codigo)
    return {"mensaje": "Casa creada exitosamente", "codigo_invitacion": codigo}


@app.get("/casas/{codigo}")
def obtener_casa(codigo: str):
    casa = buscar_casa_por_codigo(codigo)
    if not casa:
        raise HTTPException(status_code=404, detail="No existe ninguna casa con ese código")
    return {
        "id": casa["id"],
        "nombre": casa["nombre"],
        "codigo_invitacion": casa["codigo_invitacion"],
    }


@app.get("/casas/{codigo}/usuarios")
def usuarios_de_casa(codigo: str):
    casa = buscar_casa_por_codigo(codigo)
    if not casa:
        raise HTTPException(status_code=404, detail="No existe ninguna casa con ese código")
    with db() as conexion:
        conexion.ejecutar(
            "SELECT id, nombre, avatar, rol FROM usuarios WHERE casa_id = ?",
            (casa["id"],),
        )
        return conexion.todos()


@app.post("/casas/{codigo}/usuarios/crear")
def crear_usuario_en_casa(codigo: str, data: NuevoPerfilSchema):
    casa = buscar_casa_por_codigo(codigo)
    if not casa:
        raise HTTPException(status_code=404, detail="No existe ninguna casa con ese código")
    if len(data.pin) < 4:
        raise HTTPException(status_code=400, detail="El PIN debe tener al menos 4 dígitos")

    with db() as conexion:
        salt = generar_salt()
        conexion.ejecutar(
            "INSERT INTO usuarios (casa_id, nombre, pin_hash, salt, rol, avatar) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (casa["id"], data.nombre, hash_pin(data.pin, salt), salt, ROL_USUARIO, data.avatar),
        )
        conexion.commit()
    return {"mensaje": "Perfil creado exitosamente"}


@app.get("/perfil/me")
def mi_perfil(usuario: dict = Depends(verificar_pin)):
    return {
        "id": usuario["id"],
        "nombre": usuario["nombre"],
        "avatar": usuario["avatar"],
        "rol": usuario["rol"],
        "casa_id": usuario["casa_id"],
    }


@app.put("/perfil")
def editar_perfil(data: EditarPerfilSchema, usuario: dict = Depends(verificar_pin)):
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


# ---------------------------------------------------------------------------
# GESTIÓN DE USUARIOS (ADMIN_CASA)
# ---------------------------------------------------------------------------
@app.put("/usuarios/{user_id}/rol")
def cambiar_rol_usuario(
    user_id: int,
    data: CambiarRolSchema,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    if user_id == usuario["id"]:
        raise HTTPException(status_code=400, detail="No puedes cambiar tu propio rol")

    objetivo = obtener_usuario_de_la_casa(user_id, usuario)
    if objetivo["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="No puedes modificar al Master Admin")

    with db() as conexion:
        conexion.ejecutar("UPDATE usuarios SET rol = ? WHERE id = ?", (data.rol, user_id))
        conexion.commit()

    log.info("Admin %s cambió rol de %s a %s", usuario["nombre"], objetivo["nombre"], data.rol)
    return {"mensaje": f"Rol de '{objetivo['nombre']}' actualizado a {data.rol}"}


@app.delete("/usuarios/{user_id}")
def eliminar_usuario(
    user_id: int,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    if user_id == usuario["id"]:
        raise HTTPException(status_code=400, detail="No puedes eliminarte a ti mismo")

    objetivo = obtener_usuario_de_la_casa(user_id, usuario)
    if objetivo["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="No puedes eliminar al Master Admin")

    with db() as conexion:
        conexion.ejecutar("DELETE FROM usuarios WHERE id = ?", (user_id,))
        conexion.commit()

    log.info("Usuario '%s' eliminado por admin %s", objetivo["nombre"], usuario["nombre"])
    return {"mensaje": f"Usuario '{objetivo['nombre']}' eliminado"}


# ---------------------------------------------------------------------------
# MASTER ADMIN
# ---------------------------------------------------------------------------
@app.get("/master/estadisticas")
def estadisticas_globales(usuario: dict = Depends(requerir_rol(ROL_MASTER))):
    with db() as conexion:
        conexion.ejecutar("SELECT COUNT(*) AS total FROM casas")
        total_casas = conexion.uno()["total"]

        conexion.ejecutar("SELECT COUNT(*) AS total FROM usuarios")
        total_usuarios = conexion.uno()["total"]

        conexion.ejecutar("SELECT COUNT(*) AS total FROM dispositivos")
        total_dispositivos = conexion.uno()["total"]

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
        "resumen": {
            "total_casas": total_casas,
            "total_usuarios": total_usuarios,
            "total_dispositivos": total_dispositivos,
        },
        "salud_servidor": telemetria_del_servidor_local(),
        "casas": casas,
        "modo_comercial": MODO_COMERCIAL,
    }


# ---------------------------------------------------------------------------
# DISPOSITIVOS
# ---------------------------------------------------------------------------
@app.get("/dispositivos")
def listar_dispositivos(usuario: dict = Depends(verificar_pin)):
    if usuario["rol"] == ROL_MASTER:
        raise HTTPException(
            status_code=403,
            detail="El Master Admin usa /master/estadisticas, no gestiona dispositivos de una casa",
        )

    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM dispositivos WHERE casa_id = ?",
            (usuario["casa_id"],),
        )
        filas = conexion.todos()

    resultado = []
    for d in filas:
        if usuario["rol"] == ROL_USUARIO and d["asignado_a"] != usuario["id"]:
            continue

        if es_equipo_local(d):
            specs = telemetria_del_servidor_local()
        else:
            specs = {
                "cpu": d["cpu"] or "Sin datos (agente no conectado)",
                "ram": d["ram"] or "Sin datos",
                "disco": d["disco"] or "Sin datos",
                "bateria": d["bateria"] or "Sin datos",
                "procesos": d["procesos"] or 0,
            }

        resultado.append({
            "id": d["id"], "nombre": d["nombre"], "tipo": d["tipo"],
            "ip": d["ip"], "mac": d["mac"], "ubicacion": d["ubicacion"],
            "lat": d["lat"], "lng": d["lng"], "estado": d["estado"],
            "asignado_a": d["asignado_a"], "es_local": es_equipo_local(d),
            "specs": specs,
        })
    return resultado


@app.put("/dispositivos/{dev_id}/gps")
def actualizar_gps(
    dev_id: int,
    data: UbicacionUpdateSchema,
    usuario: dict = Depends(verificar_pin),
):
    obtener_dispositivo_de_la_casa(dev_id, usuario)
    with db() as conexion:
        conexion.ejecutar(
            "UPDATE dispositivos SET lat = ?, lng = ?, ubicacion = 'GPS actualizado' WHERE id = ?",
            (data.lat, data.lng, dev_id),
        )
        conexion.commit()
    return {"mensaje": "Coordenadas actualizadas"}


@app.post("/dispositivos/agregar")
def agregar_dispositivo_manual(
    data: DispositivoSchema,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    if data.mac and not mac_es_valida(data.mac):
        raise HTTPException(status_code=400, detail="La MAC no tiene un formato válido")

    with db() as conexion:
        conexion.ejecutar(
            "INSERT INTO dispositivos (casa_id, nombre, tipo, ip, mac, ubicacion, estado) "
            "VALUES (?, ?, ?, ?, ?, 'Dispositivo de red', 'ONLINE')",
            (usuario["casa_id"], data.nombre, data.tipo, data.ip, data.mac or ""),
        )
        conexion.commit()
    return {"mensaje": "Dispositivo agregado"}


@app.put("/dispositivos/{dev_id}")
def editar_dispositivo(
    dev_id: int,
    data: DispositivoSchema,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    if data.mac and not mac_es_valida(data.mac):
        raise HTTPException(status_code=400, detail="La MAC no tiene un formato válido")

    obtener_dispositivo_de_la_casa(dev_id, usuario)
    with db() as conexion:
        conexion.ejecutar(
            "UPDATE dispositivos SET nombre = ?, ip = ?, mac = ?, tipo = ? WHERE id = ?",
            (data.nombre, data.ip, data.mac or "", data.tipo, dev_id),
        )
        conexion.commit()
    return {"mensaje": "Dispositivo actualizado"}


@app.delete("/dispositivos/{dev_id}")
def eliminar_dispositivo(
    dev_id: int,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    dispositivo = obtener_dispositivo_de_la_casa(dev_id, usuario)
    with db() as conexion:
        conexion.ejecutar("DELETE FROM comandos WHERE dispositivo_id = ?", (dev_id,))
        conexion.ejecutar("DELETE FROM dispositivos WHERE id = ?", (dev_id,))
        conexion.commit()
    log.info("Dispositivo '%s' eliminado por admin %s", dispositivo["nombre"], usuario["nombre"])
    return {"mensaje": f"Dispositivo '{dispositivo['nombre']}' eliminado"}


# --- Vinculación por código ------------------------------------------------
@app.post("/dispositivos/generar-codigo")
def generar_codigo(
    data: CodigoVinculacionSchema,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    with db() as conexion:
        codigo = generar_codigo_vinculacion()
        conexion.ejecutar("SELECT 1 FROM codigos_vinculacion WHERE codigo = ?", (codigo,))
        while conexion.uno():
            codigo = generar_codigo_vinculacion()
            conexion.ejecutar("SELECT 1 FROM codigos_vinculacion WHERE codigo = ?", (codigo,))

        expira_en = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
        conexion.ejecutar(
            "INSERT INTO codigos_vinculacion (casa_id, codigo, nombre_sugerido, expira_en, usado) "
            "VALUES (?, ?, ?, ?, 0)",
            (usuario["casa_id"], codigo, data.nombre_dispositivo or "", expira_en),
        )
        conexion.commit()

    return {"codigo": codigo, "expira_en": expira_en, "valido_minutos": 10}


@app.post("/dispositivos/vincular-con-codigo")
def vincular_con_codigo(data: VincularConCodigoSchema):
    if data.mac and not mac_es_valida(data.mac):
        raise HTTPException(status_code=400, detail="La MAC no tiene un formato válido")

    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM codigos_vinculacion WHERE codigo = ?",
            (data.codigo,),
        )
        fila = conexion.uno()

        if not fila:
            raise HTTPException(status_code=404, detail="Código no encontrado")
        if fila["usado"]:
            raise HTTPException(status_code=400, detail="Este código ya fue utilizado")
        if datetime.fromisoformat(fila["expira_en"]) < datetime.utcnow():
            raise HTTPException(
                status_code=400,
                detail="El código expiró, genera uno nuevo desde el panel",
            )

        token = secrets.token_hex(16)
        conexion.ejecutar(
            "INSERT INTO dispositivos "
            "(casa_id, nombre, tipo, ip, mac, token, ubicacion, estado) "
            "VALUES (?, ?, ?, ?, ?, ?, 'Pendiente de ubicación', 'ONLINE')",
            (fila["casa_id"], data.nombre, data.tipo, data.ip, data.mac or "", token),
            es_insert=True,
        )
        dispositivo_id = conexion.id_insertado()
        conexion.ejecutar(
            "UPDATE codigos_vinculacion SET usado = 1 WHERE id = ?",
            (fila["id"],),
        )
        conexion.commit()

    log.info("Dispositivo '%s' vinculado (id=%s)", data.nombre, dispositivo_id)
    return {
        "mensaje": "Dispositivo vinculado correctamente",
        "dispositivo_id": dispositivo_id,
        "token_agente": token,
    }


# --- Instalación autónoma (sin USBs) ---------------------------------------
@app.get("/vincular/{codigo}", response_class=HTMLResponse)
def vincular_autonomo(codigo: str, request: Request):
    codigo = codigo.strip()
    if not codigo or len(codigo) != 6 or not codigo.isdigit():
        return HTMLResponse(
            _pagina_error("Código inválido", "El código debe tener 6 dígitos."),
            status_code=400,
        )

    with db() as conexion:
        conexion.ejecutar(
            "SELECT * FROM codigos_vinculacion WHERE codigo = ?",
            (codigo,),
        )
        fila = conexion.uno()

        if not fila:
            return HTMLResponse(
                _pagina_error("Código no encontrado", "Verifica el código de 6 dígitos que te compartió el Administrador."),
                status_code=404,
            )
        if fila["usado"]:
            return HTMLResponse(
                _pagina_error("Código ya utilizado", "Este código ya fue consumido. Pide uno nuevo desde el panel."),
                status_code=400,
            )
        try:
            expira = datetime.fromisoformat(fila["expira_en"])
        except (ValueError, TypeError):
            expira = datetime.utcnow() - timedelta(seconds=1)
        if expira < datetime.utcnow():
            return HTMLResponse(
                _pagina_error("Código expirado", "El código caducó a los 10 minutos. Genera uno nuevo desde el panel."),
                status_code=400,
            )

        nombre_auto = (fila["nombre_sugerido"] or "").strip()
        if not nombre_auto:
            nombre_auto = f"Equipo-{secrets.token_hex(2).upper()}"

        ip_visitante = "desconocida"
        if request.client and request.client.host:
            ip_visitante = request.client.host

        token = secrets.token_hex(16)

        conexion.ejecutar(
            "INSERT INTO dispositivos "
            "(casa_id, nombre, tipo, ip, mac, token, ubicacion, estado) "
            "VALUES (?, ?, 'pc', ?, '', ?, 'Pendiente de ubicación', 'ONLINE')",
            (fila["casa_id"], nombre_auto, ip_visitante, token),
            es_insert=True,
        )
        dispositivo_id = conexion.id_insertado()
        conexion.ejecutar(
            "UPDATE codigos_vinculacion SET usado = 1 WHERE id = ?",
            (fila["id"],),
        )
        conexion.commit()

    base = _url_base(request)
    script = _construir_script_agente(base, token)

    log.info(
        "Vinculación autónoma: dispositivo_id=%s, casa_id=%s, ip_origen=%s",
        dispositivo_id, fila["casa_id"], ip_visitante,
    )

    return HTMLResponse(_pagina_vincular(nombre_auto, token, codigo, script))


# --- Energía y automatización ----------------------------------------------
@app.post("/dispositivos/{dev_id}/energia")
def controlar_energia(
    dev_id: int,
    data: AccionEnergia,
    usuario: dict = Depends(verificar_pin),
):
    if usuario["rol"] == ROL_MASTER:
        raise HTTPException(status_code=403, detail="El Master Admin no controla dispositivos de casas")
    if usuario["rol"] == ROL_GESTOR_CASA:
        raise HTTPException(status_code=403, detail="Tu rol permite supervisión, no control de energía")

    dispositivo = obtener_dispositivo_de_la_casa(dev_id, usuario)
    if not puede_controlar_energia(usuario, dispositivo):
        raise HTTPException(status_code=403, detail="No tienes permiso sobre este dispositivo")

    accion = data.accion.upper()

    if es_equipo_local(dispositivo):
        return ejecutar_comando_local(accion)

    if accion == "ENCENDER":
        if not dispositivo["mac"]:
            raise HTTPException(
                status_code=400,
                detail="Este dispositivo no tiene MAC registrada para Wake-on-LAN",
            )
        try:
            enviar_wol(dispositivo["mac"])
            return {"mensaje": "Paquete Wake-on-LAN enviado a la red local"}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    encolar_comando(dev_id, accion)
    return {
        "mensaje": f"Comando '{accion}' encolado. Se ejecutará cuando el Agente Local lo reciba."
    }


@app.post("/dispositivos/{dev_id}/enchufe")
def controlar_enchufe(
    dev_id: int,
    data: AccionEnchufeSchema,
    usuario: dict = Depends(requerir_rol(ROL_ADMIN_CASA)),
):
    dispositivo = obtener_dispositivo_de_la_casa(dev_id, usuario)
    if not dispositivo["webhook_url"]:
        raise HTTPException(
            status_code=400,
            detail="Este dispositivo no tiene una URL de webhook configurada",
        )
    if requests is None:
        raise HTTPException(status_code=500, detail="Falta la librería 'requests' en el servidor")
    try:
        requests.post(
            dispositivo["webhook_url"],
            json={"accion": data.accion.upper()},
            timeout=5,
        )
        return {"mensaje": f"Señal '{data.accion.upper()}' enviada al enchufe"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar al enchufe: {e}")


# ---------------------------------------------------------------------------
# MERCADO PAGO — WEBHOOK DE SUSCRIPCIÓN
# ---------------------------------------------------------------------------
@app.post("/api/v1/pagos/mercado-pago/webhook")
async def webhook_mercado_pago(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    tipo = (payload.get("type") or payload.get("topic") or "").lower()
    accion = (payload.get("action") or "").lower()

    if tipo != "payment" and "payment" not in tipo:
        return {"ok": True, "ignored": tipo or accion or "unknown"}

    data = payload.get("data") or {}
    payment_id = data.get("id") or payload.get("id") or payload.get("resource")
    external_ref = payload.get("external_reference") or data.get("external_reference")

    casa_id: Optional[int] = None
    if external_ref is not None:
        try:
            casa_id = int(str(external_ref).strip())
        except (ValueError, TypeError):
            casa_id = None

    if casa_id is None and MP_ACCESS_TOKEN and requests and payment_id:
        try:
            r = requests.get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
                headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
                timeout=10,
            )
            if r.ok:
                info = r.json()
                ref = info.get("external_reference")
                status = (info.get("status") or "").lower()
                if ref is not None and status in ("approved", "authorized"):
                    casa_id = int(str(ref).strip())
        except Exception as e:
            log.warning("No se pudo consultar el pago %s en MP: %s", payment_id, e)

    if casa_id is None:
        raise HTTPException(
            status_code=400,
            detail="No se pudo determinar la casa del pago (falta external_reference)",
        )

    casa = buscar_casa_por_id(casa_id)
    if not casa:
        raise HTTPException(status_code=404, detail="La casa referenciada no existe")

    ahora = datetime.utcnow().isoformat()
    with db() as conexion:
        conexion.ejecutar(
            "UPDATE casas SET creado_en = ? WHERE id = ?",
            (ahora, casa_id),
        )
        conexion.commit()

    log.info("Suscripción renovada para casa_id=%s tras pago MP (%s)", casa_id, payment_id)
    return {
        "ok": True,
        "casa_id": casa_id,
        "renovado_en": ahora,
        "mensaje": f"Suscripción renovada por {DIAS_SUSCRIPCION} días",
    }


# ---------------------------------------------------------------------------
# AGENTE LOCAL
# ---------------------------------------------------------------------------
def _dispositivo_por_token(token: str) -> Optional[dict]:
    with db() as conexion:
        conexion.ejecutar("SELECT * FROM dispositivos WHERE token = ?", (token,))
        return conexion.uno()


def _agente_autenticado(x_device_token: Optional[str]) -> dict:
    if not x_device_token:
        raise HTTPException(status_code=401, detail="Falta X-Device-Token")
    dispositivo = _dispositivo_por_token(x_device_token)
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
            conexion.ejecutar(
                "UPDATE comandos SET estado = 'ENVIADO' WHERE id = ?",
                (comando["id"],),
            )
            conexion.commit()

    if not comando:
        return {"comando_id": None, "accion": None}
    return {"comando_id": comando["id"], "accion": comando["accion"]}


@app.post("/agente/comando/{comando_id}/completado")
def agente_marcar_completado(comando_id: int, x_device_token: str = Header(None)):
    dispositivo = _agente_autenticado(x_device_token)
    with db() as conexion:
        conexion.ejecutar(
            "UPDATE comandos SET estado = 'EJECUTADO' "
            "WHERE id = ? AND dispositivo_id = ?",
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

    ip_origen = None
    if request.client and request.client.host:
        ip_origen = request.client.host

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


# ---------------------------------------------------------------------------
# ENTRYPOINT
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
