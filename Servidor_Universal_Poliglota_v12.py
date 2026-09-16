#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
    AEGIS OS v12.0 - DUAL HYBRID B2C/B2B SAAS MASTER (PERÚ)
    FastAPI + PWA + Java WebSockets (<30ms) + Mercado Pago Real + Cupones
    Modo Hogar (Radar) vs Modo Empresa/Colegio (Matriz de Aula en Tiempo Real)
    Motor de Analítica de Productividad & Gráficos Tácticos (Chart.js)
================================================================================
"""

import os
import sys
import json
import time
import random
import uuid
import hmac
import hashlib
import asyncio
import logging
import sqlite3
import datetime
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, Request, HTTPException, status, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Soporte PostgreSQL en Render / Railway / AWS
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_DISPONIBLE = True
except ImportError:
    POSTGRES_DISPONIBLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [AEGIS-SAAS] %(message)s")
logger = logging.getLogger("AegisMasterDual")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///aegis_v12_saas.db")
AEGIS_MODO_COMERCIAL = os.getenv("AEGIS_MODO_COMERCIAL", "true").lower() in ("true", "1", "yes")

AEGIS_MASTER_KEY = os.getenv("AEGIS_MASTER_KEY", "AEGIS-PADRE-SEGURA-2026")
# Llave de Mercado Pago Perú: Configurable desde Render/Railway con la cuenta oficial (APP_USR-...)
MERCADOPAGO_ACCESS_TOKEN = os.getenv("MERCADOPAGO_ACCESS_TOKEN", "TEST-0000000000000000-000000-00000000000000000000000000000000-000000000")
MERCADOPAGO_WEBHOOK_SECRET = os.getenv("MERCADOPAGO_WEBHOOK_SECRET", "")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_POSTGRES = DATABASE_URL.startswith("postgresql://")
GEO_CACHE: Dict[str, Dict[str, Any]] = {}

PLANES_COMERCIALES = {
    "BASICO": {"nombre": "Control Esencial", "precio_pen": 19.90, "max_dispositivos": 2},
    "PREMIUM": {"nombre": "Familiar Premium", "precio_pen": 49.90, "max_dispositivos": 5},
    "MILITAR": {"nombre": "Búnker Militar / Laboratorio", "precio_pen": 89.90, "max_dispositivos": 999}
}

# ==============================================================================
# 1. BASE DE DATOS HÍBRIDA MULTITENANT (AUDITORÍA EMPRESARIAL & ESCOLAR)
# ==============================================================================
class ConexionDB:
    def __init__(self):
        self.is_pg = IS_POSTGRES
        self.conn = None

    def __enter__(self):
        if self.is_pg:
            if not POSTGRES_DISPONIBLE:
                raise RuntimeError("El driver 'psycopg2' es requerido para PostgreSQL.")
            self.conn = psycopg2.connect(DATABASE_URL)
            self.conn.autocommit = False
            return self
        else:
            db_path = DATABASE_URL.replace("sqlite:///", "")
            if not os.path.isabs(db_path):
                db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_path)
            self.conn = sqlite3.connect(db_path, timeout=30.0)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL;")
            return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type is not None:
                self.conn.rollback()
            else:
                self.conn.commit()
            self.conn.close()

    def execute(self, query: str, params: tuple = ()) -> Any:
        sql = query.replace("?", "%s") if self.is_pg else query
        cursor = self.conn.cursor(cursor_factory=RealDictCursor) if self.is_pg else self.conn.cursor()
        cursor.execute(sql, params)
        return cursor

    def fetchone(self, query: str, params: tuple = ()) -> Optional[Dict[str, Any]]:
        cursor = self.execute(query, params)
        row = cursor.fetchone()
        return dict(row) if (row and self.is_pg) else ({key: row[key] for key in row.keys()} if row else None)

    def fetchall(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        cursor = self.execute(query, params)
        rows = cursor.fetchall()
        return [dict(r) for r in rows] if self.is_pg else [{key: r[key] for key in r.keys()} for r in rows]

def inicializar_bd():
    sentencias = [
        """CREATE TABLE IF NOT EXISTS familias (
            familia_id TEXT PRIMARY KEY,
            nombre_familia TEXT NOT NULL,
            tutor_responsable TEXT DEFAULT 'Administrador',
            tipo_cuenta TEXT DEFAULT 'CLIENTE',
            tipo_entorno TEXT DEFAULT 'HOGAR',
            plan_id TEXT DEFAULT 'PREMIUM',
            max_dispositivos INTEGER DEFAULT 5,
            dias_licencia INTEGER DEFAULT 30,
            estado_pago TEXT DEFAULT 'ACTIVO',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS registro_actividad_empresarial (
            id TEXT PRIMARY KEY,
            familia_id TEXT NOT NULL,
            dispositivo_id TEXT NOT NULL,
            usuario_actual TEXT NOT NULL,
            aplicacion_abierta TEXT NOT NULL,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS cupones_descuento (
            codigo TEXT PRIMARY KEY,
            porcentaje_descuento REAL NOT NULL,
            expira_en TIMESTAMP NOT NULL,
            activo INTEGER DEFAULT 1
        );""",
        """CREATE TABLE IF NOT EXISTS miembros_familia (
            miembro_id TEXT PRIMARY KEY,
            familia_id TEXT NOT NULL,
            nombre TEXT NOT NULL,
            rol TEXT DEFAULT 'ESTUDIANTE',
            estado_disciplinario TEXT DEFAULT 'REGULAR',
            minutos_saldo_recompensa INTEGER DEFAULT 0,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(familia_id) REFERENCES familias(familia_id) ON DELETE CASCADE
        );""",
        """CREATE TABLE IF NOT EXISTS claves_temporales (
            pin TEXT PRIMARY KEY,
            familia_id TEXT NOT NULL,
            expira_en TIMESTAMP NOT NULL,
            usado INTEGER DEFAULT 0,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS dispositivos (
            dispositivo_id TEXT PRIMARY KEY,
            familia_id TEXT NOT NULL,
            miembro_id TEXT,
            nombre_equipo TEXT NOT NULL,
            usuario_actual TEXT,
            ip_dinamica TEXT,
            latitud REAL DEFAULT -12.046374,
            longitud REAL DEFAULT -77.042793,
            ciudad TEXT DEFAULT 'Perú',
            cpu_uso REAL DEFAULT 0.0,
            ram_uso REAL DEFAULT 0.0,
            disco_uso REAL DEFAULT 0.0,
            alerta_reloj BOOLEAN DEFAULT 0,
            ventana_activa TEXT DEFAULT 'Escritorio / IDLE',
            estado_en_linea BOOLEAN DEFAULT 1,
            ultima_telemetria TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(familia_id) REFERENCES familias(familia_id) ON DELETE CASCADE
        );""",
        """CREATE TABLE IF NOT EXISTS tareas_familiares (
            tarea_id TEXT PRIMARY KEY,
            familia_id TEXT NOT NULL,
            miembro_id TEXT NOT NULL,
            descripcion TEXT NOT NULL,
            minutos_recompensa INTEGER DEFAULT 60,
            estado TEXT DEFAULT 'PENDIENTE',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS incidentes_seguridad (
            incidente_id TEXT PRIMARY KEY,
            familia_id TEXT NOT NULL,
            dispositivo_id TEXT NOT NULL,
            tipo TEXT NOT NULL,
            detalle TEXT NOT NULL,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS restricciones_web (
            id TEXT PRIMARY KEY,
            familia_id TEXT NOT NULL,
            dispositivo_id TEXT NOT NULL,
            dominio TEXT NOT NULL,
            tipo_bloqueo TEXT,
            expira_en TIMESTAMP,
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS comandos_pendientes (
            comando_id TEXT PRIMARY KEY,
            dispositivo_id TEXT NOT NULL,
            accion TEXT NOT NULL,
            parametros TEXT,
            estado TEXT DEFAULT 'PENDIENTE',
            creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );""",
        """CREATE TABLE IF NOT EXISTS registro_pagos (
            transaccion_id TEXT PRIMARY KEY,
            familia_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            monto REAL NOT NULL,
            moneda TEXT DEFAULT 'PEN',
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );"""
    ]
    with ConexionDB() as db:
        for q in sentencias:
            db.execute(q)

        try:
            db.execute("ALTER TABLE familias ADD COLUMN tipo_entorno TEXT DEFAULT 'HOGAR';")
        except Exception:
            pass
        try:
            db.execute("ALTER TABLE dispositivos ADD COLUMN ventana_activa TEXT DEFAULT 'Escritorio / IDLE';")
        except Exception:
            pass

        # Semilla Familiar B2C
        fam = db.fetchone("SELECT familia_id FROM familias WHERE familia_id = ?", ("FAMILIA-ALPHA-PERU",))
        if not fam:
            db.execute("""INSERT INTO familias 
                          (familia_id, nombre_familia, tutor_responsable, tipo_cuenta, tipo_entorno, plan_id, max_dispositivos, dias_licencia, estado_pago) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       ("FAMILIA-ALPHA-PERU", "Familia Quispe Mendoza", "Carlos Quispe (Tutor)", "CLIENTE", "HOGAR", "PREMIUM", 5, 30, "ACTIVO"))
            db.execute("INSERT INTO miembros_familia (miembro_id, familia_id, nombre, rol, estado_disciplinario, minutos_saldo_recompensa) VALUES (?, ?, ?, ?, ?, ?)",
                       ("MBR-01", "FAMILIA-ALPHA-PERU", "Joaquín", "SECUNDARIA", "REGULAR", 60))
            db.execute("INSERT INTO miembros_familia (miembro_id, familia_id, nombre, rol, estado_disciplinario, minutos_saldo_recompensa) VALUES (?, ?, ?, ?, ?, ?)",
                       ("MBR-02", "FAMILIA-ALPHA-PERU", "Valeria", "PRIMARIA", "REGULAR", 30))
            db.execute("""INSERT INTO dispositivos 
                          (dispositivo_id, familia_id, miembro_id, nombre_equipo, usuario_actual, ip_dinamica, latitud, longitud, ciudad, cpu_uso, ram_uso, disco_uso, alerta_reloj, ventana_activa, estado_en_linea) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       ("NODE-LIM-01", "FAMILIA-ALPHA-PERU", "MBR-01", "LAPTOP-JOAQUIN", "JoaquinQ", "190.237.14.88", -12.0931, -77.0465, "San Isidro, Lima", 26.5, 62.0, 48.0, 0, "Visual Studio Code", 1))

        # Semilla Corporativa B2B (Colegio)
        colegio = db.fetchone("SELECT familia_id FROM familias WHERE familia_id = ?", ("LAB-INFORMATICA-COLEGIO",))
        if not colegio:
            db.execute("""INSERT INTO familias 
                          (familia_id, nombre_familia, tutor_responsable, tipo_cuenta, tipo_entorno, plan_id, max_dispositivos, dias_licencia, estado_pago) 
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       ("LAB-INFORMATICA-COLEGIO", "Colegio San Martín - Lab 01", "Prof. Ramos", "CLIENTE", "EMPRESA", "MILITAR", 40, 90, "ACTIVO"))
            for i in range(1, 6):
                db.execute("""INSERT INTO dispositivos 
                              (dispositivo_id, familia_id, nombre_equipo, usuario_actual, ip_dinamica, latitud, longitud, ciudad, cpu_uso, ram_uso, disco_uso, alerta_reloj, ventana_activa, estado_en_linea) 
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                           (f"PC-LAB-{i:02d}", "LAB-INFORMATICA-COLEGIO", f"PC-ALUMNO-{i:02d}", f"Alumno_{i}", "192.168.10.100", -12.0463, -77.0427, "Lima Centro", 15.0 + i * 4, 35.0 + i * 3, 28.0, 0, "Google Chrome - Examen Virtual" if i % 2 == 0 else "Roblox Player (No autorizado)", 1))

        c1 = db.fetchone("SELECT codigo FROM cupones_descuento WHERE codigo = ?", ("AEGISYAPE50",))
        if not c1:
            expira_cupon = (datetime.datetime.utcnow() + datetime.timedelta(days=365)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute("INSERT INTO cupones_descuento (codigo, porcentaje_descuento, expira_en, activo) VALUES (?, ?, ?, 1)", ("AEGISYAPE50", 0.50, expira_cupon))
            db.execute("INSERT INTO cupones_descuento (codigo, porcentaje_descuento, expira_en, activo) VALUES (?, ?, ?, 1)", ("LANZAMIENTO", 1.00, expira_cupon))
            logger.info("Entorno dual B2C/B2B y cupones comerciales inicializados sin errores de bindings.")


# ==============================================================================
# 2. RESOLUCIÓN GEO-IP & WEBSOCKETS HUB
# ==============================================================================
def resolver_geo_ip(ip: str) -> Dict[str, Any]:
    if not ip or ip in ("127.0.0.1", "localhost") or ip.startswith("192.168.") or ip.startswith("10."):
        return {"ciudad": "Red Local / Perú", "lat": -12.046374, "lon": -77.042793}
    if ip in GEO_CACHE:
        return GEO_CACHE[ip]
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,country,regionName,city,lat,lon"
        req = urllib.request.Request(url, headers={"User-Agent": "AegisOS-GeoResolver/12.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("status") == "success":
                geo = {
                    "ciudad": f"{data.get('city', '')}, {data.get('regionName', '')} ({data.get('country', 'PE')})",
                    "lat": float(data.get("lat", -12.046374)),
                    "lon": float(data.get("lon", -77.042793))
                }
                GEO_CACHE[ip] = geo
                return geo
    except Exception:
        pass
    return {"ciudad": "Ubicación Dinámica (Perú)", "lat": -12.046374, "lon": -77.042793}

def extraer_ip_real(request: Request) -> str:
    for h in ["cf-connecting-ip", "x-real-ip", "x-forwarded-for"]:
        val = request.headers.get(h)
        if val:
            return val.split(",")[0].strip()
    return request.client.host if request.client else "127.0.0.1"

class SocketManager:
    def __init__(self):
        self.connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, ws: WebSocket, familia_id: str):
        await ws.accept()
        self.connections.setdefault(familia_id, []).append(ws)

    def disconnect(self, ws: WebSocket, familia_id: str):
        if familia_id in self.connections and ws in self.connections[familia_id]:
            self.connections[familia_id].remove(ws)

    async def broadcast(self, familia_id: str, data: dict):
        if familia_id in self.connections:
            for ws in list(self.connections[familia_id]):
                try:
                    await ws.send_json(data)
                except Exception:
                    self.disconnect(ws, familia_id)

ws_hub = SocketManager()

# ==============================================================================
# 3. FASTAPI & SEGURIDAD MULTI-ENTORNO
# ==============================================================================
app = FastAPI(title="Aegis OS v12.0 Dual Master", version="12.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def verificar_autorizacion_tutor(x_aegis_token: Optional[str] = Header(None)):
    if not x_aegis_token or x_aegis_token != AEGIS_MASTER_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="ACCESO DENEGADO: Clave de Tutor inválida.")
    return True

@app.on_event("startup")
async def iniciar_watchdogs_fondo():
    async def watchdog_loop():
        while True:
            await asyncio.sleep(12)
            ahora = datetime.datetime.utcnow()
            limite_desconexion = (ahora - datetime.timedelta(seconds=25)).strftime("%Y-%m-%d %H:%M:%S")

            try:
                with ConexionDB() as db:
                    db.execute("UPDATE dispositivos SET estado_en_linea = 0 WHERE ultima_telemetria < ? AND estado_en_linea = 1", (limite_desconexion,))
                    
                    familias_vencidas = db.fetchall("""
                        SELECT familia_id FROM familias 
                        WHERE dias_licencia <= 0 
                          AND estado_pago != 'SUSPENDIDO'
                          AND tipo_cuenta NOT IN ('FAMILIA_SISTEMA', 'DEVELOPER')
                    """)
                    for fam in familias_vencidas:
                        fam_id = fam["familia_id"]
                        db.execute("UPDATE familias SET estado_pago = 'SUSPENDIDO' WHERE familia_id = ?", (fam_id,))
                        await ws_hub.broadcast(fam_id, {
                            "tipo": "ORDEN_MASIVA_SIMULTANEA",
                            "accion": "BLOQUEAR_SISTEMA",
                            "motivo": "SUSCRIPCION_VENCIDA",
                            "mensaje": "Suscripción Aegis OS vencida. Solicite al tutor o administrador la renovación."
                        })
            except Exception as e:
                logger.error(f"Error watchdog: {e}")

    asyncio.create_task(watchdog_loop())

# ==============================================================================
# 4. MODELOS PYDANTIC
# ==============================================================================
class LoginAuthInput(BaseModel):
    token: str

class VincularPinInput(BaseModel):
    pin: str
    nombre_equipo: str
    usuario_actual: Optional[str] = "USUARIO"

class TelemetriaInput(BaseModel):
    familia_id: str
    dispositivo_id: str
    nombre_equipo: str
    usuario_actual: str = "USUARIO"
    cpu_uso: float
    ram_uso: float
    disco_uso: float
    client_epoch: Optional[float] = None
    ventana_activa: Optional[str] = "Escritorio / IDLE"
    latitud: Optional[float] = None
    longitud: Optional[float] = None

class RestriccionInput(BaseModel):
    familia_id: str
    dispositivo_id: str
    dominio: str
    tipo_bloqueo: str
    minutos_castigo: Optional[int] = 60

class AccionEnergiaInput(BaseModel):
    familia_id: str
    accion: str = Field(..., pattern="^(APAGAR|REINICIAR|BLOQUEAR_SISTEMA|DESBLOQUEAR|ENCENDER|CONGELAR_PANTALLA|DESCONGELAR)$")
    parametros: Optional[str] = "{}"

class AccionMasivaInput(BaseModel):
    familia_id: str
    dispositivos_ids: List[str]
    accion: str = Field(..., pattern="^(APAGAR|REINICIAR|BLOQUEAR_SISTEMA|DESBLOQUEAR|ENCENDER|CONGELAR_PANTALLA|DESCONGELAR)$")
    parametros: Optional[str] = "{}"


class CrearPreferenciaInput(BaseModel):
    familia_id: str
    plan_id: str = Field(..., pattern="^(BASICO|PREMIUM|MILITAR)$")
    codigo_cupon: Optional[str] = None


class NuevaTareaInput(BaseModel):
    familia_id: str
    miembro_id: str
    descripcion: str
    minutos_recompensa: int = 60

# ==============================================================================
# 5. TELEMETRÍA CON AUDITORÍA EMPRESARIAL & WEBSOCKETS EN TIEMPO REAL
# ==============================================================================
@app.post("/agente/telemetria")
async def telemetria(data: TelemetriaInput, request: Request):
    ip = extraer_ip_real(request)
    ahora_dt = datetime.datetime.utcnow()
    ahora_str = ahora_dt.strftime("%Y-%m-%d %H:%M:%S")
    server_epoch = time.time()
    alerta_reloj = False

    # Time-Drift Shield: Detección de adulteración horaria
    if data.client_epoch is not None:
        diferencia_segundos = abs(server_epoch - data.client_epoch)
        if diferencia_segundos > 180:
            alerta_reloj = True
            with ConexionDB() as db:
                db.execute("INSERT INTO incidentes_seguridad (incidente_id, familia_id, dispositivo_id, tipo, detalle) VALUES (?, ?, ?, ?, ?)",
                           (f"INC-{uuid.uuid4().hex[:6].upper()}", data.familia_id, data.dispositivo_id, "MANIPULACION_RELOJ", f"Desviación de reloj: {int(diferencia_segundos)}s"))

            await ws_hub.broadcast(data.familia_id, {
                "tipo": "ORDEN_DIRECTA",
                "dispositivo_id": data.dispositivo_id,
                "accion": "BLOQUEAR_SISTEMA",
                "motivo": "MANIPULACION_RELOJ",
                "mensaje": "Manipulación de reloj detectada. Terminal bloqueado por seguridad."
            })

    geo = resolver_geo_ip(ip) if (data.latitud is None or data.longitud is None) else {"lat": data.latitud, "lon": data.longitud, "ciudad": resolver_geo_ip(ip)["ciudad"]}
    ventana_actual = (data.ventana_activa or "Escritorio / IDLE").strip()

    with ConexionDB() as db:
        cliente = db.fetchone("SELECT tipo_entorno FROM familias WHERE familia_id = ?", (data.familia_id,))
        entorno = cliente["tipo_entorno"] if cliente else "HOGAR"

        if entorno == "EMPRESA" and ventana_actual and ventana_actual != "Escritorio / IDLE":
            db.execute("""INSERT INTO registro_actividad_empresarial 
                          (id, familia_id, dispositivo_id, usuario_actual, aplicacion_abierta) 
                          VALUES (?, ?, ?, ?, ?)""",
                       (f"LOG-{uuid.uuid4().hex[:6].upper()}", data.familia_id, data.dispositivo_id, data.usuario_actual, ventana_actual))

        db.execute("""UPDATE dispositivos SET 
                      nombre_equipo = ?, usuario_actual = ?, ip_dinamica = ?,
                      latitud = ?, longitud = ?, ciudad = ?,
                      cpu_uso = ?, ram_uso = ?, disco_uso = ?, estado_en_linea = 1, 
                      alerta_reloj = ?, ventana_activa = ?, ultima_telemetria = ? 
                      WHERE dispositivo_id = ? AND familia_id = ?""",
                   (data.nombre_equipo, data.usuario_actual, ip, geo["lat"], geo["lon"], geo["ciudad"],
                    data.cpu_uso, data.ram_uso, data.disco_uso, 1 if alerta_reloj else 0, ventana_actual, ahora_str, data.dispositivo_id, data.familia_id))

    await ws_hub.broadcast(data.familia_id, {
        "tipo": "ACTUALIZACION_TELEMETRIA", 
        "dispositivo_id": data.dispositivo_id, 
        "alerta_reloj": alerta_reloj,
        "ventana_activa": ventana_actual
    })
    return {"status": "ACK", "server_epoch": server_epoch}

@app.post("/api/v1/admin/acreditar-licencia")
async def acreditar_licencia_manual(familia_id: str, dias: int, x_aegis_token: Optional[str] = Header(None)):
    """Permite acreditar saldo manualmente tras recibir transferencias bancarias o efectivo de colegios/empresas."""
    verificar_autorizacion_tutor(x_aegis_token)
    with ConexionDB() as db:
        cliente = db.fetchone("SELECT familia_id FROM familias WHERE familia_id = ?", (familia_id,))
        if not cliente:
            raise HTTPException(status_code=404, detail="Institución o familia no encontrada.")
        db.execute(
            "UPDATE familias SET dias_licencia = dias_licencia + ?, estado_pago = 'ACTIVO' WHERE familia_id = ?",
            (dias, familia_id)
        )
    await ws_hub.broadcast(familia_id, {"tipo": "LICENCIA_ACTUALIZADA", "dias_agregados": dias})
    logger.info(f"ACREDITACIÓN MANUAL B2B: {dias} días inyectados a {familia_id}.")
    return {"status": "SUCCESS", "detail": f"Se agregaron {dias} días de cobertura a {familia_id}."}

@app.post("/api/v1/auth/verificar-token")
async def verificar_token_padre(payload: LoginAuthInput):
    if payload.token.strip() == AEGIS_MASTER_KEY:
        return {"status": "SUCCESS", "token": AEGIS_MASTER_KEY}
    raise HTTPException(status_code=401, detail="Clave Maestra incorrecta.")

@app.post("/api/v1/pagos/crear-preferencia")
async def crear_preferencia_mp(payload: CrearPreferenciaInput, request: Request, x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    plan = PLANES_COMERCIALES[payload.plan_id]
    precio_final = plan["precio_pen"]
    external_ref = f"{payload.familia_id}:{payload.plan_id}"

    if payload.codigo_cupon:
        ahora = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        with ConexionDB() as db:
            cup = db.fetchone("SELECT porcentaje_descuento FROM cupones_descuento WHERE codigo = ? AND activo = 1 AND expira_en > ?",
                              (payload.codigo_cupon.strip().upper(), ahora))
            if cup:
                precio_final = max(1.00, round(precio_final * (1.0 - cup["porcentaje_descuento"]), 2))

    if MERCADOPAGO_ACCESS_TOKEN and not MERCADOPAGO_ACCESS_TOKEN.startswith("TEST-0000"):
        mp_payload = {
            "items": [{
                "title": f"Aegis OS - Plan {plan['nombre']}",
                "quantity": 1,
                "currency_id": "PEN",
                "unit_price": precio_final
            }],
            "external_reference": external_ref,
            "back_urls": {"success": str(request.base_url), "failure": str(request.base_url)},
            "auto_return": "approved"
        }
        try:
            req = urllib.request.Request(
                "https://api.mercadopago.com/checkout/preferences",
                data=json.dumps(mp_payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {MERCADOPAGO_ACCESS_TOKEN}"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                return {"status": "SUCCESS", "init_point": res.get("init_point"), "monto_cobrado": precio_final}
        except Exception as e:
            logger.error(f"Falla Mercado Pago: {e}")

    checkout_url = f"/api/v1/pagos/checkout-simulado?external_reference={external_ref}&monto={precio_final}"
    return {"status": "SUCCESS", "init_point": checkout_url, "monto_cobrado": precio_final}

@app.get("/api/v1/pagos/checkout-simulado")
async def simular_checkout(external_reference: str, monto: float):
    fam_id, plan_id = external_reference.split(":")[:2]
    plan_info = PLANES_COMERCIALES.get(plan_id, PLANES_COMERCIALES["PREMIUM"])

    with ConexionDB() as db:
        tx_id = f"SIM-{int(time.time())}"
        db.execute("INSERT INTO registro_pagos (transaccion_id, familia_id, plan_id, monto) VALUES (?, ?, ?, ?)",
                   (tx_id, fam_id, plan_id, monto))
        db.execute("""UPDATE familias SET 
                      dias_licencia = dias_licencia + 30, 
                      plan_id = ?, 
                      max_dispositivos = ?, 
                      estado_pago = 'ACTIVO' 
                      WHERE familia_id = ?""",
                   (plan_id, plan_info["max_dispositivos"], fam_id))

    await ws_hub.broadcast(fam_id, {"tipo": "PAGO_CONFIRMADO", "plan": plan_info["nombre"]})
    return HTMLResponse(f"""
    <html><body style="background:#010409;color:#00ff66;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;">
        <h2>✔ PAGO REGISTRADO EN MERCADO PAGO PERÚ</h2>
        <p>Acreditado S/ {monto:.2f} PEN a la {fam_id} para el Plan {plan_info['nombre']}.</p>
        <a href="/" style="color:#00f0ff;margin-top:20px;text-decoration:none;border:1px solid #00f0ff;padding:10px 20px;border-radius:6px;">VOLVER AL PANEL</a>
    </body></html>
    """)

@app.post("/agente/vincular")
async def vincular_agente(payload: VincularPinInput, request: Request):
    ahora = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    pin_limpio = payload.pin.strip()

    with ConexionDB() as db:
        clave = db.fetchone("SELECT familia_id FROM claves_temporales WHERE pin = ? AND usado = 0 AND expira_en > ?", (pin_limpio, ahora))
        if not clave:
            raise HTTPException(status_code=403, detail="PIN inválido o expirado.")
        
        fam_id = clave["familia_id"]
        fam = db.fetchone("SELECT tipo_cuenta, plan_id, max_dispositivos, dias_licencia, estado_pago FROM familias WHERE familia_id = ?", (fam_id,))
        
        if fam["tipo_cuenta"] not in ('FAMILIA_SISTEMA', 'DEVELOPER'):
            if fam["estado_pago"] == "SUSPENDIDO" or fam["dias_licencia"] <= 0:
                raise HTTPException(status_code=402, detail="Suscripción suspendida.")
            total_devs = db.fetchone("SELECT COUNT(dispositivo_id) as total FROM dispositivos WHERE familia_id = ?", (fam_id,))["total"]
            if total_devs >= fam["max_dispositivos"]:
                raise HTTPException(status_code=403, detail=f"Límite alcanzado para el Plan {fam['plan_id']}.")

        dev_id = f"NODE-{uuid.uuid4().hex[:6].upper()}"
        ip = extraer_ip_real(request)
        geo = resolver_geo_ip(ip)

        db.execute("UPDATE claves_temporales SET usado = 1 WHERE pin = ?", (pin_limpio,))
        db.execute("""INSERT INTO dispositivos 
                      (dispositivo_id, familia_id, nombre_equipo, usuario_actual, ip_dinamica, latitud, longitud, ciudad, cpu_uso, ram_uso, disco_uso, ventana_activa, estado_en_linea, ultima_telemetria) 
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, 12.0, 40.0, 30.0, 'Escritorio / IDLE', 1, ?)""",
                   (dev_id, fam_id, payload.nombre_equipo, payload.usuario_actual, ip, geo["lat"], geo["lon"], geo["ciudad"], ahora))

    await ws_hub.broadcast(fam_id, {"tipo": "DISPOSITIVO_VINCULADO", "dispositivo_id": dev_id})
    return {"status": "PAIRED", "familia_id": fam_id, "dispositivo_id": dev_id, "ciudad": geo["ciudad"]}

@app.websocket("/ws/canal/{familia_id}")
async def socket_canal(ws: WebSocket, familia_id: str):
    await ws_hub.connect(ws, familia_id)
    try:
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        ws_hub.disconnect(ws, familia_id)

@app.post("/api/v1/familias/generar-pin")
async def generar_pin_otp(familia_id: str = "FAMILIA-ALPHA-PERU", x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    pin = f"{random.randint(100, 999)}-{random.randint(100, 999)}"
    expira_en = (datetime.datetime.utcnow() + datetime.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    with ConexionDB() as db:
        db.execute("INSERT INTO claves_temporales (pin, familia_id, expira_en, usado) VALUES (?, ?, ?, 0)",
                   (pin, familia_id, expira_en))
    return {"pin": pin, "expira_en": expira_en}

@app.post("/dispositivos/{dev_id}/restringir")
async def restringir(dev_id: str, payload: RestriccionInput, x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    r_id = f"RST-{uuid.uuid4().hex[:6].upper()}"
    expira = (datetime.datetime.utcnow() + datetime.timedelta(minutes=payload.minutos_castigo)).strftime("%Y-%m-%d %H:%M:%S") if payload.tipo_bloqueo == "TEMPORAL" else None
    with ConexionDB() as db:
        db.execute("INSERT INTO restricciones_web (id, familia_id, dispositivo_id, dominio, tipo_bloqueo, expira_en) VALUES (?, ?, ?, ?, ?, ?)",
                   (r_id, payload.familia_id, dev_id, payload.dominio.lower().strip(), payload.tipo_bloqueo, expira))
    await ws_hub.broadcast(payload.familia_id, {"tipo": "NUEVA_RESTRICCION_WEB"})
    return {"status": "SUCCESS"}

@app.delete("/api/v1/restricciones/{regla_id}")
async def eliminar_restriccion(regla_id: str, familia_id: str = "FAMILIA-ALPHA-PERU", x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    with ConexionDB() as db:
        db.execute("DELETE FROM restricciones_web WHERE id = ? AND familia_id = ?", (regla_id, familia_id))
    await ws_hub.broadcast(familia_id, {"tipo": "NUEVA_RESTRICCION_WEB"})
    return {"status": "REVOKED"}

@app.post("/dispositivos/{dev_id}/energia")
async def orden_energia(dev_id: str, payload: AccionEnergiaInput, x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    c_id = f"CMD-{uuid.uuid4().hex[:6].upper()}"
    with ConexionDB() as db:
        db.execute("INSERT INTO comandos_pendientes (comando_id, dispositivo_id, accion, parametros) VALUES (?, ?, ?, ?)",
                   (c_id, dev_id, payload.accion, payload.parametros))
    await ws_hub.broadcast(payload.familia_id, {"tipo": "ORDEN_DIRECTA", "dispositivo_id": dev_id, "accion": payload.accion})
    return {"status": "DISPATCHED"}

@app.post("/casas/dispositivos/accion-masiva")
async def accion_masiva(payload: AccionMasivaInput, x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    with ConexionDB() as db:
        for dev_id in payload.dispositivos_ids:
            c_id = f"MASS-{uuid.uuid4().hex[:6].upper()}"
            db.execute("INSERT INTO comandos_pendientes (comando_id, dispositivo_id, accion, parametros) VALUES (?, ?, ?, ?)",
                       (c_id, dev_id, payload.accion, payload.parametros))
    await ws_hub.broadcast(payload.familia_id, {"tipo": "ORDEN_MASIVA_SIMULTANEA", "accion": payload.accion, "afectados": payload.dispositivos_ids})
    return {"status": "MASS_SENT"}

@app.post("/api/v1/tareas/crear")
async def crear_tarea(payload: NuevaTareaInput, x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    t_id = f"TSK-{uuid.uuid4().hex[:6].upper()}"
    with ConexionDB() as db:
        db.execute("INSERT INTO tareas_familiares (tarea_id, familia_id, miembro_id, descripcion, minutos_recompensa) VALUES (?, ?, ?, ?, ?)",
                   (t_id, payload.familia_id, payload.miembro_id, payload.descripcion, payload.minutos_recompensa))
    await ws_hub.broadcast(payload.familia_id, {"tipo": "ACTUALIZACION_TAREAS"})
    return {"status": "SUCCESS", "tarea_id": t_id}

@app.post("/api/v1/tareas/{tarea_id}/aprobar")
async def aprobar_tarea(tarea_id: str, familia_id: str = "FAMILIA-ALPHA-PERU", x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    with ConexionDB() as db:
        tarea = db.fetchone("SELECT miembro_id, minutos_recompensa FROM tareas_familiares WHERE tarea_id = ? AND familia_id = ?", (tarea_id, familia_id))
        if not tarea:
            raise HTTPException(status_code=404, detail="Tarea no encontrada.")
        
        db.execute("UPDATE tareas_familiares SET estado = 'APROBADA' WHERE tarea_id = ?", (tarea_id,))
        db.execute("UPDATE miembros_familia SET minutos_saldo_recompensa = minutos_saldo_recompensa + ? WHERE miembro_id = ?",
                   (tarea["minutos_recompensa"], tarea["miembro_id"]))
        
    await ws_hub.broadcast(familia_id, {
        "tipo": "ORDEN_DIRECTA",
        "accion": "DESBLOQUEAR",
        "mensaje": f"Misión aprobada. Se han otorgado {tarea['minutos_recompensa']} minutos libres."
    })
    return {"status": "APPROVED", "minutos": tarea["minutos_recompensa"]}

@app.get("/api/v1/reportes/analitica-productividad")
async def obtener_analitica_aula(familia_id: str = "LAB-INFORMATICA-COLEGIO", x_aegis_token: Optional[str] = Header(None)):
    verificar_autorizacion_tutor(x_aegis_token)
    palabras_ocio = ["roblox", "minecraft", "steam", "youtube", "facebook", "tiktok", "juego", "poker"]
    tiempo_estudio = 0
    tiempo_ocio = 0
    with ConexionDB() as db:
        registros = db.fetchall("SELECT aplicacion_abierta FROM registro_actividad_empresarial WHERE familia_id = ?", (familia_id,))
        for reg in registros:
            app_name = (reg["aplicacion_abierta"] or "").lower()
            if any(ocio in app_name for ocio in palabras_ocio):
                tiempo_ocio += 12
            else:
                tiempo_estudio += 12
    total = tiempo_estudio + tiempo_ocio
    indice = round((tiempo_estudio / (total if total > 0 else 1)) * 100, 1)
    return {
        "status": "SUCCESS",
        "tiempo_estudio_minutos": round(tiempo_estudio / 60, 1),
        "tiempo_ocio_minutos": round(tiempo_ocio / 60, 1),
        "indice_productividad": indice
    }

@app.get("/api/v1/dashboard/estado")
async def obtener_estado(familia_id: str = "FAMILIA-ALPHA-PERU"):
    with ConexionDB() as db:
        familia = db.fetchone("SELECT * FROM familias WHERE familia_id = ?", (familia_id,))
        if not familia:
            raise HTTPException(status_code=404, detail="Cuenta no encontrada.")

        if familia["tipo_cuenta"] in ('FAMILIA_SISTEMA', 'DEVELOPER'):
            familia["estado_pago"] = "ACTIVO"
            familia["dias_licencia"] = 9999
        elif AEGIS_MODO_COMERCIAL and familia["estado_pago"] == "SUSPENDIDO":
            raise HTTPException(status_code=402, detail="Suscripción suspendida.")

        miembros = db.fetchall("SELECT * FROM miembros_familia WHERE familia_id = ? ORDER BY nombre ASC", (familia_id,))
        dispositivos = db.fetchall("""
            SELECT d.*, m.nombre as hijo_nombre 
            FROM dispositivos d 
            LEFT JOIN miembros_familia m ON d.miembro_id = m.miembro_id 
            WHERE d.familia_id = ? ORDER BY d.nombre_equipo ASC
        """, (familia_id,))
        restricciones = db.fetchall("SELECT * FROM restricciones_web WHERE familia_id = ? ORDER BY creado_en DESC", (familia_id,))
        tareas = db.fetchall("SELECT * FROM tareas_familiares WHERE familia_id = ? ORDER BY creado_en DESC", (familia_id,))

    return {
        "familia": familia,
        "miembros": miembros,
        "dispositivos": dispositivos,
        "restricciones": restricciones,
        "tareas": tareas,
        "planes": PLANES_COMERCIALES
    }

# ==============================================================================
# 6. AGENTE NATIVO JAVA CON CAPTURA DE VENTANA Y KEEP-ALIVE
# ==============================================================================
JAVA_AGENT_CODE = '''import java.io.BufferedReader;
import java.io.File;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.lang.management.ManagementFactory;
import java.lang.management.OperatingSystemMXBean;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URI;
import java.net.URL;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.nio.charset.StandardCharsets;
import java.util.Scanner;
import java.util.concurrent.CompletionStage;

public class AegisAgent {
    private static final String SERVER_URL = System.getenv().getOrDefault("AEGIS_SERVER_URL", "http://127.0.0.1:8000");
    private static String FAMILIA_ID = "FAMILIA-ALPHA-PERU";
    private static String DISPOSITIVO_ID = null;
    private static String ultimaVentanaDetectada = "Escritorio / IDLE";

    private static final String[] PROCESOS_PROHIBIDOS = {"roblox", "steam", "discord", "minecraft", "poker", "anime"};

    public static void main(String[] args) {
        System.out.println("====================================================");
        System.out.println("   AEGIS OS v12.0 // AGENTE RED EN MALLA ACTIVO     ");
        System.out.println("   Auditoría de Software y Reacción <30ms Lista     ");
        System.out.println("====================================================");

        if (DISPOSITIVO_ID == null) {
            Scanner scanner = new Scanner(System.in);
            System.out.print("[?] Ingrese el PIN OTP del Dashboard (ej: 482-910): ");
            String pin = scanner.nextLine().trim();
            if (!vincularConPin(pin)) {
                System.err.println("[X] Vinculación rechazada por el servidor.");
                return;
            }
        }

        System.out.println("[✔] ENLACE ACTIVO CON LA BASE CENTRAL.");
        conectarWebSocket();

        while (true) {
            try {
                inspeccionarVentanaYProcesos();
                enviarTelemetria();
                Thread.sleep(12000);
            } catch (Exception ignored) {}
        }
    }

    private static void conectarWebSocket() {
        try {
            String wsUri = SERVER_URL.replace("http://", "ws://").replace("https://", "wss://") + "/ws/canal/" + FAMILIA_ID;
            HttpClient client = HttpClient.newHttpClient();
            client.newWebSocketBuilder()
                .buildAsync(URI.create(wsUri), new WebSocket.Listener() {
                    @Override
                    public void onOpen(WebSocket webSocket) {
                        System.out.println("[⚡] CANAL WEBSOCKET CONECTADO (<30ms).");
                        WebSocket.Listener.super.onOpen(webSocket);
                    }

                    @Override
                    public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
                        procesarMensaje(data.toString());
                        return WebSocket.Listener.super.onText(webSocket, data, last);
                    }

                    @Override
                    public CompletionStage<?> onClose(WebSocket webSocket, int statusCode, String reason) {
                        new Thread(() -> { try { Thread.sleep(3000); conectarWebSocket(); } catch (Exception ignored) {} }).start();
                        return WebSocket.Listener.super.onClose(webSocket, statusCode, reason);
                    }
                });
        } catch (Exception e) {
            System.err.println("[!] Error WebSocket: " + e.getMessage());
        }
    }

    // Marco flotante para la directiva táctica "Atención al Frente"
    private static javax.swing.JFrame pantallaAtencion = null;

    private static void procesarMensaje(String res) {
        if (!res.contains(DISPOSITIVO_ID) && !res.contains("ORDEN_MASIVA_SIMULTANEA")) return;

        String so = System.getProperty("os.name").toLowerCase();
        System.out.println("[⚡] DIRECTIVA EN TIEMPO REAL RECIBIDA");

        try {
            if (res.contains("CONGELAR_PANTALLA")) {
                activarModoAtencionAlFrente();
            } else if (res.contains("DESCONGELAR") || res.contains("DESBLOQUEAR")) {
                desactivarModoAtencionAlFrente();
            } else if (res.contains("BLOQUEAR_SISTEMA")) {
                if (so.contains("win")) Runtime.getRuntime().exec("rundll32.exe user32.dll,LockWorkStation");
                else Runtime.getRuntime().exec("xdg-screensaver lock");
            } else if (res.contains("APAGAR")) {
                Runtime.getRuntime().exec(so.contains("win") ? "shutdown /s /t 2" : "shutdown -h now");
            } else if (res.contains("REINICIAR")) {
                Runtime.getRuntime().exec(so.contains("win") ? "shutdown /r /t 2" : "shutdown -r now");
            }
        } catch (Exception e) {
            System.err.println("[!] Error ejecutando orden: " + e.getMessage());
        }
    }

    private static void activarModoAtencionAlFrente() {
        javax.swing.SwingUtilities.invokeLater(() -> {
            if (pantallaAtencion != null && pantallaAtencion.isVisible()) return;
            pantallaAtencion = new javax.swing.JFrame();
            pantallaAtencion.setUndecorated(true);
            pantallaAtencion.setAlwaysOnTop(true);
            pantallaAtencion.setExtendedState(javax.swing.JFrame.MAXIMIZED_BOTH);
            pantallaAtencion.getContentPane().setBackground(new java.awt.Color(1, 4, 9));

            javax.swing.JLabel etiqueta = new javax.swing.JLabel(
                "<html><center><h1 style='color:#00f0ff;font-size:36px;font-family:monospace;'>⚠️ ATENCIÓN AL FRENTE</h1>" +
                "<p style='color:#ff0055;font-size:20px;font-family:monospace;'>DIRECTIVA DEL ADMINISTRADOR / PROFESOR</p>" +
                "<p style='color:#94a3b8;font-size:14px;font-family:monospace;'>Por favor preste atención a la explicación en clase.</p></center></html>",
                javax.swing.SwingConstants.CENTER
            );
            pantallaAtencion.add(etiqueta);
            pantallaAtencion.setVisible(true);
        });
    }

    private static void desactivarModoAtencionAlFrente() {
        javax.swing.SwingUtilities.invokeLater(() -> {
            if (pantallaAtencion != null) {
                pantallaAtencion.setVisible(false);
                pantallaAtencion.dispose();
                pantallaAtencion = null;
            }
        });
    }

    private static void inspeccionarVentanaYProcesos() {
        String so = System.getProperty("os.name").toLowerCase();
        try {
            Process p = so.contains("win") ? Runtime.getRuntime().exec("tasklist.exe /v /fo csv") : Runtime.getRuntime().exec("ps -eo comm");
            BufferedReader in = new BufferedReader(new InputStreamReader(p.getInputStream()));
            String linea;
            String ventanaDetectada = "Escritorio / IDLE";

            while ((linea = in.readLine()) != null) {
                String lLower = linea.toLowerCase();
                for (String prohibido : PROCESOS_PROHIBIDOS) {
                    if (lLower.contains(prohibido)) {
                        System.err.println("[!] Proceso no autorizado detectado: " + prohibido + ". Cerrando...");
                        if (so.contains("win")) Runtime.getRuntime().exec("taskkill /F /IM " + prohibido + "*");
                        else Runtime.getRuntime().exec("pkill -9 " + prohibido);
                    }
                }
                if (so.contains("win") && linea.contains(",") && !lLower.contains("tasklist") && !lLower.contains("svchost") && !lLower.contains("explorer")) {
                    String[] partes = linea.split(",");
                    if (partes.length >= 9 && partes[8].length() > 2) {
                        String titulo = partes[8].replace("\"", "").trim();
                        if (!titulo.equalsIgnoreCase("n/a") && !titulo.isEmpty()) {
                            ventanaDetectada = titulo;
                        }
                    }
                }
            }
            in.close();
            ultimaVentanaDetectada = ventanaDetectada;
        } catch (Exception ignored) {}
    }

    private static boolean vincularConPin(String pin) {
        try {
            String payload = String.format("{\"pin\":\"%s\",\"nombre_equipo\":\"%s\",\"usuario_actual\":\"%s\"}",
                pin, InetAddress.getLocalHost().getHostName(), System.getProperty("user.name"));
            URL url = new URL(SERVER_URL + "/agente/vincular");
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("Connection", "Keep-Alive");
            conn.setDoOutput(true);
            try (OutputStream os = conn.getOutputStream()) { os.write(payload.getBytes(StandardCharsets.UTF_8)); }

            if (conn.getResponseCode() == 200) {
                BufferedReader in = new BufferedReader(new InputStreamReader(conn.getInputStream()));
                StringBuilder sb = new StringBuilder();
                String l;
                while ((l = in.readLine()) != null) sb.append(l);
                in.close();
                String res = sb.toString();
                DISPOSITIVO_ID = extraerValor(res, "dispositivo_id");
                FAMILIA_ID = extraerValor(res, "familia_id");
                return true;
            }
        } catch (Exception ignored) {}
        return false;
    }

    private static void enviarTelemetria() {
        try {
            OperatingSystemMXBean os = ManagementFactory.getOperatingSystemMXBean();
            Runtime rt = Runtime.getRuntime();
            double cpu = Math.max(5.0, os.getSystemLoadAverage() * 10.0);
            if (cpu < 0 || Double.isNaN(cpu)) cpu = 15.0;
            double ram = ((double)(rt.totalMemory() - rt.freeMemory()) / rt.totalMemory()) * 100.0;
            File root = new File(File.separator);
            double disco = ((double)(root.getTotalSpace() - root.getFreeSpace()) / root.getTotalSpace()) * 100.0;
            double epochActual = System.currentTimeMillis() / 1000.0;

            String ventanaEscapada = ultimaVentanaDetectada.replace("\"", "'");
            String payload = String.format("{\"familia_id\":\"%s\",\"dispositivo_id\":\"%s\",\"nombre_equipo\":\"%s\",\"usuario_actual\":\"%s\",\"cpu_uso\":%.1f,\"ram_uso\":%.1f,\"disco_uso\":%.1f,\"client_epoch\":%.1f,\"ventana_activa\":\"%s\"}",
                FAMILIA_ID, DISPOSITIVO_ID, InetAddress.getLocalHost().getHostName(), System.getProperty("user.name"), cpu, ram, disco, epochActual, ventanaEscapada);
            URL url = new URL(SERVER_URL + "/agente/telemetria");
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setRequestProperty("Content-Type", "application/json");
            conn.setRequestProperty("Connection", "Keep-Alive");
            conn.setDoOutput(true);
            try (OutputStream os = conn.getOutputStream()) { os.write(payload.getBytes(StandardCharsets.UTF_8)); }
            conn.getResponseCode();
            conn.disconnect();
        } catch (Exception ignored) {}
    }

    private static String extraerValor(String json, String clave) {
        String p = "\"" + clave + "\":\"";
        int i = json.indexOf(p);
        if (i == -1) return "";
        i += p.length();
        int f = json.indexOf("\"", i);
        return (f != -1) ? json.substring(i, f) : "";
    }
}
'''

@app.get("/agente/descargar/AegisAgent.java")
async def descargar_agente_java():
    return Response(content=JAVA_AGENT_CODE, media_type="text/plain", headers={"Content-Disposition": "attachment; filename=AegisAgent.java"})

# ==============================================================================
# 7. ASSETS PWA EMBEBIDOS
# ==============================================================================
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
    <rect width="512" height="512" rx="128" fill="#010409"/>
    <path d="M256 64L96 128v128c0 106.04 68.27 205.28 160 230 91.73-24.72 160-123.96 160-230V128L256 64z" fill="none" stroke="#00f0ff" stroke-width="28" stroke-linejoin="round"/>
    <path d="M256 160l-70 120h140z" fill="none" stroke="#ff0055" stroke-width="20" stroke-linejoin="round"/>
    <circle cx="256" cy="270" r="16" fill="#00ff66"/>
</svg>"""

@app.get("/icon.svg")
async def get_icon():
    return Response(content=ICON_SVG, media_type="image/svg+xml")

MANIFEST_JSON = {
    "name": "Aegis OS v12 - Dual Cyberbunker",
    "short_name": "Aegis OS",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#010409",
    "theme_color": "#010409",
    "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]
}

@app.get("/manifest.json")
async def get_manifest():
    return JSONResponse(content=MANIFEST_JSON, media_type="application/manifest+json")

SERVICE_WORKER_JS = """
const CACHE_NAME = 'aegis-cache-v12.0';
const STATIC_ASSETS = ['/', '/app.js', '/manifest.json', '/icon.svg', 'https://cdn.tailwindcss.com', 'https://unpkg.com/lucide@latest', 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css', 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js', 'https://cdn.jsdelivr.net/npm/chart.js'];

self.addEventListener('install', (e) => { e.waitUntil(caches.open(CACHE_NAME).then((c) => c.addAll(STATIC_ASSETS))); self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))))); self.clients.claim(); });
self.addEventListener('fetch', (e) => {
    if (e.request.url.includes('/ws/') || e.request.url.includes('/api/')) return;
    e.respondWith(caches.match(e.request).then((c) => c || fetch(e.request).catch(() => caches.match('/'))));
});
"""

@app.get("/sw.js")
async def get_sw():
    return Response(content=SERVICE_WORKER_JS, media_type="application/javascript")

# ==============================================================================
# 8. JAVASCRIPT DEL CLIENTE PWA CON INTERFAZ ADAPTATIVA (HOGAR VS EMPRESA)
# ==============================================================================
APP_JS = """
let FAMILIA_ID = "FAMILIA-ALPHA-PERU";
let MASTER_TOKEN = sessionStorage.getItem("AEGIS_SESSION_TOKEN") || null;
let map = null, markers = null, socket = null, deferredPrompt = null;

let miGraficoProductividad = null;

async function actualizarGraficoProductividad() {
    try {
        const res = await fetch(`/api/v1/reportes/analitica-productividad?familia_id=${FAMILIA_ID}`, {
            headers: { 'X-Aegis-Token': MASTER_TOKEN }
        });
        if (!res.ok) return;
        const data = await res.json();
        const elem = document.getElementById('txt-indice-prod');
        if (elem) elem.innerText = `${data.indice_productividad}%`;
        
        const canvas = document.getElementById('chart-productividad');
        if (!canvas) return;
        const ctx = canvas.getContext('2d');
        if (miGraficoProductividad) miGraficoProductividad.destroy();
        
        miGraficoProductividad = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: ['Estudio/Trabajo', 'Ocio/Distracción'],
                datasets: [{
                    data: [data.tiempo_estudio_minutos, data.tiempo_ocio_minutos],
                    backgroundColor: ['#00ff66', '#ff0055'],
                    borderColor: '#010409',
                    borderWidth: 2
                }]
            },
            options: {
                plugins: { legend: { display: true, labels: { color: '#94a3b8', font: { family: 'monospace', size: 10 } } } },
                responsive: true,
                maintainAspectRatio: false
            }
        });
    } catch (e) {
        console.error("Error cargando analítica:", e);
    }
}

let planSeleccionado = "PREMIUM";

if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => navigator.serviceWorker.register('/sw.js').catch(console.error));
}

function initMap() {
    map = L.map('map-radar', { zoomControl: false }).setView([-9.19, -75.015], 5);
    L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { maxZoom: 19 }).addTo(map);
    markers = L.layerGroup().addTo(map);
}

function initSocket() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(`${proto}//${location.host}/ws/canal/${FAMILIA_ID}`);
    socket.onmessage = () => syncState();
    socket.onclose = () => setTimeout(initSocket, 3000);
}

async function autenticarTutor(e) {
    e.preventDefault();
    const tokenInput = document.getElementById('input-master-token').value.trim();
    try {
        const res = await fetch('/api/v1/auth/verificar-token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token: tokenInput })
        });
        if (res.ok) {
            MASTER_TOKEN = tokenInput;
            sessionStorage.setItem("AEGIS_SESSION_TOKEN", MASTER_TOKEN);
            document.getElementById('portal-login').classList.add('hidden');
            syncState();
        } else {
            document.getElementById('login-error').classList.remove('hidden');
        }
    } catch (err) {
        alert("Error de enlace con la base central.");
    }
}

function verificarSesionActiva() {
    if (!MASTER_TOKEN) {
        document.getElementById('portal-login').classList.remove('hidden');
    } else {
        document.getElementById('portal-login').classList.add('hidden');
        syncState();
    }
}

async function syncState() {
    if (!MASTER_TOKEN) return;
    try {
        const res = await fetch(`/api/v1/dashboard/estado?familia_id=${FAMILIA_ID}`);
        if (res.status === 402) {
            document.getElementById('modal-pago').classList.remove('hidden');
            return;
        }
        const data = await res.json();
        const tipoEntorno = data.familia.tipo_entorno || "HOGAR";

        document.getElementById('txt-familia').innerText = data.familia.nombre_familia;
        document.getElementById('txt-plan-badge').innerText = `${tipoEntorno} // PLAN ${data.familia.plan_id}`;
        document.getElementById('txt-licencia').innerText = `${data.familia.dias_licencia} DÍAS`;
        document.getElementById('txt-total-devs').innerText = `${data.dispositivos.length} / ${data.familia.max_dispositivos} TERMINALES`;

        renderMiembros(data.miembros);

        // Renderizado Inteligente según el cliente: Hogar vs Empresa
        if (tipoEntorno === "EMPRESA") {
            document.getElementById('panel-radar-satelital').classList.add('hidden'); // Oculta mapa en aula
            renderVistaEmpresarialGrid(data.dispositivos); // Activa matriz compacta de laboratorio
        } else {
            document.getElementById('panel-radar-satelital').classList.remove('hidden');
            renderDispositivos(data.dispositivos);
            actualizarRadar(data.dispositivos);
        }

        renderRestricciones(data.restricciones);
        actualizarGraficoProductividad();
        renderTareas(data.tareas);
        poblarSelectores(data.dispositivos, data.miembros);
        lucide.createIcons();
    } catch (e) {
        console.error("Error sincronizando:", e);
    }
}

// Vista Familiar Normal
function renderDispositivos(dispositivos) {
    const grid = document.getElementById('grid-dispositivos');
    grid.className = "grid grid-cols-1 md:grid-cols-2 gap-4";
    grid.innerHTML = "";

    dispositivos.forEach(d => {
        const critico = (d.cpu_uso > 85 || d.ram_uso > 90);
        const trampaReloj = Boolean(d.alerta_reloj);
        const card = document.createElement('div');
        card.className = `glass-panel rounded-xl p-5 relative transition duration-300 ${trampaReloj ? 'border-yellow-500 shadow-[0_0_20px_rgba(255,183,0,0.4)]' : (critico ? 'border-pink-500/80 alert-critical' : 'border-cyan-500/30')}`;
        card.innerHTML = `
            <div class="flex items-center justify-between pb-3 border-b border-slate-800">
                <div>
                    <div class="flex items-center space-x-2">
                        <span class="w-2.5 h-2.5 rounded-full ${d.estado_en_linea ? 'bg-cyberCyan shadow-[0_0_8px_#00f0ff]' : 'bg-red-500 shadow-[0_0_8px_#ff0000]'}"></span>
                        <h4 class="font-black text-sm tracking-wider text-white">${d.nombre_equipo}</h4>
                    </div>
                    <p class="text-[11px] text-slate-400 mt-0.5">ASIGNADO: <span class="text-yellow-400 font-bold">${d.hijo_nombre || 'Sin Asignar'}</span> | IP: ${d.ip_dinamica}</p>
                </div>
                <div class="text-right">
                    <span class="text-[10px] text-slate-300 font-bold block">${d.ciudad}</span>
                    <span class="text-[10px] font-bold ${trampaReloj ? 'text-yellow-400 font-black' : (!d.estado_en_linea ? 'text-red-500' : 'text-cyberCyan')}">
                        ${trampaReloj ? '⚠️ TRAMPA DE RELOJ' : (!d.estado_en_linea ? 'DESCONECTADO' : 'EN LÍNEA')}
                    </span>
                </div>
            </div>

            <div class="my-2.5 bg-black/40 p-2 rounded border border-slate-800 flex items-center justify-between">
                <span class="text-[10px] text-slate-400 uppercase">Actividad:</span>
                <span class="text-[11px] font-bold text-yellow-400 truncate max-w-[220px]">👀 ${d.ventana_activa || 'Escritorio / IDLE'}</span>
            </div>

            <div class="grid grid-cols-3 gap-3 my-3">
                <div class="bg-black/50 p-2 rounded border border-slate-800">
                    <div class="flex justify-between text-[10px] mb-1"><span class="text-slate-400">CPU</span><span class="font-bold text-cyberCyan">${d.cpu_uso}%</span></div>
                    <div class="w-full bg-slate-900 h-1.5 rounded-full overflow-hidden"><div class="h-full bg-cyberCyan" style="width:${d.cpu_uso}%"></div></div>
                </div>
                <div class="bg-black/50 p-2 rounded border border-slate-800">
                    <div class="flex justify-between text-[10px] mb-1"><span class="text-slate-400">RAM</span><span class="font-bold text-cyberCyan">${d.ram_uso}%</span></div>
                    <div class="w-full bg-slate-900 h-1.5 rounded-full overflow-hidden"><div class="h-full bg-cyberCyan" style="width:${d.ram_uso}%"></div></div>
                </div>
                <div class="bg-black/50 p-2 rounded border border-slate-800">
                    <div class="flex justify-between text-[10px] mb-1"><span class="text-slate-400">DISCO</span><span class="font-bold text-white">${d.disco_uso}%</span></div>
                    <div class="w-full bg-slate-900 h-1.5 rounded-full overflow-hidden"><div class="h-full bg-slate-400" style="width:${d.disco_uso}%"></div></div>
                </div>
            </div>

            <div class="grid grid-cols-4 gap-1.5 pt-2">
                <button onclick="disparar('${d.dispositivo_id}','BLOQUEAR_SISTEMA')" class="tactical-btn py-1.5 rounded text-[10px] font-bold text-yellow-400">BLOQUEAR</button>
                <button onclick="disparar('${d.dispositivo_id}','DESBLOQUEAR')" class="tactical-btn py-1.5 rounded text-[10px] font-bold text-neonGreen">LIBERAR</button>
                <button onclick="disparar('${d.dispositivo_id}','REINICIAR')" class="tactical-btn py-1.5 rounded text-[10px] font-bold text-cyberCyan">REINICIAR</button>
                <button onclick="disparar('${d.dispositivo_id}','APAGAR')" class="tactical-btn danger-btn py-1.5 rounded text-[10px] font-bold text-cyberPink">APAGAR</button>
            </div>
        `;
        grid.appendChild(card);
    });
}

// Nueva Vista de Matriz de Aula / Empresa Compacta (Control B2B de Laboratorios)
function renderVistaEmpresarialGrid(dispositivos) {
    const grid = document.getElementById('grid-dispositivos');
    grid.className = "grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-3";
    grid.innerHTML = "";

    const palabrasProhibidas = ["roblox", "steam", "discord", "minecraft", "anime", "juego", "poker"];

    dispositivos.forEach(d => {
        const software = (d.ventana_activa || "").toLowerCase();
        const tieneInfraccion = palabrasProhibidas.some(p => software.includes(p));
        const trampaReloj = Boolean(d.alerta_reloj);

        const gridCard = document.createElement('div');
        gridCard.className = `border p-3 rounded-xl flex flex-col justify-between transition ${
            tieneInfraccion
                ? 'bg-red-950/30 border-cyberPink shadow-[0_0_15px_#ff0055] alert-critical'
                : trampaReloj
                    ? 'bg-slate-950 border-yellow-500 shadow-[0_0_12px_rgba(255,183,0,0.3)]'
                    : d.estado_en_linea
                        ? 'bg-slate-950 border-cyan-500/30 shadow-[0_0_10px_rgba(0,240,255,0.08)]'
                        : 'bg-slate-950 border-slate-800 opacity-60'
        }`;

        gridCard.innerHTML = `
            <div>
                <div class="flex items-center justify-between border-b border-slate-800 pb-1.5">
                    <span class="text-[11px] font-black text-white truncate max-w-[100px]">${d.nombre_equipo}</span>
                    <span class="w-2 h-2 rounded-full ${d.estado_en_linea ? 'bg-cyberCyan shadow-[0_0_6px_#00f0ff]' : 'bg-red-500'}"></span>
                </div>
                <div class="my-2">
                    <span class="text-[9px] text-slate-400 block uppercase">Software Activo:</span>
                    <p class="text-[11px] font-bold ${tieneInfraccion ? 'text-cyberPink font-black' : 'text-yellow-400'} truncate" title="${d.ventana_activa}">
                        ${tieneInfraccion ? '🚨 ' : '👀 '}${d.ventana_activa || 'Escritorio / IDLE'}
                    </p>
                </div>
                <div class="flex justify-between text-[9px] text-slate-400 mb-2">
                    <span>CPU: <b class="text-white">${d.cpu_uso}%</b></span>
                    <span>RAM: <b class="text-white">${d.ram_uso}%</b></span>
                </div>
            </div>
            <div class="grid grid-cols-2 gap-1 pt-1.5 border-t border-slate-900">
                <button onclick="disparar('${d.dispositivo_id}','CONGELAR_PANTALLA')" class="bg-yellow-950/50 text-yellow-300 border border-yellow-800/40 rounded py-1 text-[9px] font-bold hover:bg-yellow-900/60">CONGELAR</button>
                <button onclick="disparar('${d.dispositivo_id}','DESCONGELAR')" class="bg-cyan-950/50 text-cyberCyan border border-cyan-800/40 rounded py-1 text-[9px] font-bold hover:bg-cyan-900/60">LIBERAR</button>
            </div>
        `;
        grid.appendChild(gridCard);
    });
}

function renderMiembros(miembros) {
    const cont = document.getElementById('contenedor-miembros');
    cont.innerHTML = "";
    miembros.forEach(m => {
        const d = document.createElement('div');
        d.className = "bg-black/60 border border-slate-800 rounded-lg p-3 flex flex-col justify-between hover:border-cyan-500/40 transition";
        d.innerHTML = `
            <div class="flex items-center justify-between">
                <span class="text-xs font-black text-white">${m.nombre}</span>
                <span class="text-[9px] px-1.5 py-0.5 rounded bg-cyan-950 text-cyberCyan border border-cyan-800 font-bold">${m.rol}</span>
            </div>
            <div class="flex items-center justify-between text-[10px] text-slate-400 mt-2">
                <span>Saldo Bonus: <b class="text-neonGreen">${m.minutos_saldo_recompensa || 0}m</b></span>
                <span class="text-cyberCyan font-bold">● ACTIVO</span>
            </div>
        `;
        cont.appendChild(d);
    });
}

function renderTareas(tareas) {
    const c = document.getElementById('contenedor-tareas');
    c.innerHTML = "";
    if (tareas.length === 0) { c.innerHTML = '<p class="text-[11px] text-slate-500 italic">No hay misiones asignadas.</p>'; return; }
    tareas.forEach(t => {
        const d = document.createElement('div');
        d.className = "flex items-center justify-between p-2 rounded bg-black/60 border border-slate-800 text-[11px]";
        d.innerHTML = `
            <div>
                <span class="font-bold text-white">${t.descripcion}</span>
                <span class="text-[10px] text-neonGreen block">+${t.minutos_recompensa} min libre</span>
            </div>
            <div>
                ${t.estado === 'PENDIENTE' ? `<button onclick="aprobarTarea('${t.tarea_id}')" class="tactical-btn px-2.5 py-1 rounded text-[10px] font-bold text-neonGreen border-green-500/40">APROBAR</button>` : '<span class="text-[9px] text-slate-500 uppercase font-bold">COMPLETADA</span>'}
            </div>
        `;
        c.appendChild(d);
    });
}

function renderRestricciones(restricciones) {
    const c = document.getElementById('lista-restricciones');
    c.innerHTML = "";
    if (restricciones.length === 0) { c.innerHTML = '<p class="text-[11px] text-slate-500 italic">Sin dominios bloqueados.</p>'; return; }
    restricciones.forEach(r => {
        const d = document.createElement('div');
        d.className = "flex items-center justify-between p-2 rounded bg-black/50 border border-slate-800 text-[11px]";
        d.innerHTML = `
            <div class="flex items-center space-x-2">
                <i data-lucide="slash" class="w-3.5 h-3.5 text-cyberPink"></i>
                <span class="font-bold text-slate-200">${r.dominio}</span>
                <span class="text-[9px] px-1.5 py-0.5 rounded bg-pink-950 text-cyberPink">${r.tipo_bloqueo}</span>
            </div>
            <button onclick="eliminarRestriccion('${r.id}')" class="text-slate-400 hover:text-cyberCyan"><i data-lucide="trash-2" class="w-3.5 h-3.5"></i></button>
        `;
        c.appendChild(d);
    });
}

function actualizarRadar(devs) {
    if (!map || !markers) return;
    markers.clearLayers();
    const bounds = [];
    devs.forEach(d => {
        if (d.latitud && d.longitud) {
            bounds.push([d.latitud, d.longitud]);
            const m = L.circleMarker([d.latitud, d.longitud], { radius: 7, fillColor: d.estado_en_linea ? "#00f0ff" : "#ff0055", color: "#fff", weight: 2, fillOpacity: 0.9 });
            m.bindPopup(`<b>${d.nombre_equipo}</b><br>Usuario: ${d.usuario_actual}<br>Actividad: ${d.ventana_activa}`);
            markers.addLayer(m);
        }
    });
    if (bounds.length > 0) map.fitBounds(bounds, { padding: [40, 40], maxZoom: 13 });
}

function poblarSelectores(devs, miembros) {
    const s = document.getElementById('sel-dev');
    s.innerHTML = "";
    devs.forEach(d => {
        const o = document.createElement('option');
        o.value = d.dispositivo_id;
        o.innerText = `${d.nombre_equipo} (${d.usuario_actual})`;
        s.appendChild(o);
    });

    const sm = document.getElementById('sel-miembro-tarea');
    sm.innerHTML = "";
    miembros.forEach(m => {
        const o = document.createElement('option');
        o.value = m.miembro_id;
        o.innerText = `${m.nombre} (${m.rol})`;
        sm.appendChild(o);
    });
}

async function aprobarTarea(tId) {
    await fetch(`/api/v1/tareas/${tId}/aprobar?familia_id=${FAMILIA_ID}`, { method: 'POST', headers: { 'X-Aegis-Token': MASTER_TOKEN } });
    syncState();
}

async function crearTarea(e) {
    e.preventDefault();
    const mId = document.getElementById('sel-miembro-tarea').value;
    const desc = document.getElementById('in-desc-tarea').value;
    const min = parseInt(document.getElementById('in-min-tarea').value) || 60;
    await fetch('/api/v1/tareas/crear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Aegis-Token': MASTER_TOKEN },
        body: JSON.stringify({ familia_id: FAMILIA_ID, miembro_id: mId, descripcion: desc, minutos_recompensa: min })
    });
    document.getElementById('in-desc-tarea').value = "";
    syncState();
}

async function abrirModalPin() {
    const res = await fetch(`/api/v1/familias/generar-pin?familia_id=${FAMILIA_ID}`, { method: 'POST', headers: { 'X-Aegis-Token': MASTER_TOKEN } });
    const d = await res.json();
    document.getElementById('pin-display').innerText = d.pin;
    document.getElementById('modal-otp').classList.remove('hidden');
}

async function disparar(devId, accion) {
    if (!confirm(`¿Ejecutar orden [${accion}] en el terminal [${devId}]?`)) return;
    await fetch(`/dispositivos/${devId}/energia`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Aegis-Token': MASTER_TOKEN },
        body: JSON.stringify({ familia_id: FAMILIA_ID, accion: accion, parametros: "{}" })
    });
    syncState();
}

async function ejecutarAccionMasiva(accion) {
    if (!confirm(`¿Propagar [${accion}] a TODA LA MATRIZ simultáneamente?`)) return;
    const res = await fetch(`/api/v1/dashboard/estado?familia_id=${FAMILIA_ID}`);
    const data = await res.json();
    const ids = data.dispositivos.map(d => d.dispositivo_id);
    await fetch('/casas/dispositivos/accion-masiva', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Aegis-Token': MASTER_TOKEN },
        body: JSON.stringify({ familia_id: FAMILIA_ID, dispositivos_ids: ids, accion: accion, parametros: "{}" })
    });
    syncState();
}

async function bloquearWeb(e) {
    e.preventDefault();
    const devId = document.getElementById('sel-dev').value;
    const dom = document.getElementById('in-dom').value;
    const tipo = document.getElementById('sel-tipo').value;
    const min = parseInt(document.getElementById('in-min').value) || 60;
    await fetch(`/dispositivos/${devId}/restringir`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Aegis-Token': MASTER_TOKEN },
        body: JSON.stringify({ familia_id: FAMILIA_ID, dominio: dom, tipo_bloqueo: tipo, minutos_castigo: min })
    });
    document.getElementById('in-dom').value = "";
    syncState();
}

async function eliminarRestriccion(id) {
    await fetch(`/api/v1/restricciones/${id}?familia_id=${FAMILIA_ID}`, { method: 'DELETE', headers: { 'X-Aegis-Token': MASTER_TOKEN } });
    syncState();
}

function seleccionarPlan(pId) {
    planSeleccionado = pId;
    document.getElementById('modal-planes').classList.add('hidden');
    document.getElementById('modal-checkout-cupon').classList.remove('hidden');
    document.getElementById('lbl-plan-nombre').innerText = `Plan ${pId}`;
}

async function aplicarCuponYProceder() {
    const cod = document.getElementById('input-cupon').value.trim();
    const res = await fetch('/api/v1/pagos/crear-preferencia', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Aegis-Token': MASTER_TOKEN },
        body: JSON.stringify({ familia_id: FAMILIA_ID, plan_id: planSeleccionado, codigo_cupon: cod || null })
    });
    const data = await res.json();
    if (data.init_point) window.location.href = data.init_point;
}

function cambiarEntornoLaboratorio() {
    FAMILIA_ID = (FAMILIA_ID === "FAMILIA-ALPHA-PERU") ? "LAB-INFORMATICA-COLEGIO" : "FAMILIA-ALPHA-PERU";
    initSocket();
    syncState();
}

function cerrarSesion() {
    sessionStorage.removeItem("AEGIS_SESSION_TOKEN");
    MASTER_TOKEN = null;
    document.getElementById('portal-login').classList.remove('hidden');
}

window.addEventListener('DOMContentLoaded', () => { initMap(); initSocket(); verificarSesionActiva(); });
"""

@app.get("/app.js")
async def get_app_js():
    return Response(content=APP_JS, media_type="application/javascript")

# ==============================================================================
# 9. HTML5 TÁCTICO ADAPTATIVO (CON CONTENEDOR ESPECÍFICO DE RADAR)
# ==============================================================================
HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="es" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AEGIS OS v12 // DUAL HOGAR & COLEGIO</title>
    <link rel="manifest" href="/manifest.json">
    <link rel="icon" href="/icon.svg" type="image/svg+xml">
    <meta name="theme-color" content="#010409">
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: { bunker: '#010409', cyberCyan: '#00f0ff', cyberPink: '#ff0055', neonGreen: '#00ff66', tacticalAmber: '#ffb700' },
                    fontFamily: { mono: ['JetBrains Mono', 'Menlo', 'monospace'] }
                }
            }
        }
    </script>
    <style>
        body {
            background-color: #010409;
            background-image: radial-gradient(rgba(0,240,255,0.07) 1px, transparent 0), radial-gradient(rgba(255,0,85,0.04) 1px, transparent 0);
            background-position: 0 0, 25px 25px;
            background-size: 50px 50px;
        }
        .glass-panel { background: rgba(7, 13, 24, 0.88); backdrop-filter: blur(16px); border: 1px solid rgba(0, 240, 255, 0.22); }
        .tactical-btn { background: rgba(13, 22, 38, 0.75); border: 1px solid rgba(0, 240, 255, 0.35); transition: all 0.2s ease; }
        .tactical-btn:hover { background: rgba(0, 240, 255, 0.2); border-color: #00f0ff; box-shadow: 0 0 15px rgba(0,240,255,0.4); transform: translateY(-1px); }
        .danger-btn { background: rgba(35, 10, 22, 0.75); border: 1px solid rgba(255, 0, 85, 0.45); }
        .danger-btn:hover { background: rgba(255, 0, 85, 0.3); border-color: #ff0055; box-shadow: 0 0 18px rgba(255,0,85,0.6); }
        .amber-btn { background: rgba(35, 25, 10, 0.75); border: 1px solid rgba(255, 183, 0, 0.45); }
        .amber-btn:hover { background: rgba(255, 183, 0, 0.3); border-color: #ffb700; box-shadow: 0 0 18px rgba(255,183,0,0.6); }
        @keyframes pulse-neon { 0%, 100% { opacity: 1; filter: drop-shadow(0 0 8px #ff0055); } 50% { opacity: 0.3; filter: drop-shadow(0 0 2px #ff0055); } }
        .alert-critical { animation: pulse-neon 1s infinite; }
    </style>
</head>
<body class="text-slate-200 font-mono min-h-screen w-screen overflow-x-hidden flex flex-col m-0 p-0">

    <!-- PORTAL DE LOGIN TÁCTICO -->
    <div id="portal-login" class="fixed inset-0 bg-black/95 backdrop-blur-2xl z-50 flex items-center justify-center p-4">
        <div class="glass-panel rounded-2xl max-w-md w-full p-8 border border-cyan-500/50 space-y-6 text-center shadow-[0_0_50px_rgba(0,240,255,0.2)]">
            <div class="p-3 border border-cyan-500/40 rounded-full bg-cyan-500/10 text-cyberCyan w-16 h-16 mx-auto flex items-center justify-center shadow-[0_0_15px_#00f0ff]">
                <i data-lucide="shield-check" class="w-8 h-8"></i>
            </div>
            <div>
                <h2 class="text-xl font-black text-white tracking-widest">AEGIS OS // AUTORIZACIÓN</h2>
                <p class="text-xs text-slate-400 mt-1">Ingrese la Clave Maestra de Tutor o Administrador.</p>
            </div>
            <form onsubmit="autenticarTutor(event)" class="space-y-4">
                <input type="password" id="input-master-token" placeholder="••••••••••••••••" required autofocus
                       class="w-full bg-black/70 border border-cyan-500/40 rounded-lg px-4 py-3 text-center text-sm text-cyberCyan tracking-widest focus:outline-none focus:border-cyan-400">
                <p id="login-error" class="text-xs text-cyberPink hidden font-bold">⚠️ Clave Maestra Incorrecta.</p>
                <button type="submit" class="w-full tactical-btn py-3 rounded-lg text-xs text-cyberCyan font-bold uppercase tracking-wider">
                    INGRESAR A LA PLATAFORMA
                </button>
            </form>
            <p class="text-[10px] text-slate-500">Clave de acceso: <span class="text-slate-300">AEGIS-PADRE-SEGURA-2026</span></p>
        </div>
    </div>

    <!-- HEADER MILITAR TÁCTICO -->
    <header class="w-full border-b border-cyan-500/30 bg-black/90 px-6 py-3.5 flex flex-wrap items-center justify-between backdrop-blur-md sticky top-0 z-40">
        <div class="flex items-center space-x-4">
            <div class="p-2 border border-cyan-500/50 rounded bg-cyan-500/15 text-cyberCyan shadow-[0_0_12px_rgba(0,240,255,0.3)]">
                <i data-lucide="shield-alert" class="w-7 h-7"></i>
            </div>
            <div>
                <div class="flex items-center space-x-2">
                    <span class="text-xl font-black tracking-widest text-white">AEGIS OS</span>
                    <span id="txt-plan-badge" class="text-xs px-2 py-0.5 rounded bg-cyan-500/20 text-cyberCyan border border-cyan-500/40 font-bold">HOGAR // PLAN PREMIUM</span>
                    <span class="text-xs px-2 py-0.5 rounded bg-pink-500/20 text-cyberPink border border-pink-500/40 font-bold">PERÚ</span>
                </div>
                <p class="text-[11px] text-slate-400">NÚCLEO: <b class="text-white" id="txt-familia">Cargando...</b></p>
            </div>
        </div>

        <div class="flex items-center space-x-3 text-xs mt-2 md:mt-0">
            <button onclick="cambiarEntornoLaboratorio()" title="Alternar entre Familia y Laboratorio Escolar" class="tactical-btn text-cyan-300 font-bold px-3 py-1.5 rounded flex items-center space-x-2 border-cyan-500/40">
                <i data-lucide="repeat" class="w-4 h-4"></i>
                <span>CAMBIAR ENTORNO</span>
            </button>
            <button onclick="document.getElementById('modal-planes').classList.remove('hidden')" class="tactical-btn text-yellow-400 font-bold px-3 py-1.5 rounded flex items-center space-x-2 border-yellow-500/40">
                <i data-lucide="sparkles" class="w-4 h-4"></i><span>PLANES Y CUPONES</span>
            </button>
            <button onclick="abrirModalPin()" class="tactical-btn text-cyberCyan font-bold px-3 py-1.5 rounded flex items-center space-x-2">
                <i data-lucide="key" class="w-4 h-4"></i><span>VINCULAR EQUIPO (OTP)</span>
            </button>
            <a href="/agente/descargar/AegisAgent.java" download class="tactical-btn text-neonGreen font-bold px-3 py-1.5 rounded flex items-center space-x-2 border-green-500/40">
                <i data-lucide="download" class="w-4 h-4"></i><span>BAJAR AGENTE JAVA</span>
            </a>
            <div class="bg-slate-900 border border-slate-700 px-3 py-1.5 rounded flex items-center space-x-2">
                <i data-lucide="credit-card" class="w-4 h-4 text-yellow-400"></i>
                <span>LICENCIA: <b class="text-yellow-400" id="txt-licencia">-- DÍAS</b></span>
            </div>
            <button onclick="cerrarSesion()" class="tactical-btn text-slate-400 p-1.5 rounded hover:text-white"><i data-lucide="log-out" class="w-4 h-4"></i></button>
        </div>
    </header>

    <main class="w-full flex-1 p-6 space-y-6">

        <!-- NÚCLEO PROTEGIDO -->
        <section class="glass-panel rounded-xl p-4 border border-cyan-500/30 space-y-3">
            <div class="flex items-center space-x-2 text-cyberCyan">
                <i data-lucide="users" class="w-5 h-5"></i>
                <span class="text-xs font-black uppercase tracking-wider text-slate-200">Integrantes / Alumnos Registrados:</span>
            </div>
            <div id="contenedor-miembros" class="grid grid-cols-2 sm:grid-cols-4 md:grid-cols-6 gap-3"></div>
        </section>

        <!-- ACCIONES MASIVAS -->
        <section class="glass-panel rounded-xl p-4 flex flex-wrap items-center justify-between gap-4 border border-cyan-500/30">
            <div class="flex items-center space-x-3">
                <i data-lucide="zap" class="text-cyberCyan w-5 h-5"></i>
                <span class="text-sm font-bold uppercase tracking-wider text-slate-200">Control de Red Simultáneo:</span>
            </div>
            <div class="flex flex-wrap gap-2">
                <button onclick="ejecutarAccionMasiva('CONGELAR_PANTALLA')" class="tactical-btn px-3.5 py-2 rounded text-xs text-yellow-300 font-bold flex items-center space-x-2 border-yellow-500/40">
                    <i data-lucide="eye" class="w-4 h-4"></i><span>CONGELAR AULA (ATENCIÓN)</span>
                </button>
                <button onclick="ejecutarAccionMasiva('DESCONGELAR')" class="tactical-btn px-3.5 py-2 rounded text-xs text-neonGreen font-bold flex items-center space-x-2 border-green-500/40">
                    <i data-lucide="unlock" class="w-4 h-4"></i><span>LIBERAR AULA</span>
                </button>
                <button onclick="ejecutarAccionMasiva('BLOQUEAR_SISTEMA')" class="tactical-btn px-3.5 py-2 rounded text-xs text-yellow-400 font-bold flex items-center space-x-2">
                    <i data-lucide="shield-ban" class="w-4 h-4"></i><span>BLOQUEO TOTAL</span>
                </button>
                <button onclick="ejecutarAccionMasiva('REINICIAR')" class="tactical-btn px-3.5 py-2 rounded text-xs text-cyan-300 font-bold flex items-center space-x-2">
                    <i data-lucide="rotate-ccw" class="w-4 h-4"></i><span>REINICIAR</span>
                </button>
                <button onclick="ejecutarAccionMasiva('APAGAR')" class="tactical-btn danger-btn px-3.5 py-2 rounded text-xs text-cyberPink font-bold flex items-center space-x-2">
                    <i data-lucide="power" class="w-4 h-4"></i><span>APAGADO GENERAL</span>
                </button>
            </div>
        </section>

        <div class="w-full grid grid-cols-1 xl:grid-cols-3 gap-6">
            <div class="xl:col-span-2 space-y-4">
                <div class="flex items-center justify-between">
                    <h2 class="text-sm font-extrabold uppercase tracking-wider text-cyan-400 flex items-center space-x-2">
                        <i data-lucide="cpu" class="w-4 h-4"></i><span>PANEL DE TERMINALES EN TIEMPO REAL</span>
                    </h2>
                    <span class="text-xs text-slate-400" id="txt-total-devs">Cargando...</span>
                </div>
                
                <!-- GRID DINÁMICO (Cambia a Matriz de Aula en modo EMPRESA) -->
                <div id="grid-dispositivos"></div>

                <!-- MÓDULO DE TAREAS Y RECOMPENSAS -->
                <div class="glass-panel rounded-xl p-5 border border-neonGreen/30 space-y-4">
                    <div class="flex items-center space-x-2 text-neonGreen">
                        <i data-lucide="check-circle-2" class="w-5 h-5"></i>
                        <h3 class="text-sm font-black uppercase tracking-wider">Misiones por Saldo de Pantalla (Gamificación)</h3>
                    </div>
                    <form onsubmit="crearTarea(event)" class="grid grid-cols-1 md:grid-cols-4 gap-2">
                        <select id="sel-miembro-tarea" class="bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs text-white"></select>
                        <input type="text" id="in-desc-tarea" placeholder="Misión (ej. Repasar álgebra)" required class="md:col-span-2 bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs text-white">
                        <div class="flex space-x-1">
                            <input type="number" id="in-min-tarea" value="60" min="15" max="300" class="w-20 bg-slate-900 border border-slate-700 rounded px-2 py-2 text-xs text-white">
                            <button type="submit" class="flex-1 tactical-btn text-neonGreen font-bold py-2 rounded text-xs uppercase">ASIGNAR</button>
                        </div>
                    </form>
                    <div id="contenedor-tareas" class="space-y-2 max-h-[140px] overflow-y-auto"></div>
                </div>
            </div>

            <div class="space-y-6">
                <!-- CONTENEDOR DEL RADAR SATELITAL (Se oculta automáticamente en modo EMPRESA) -->
                <div id="panel-radar-satelital" class="glass-panel rounded-xl p-4 flex flex-col h-[320px]">
                    <div class="flex items-center justify-between mb-2">
                        <h3 class="text-xs font-bold text-slate-200 uppercase flex items-center space-x-2">
                            <i data-lucide="map-pin" class="w-4 h-4 text-cyberPink"></i><span>Radar de Ubicación Global</span>
                        </h3>
                        <span class="text-[10px] text-cyberCyan px-2 py-0.5 rounded bg-cyan-950 border border-cyan-800 font-bold">PERÚ & EXTERIOR</span>
                    </div>
                    <div id="map-radar" class="w-full flex-1 rounded border border-slate-800 z-10"></div>
                </div>

                <!-- PANEL DE PRODUCTIVIDAD CORPORATIVA (CHART.JS) -->
                <div class="glass-panel rounded-xl p-5 border border-cyan-500/30 space-y-4">
                    <div class="flex items-center space-x-2 text-cyberCyan">
                        <i data-lucide="bar-chart-3" class="w-5 h-5"></i>
                        <h3 class="text-sm font-black uppercase tracking-wider">Índice de Productividad del Aula</h3>
                    </div>
                    <div class="relative w-full h-44 flex items-center justify-center">
                        <canvas id="chart-productividad"></canvas>
                    </div>
                    <div class="text-center text-xs">
                        <p class="text-slate-400">Rendimiento General: <b id="txt-indice-prod" class="text-neonGreen">0%</b></p>
                    </div>
                </div>

                <!-- POLÍTICAS ACTIVAS -->
                <div class="glass-panel rounded-xl p-4 space-y-3">
                    <h4 class="text-xs font-bold text-slate-300 uppercase flex items-center space-x-2">
                        <i data-lucide="list-filter" class="w-4 h-4 text-cyberCyan"></i><span>Políticas Web Vigentes</span>
                    </h4>
                    <div id="lista-restricciones" class="space-y-2 max-h-[160px] overflow-y-auto"></div>
                </div>

                <!-- CASTIGO WEB -->
                <div class="glass-panel rounded-xl p-5 border border-pink-500/30 space-y-4">
                    <div class="flex items-center space-x-2 text-cyberPink">
                        <i data-lucide="lock" class="w-5 h-5"></i><h3 class="text-sm font-black uppercase tracking-wider">Castigo Web Inmediato</h3>
                    </div>
                    <form onsubmit="bloquearWeb(event)" class="space-y-3">
                        <div>
                            <label class="text-[10px] text-slate-400 uppercase">Terminal Objetivo:</label>
                            <select id="sel-dev" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs text-cyan-300"></select>
                        </div>
                        <div>
                            <label class="text-[10px] text-slate-400 uppercase">Dominio (TikTok, Redes, Juegos):</label>
                            <input type="text" id="in-dom" placeholder="ej: tiktok.com, roblox.com" required class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs text-white">
                        </div>
                        <div class="grid grid-cols-2 gap-2">
                            <div>
                                <label class="text-[10px] text-slate-400 uppercase">Tipo:</label>
                                <select id="sel-tipo" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs text-white">
                                    <option value="TEMPORAL">TEMPORAL</option>
                                    <option value="PERMANENTE">PERMANENTE</option>
                                </select>
                            </div>
                            <div>
                                <label class="text-[10px] text-slate-400 uppercase">Minutos:</label>
                                <input type="number" id="in-min" value="120" min="5" class="w-full bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs text-white">
                            </div>
                        </div>
                        <button type="submit" class="w-full tactical-btn danger-btn text-cyberPink font-black py-2 rounded text-xs flex items-center justify-center space-x-2">
                            <i data-lucide="shield-x" class="w-4 h-4"></i><span>BLOQUEAR DOMINIO WEB</span>
                        </button>
                    </form>
                </div>
            </div>
        </div>
    </main>

    <!-- MODAL OTP PIN -->
    <div id="modal-otp" class="fixed inset-0 bg-black/85 backdrop-blur-md z-50 hidden flex items-center justify-center p-4">
        <div class="glass-panel rounded-2xl max-w-md w-full p-6 border border-yellow-500/50 space-y-5 text-center">
            <h3 class="font-black text-sm tracking-widest text-yellow-400 uppercase">PIN TEMPORAL DE VINCULACIÓN</h3>
            <p class="text-xs text-slate-300">Introduce este código en el agente Java para enlazar la computadora:</p>
            <div class="bg-black/60 p-4 rounded-xl border border-yellow-500/40">
                <span class="text-4xl font-black text-yellow-400 tracking-widest font-mono" id="pin-display">--- ---</span>
                <p class="text-[10px] text-slate-400 mt-2">Válido por 10 minutos (Un solo uso)</p>
            </div>
            <button onclick="document.getElementById('modal-otp').classList.add('hidden')" class="w-full tactical-btn py-2.5 rounded text-xs text-white uppercase font-bold">Cerrar</button>
        </div>
    </div>

    <!-- MODAL PLANES SAAS -->
    <div id="modal-planes" class="fixed inset-0 bg-black/90 backdrop-blur-md z-50 hidden flex items-center justify-center p-4">
        <div class="glass-panel rounded-2xl max-w-3xl w-full p-6 border border-cyan-500/50 space-y-6">
            <div class="flex items-center justify-between border-b border-slate-800 pb-3">
                <div class="flex items-center space-x-3 text-cyberCyan">
                    <i data-lucide="sparkles" class="w-6 h-6"></i>
                    <h3 class="font-black text-sm tracking-wider uppercase">Planes Comerciales Aegis OS (Perú)</h3>
                </div>
                <button onclick="document.getElementById('modal-planes').classList.add('hidden')" class="text-slate-400 hover:text-white">&times;</button>
            </div>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-4 text-center">
                <div class="bg-black/60 border border-slate-800 rounded-xl p-4 flex flex-col justify-between hover:border-cyan-500/50 transition">
                    <div>
                        <h4 class="font-bold text-white text-sm">Control Esencial</h4>
                        <p class="text-xs text-slate-400 mt-1">Hasta 2 dispositivos</p>
                        <p class="text-2xl font-black text-neonGreen my-3">S/ 19.90 <span class="text-[10px] text-slate-400 font-normal">/mes</span></p>
                    </div>
                    <button onclick="seleccionarPlan('BASICO')" class="w-full tactical-btn py-2 rounded text-xs font-bold text-cyan-300">ELEGIR BÁSICO</button>
                </div>
                <div class="bg-cyan-950/20 border-2 border-cyan-500 rounded-xl p-4 flex flex-col justify-between shadow-[0_0_20px_rgba(0,240,255,0.2)]">
                    <div>
                        <span class="text-[9px] bg-cyan-500 text-black font-black px-2 py-0.5 rounded uppercase">Recomendado</span>
                        <h4 class="font-bold text-white text-sm mt-1">Familiar Premium</h4>
                        <p class="text-xs text-slate-400 mt-1">Hasta 5 dispositivos</p>
                        <p class="text-2xl font-black text-neonGreen my-3">S/ 49.90 <span class="text-[10px] text-slate-400 font-normal">/mes</span></p>
                    </div>
                    <button onclick="seleccionarPlan('PREMIUM')" class="w-full bg-cyan-500 hover:bg-cyan-400 text-black py-2 rounded text-xs font-black uppercase">ELEGIR PREMIUM</button>
                </div>
                <div class="bg-black/60 border border-slate-800 rounded-xl p-4 flex flex-col justify-between hover:border-pink-500/50 transition">
                    <div>
                        <h4 class="font-bold text-white text-sm">Búnker / Laboratorio</h4>
                        <p class="text-xs text-slate-400 mt-1">Dispositivos ilimitados</p>
                        <p class="text-2xl font-black text-neonGreen my-3">S/ 89.90 <span class="text-[10px] text-slate-400 font-normal">/mes</span></p>
                    </div>
                    <button onclick="seleccionarPlan('MILITAR')" class="w-full tactical-btn amber-btn py-2 rounded text-xs font-bold text-yellow-400">ELEGIR ILIMITADO</button>
                </div>
            </div>
        </div>
    </div>

    <!-- MODAL CHECKOUT CUPÓN -->
    <div id="modal-checkout-cupon" class="fixed inset-0 bg-black/90 backdrop-blur-md z-50 hidden flex items-center justify-center p-4">
        <div class="glass-panel rounded-2xl max-w-md w-full p-6 border border-yellow-500/50 space-y-5 text-center">
            <h3 class="font-black text-sm tracking-wider text-yellow-400 uppercase">FINALIZAR SUSCRIPCIÓN</h3>
            <p class="text-xs text-slate-300">Plan seleccionado: <b id="lbl-plan-nombre" class="text-white">---</b></p>
            <div class="space-y-2">
                <input type="text" id="input-cupon" placeholder="Código de Cupón (ej: AEGISYAPE50)"
                       class="w-full bg-black/70 border border-yellow-500/40 rounded-lg px-3 py-2 text-center text-xs text-yellow-400 uppercase tracking-widest focus:outline-none focus:border-yellow-400">
                <p class="text-[10px] text-slate-400">¿Tienes cupón de descuento? Ingrésalo antes de pagar.</p>
            </div>
            <div class="flex space-x-2">
                <button onclick="document.getElementById('modal-checkout-cupon').classList.add('hidden')" class="flex-1 tactical-btn py-2 rounded text-xs text-slate-400 font-bold">CANCELAR</button>
                <button onclick="aplicarCuponYProceder()" class="flex-1 bg-sky-500 hover:bg-sky-400 text-black py-2 rounded text-xs font-black uppercase">PAGAR CON MERCADO PAGO</button>
            </div>
        </div>
    </div>

    <!-- MODAL SUSPENSIÓN DE SERVICIO -->
    <div id="modal-pago" class="fixed inset-0 bg-black/95 backdrop-blur-xl z-50 hidden flex items-center justify-center p-4">
        <div class="glass-panel rounded-2xl max-w-md w-full p-6 border border-pink-500/60 space-y-5 text-center alert-critical">
            <div class="p-3 border border-pink-500/50 rounded-full bg-pink-500/10 text-cyberPink w-14 h-14 mx-auto flex items-center justify-center">
                <i data-lucide="shield-x" class="w-8 h-8"></i>
            </div>
            <div>
                <h3 class="font-black text-base tracking-wider text-white uppercase">SERVICIO SUSPENDIDO</h3>
                <p class="text-xs text-slate-300 mt-2">La suscripción ha vencido. Los terminales vinculados han sido suspendidos preventivamente.</p>
            </div>
            <button onclick="document.getElementById('modal-planes').classList.remove('hidden')" class="w-full bg-sky-500 hover:bg-sky-400 text-black font-black py-3 rounded text-xs uppercase tracking-wider">
                REGULARIZAR SUSCRIPCIÓN CON MERCADO PAGO
            </button>
        </div>
    </div>

    <script src="/app.js"></script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
async def servir_dashboard_ui():
    return HTMLResponse(content=HTML_DASHBOARD, status_code=200)

# ==============================================================================
# 10. ARRANQUE RESILIENTE DEL SERVIDOR ASGI
# ==============================================================================
if __name__ == "__main__":
    import uvicorn
    puerto = int(os.getenv("PORT", 8000))
    logger.info(f"Levantando Aegis OS v12.0 Dual Master en 0.0.0.0:{puerto}")
    uvicorn.run(app, host="0.0.0.0", port=puerto)
