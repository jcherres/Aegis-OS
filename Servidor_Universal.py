import os
import sqlite3
import subprocess
import psutil
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Aegis OS Dashboard API", version="9.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"]
)

DB_NAME = "familia_control.db"

# Modelos Pydantic
class AccionEnergia(BaseModel):
    accion: str

class DispositivoSchema(BaseModel):
    nombre: str
    ip: str

class UbicacionUpdateSchema(BaseModel):
    lat: float
    lng: float

class NuevoPerfilSchema(BaseModel):
    nombre: str
    pin: str
    avatar: str
    rol: str = "Usuario"

class EditarPerfilSchema(BaseModel):
    nombre: str
    pin: str
    avatar: str

# Inicialización de la base de datos
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            pin TEXT NOT NULL UNIQUE,
            rol TEXT DEFAULT 'Usuario',
            avatar TEXT DEFAULT '👤'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dispositivos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            ip TEXT NOT NULL,
            mac TEXT DEFAULT '',
            ubicacion TEXT DEFAULT 'Ubicación GPS Real',
            lat REAL DEFAULT 0.0,
            lng REAL DEFAULT 0.0,
            estado TEXT DEFAULT 'ONLINE'
        )
    """)
    cursor.execute("SELECT COUNT(*) FROM usuarios")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO usuarios (nombre, pin, rol, avatar) VALUES ('Manuel', '1234', 'Administrador', '👨‍💻')")
        cursor.execute("INSERT INTO usuarios (nombre, pin, rol, avatar) VALUES ('Jessica', '1111', 'Estudiante', '🎓')")
        cursor.execute("INSERT INTO usuarios (nombre, pin, rol, avatar) VALUES ('Trabajo', '2222', 'Operador', '💼')")
        cursor.execute("INSERT INTO usuarios (nombre, pin, rol, avatar) VALUES ('Invitado', '0000', 'Invitado', '👤')")
    
    cursor.execute("SELECT COUNT(*) FROM dispositivos")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO dispositivos (nombre, ip, mac, ubicacion, lat, lng, estado) 
            VALUES ('Equipo Principal Local', '127.0.0.1', '00:11:22:33:44:55', 'Ubicación Detectada', -12.0464, -77.0428, 'ONLINE')
        """)
    conn.commit()
    conn.close()

init_db()

# Dependencia de seguridad para verificar el PIN
def verificar_pin(x_pin: str = Header(None)):
    if not x_pin:
        raise HTTPException(status_code=401, detail="Falta el PIN de autorización")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, rol, avatar, pin FROM usuarios WHERE pin = ?", (x_pin,))
    user = cursor.fetchone()
    conn.close()
    if not user:
        raise HTTPException(status_code=403, detail="PIN no válido")
    return {"id": user[0], "nombre": user[1], "rol": user[2], "avatar": user[3], "pin": user[4]}

# Rutas públicas y de interfaz
@app.get("/")
def cargar_interfaz():
    if not os.path.exists("index.html"):
        raise HTTPException(status_code=404, detail="No se encontró index.html")
    return FileResponse("index.html")

@app.get("/public/usuarios")
def obtener_usuarios_publico():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, avatar, rol FROM usuarios")
    users = [{"id": r[0], "nombre": r[1], "avatar": r[2], "rol": r[3]} for r in cursor.fetchall()]
    conn.close()
    return users

@app.post("/public/usuarios/crear")
def crear_usuario(data: NuevoPerfilSchema):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO usuarios (nombre, pin, rol, avatar) VALUES (?, ?, ?, ?)", 
                       (data.nombre, data.pin, data.rol, data.avatar))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="El PIN ya existe. Elige otro.")
    conn.close()
    return {"mensaje": "Perfil creado exitosamente"}

# Rutas protegidas
@app.get("/dispositivos")
def listar_dispositivos(user: dict = Depends(verificar_pin)):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT id, nombre, ip, mac, ubicacion, lat, lng, estado FROM dispositivos")
    rows = cursor.fetchall()
    conn.close()

    cpu_val = f"{psutil.cpu_percent(interval=None)}%"
    ram_val = f"{psutil.virtual_memory().percent}%"
    
    try:
        disco_val = f"{psutil.disk_usage('/').percent}%"
    except Exception:
        disco_val = "N/A"

    try:
        battery = psutil.sensors_battery()
        bat_val = f"{int(battery.percent)}%" if battery else "AC Directo"
    except Exception:
        bat_val = "Servidor Nube"

    dispositivos = []
    for r in rows:
        dispositivos.append({
            "id": r[0], "nombre": r[1], "ip": r[2], "mac": r[3],
            "ubicacion": r[4], "lat": r[5], "lng": r[6], "estado": r[7],
            "specs": {
                "cpu": cpu_val, "ram": ram_val, "disco": disco_val,
                "bateria": bat_val, "procesos": len(psutil.pids())
            }
        })
    return dispositivos

@app.put("/dispositivos/{dev_id}/gps")
def actualizar_gps(dev_id: int, data: UbicacionUpdateSchema, user: dict = Depends(verificar_pin)):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE dispositivos SET lat = ?, lng = ?, ubicacion = 'GPS Real Detectado' WHERE id = ?", (data.lat, data.lng, dev_id))
    conn.commit()
    conn.close()
    return {"mensaje": "Coordenadas actualizadas"}

@app.post("/dispositivos/agregar")
def agregar_dispositivo(data: DispositivoSchema, user: dict = Depends(verificar_pin)):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO dispositivos (nombre, ip, ubicacion, lat, lng, estado)
        VALUES (?, ?, 'Dispositivo Red', 0.0, 0.0, 'ONLINE')
    """, (data.nombre, data.ip))
    conn.commit()
    conn.close()
    return {"mensaje": "Dispositivo agregado"}

@app.put("/dispositivos/{dev_id}")
def editar_dispositivo(dev_id: int, data: DispositivoSchema, user: dict = Depends(verificar_pin)):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE dispositivos SET nombre = ?, ip = ? WHERE id = ?", (data.nombre, data.ip, dev_id))
    conn.commit()
    conn.close()
    return {"mensaje": "Dispositivo actualizado"}

@app.put("/perfil")
def editar_perfil(data: EditarPerfilSchema, user: dict = Depends(verificar_pin)):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE usuarios SET nombre = ?, pin = ?, avatar = ? WHERE id = ?", (data.nombre, data.pin, data.avatar, user["id"]))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="El PIN ya está en uso")
    conn.close()
    return {"mensaje": "Perfil actualizado", "nombre": data.nombre, "pin": data.pin, "avatar": data.avatar}

@app.post("/dispositivos/{dev_id}/energia")
def controlar_energia(dev_id: int, data: AccionEnergia, user: dict = Depends(verificar_pin)):
    accion = data.accion.upper()
    try:
        if os.name == 'nt':
            if accion == "BLOQUEAR":
                subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"])
                return {"mensaje": "Equipo bloqueado"}
            elif accion == "APAGAR":
                subprocess.run(["shutdown", "/s", "/t", "5"])
                return {"mensaje": "Apagando..."}
            elif accion == "REINICIAR":
                subprocess.run(["shutdown", "/r", "/t", "5"])
                return {"mensaje": "Reiniciando..."}
        return {"mensaje": f"Comando {accion} registrado (Modo Nube)"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000) 
