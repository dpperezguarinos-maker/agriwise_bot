# AgriWise Bot — Free (versión estable con notificaciones enriquecidas y /registrar HH.MM)

import io
import random
import os
import csv
import sqlite3
from datetime import datetime, time as dtime
from calendar import monthrange
from dotenv import load_dotenv

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================
# Config
# =========================
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")

DB_PATH   = "db.sqlite3"
KC_CSV    = "data/agriwise_kc_table_v1.csv"
ADJ_CSV   = "data/agriwise_adjustments_v1.csv"
CANOPY_CSV= "data/agriwise_canopy_factors_v1.csv"

# Estados de conversación
(
    PERFIL_CULTIVO, PERFIL_SUELO, PERFIL_CUBIERTA, PERFIL_EFICIENCIA, PERFIL_CAUDAL,
    RIEGO_ETO, RIEGO_STRESS,
    REG_CULTIVO, REG_SECTOR, REG_FECHA, REG_HORAS, REG_NOTA,
    PERFIL_CANOPY, PERFIL_MARCO_X, PERFIL_MARCO_Y,
    ESTADO_PRESION, ESTADO_FILTROS, ESTADO_VALVULAS, ESTADO_GOTEROS, ESTADO_NOTA,
    MANT_TAREA, MANT_CONFIRM,
    ALERTA_DESC, ALERTA_SECTOR
) = range(24)

# Estados extra
ETO_RAPIDA_VALOR, AJUAGUA_OBJ, AJUAGUA_PRECIO = range(24, 27)

# =========================
# Carga CSV sin pandas
# =========================
def load_kc_rows(path: str):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            r["crop"]         = r["crop"].strip()
            r["start_month"]  = r["start_month"].strip()
            r["end_month"]    = r["end_month"].strip()
            r["kc_min"]       = float(r["kc_min"])
            r["kc_max"]       = float(r["kc_max"])
            r["kc_default"]   = float(r["kc_default"])
            rows.append(r)
    return rows

def load_adjustments(path: str):
    d = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            d[r["parameter"].strip()] = float(r["value"])
    return d

def load_canopy_factors(path: str):
    d = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d[r["canopy_class"].strip().lower()] = float(r["f_copa"])
    return d

KC_ROWS = load_kc_rows(KC_CSV)
ADJ     = load_adjustments(ADJ_CSV)
CANOPY  = load_canopy_factors(CANOPY_CSV)

SOIL_MAP  = {"arenoso": ADJ.get("soil_factor_sandy",1.05),
             "franco" : ADJ.get("soil_factor_loam", 1.00),
             "arcilloso":ADJ.get("soil_factor_clay", 0.95)}
COVER_MAP = {"si": ADJ.get("cover_crop_active",1.10), "no": 1.0}
EFF_DEFAULT = ADJ.get("efficiency_drip_avg",0.92)

MONTH_TO_IDX = {"Ene":1,"Feb":2,"Mar":3,"Abr":4,"May":5,"Jun":6,
                "Jul":7,"Ago":8,"Sep":9,"Oct":10,"Nov":11,"Dic":12}

def month_in_range(start_m: str, end_m: str, target_idx: int) -> bool:
    s = MONTH_TO_IDX[start_m]; e = MONTH_TO_IDX[end_m]
    if s <= e: return s <= target_idx <= e
    return target_idx >= s or target_idx <= e

def kc_default_for(cultivo: str, month_idx: int) -> float:
    cult = (cultivo or "").strip().lower()
    candidates = [r for r in KC_ROWS if r["crop"].strip().lower() == cult]
    if not candidates: return 0.6
    matches    = [r for r in candidates if month_in_range(r["start_month"], r["end_month"], month_idx)]
    ref        = matches if matches else candidates
    vals       = [r["kc_default"] for r in ref]
    return sum(vals)/len(vals) if vals else 0.6

# =========================
# DB helpers
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
    CREATE TABLE IF NOT EXISTS profiles (
        user_id INTEGER PRIMARY KEY,
        cultivo TEXT,
        suelo TEXT,
        cubierta TEXT,
        eficiencia REAL,
        caudal_m3h_ha REAL,
        canopy_class TEXT,
        spacing_x_m REAL,
        spacing_y_m REAL,
        plants_per_ha REAL
    );""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        fecha TEXT,
        cultivo TEXT,
        sector TEXT,
        horas REAL,
        nota TEXT
    );""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS sys_estado (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        fecha TEXT,
        presion TEXT,
        filtros TEXT,
        fugas TEXT,          -- legado (compatibilidad)
        valvulas TEXT,
        goteros TEXT,
        nota TEXT
    );""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS sys_mant (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        fecha TEXT,
        tarea TEXT,
        comentario TEXT
    );""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS sys_alerta (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        fecha TEXT,
        descripcion TEXT,
        sector TEXT,
        resuelta INTEGER DEFAULT 0
    );""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS waitlist (
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        date TEXT
    );""")

    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id INTEGER PRIMARY KEY,
        objetivo_m3ha_mes REAL,
        precio_m3 REAL,
        notify_enabled INTEGER,
        notify_time TEXT,
        notify_kind TEXT,
        notify_freq TEXT,
        notif_last_idx INTEGER DEFAULT -1
    );""")

    # Compatibilidad: añade columnas si faltan
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sys_estado);").fetchall()]
        if "valvulas" not in cols:
            conn.execute("ALTER TABLE sys_estado ADD COLUMN valvulas TEXT;")
        if "goteros" not in cols:
            conn.execute("ALTER TABLE sys_estado ADD COLUMN goteros TEXT;")
        conn.commit()
    except Exception:
        pass

    # Añadir notif_last_idx si falta
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_settings);").fetchall()]
        if "notif_last_idx" not in cols:
            conn.execute("ALTER TABLE user_settings ADD COLUMN notif_last_idx INTEGER DEFAULT -1;")
        conn.commit()
    except Exception:
        pass

    return conn

def get_profile(user_id: int):
    conn = db()
    row  = conn.execute(
        "SELECT cultivo, suelo, cubierta, eficiencia, caudal_m3h_ha FROM profiles WHERE user_id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    if row:
        return {"cultivo": row[0] or "",
                "suelo":   row[1] or "",
                "cubierta":row[2] or "no",
                "eficiencia": row[3] or EFF_DEFAULT,
                "caudal_m3h_ha": row[4] or 0.0}
    return None

def save_profile(user_id: int, cultivo, suelo, cubierta, eficiencia, caudal):
    conn = db()
    conn.execute("""
    INSERT INTO profiles(user_id, cultivo, suelo, cubierta, eficiencia, caudal_m3h_ha)
    VALUES (?,?,?,?,?,?)
    ON CONFLICT(user_id) DO UPDATE SET
      cultivo=excluded.cultivo,
      suelo=excluded.suelo,
      cubierta=excluded.cubierta,
      eficiencia=excluded.eficiencia,
      caudal_m3h_ha=excluded.caudal_m3h_ha
    """,(user_id, cultivo, suelo, cubierta, eficiencia, caudal))
    conn.commit(); conn.close()

def save_profile_adv(user_id:int, canopy_class, spacing_x_m, spacing_y_m, plants_per_ha):
    conn = db()
    if conn.execute("SELECT 1 FROM profiles WHERE user_id=?",(user_id,)).fetchone() is None:
        conn.execute("INSERT INTO profiles(user_id, cultivo, suelo, cubierta, eficiencia, caudal_m3h_ha) VALUES (?,?,?,?,?,?)",
                     (user_id,"","","no",EFF_DEFAULT,0.0))
    conn.execute("""
        UPDATE profiles
           SET canopy_class=?,
               spacing_x_m=?,
               spacing_y_m=?,
               plants_per_ha=?
         WHERE user_id=?""",
        (canopy_class, spacing_x_m, spacing_y_m, plants_per_ha, user_id))
    conn.commit(); conn.close()

def add_log(user_id:int, fecha:str, cultivo:str, sector:str, horas:float, nota:str):
    conn = db()
    conn.execute("INSERT INTO logs(user_id, fecha, cultivo, sector, horas, nota) VALUES (?,?,?,?,?,?)",
                 (user_id, fecha, cultivo, sector, horas, nota))
    conn.commit(); conn.close()

def get_logs(user_id:int, limit=10):
    conn = db()
    rows = conn.execute("SELECT fecha, cultivo, sector, horas, nota FROM logs WHERE user_id=? ORDER BY id DESC LIMIT ?",
                        (user_id, limit)).fetchall()
    conn.close()
    return rows

def add_estado(user_id:int, presion:str, filtros:str, valvulas:str, goteros:str, nota:str):
    conn = db()
    conn.execute("""
        INSERT INTO sys_estado(user_id, fecha, presion, filtros, valvulas, goteros, nota)
        VALUES (?,?,?,?,?,?,?)
    """,(user_id, datetime.now().strftime("%Y-%m-%d"), presion, filtros, valvulas, goteros, nota))
    conn.commit(); conn.close()

def add_mant(user_id:int, tarea:str, comentario:str):
    conn = db()
    conn.execute("""
        INSERT INTO sys_mant(user_id, fecha, tarea, comentario)
        VALUES (?,?,?,?)
    """,(user_id, datetime.now().strftime("%Y-%m-%d"), tarea, comentario))
    conn.commit(); conn.close()

# Ajustes de usuario (agua y notificaciones)
def get_settings(uid:int):
    conn = db()
    r = conn.execute("""
        SELECT objetivo_m3ha_mes, precio_m3,
               COALESCE(notify_enabled,0),
               COALESCE(notify_time,'08:00'),
               COALESCE(notify_kind,'mixto'),
               COALESCE(notify_freq,'diaria'),
               COALESCE(notif_last_idx,-1)
          FROM user_settings
         WHERE user_id=?""",(uid,)).fetchone()
    conn.close()
    if not r:
        return {"objetivo":None,"precio":None,
                "notify_enabled":0,"notify_time":"08:00",
                "notify_kind":"mixto","notify_freq":"diaria",
                "notif_last_idx":-1}
    return {"objetivo":r[0], "precio":r[1],
            "notify_enabled":int(r[2] or 0), "notify_time":r[3],
            "notify_kind":r[4], "notify_freq":r[5],
            "notif_last_idx": int(r[6] if r[6] is not None else -1)}

def save_settings(uid:int, objetivo=None, precio=None,
                  notify_enabled=None, notify_time=None, notify_kind=None, notify_freq=None):
    conn = db()
    conn.execute("""
      INSERT INTO user_settings(user_id, objetivo_m3ha_mes, precio_m3, notify_enabled, notify_time, notify_kind, notify_freq)
      VALUES (?,?,?,?,?,?,?)
      ON CONFLICT(user_id) DO UPDATE SET
        objetivo_m3ha_mes = COALESCE(?, objetivo_m3ha_mes),
        precio_m3         = COALESCE(?, precio_m3),
        notify_enabled    = COALESCE(?, notify_enabled),
        notify_time       = COALESCE(?, notify_time),
        notify_kind       = COALESCE(?, notify_kind),
        notify_freq       = COALESCE(?, notify_freq)
    """,(uid, objetivo, precio, notify_enabled, notify_time, notify_kind, notify_freq,
         objetivo, precio, notify_enabled, notify_time, notify_kind, notify_freq))
    conn.commit(); conn.close()

# =========================
# Helpers de cálculo / formato
# =========================
def calc_plants_per_ha(spacing_x_m: float, spacing_y_m: float):
    try:
        if spacing_x_m and spacing_y_m and spacing_x_m>0 and spacing_y_m>0:
            return 10000.0/(spacing_x_m*spacing_y_m)
    except:
        pass
    return None

def canopy_factor(canopy_class: str | None) -> float:
    if not canopy_class: return 1.0
    return CANOPY.get(canopy_class.strip().lower(), 1.0)

def fmt_horas_min(h):
    if h is None: return None
    total_min = int(round(h*60))
    return f"{total_min//60} h {total_min%60:02d} min"

def calc_riego(eto: float, cultivo: str, month_num: int,
               suelo: str, cubierta: str, eficiencia: float,
               stress_factor: float = 1.0,
               caudal_m3h_ha: float | None = None,
               f_copa: float = 1.0):
    kc = kc_default_for(cultivo, month_num)
    kc = kc * (f_copa if f_copa and f_copa > 0 else 1.0)
    soil_factor  = SOIL_MAP.get((suelo or "").lower(), 1.0)
    cover_factor = COVER_MAP.get((cubierta or "").lower(), 1.0)
    etc     = eto * kc
    etc_adj = etc * soil_factor * cover_factor * stress_factor
    if eficiencia <= 0 or eficiencia > 1.0:
        eficiencia = EFF_DEFAULT
    riego_mm   = etc_adj / eficiencia
    m3_ha_dia  = riego_mm * 10.0
    horas = None
    if caudal_m3h_ha and caudal_m3h_ha > 0:
        horas = m3_ha_dia / caudal_m3h_ha
    return {
        "kc": kc, "soil_factor": soil_factor, "cover_factor": cover_factor,
        "efficiency": eficiencia, "eto": eto, "etc": etc, "etc_adj": etc_adj,
        "riego_mm": riego_mm, "m3_ha_dia": m3_ha_dia, "horas_dia": horas
    }
# =========================
# Consejos diarios (breves)
# =========================
TIPS_DIARIOS = [
    "Riega con criterio, no por rutina.",
    "Si la ETo cambia, tu riego también.",
    "Dos riegos cortos suelen ser mejores que uno largo.",
    "Presión estable = dosis estable.",
    "Camina la finca: la uniformidad se ve en el suelo.",
    "Las raíces no entienden de excusas",
    "Una válvula abierta de más puede costarte cientos de euros al mes",
    "Si no anotas, no aprendes",
    "El caudal no se supone. Se mide",
    "El goteo es preciso sólo si tú lo eres",
    "Un buen riego empieza en el cabezal. Preocúpate por que esté en condiciones.",
    "Limpieza hoy. Uniformidad mañana.",
    "Los filtros no se limpian solos, aunque lo digan los folletos.",
    "Un inyector tocado puede arruinar la fertirrigación entera.",
    "Mide la presión también en las lineas, no solo en el cabezal.",
    "Cada mantenimiento que no haces, resta eficiencia.",
    "Los goteros viejos no riegan menos, riegan peor.",
    "No cambies piezas, cambia rutinas.",
    "Un buen sistema sin seguimiento, es un problema aplazado.",
    "Si no mides y no anotas, no mejoras.",
    "Registra hoy: mañana lo agradecerás",
    "Tus datos valen más que cualquier sensor.",
    "Eficiencia no es ahorrar agua: es usarla bien.",
    "Si no sabes cuántos m3 aplicas, no sabes lo que gastas. Usa contadores.",
    "Un registro vale más que mil suposiciones.",
    "Los números no mienten, pero hay que leerlos.",
    "Los partes son memoria técnica: sin ellos, todo se olvida.",
    "La constancia vale más que la tecnología.",
    "El campo no necesita más datos, necesita mejores decisiones.",
    "Control es saber lo que pasa, no pensar que lo sabes.",
    "A pete can de mor, hande more nor: riega sabiendo, el fruto será mayor.",
    "Un filtro sucio es un problema.",
    "Registra hoy: lo que no se mide, no se mejora.",
    "Riega temprano: menos evaporación, más eficiencia.",
    "Antes de fertirrigar, comprueba presiones.",
    "Mide caudal en campo: vasos + cronómetro.",
    "Un sistema de riego limpio ahorra más agua que el mejor algoritmo.",
    "El suelo manda la frecuencia, no la costumbre.",
    "Eficiencia real es la de hoy; no la de catálogo.",
    "Pequeñas fugas son grandes pérdidas en el mes.",
]

def consejo_del_dia() -> str:
    # Determinista por fecha: mismo consejo durante el día, cambia al siguiente
    idx = datetime.now().toordinal() % len(TIPS_DIARIOS)
    return f"💡 Consejo de hoy: {TIPS_DIARIOS[idx]}"

# --- Parser HH.MM a horas decimales (para /registrar)
def parse_horas_dotmin(txt: str) -> float | None:
    s = txt.strip().replace(",", ".")
    if s.isdigit():
        return float(s)
    parts = s.split(".")
    if len(parts) != 2:
        return None
    hh, mm = parts[0], parts[1]
    if (not hh.isdigit()) or (not mm.isdigit()) or len(mm) != 2:
        return None
    h = int(hh); m = int(mm)
    if m < 0 or m > 59:
        return None
    return h + (m/60.0)

# =========================
# Teclados comunes
# =========================
def kb_main():
    return ReplyKeyboardMarkup(
        [
            ["/menu_finca", "/menu_riego"],
            ["/menu_sistema", "/menu_ajustes_acercade"],
            ["/descargas", "/cancelar 🔴"]
        ],
        resize_keyboard=True
    )

def kb_with_cancel(rows):
    return ReplyKeyboardMarkup(rows + [["/cancelar 🔴"]], resize_keyboard=True, one_time_keyboard=True)

def kb_cancel_only():
    return ReplyKeyboardMarkup([["/cancelar 🔴"]], resize_keyboard=True, one_time_keyboard=True)

# =========================
# Base handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 👉 registra el evento de inicio
    log_event(update.effective_user.id, "start", {"username": update.effective_user.username})

    text = (
        "*AgriWise Bot*\n"
        "Tu asistente técnico de riego — calcula, registra y gestiona con criterio. Privado, claro y 100% bajo tu control.\n\n"
        f"{consejo_del_dia()}"
    )

    await (
        update.message.reply_text(
            text,
            reply_markup=kb_main(),
            parse_mode="Markdown"
        )
        if update.message else
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=kb_main(),
            parse_mode="Markdown"
        )
    )

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📖 *Ayuda rápida*\n"
        "• /menu_finca → Configura tu finca y tus datos base\n"
        "• /menu_riego → Calcula, registra y mejora tu riego día a día\n"
        "• /menu_sistema → Mantén tu instalación en forma y sin sorpresas\n"
        "• /menu_ajustes_acercade → Ajusta notificaciones y conócenos\n"
        "• /exportar_riego_txt → Descarga los últimos riegos en TXT\n"
        "• /exportar_sistema_txt → Exporta checklist, mantenimiento y alertas\n"
    )
    await update.message.reply_text(txt, reply_markup=kb_main())

async def cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🔴 Cancelado. Vuelve con /start o el teclado.", reply_markup=kb_main())
    return ConversationHandler.END

# =========================
# FINCA
# =========================
async def finca_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🌱 **FINCA**\n"
        "Sin datos, no hay gestión. Y sin gestión, la finca manda sobre ti. Configuralo en solo 1 minuto.\n"
        "• /perfil – datos base\n"
        "• /avanzado – tamaño de copa y marco\n"
        "• /ajustes_agua – objetivo (m³/ha/mes) y €/m³\n"
        "• /perfil_ver – ver tu perfil guardado"
    )
    kb = kb_with_cancel([
        ["/perfil", "/avanzado"],
        ["/ajustes_agua", "/perfil_ver"]
    ])
    await update.message.reply_text(txt, reply_markup=kb)

async def perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = kb_with_cancel([["Almendro","Olivo","Viña"],["Cítricos","Pistacho","Aguacate"]])
    await update.message.reply_text("Cultivo principal de la finca:", reply_markup=kb)
    return PERFIL_CULTIVO

async def perfil_cultivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cultivo"] = update.message.text
    kb = kb_with_cancel([["arenoso","franco","arcilloso"]])
    await update.message.reply_text("Tipo de suelo:", reply_markup=kb)
    return PERFIL_SUELO

async def perfil_suelo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["suelo"] = update.message.text
    kb = kb_with_cancel([["si","no"]])
    await update.message.reply_text("¿Hay cubierta vegetal activa? (si/no)", reply_markup=kb)
    return PERFIL_CUBIERTA

async def perfil_cubierta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cubierta"] = update.message.text
    await update.message.reply_text("Eficiencia del sistema (0.80–0.95), ej. 0.92:", reply_markup=kb_cancel_only())
    return PERFIL_EFICIENCIA

async def perfil_eficiencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        eficiencia = float(update.message.text.replace(",", "."))
    except:
        eficiencia = EFF_DEFAULT
    context.user_data["eficiencia"] = eficiencia
    await update.message.reply_text("Caudal del sistema en m³/h/ha (si no sabes, pulsa Omitir):",
                                    reply_markup=kb_with_cancel([["Omitir"]]))
    return PERFIL_CAUDAL

async def perfil_caudal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.lower() == "omitir":
        caudal = 0.0
    else:
        try:
            caudal = float(txt.replace(",", "."))
        except:
            await update.message.reply_text("Número no válido. Escribe un valor o pulsa Omitir.",
                                            reply_markup=kb_with_cancel([["Omitir"]]))
            return PERFIL_CAUDAL
    context.user_data["caudal_m3h_ha"] = caudal
    user_id = update.message.from_user.id
    save_profile(user_id,
                 context.user_data["cultivo"],
                 context.user_data["suelo"],
                 context.user_data["cubierta"],
                 context.user_data["eficiencia"],
                 context.user_data["caudal_m3h_ha"])
    await update.message.reply_text("✅ Perfil guardado. Ajusta objetivo/precio del agua en /ajustes_agua.", reply_markup=kb_main())
    return ConversationHandler.END

# Avanzado (/avanzado)
async def perfil_avanzado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = kb_with_cancel([["joven","desarrollo","adulta"],["saltar"]])
    await update.message.reply_text("Tamaño de copa (elige):", reply_markup=kb)
    return PERFIL_CANOPY

async def perfil_canopy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip().lower()
    context.user_data["canopy_class"] = None if val == "saltar" else val
    await update.message.reply_text("Marco X (m). Ej: 6  (o escribe 'saltar')", reply_markup=kb_cancel_only())
    return PERFIL_MARCO_X

async def perfil_marco_x(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().lower()
    if txt == "saltar":
        save_profile_adv(update.message.from_user.id, context.user_data.get("canopy_class"), None, None, None)
        await update.message.reply_text("✅ Perfil avanzado guardado (solo copa).", reply_markup=kb_main())
        return ConversationHandler.END
    try:
        x = float(txt.replace(",", "."))
    except:
        await update.message.reply_text("Número no válido. Escribe 6 o 5,5. O 'saltar'.")
        return PERFIL_MARCO_X
    context.user_data["spacing_x_m"] = x
    await update.message.reply_text("Marco Y (m). Ej: 4", reply_markup=kb_cancel_only())
    return PERFIL_MARCO_Y

async def perfil_marco_y(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        y = float(update.message.text.strip().replace(",", "."))
    except:
        await update.message.reply_text("Número no válido. Escribe 4 o 3,5.")
        return PERFIL_MARCO_Y
    x = context.user_data.get("spacing_x_m")
    ppha = calc_plants_per_ha(x, y)
    save_profile_adv(update.message.from_user.id, context.user_data.get("canopy_class"), x, y, ppha)
    fin = f"✅ Perfil avanzado guardado. Marco: {x}×{y} m"
    if ppha:
        fin += f" ({ppha:.0f} plantas/ha)."
    await update.message.reply_text(fin, reply_markup=kb_main())
    return ConversationHandler.END

# /perfil_ver
async def perfil_ver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    profile = get_profile(user_id)
    if not profile:
        await update.message.reply_text("No tienes perfil aún. Usa /perfil.", reply_markup=kb_main())
        return
    conn = db()
    adv = conn.execute("SELECT canopy_class, spacing_x_m, spacing_y_m, plants_per_ha FROM profiles WHERE user_id=?",
                       (user_id,)).fetchone()
    conn.close()
    canopy, sx, sy, ppha = (adv if adv else (None, None, None, None))

    s = get_settings(user_id)
    objetivo = s.get("objetivo")
    precio   = s.get("precio")

    txt = (
        "👤 **Tu perfil de riego**\n"
        f"- Cultivo: {profile['cultivo']}\n"
        f"- Suelo: {profile['suelo']}\n"
        f"- Cubierta: {profile['cubierta']}\n"
        f"- Eficiencia: {profile['eficiencia']}\n"
        f"- Caudal m³/h/ha: {profile['caudal_m3h_ha']}\n"
    )
    if canopy or sx or sy or ppha:
        txt += "— — — — — — — —\n"
        if canopy: txt += f"- Tamaño de copa: {canopy}\n"
        if sx and sy: txt += f"- Marco: {sx} × {sy} m\n"
        if ppha: txt += f"- Plantas/ha: {ppha:.0f}\n"

    txt += "— — — — — — — —\n"
    if (objetivo is not None) or (precio is not None):
        if objetivo is not None: txt += f"- Objetivo mensual: {float(objetivo):.0f} m³/ha/mes\n"
        if precio   is not None: txt += f"- Precio del agua: {float(precio):.3f} €/m³\n"
    else:
        txt += "- Objetivo mensual y €/m³: *sin configurar*\n"

    kb = kb_with_cancel([["/ajustes_agua"], ["/menu_finca"]])
    await update.message.reply_text(txt, reply_markup=kb)

# =========================
# RIEGO
# =========================
ETO_MESES = {
    1:[1.0,1.5,2.0],  2:[1.5,2.0,2.5], 3:[2.5,3.0,3.5], 4:[3.5,4.0,4.5],
    5:[4.5,5.5,6.0],  6:[5.5,6.0,6.5], 7:[6.0,6.5,7.0], 8:[5.5,6.0,6.5],
    9:[4.0,4.5,5.0], 10:[3.0,3.5,4.0],11:[2.0,2.5,3.0],12:[1.0,1.5,2.0]
}

def kb_vals(vals): return kb_with_cancel([[f"{v:.1f}" for v in vals]])

async def riego_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "💧 **RIEGO**\n"
        "El agua no perdona errores. Si no sabes lo que  aplicas, ni por qué, el problema no está en el clima.\n"
        "• /riego – calcular riego (introduces ETo)\n"
        "• /eto_rapida – atajos ETo por mes (rápido)\n"
        "• /registrar – guardar un riego\n"
        "• /historial – ver últimos riegos\n"
        "• /mi_agua – objetivo mensual vs consumo"
    )
    kb = kb_with_cancel([
        ["/riego", "/eto_rapida"],
        ["/registrar", "/historial"],
        ["/mi_agua"]
    ])
    await update.message.reply_text(txt, reply_markup=kb)

async def riego_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Introduce la ETo (mm/día) para hoy o media de la semana (ej. 6.2):",
                                    reply_markup=kb_cancel_only())
    return RIEGO_ETO

async def riego_eto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        eto = float(update.message.text.replace(",", "."))
    except:
        await update.message.reply_text("Valor no válido. Prueba con un número, ej. 5.8", reply_markup=kb_cancel_only())
        return RIEGO_ETO
    context.user_data["eto"] = eto
    reply_kb = kb_with_cancel([["sin_estres","leve","moderado"]])
    await update.message.reply_text("Nivel de estrés hídrico (déficit controlado):", reply_markup=reply_kb)
    return RIEGO_STRESS

async def riego_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stress = update.message.text.strip().lower()
    stress_map = {"sin_estres":1.0, "leve":ADJ.get("stress_reduction_mild",0.95),
                  "moderado":ADJ.get("stress_reduction_moderate",0.90)}
    sf = stress_map.get(stress, 1.0)

    user_id = update.message.from_user.id
    profile = get_profile(user_id)
    if not profile:
        await update.message.reply_text("Primero configura tu /perfil.", reply_markup=kb_main())
        return ConversationHandler.END

    eto       = context.user_data["eto"]
    month_num = datetime.now().month

    conn = db()
    adv  = conn.execute("SELECT canopy_class, spacing_x_m, spacing_y_m, plants_per_ha FROM profiles WHERE user_id=?",
                        (user_id,)).fetchone()
    conn.close()
    canopy, sx, sy, ppha = (adv if adv else (None, None, None, None))
    f_copa = canopy_factor(canopy)

    res = calc_riego(
        eto=eto,
        cultivo=profile["cultivo"],
        month_num=month_num,
        suelo=profile["suelo"],
        cubierta=profile["cubierta"],
        eficiencia=profile["eficiencia"],
        stress_factor=sf,
        caudal_m3h_ha=profile["caudal_m3h_ha"],
        f_copa=f_copa
    )

    msg = (
        f"📍 Cultivo: {profile['cultivo']} | Mes: {month_num}\n"
        f"🧮 ETo: {res['eto']:.2f} mm/día | Kc: {res['kc']:.2f}"
    )
    if canopy:
        msg += f" (copa: {canopy})"
    msg += "\n"
    msg += (
        f"⚙️ Ajustes → suelo:{res['soil_factor']:.2f} · cubierta:{res['cover_factor']:.2f} · estrés:{sf:.2f}\n"
        f"➡️ ETc: {res['etc']:.2f} → ETc_aj: {res['etc_adj']:.2f}\n"
        f"💧 Riego recomendado: {res['riego_mm']:.2f} mm/día  (~{res['m3_ha_dia']:.1f} m³/ha/día)\n"
    )
    if res["horas_dia"] is not None:
        hm = fmt_horas_min(res["horas_dia"])
        msg += f"⏱️ Equivale a ~{hm} al día con tu caudal.\n"
    if ppha and ppha > 0:
        l_planta_dia = (res['m3_ha_dia'] * 1000.0) / ppha
        msg += f"🌳 Dosis ~{l_planta_dia:.0f} L/planta/día (con {ppha:.0f} plantas/ha).\n"
    msg += "💡 Consejo: divide en 1–3 turnos según infiltración y presión."

    await update.message.reply_text(msg, reply_markup=kb_main())
    return ConversationHandler.END

# =========================
# Registro de riego (SIN pedir cultivo)
# =========================
async def registrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sector/Parcela (ej. S3):", reply_markup=kb_cancel_only())
    return REG_SECTOR

async def reg_sector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["reg_sector"] = update.message.text
    await update.message.reply_text("Fecha (YYYY-MM-DD) o pulsa **Hoy**:", reply_markup=kb_with_cancel([["Hoy"]]))
    return REG_FECHA

async def reg_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    if txt.lower() in ("hoy","today"):
        context.user_data["reg_fecha"] = datetime.now().strftime("%Y-%m-%d")
    else:
        context.user_data["reg_fecha"] = txt
    await update.message.reply_text("Horas de riego en formato HH.MM (ej. 2.20, 1.05, 3.00):", reply_markup=kb_cancel_only())
    return REG_HORAS

async def reg_horas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_horas_dotmin(update.message.text)
    if parsed is None:
        await update.message.reply_text("Formato no válido. Usa HH.MM con minutos en 2 dígitos (ej. 2.20, 1.05).", reply_markup=kb_cancel_only())
        return REG_HORAS
    context.user_data["reg_horas"] = parsed
    await update.message.reply_text("Nota (opcional). Escribe texto o pulsa **Omitir**:",
                                    reply_markup=kb_with_cancel([["Omitir"]]))
    return REG_NOTA

async def reg_nota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nota = update.message.text or ""
    if nota.lower() == "omitir":
        nota = ""
    user_id = update.message.from_user.id
    prof = get_profile(user_id)
    cultivo = (prof["cultivo"] if prof else "") or ""
    add_log(user_id,
            context.user_data["reg_fecha"],
            cultivo,
            context.user_data["reg_sector"],
            context.user_data["reg_horas"],
            nota)
    await update.message.reply_text("✅ Riego registrado.", reply_markup=kb_main())
    return ConversationHandler.END

async def historial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_logs(update.message.from_user.id, 10)
    if not rows:
        await update.message.reply_text("No hay registros aún. Usa /registrar para añadir el primero.", reply_markup=kb_main())
        return
    lines = ["🧾 Últimos riegos:"]
    for f, c, s, h, n in rows:
        n_sh = (n[:40]+"…") if n and len(n)>40 else (n or "")
        lines.append(f"- {f} | {s} | {h:.2f} h{(' · '+n_sh) if n_sh else ''}")
    await update.message.reply_text("\n".join(lines), reply_markup=kb_main())

# =========================
# Exportación TXT básica
# =========================
async def exportar_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    # Últimos 10 riegos (puedes cambiar el límite si quieres)
    rows = get_logs(uid, limit=10)
    if not rows:
        await update.message.reply_text("No hay registros para exportar. Usa /registrar para añadir el primero.", reply_markup=kb_main())
        return

    # Formato de líneas: Fecha | Cultivo | Sector | Horas | Nota
    lines = ["AgriWise — Últimos riegos", "==========================", ""]
    for f, c, s, h, n in rows[::-1]:  # del más antiguo al más reciente dentro de los 10
        nota = (n or "").replace("\n", " ").strip()
        lines.append(f"{f} | {c} | {s} | {h} h | {nota}")

    content = "\n".join(lines) + "\n"
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = f"agriwise_riegos_{datetime.now().strftime('%Y%m%d')}.txt"

    await update.message.reply_document(document=buf, caption="📄 Exportación básica (TXT) — últimos 10 riegos")

# =========================
# SISTEMA
# =========================
async def sistema_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "🧰 **SISTEMA** (Gestión del sistema de riego)\n"
"El patrón, la variedad, el marco... todo eso es importante. Pero si tu sistema de riego no funciona como debe, estás perdiendo el tiempo.\n"
        "• /estado – checklist rápido\n"
        "• /mantenimiento – registrar tarea\n"
        "• /alerta – registrar incidencia\n"
        "• /resumen – últimos registros de todo"
    )
    kb = kb_with_cancel([
        ["/estado", "/mantenimiento"],
        ["/alerta", "/resumen"]
    ])
    await update.message.reply_text(txt, reply_markup=kb)

# /estado → Presión → Filtros → Válvulas → Goteros → Nota
async def estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = kb_with_cancel([["✅ Presión","⚠️ Presión","❌ Presión"]])
    await update.message.reply_text("Presión en cabezal:", reply_markup=kb)
    return ESTADO_PRESION

async def estado_presion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["presion"] = update.message.text
    kb = kb_with_cancel([["✅ Filtros","⚠️ Filtros","❌ Filtros"]])
    await update.message.reply_text("Filtros:", reply_markup=kb)
    return ESTADO_FILTROS

async def estado_filtros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["filtros"] = update.message.text
    kb = kb_with_cancel([["✅ Válvulas","⚠️ Válvulas","❌ Válvulas"]])
    await update.message.reply_text("Válvulas:", reply_markup=kb)
    return ESTADO_VALVULAS

async def estado_valvulas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["valvulas"] = update.message.text
    kb = kb_with_cancel([["✅ Goteros","⚠️ Goteros","❌ Goteros"]])
    await update.message.reply_text("Goteros:", reply_markup=kb)
    return ESTADO_GOTEROS

async def estado_goteros(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["goteros"] = update.message.text
    await update.message.reply_text("Nota (opcional) o pulsa **Omitir**:",
                                    reply_markup=kb_with_cancel([["Omitir"]]))
    return ESTADO_NOTA

async def estado_nota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nota = update.message.text or ""
    if nota.lower() == "omitir":
        nota = ""
    user_id  = update.message.from_user.id
    presion  = context.user_data.get("presion","")
    filtros  = context.user_data.get("filtros","")
    valvulas = context.user_data.get("valvulas","")
    goteros  = context.user_data.get("goteros","")
    add_estado(user_id, presion, filtros, valvulas, goteros, nota)
    await update.message.reply_text("✅ Estado guardado.", reply_markup=kb_main())
    return ConversationHandler.END

# /mantenimiento
async def mantenimiento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = kb_with_cancel([
        ["Limpieza de filtros","Lavado de tuberías"],
        ["Revisión de presiones","Limpieza de goteros"],
        ["Otra"]
    ])
    await update.message.reply_text("Tarea realizada o a realizar:", reply_markup=kb)
    return MANT_TAREA

async def mant_tarea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["tarea"] = update.message.text
    await update.message.reply_text("Comentario/nota (opcional). Pulsa **Omitir** si no quieres añadir:",
                                    reply_markup=kb_with_cancel([["Omitir"]]))
    return MANT_CONFIRM

# ✅ Tras registrar mantenimiento → NO /start (no reimprime portada)
async def mant_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comentario = update.message.text or ""
    if comentario.lower() == "omitir":
        comentario = ""
    add_mant(update.message.from_user.id, context.user_data["tarea"], comentario)
    await update.message.reply_text("✅ Mantenimiento registrado.", reply_markup=kb_main())
    return ConversationHandler.END

# /alerta
async def alerta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Describe la incidencia (ej. 'Baja presión en S3'):",
                                    reply_markup=kb_cancel_only())
    return ALERTA_DESC

async def alerta_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["alerta_desc"] = update.message.text
    await update.message.reply_text("Sector/Parcela (opcional). Pulsa **Omitir** si no aplica:",
                                    reply_markup=kb_with_cancel([["Omitir"]]))
    return ALERTA_SECTOR

# ✅ Tras registrar alerta → NO /start (no reimprime portada)
async def alerta_sector(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sector = update.message.text or ""
    if sector.lower() == "omitir":
        sector = ""
    conn = db()
    conn.execute("INSERT INTO sys_alerta(user_id, fecha, descripcion, sector) VALUES (?,?,?,?)",
                 (update.message.from_user.id, datetime.now().strftime("%Y-%m-%d"),
                  context.user_data["alerta_desc"], sector))
    conn.commit(); conn.close()
    await update.message.reply_text("✅ Alerta registrada.", reply_markup=kb_main())
    return ConversationHandler.END

# =========================
# RESUMEN (últimas 5)
# =========================
def short(s: str | None, n: int) -> str:
    if not s: return ""
    s = s.strip()
    return (s[:n]+"…") if len(s)>n else s

async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    conn = db()

    est = conn.execute("""
        SELECT fecha, presion, filtros, COALESCE(valvulas,''), COALESCE(goteros,''), COALESCE(nota,'')
          FROM sys_estado
         WHERE user_id=?
         ORDER BY id DESC LIMIT 5""",(uid,)).fetchall()

    mant = conn.execute("""
        SELECT fecha, tarea, COALESCE(comentario,'')
          FROM sys_mant
         WHERE user_id=?
         ORDER BY id DESC LIMIT 5""",(uid,)).fetchall()

    alr = conn.execute("""
        SELECT fecha, descripcion, COALESCE(sector,''), resuelta
          FROM sys_alerta
         WHERE user_id=?
         ORDER BY id DESC LIMIT 5""",(uid,)).fetchall()

    conn.close()

    lines = ["📊 **RESUMEN DEL SISTEMA (últimas 5 por cada apartado)**"]

    lines.append("\n🔎 Estado (checklist):")
    if not est:
        lines.append("— Sin registros.")
    else:
        for i, (f, p, fi, va, go, n) in enumerate(est, start=1):
            extra = f" · {short(n, 50)}" if n else ""
            lines.append(f"{i}. {f} | {p} · {fi} · {va} · {go}{extra}")

    lines.append("\n🛠️ Mantenimiento:")
    if not mant:
        lines.append("— Sin registros.")
    else:
        for i, (f, t, cmt) in enumerate(mant, start=1):
            extra = f" · {short(cmt,60)}" if cmt else ""
            lines.append(f"{i}. {f} | {t}{extra}")

    lines.append("\n⚠️ Alertas:")
    if not alr:
        lines.append("— Sin registros.")
    else:
        for i, (f, d, s, r) in enumerate(alr, start=1):
            status = "✅ resuelta" if r else "🟡 abierta"
            sector = f" · {s}" if s else ""
            lines.append(f"{i}. {f} | {d}{sector} · {status}")

    await update.message.reply_text("\n".join(lines), reply_markup=kb_main())

# =========================
# AJUSTES & ACERCA DE
# =========================
async def ajustes_acercade_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "⚙️ **AJUSTES & ACERCA DE**\n"
        "Los datos solo valen si te sirven a ti. Cada ajuste debe ser una declaración de independencia.\n"
        "• /proposito – por qué hacemos esto\n"
        "• /agriwisePRO – info del próximo AgriWise Pro + avisos\n"
        "• /notificaciones – activar/desactivar y horario\n"
        "• /reset – borra datos y empieza de cero (elige: solo registros o todo)\n"

    )
    kb = kb_with_cancel([[ "/proposito" ], [ "/agriwisePRO" ], [ "/notificaciones" ]])
    await update.message.reply_text(txt, reply_markup=kb)

async def ajustes_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ajustes_acercade_menu(update, context)

async def acerca_de_ajustes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await ajustes_acercade_menu(update, context)

async def agriwisePRO(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pro_text = (
        "🚀 Próximamente — AgriWise Pro\n"
        "Misma filosofía: simple, privada y bajo tu control."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔔 Avísame cuando salga", callback_data="waitlist_pro")]])
    await update.message.reply_text(pro_text, reply_markup=kb)

async def waitlist_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    name = q.from_user.first_name or ""
    conn = db()
    conn.execute("INSERT OR IGNORE INTO waitlist(user_id, name, date) VALUES (?,?,?)",
                 (uid, name, datetime.now().strftime("%Y-%m-%d")))
    conn.commit(); conn.close()
    await q.edit_message_text("✅ Te aviso al lanzar Pro. ¡Gracias! 🌱")

async def proposito(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Creemos en tecnología que devuelve el control al agricultor. Privada, clara y útil.",
        reply_markup=kb_main()
    )

# =========================
# MI AGUA + AJUSTES (wizard)
# =========================
async def mi_agua(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.message.from_user.id
    s    = get_settings(uid)
    prof = get_profile(uid)

    if not prof or (prof.get("caudal_m3h_ha") or 0) <= 0:
        await update.message.reply_text("Falta el caudal del sistema en tu perfil. Ve a /perfil (m³/h/ha).", reply_markup=kb_main())
        return
    if s["objetivo"] is None or s["precio"] is None:
        await update.message.reply_text("Vamos a configurarlo primero en /ajustes_agua.", reply_markup=kb_main())
        return

    hoy = datetime.now(); y, m = hoy.year, hoy.month
    ini = f"{y}-{m:02d}-01"
    fin = f"{y}-{m:02d}-{monthrange(y,m)[1]:02d}"

    conn = db()
    rows = conn.execute("SELECT horas FROM logs WHERE user_id=? AND fecha>=? AND fecha<=?",
                        (uid, ini, fin)).fetchall()
    conn.close()

    horas_tot = sum((r[0] or 0) for r in rows) if rows else 0.0
    m3ha      = horas_tot * float(prof["caudal_m3h_ha"] or 0.0)
    obj       = float(s["objetivo"])
    pct       = (m3ha/obj*100) if obj>0 else 0

    if pct < 70:  sem = "🟢"
    elif pct < 100: sem = "🟡"
    else:         sem = "🟥"

    txt = (f"💧 *Mi Agua* (mes actual)\n"
           f"- Objetivo: {obj:.0f} m³/ha/mes\n"
           f"- Acumulado (estimado): {m3ha:.0f} m³/ha\n"
           f"- Progreso: {pct:.0f}% {sem}\n")

    if s["precio"] not in (None, 0):
        coste = m3ha * float(s["precio"])
        txt  += f"- Coste estimado: ~{coste:.0f} € (a {float(s['precio']):.3f} €/m³)\n"

    await update.message.reply_text(txt, reply_markup=kb_main())

# /ajustes_agua: objetivo -> precio
async def ajustes_agua_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Objetivo mensual de agua (m³/ha). Ej: 1200", reply_markup=kb_cancel_only())
    return AJUAGUA_OBJ

async def ajustes_agua_obj(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        objetivo = float(update.message.text.replace(",", "."))
    except:
        await update.message.reply_text("Número inválido. Escribe solo el objetivo en m³/ha (ej: 1200).", reply_markup=kb_cancel_only())
        return AJUAGUA_OBJ
    save_settings(update.message.from_user.id, objetivo=objetivo)
    await update.message.reply_text("Precio del agua en €/m³. Ej: 0.12", reply_markup=kb_cancel_only())
    return AJUAGUA_PRECIO

async def ajustes_agua_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        precio = float(update.message.text.replace(",", "."))
    except:
        await update.message.reply_text("Número inválido. Escribe el precio en €/m³ (ej: 0.12).", reply_markup=kb_cancel_only())
        return AJUAGUA_PRECIO
    save_settings(update.message.from_user.id, precio=precio)
    await update.message.reply_text("✅ Ajustes guardados. Ya puedes ver /mi_agua.", reply_markup=kb_main())
    return ConversationHandler.END

# =========================
# ETo RÁPIDA
# =========================
async def eto_rapida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mes  = datetime.now().month
    vals = ETO_MESES.get(mes, [3.0, 4.0, 5.0])
    kb   = kb_with_cancel([[f"{v:.1f}" for v in vals]])
    await update.message.reply_text(f"ETo rápida — Mes {mes}. Elige un valor:", reply_markup=kb)
    return ETO_RÁPIDA_VALOR if False else ETO_RAPIDA_VALOR  # mantener nombre correcto

async def eto_rapida_valor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        eto = float(update.message.text.replace(",", "."))
    except:
        await update.message.reply_text("Elige un valor del teclado (ej. 3.5).", reply_markup=kb_cancel_only())
        return ETO_RAPIDA_VALOR
    context.user_data["eto"] = eto
    reply_kb = kb_with_cancel([["sin_estres","leve","moderado"]])
    await update.message.reply_text("Nivel de estrés hídrico:", reply_markup=reply_kb)
    return RIEGO_STRESS  # reutiliza riego_calc

# =========================
# NOTIFICACIONES — enriquecidas "Titular + Ver más"
# =========================
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Madrid")
except Exception:
    TZ = None

def parse_hhmm(s:str):
    try:
        hh, mm = s.split(":")
        return dtime(int(hh), int(mm), tzinfo=TZ)
    except:
        return dtime(8, 0, tzinfo=TZ)

def _mark(label:str, is_selected:bool) -> str:
    return f"✅ {label}" if is_selected else label

def _kind_label(k: str) -> str:
    m = {
        "habitos": "Hábitos",
        "micro": "Micro-formación",
        "mixto": "Mixto",
        # compat nombres antiguos:
        "riego": "Micro-formación",
        "mantenimiento": "Hábitos",
    }
    return m.get((k or "mixto").lower(), "Mixto")

def notif_status_text(uid:int):
    s = get_settings(uid)
    estado = "activas" if s['notify_enabled'] else "inactivas"
    tipo = _kind_label(s.get("notify_kind"))
    return (f"🔔 *Notificaciones*\n"
            f"- Estado: {estado}\n"
            f"- Hora: {s['notify_time']}\n"
            f"- Tipo: {tipo}\n"
            f"- Frecuencia: {s['notify_freq']}")

# -------------------------
# 100 notificaciones (1-50 Hábitos / 51-100 Micro-formación)
# -------------------------
NOTIFICATIONS = [
    # HÁBITOS (1..50)
    (1,  "HÁBITOS", "Revisa la presión", "Una presión estable es el corazón de un riego eficiente. Comprueba manómetros en cada turno."),
    (2,  "HÁBITOS", "Limpia los filtros", "Un filtro sucio es como un corazón con colesterol. Limpia y revisa el doble de lo que consideres necesario."),
    (3,  "HÁBITOS", "Registra tus horas de riego", "Lo que no se mide, no se mejora. Anota las horas reales y comparalas con lo recomendado."),
    (4,  "HÁBITOS", "Observa la uniformidad", "Camina la finca: si hay zonas secas o encharcadas, algo no va bien."),
    (5,  "HÁBITOS", "Comprueba goteros", "Un simple recorrido te mostrará goteros obturados o abiertos. La limpieza preventiva evita caídas de uniformidad."),
    (6,  "HÁBITOS", "La tecnología ayuda...", "pero el que riega eres tú, no una app."),
    (7,  "HÁBITOS", "Anota incidencias", "Usa /alerta para registrar fugas o problemas. El histórico te ayuda a ver patrones de fallo. Y esto es oro."),
    (8,  "HÁBITOS", "Controla el caudal", "Revisa el contador principal: una caída o subida repentina puede ser señal de fuga o válvula cerrada."),
    (9,  "HÁBITOS", "Verifica válvulas", "Las válvulas que no cierran bien hacen perder presión y uniformidad. Un repaso semanal evita sorpresas."),
    (10, "HÁBITOS", "Mide la presión en sectores y puntas", "La diferencia entre válvula y final de linea no debe superar el diseño en más de 10%. Si es mayor, toca mantenimiento."),
    (11, "HÁBITOS", "No riegues de noche sin control", "Un fallo nocturno puede pasar desapercibido. Si es necesario, instala alarmas o revisa temprano."),
    (12, "HÁBITOS", "Frase típica de un pecador de la pradera", "Si no funciona, no se arregla. Lo del mantenimiento ya tal si eso."),
    (13, "HÁBITOS", "Planifica el lavado de tuberías", "Un calendario de lavado evita olvidos. Programa según la turbidez de tu agua."),
    (14, "HÁBITOS", "Usa /resumen cada semana", "Tu registro vale oro. Con /resumen verás en segundos la salud del sistema."),
    (15, "HÁBITOS", "Cierra válvulas manuales tras cada turno", "Pequeños descuidos provocan grandes pérdidas. Cierra y revisa antes de irte."),
    (16, "HÁBITOS", "Más vale mancha de barro...", "que semana de dudas."),
    (17, "HÁBITOS", "Compara sectores", "¿Uno necesita más horas que otro? Puede haber diferencias en suelo o caudal. Investígalo."),
    (18, "HÁBITOS", "Revisa programadores", "Un error en el reloj puede tirar por tierra tus cálculos. Sincronízalos con el horario real."),
    (19, "HÁBITOS", "Documenta cambios", "Si cambias goteros o válvulas, regístralo en tus notas. Te ahorrará dudas futuras."),
    (20, "HÁBITOS", "Observa las hojas", "Las plantas hablan. Si el color o vigor cambian, revisa riego y estrés hídrico."),
    (21, "HÁBITOS", "Controla el mantenimiento", "Un mantenimiento ordenado evita parones. Usa /mantenimiento para dejar constancia."),
    (22, "HÁBITOS", "No te fíes del ojo", "La sensación visual puede engañar. La ciencia es tu mejor aliada."),
    (23, "HÁBITOS", "Evita riegos largos", "Mejor dos riegos cortos que uno largo. Favorecen la oxigenación y reducen pérdidas."),
    (24, "HÁBITOS", "Revisa antes de fertirrigar", "Un sistema en mal estado reparte mal el fertilizante. Asegúrate de que todo esté en orden."),
    (25, "HÁBITOS", "Comprueba fugas pequeñas", "Las pérdidas pequeñas suman. Un 1 % diario es un pozo sin fondo de agua y energía."),
    (26, "HÁBITOS", "Mide la ETo local", "Y ten en cuenta que la estación más cercana puede estar a kilómetros. Ajusta según tu microclima."),
    (27, "HÁBITOS", "Asegura la eficiencia", "La eficiencia del sistema cambia con el tiempo. Revisa y recalcula cada temporada."),
    (28, "HÁBITOS", "No ignores el ruido", "Si silba, no es que esté cantando flamenco. Te está pidiendo mantenimiento."),
    (29, "HÁBITOS", "Observa la presión en el retorno de la bomba", "Un retorno alto indica filtros sucios. Actúa antes de que bajen los caudales."),
    (30, "HÁBITOS", "Registra lluvias", "Guarda datos de lluvia: te ayudarán a sacar conclusiones con el balance total de agua aplicada."),
    (31, "HÁBITOS", "Controla el pH del agua", "Un pH fuera de rango obstruye emisores. Ajusta con ácido si es necesario."),
    (32, "HÁBITOS", "Mantén limpia la caseta", "El orden en la instalación refleja el orden en la gestión. Evita polvo y humedad."),
    (33, "HÁBITOS", "Anota limpiezas de filtros", "Llevar control evita sobrelavados y ahorra energía."),
    (34, "HÁBITOS", "Ajusta la frecuencia de riego", "Cambia con la evapotranspiración y el desarrollo del cultivo. No te quedes con la rutina."),
    (35, "HÁBITOS", "Usa sensores con criterio", "Los sensores ayudan, pero no sustituyen tu criterio. Combina datos y experiencia."),
    (36, "HÁBITOS", "Comprueba la ETo antes de regar", "Una simple consulta diaria puede ahorrarte miles de litros."),
    (37, "HÁBITOS", "No fertilices sin presión estable", "Los fertilizantes mal repartidos queman raíces y hojas. Asegura uniformidad antes de inyectar."),
    (38, "HÁBITOS", "Mide caudales en campo", "Usa cubos y cronómetro en distintos puntos. Es la prueba más sencilla de uniformidad."),
    (39, "HÁBITOS", "Ajusta por tipo de suelo", "El suelo arcilloso necesita descansos; el arenoso, más frecuencia. Adáptate."),
    (40, "HÁBITOS", "No olvides registrar", "Tus datos son tu historia. Cada parte guardado mejora tu toma de decisiones."),
    (41, "HÁBITOS", "Limpia goteros con ácido suave", "Elimina carbonatos sin dañar plásticos. Usa dosis controladas y enjuaga."),
    (42, "HÁBITOS", "Verifica contadores", "Contadores imprecisos engañan. Contrasta con tiempos reales de riego."),
    (43, "HÁBITOS", "¿Cuál es el animal más antiguo?", "La zebra, porque está en blanco y negro."),
    (44, "HÁBITOS", "Evita riegos cortos sin sentido", "El riego debe llegar al bulbo (y no quedarse en la superficie). No dispares el sistema por inercia."),
    (45, "HÁBITOS", "Comprueba la uniformidad al menos cada 2 meses", "Un chequeo rápido detecta fallos ocultos y mantiene la eficiencia alta."),
    (46, "HÁBITOS", "Mantén repuestos a mano", "Una caja con juntas, manómetros, tapones y goteros te salvará en plena campaña."),
    (47, "HÁBITOS", "Educa al equipo", "Si el equipo entiende el porqué de cada acción, los errores bajan drásticamente."),
    (48, "HÁBITOS", "Programa alertas internas", "Usa recordatorios o calendarios para no dejar pasar mantenimientos."),
    (49, "HÁBITOS", "Evalúa antes de ampliar", "Si piensas añadir sectores a un turno de riego, revisa que presión y caudal estén en rango."),
    (50, "HÁBITOS", "Cuida tu sistema", "El riego bien gestionado ahorra agua, energía y problemas. Cada revisión cuenta."),
    # MICRO-FORMACIÓN (51..100)
    (51, "MICRO-FORMACIÓN", "Qué es la ETo", "Es la evapotranspiración de referencia: la demanda de agua de una superficie estándar. Base de cualquier cálculo de riego."),
    (52, "MICRO-FORMACIÓN", "Cómo se calcula la ETc", "ETc = ETo × Kc. Donde Kc depende del cultivo y su fase. Así sabes cuánta agua necesita tu planta."),
    (53, "MICRO-FORMACIÓN", "Qué significa Kc", "El coeficiente de cultivo traduce la ETo en necesidad real. Varía con el desarrollo y cobertura del cultivo."),
    (54, "MICRO-FORMACIÓN", "La importancia del suelo", "El tipo de suelo determina la capacidad de retención y la frecuencia de riego. No hay receta única."),
    (55, "MICRO-FORMACIÓN", "La eficiencia del sistema", "Si tu sistema tiene una eficiencia del 90 %, el 10 % del agua se pierde. Cuanto más preciso, menos desperdicio."),
    (56, "MICRO-FORMACIÓN", "Cómo afecta la cubierta vegetal", "Aumenta la evaporación y puede necesitar un ajuste de +10 % en el cálculo de riego."),
    (57, "MICRO-FORMACIÓN", "Qué es el estrés hídrico controlado", "Un déficit leve puede mejorar la calidad del fruto si se aplica en el momento adecuado."),
    (58, "MICRO-FORMACIÓN", "El papel de la uniformidad", "Un sistema uniforme permite aplicar la dosis exacta en toda la finca, sin zonas de exceso ni déficit."),
    (59, "MICRO-FORMACIÓN", "Qué es la presión nominal", "Cada emisor tiene una presión óptima. Por debajo o por encima, el caudal varía y la uniformidad cae."),
    (60, "MICRO-FORMACIÓN", "Por qué medir la ETo local", "Cada microclima es distinto. Una estación cercana puede no representar tu finca real."),
    (61, "MICRO-FORMACIÓN", "Qué significa eficiencia de aplicación", "Es la proporción de agua efectivamente aprovechada por el cultivo. Depende del diseño y manejo."),
    (62, "MICRO-FORMACIÓN", "Cómo se calcula el volumen de riego", "Riego (mm) × 10 = m³/ha. Multiplica por tus hectáreas para saber el volumen total aplicado."),
    (63, "MICRO-FORMACIÓN", "Qué es la uniformidad de Christiansen", "Es un índice que mide la homogeneidad del riego. Cuanto más cercano a 1, mejor."),
    (64, "MICRO-FORMACIÓN", "El papel de la temperatura", "A mayor temperatura y viento, mayor demanda de agua. Ajusta frecuencias."),
    (65, "MICRO-FORMACIÓN", "Cómo afecta la salinidad", "Aguas salinas elevan el estrés osmótico. Lava el perfil con riegos más largos periódicos."),
    (66, "MICRO-FORMACIÓN", "Qué es la curva de infiltración", "Describe cómo entra el agua en el suelo con el tiempo. Ayuda a definir duración de turnos."),
    (67, "MICRO-FORMACIÓN", "Qué es el punto de marchitez permanente", "Es cuando la planta no puede recuperar turgencia. Evita que el suelo llegue a ese nivel."),
    (68, "MICRO-FORMACIÓN", "Por qué dividir riegos", "Repartir la dosis mejora oxigenación y reduce pérdidas por percolación."),
    (69, "MICRO-FORMACIÓN", "Qué es el bulbo húmedo", "Es la zona del suelo mojada por el emisor. Su forma depende del tipo de suelo y caudal."),
    (70, "MICRO-FORMACIÓN", "Qué es la curva de retención", "Relaciona humedad del suelo y succión. Permite saber cuánta agua disponible hay realmente."),
    (71, "MICRO-FORMACIÓN", "Cómo interpretar la ETo", "Si la ETo sube, aumenta el riego; si baja, reduce. Es el pulso climático diario del cultivo."),
    (72, "MICRO-FORMACIÓN", "Qué es el coeficiente dual Kcb+Ke", "Separa la transpiración del cultivo y la evaporación del suelo. Mejora la precisión en cultivos jóvenes."),
    (73, "MICRO-FORMACIÓN", "Por qué registrar datos", "Los datos acumulados revelan patrones: eficiencia, consumo, mantenimiento. Es conocimiento aplicado."),
    (74, "MICRO-FORMACIÓN", "Cómo afecta la densidad de plantación", "Más plantas por hectárea = mayor consumo total, pero menor por planta. El equilibrio importa."),
    (75, "MICRO-FORMACIÓN", "Qué es la curva de estrés", "Muestra cómo responde la planta a déficit de agua. Úsala para programar riegos deficitarios controlados."),
    (76, "MICRO-FORMACIÓN", "Qué es la pluviometría efectiva", "No toda la lluvia cuenta. Parte se pierde por escorrentía o evaporación."),
    (77, "MICRO-FORMACIÓN", "Por qué usar ETo y no solo lluvia", "La lluvia no mide la demanda atmosférica. La ETo sí: integra sol, viento, temperatura y humedad."),
    (78, "MICRO-FORMACIÓN", "Qué es el coeficiente de reducción", "Ajusta la ETo por factores de estrés o salinidad. Mantiene el balance realista."),
    (79, "MICRO-FORMACIÓN", "Qué significa déficit de presión", "Una caída repentina indica obstrucciones o fugas. Siempre investiga la causa."),
    (80, "MICRO-FORMACIÓN", "Cómo leer un manómetro", "Asegúrate de que esté calibrado y ubicado en puntos clave: cabezal y final de línea."),
    (81, "MICRO-FORMACIÓN", "Qué es la capacidad de campo", "Es la humedad máxima que el suelo retiene después del drenaje. Base para definir frecuencia de riego."),
    (82, "MICRO-FORMACIÓN", "Cómo afecta el viento", "Aumenta evaporación y reduce eficiencia. Evita riegos con viento fuerte."),
    (83, "MICRO-FORMACIÓN", "Qué es la evapotranspiración real", "Es la pérdida real de agua del cultivo, ajustada a sus condiciones actuales."),
    (84, "MICRO-FORMACIÓN", "Qué significa Kc medio", "Es el valor promedio del ciclo. Puede ser útil para balances estacionales."),
    (85, "MICRO-FORMACIÓN", "Cómo afecta la cobertura del suelo", "Cuanto más cubierto el suelo, menor evaporación directa y mejor eficiencia."),
    (86, "MICRO-FORMACIÓN", "Qué es la fracción de lavado", "Es el exceso de agua aplicado para evitar acumulación de sales. Necesario en aguas salinas."),
    (87, "MICRO-FORMACIÓN", "Cómo influye la pendiente", "Las laderas favorecen escorrentía. Ajusta duración o caudal para evitar pérdidas."),
    (88, "MICRO-FORMACIÓN", "Qué es la presión dinámica", "Es la presión durante el flujo. Siempre contrólala, no te bases solo en presión estática."),
    (89, "MICRO-FORMACIÓN", "Qué es un balance hídrico", "Resume entradas y salidas de agua. Permite saber si riegas más o menos de lo que el cultivo necesita."),
    (90, "MICRO-FORMACIÓN", "Qué es el punto de recarga", "Nivel mínimo aceptable de humedad antes de volver a regar."),
    (91, "MICRO-FORMACIÓN", "Cómo afecta el tamaño de copa", "Más copa = más transpiración. Ajusta el riego según el desarrollo vegetativo."),
    (92, "MICRO-FORMACIÓN", "Qué es la presión crítica", "Es el valor mínimo que garantiza el caudal nominal. Si bajas de ahí, pierdes uniformidad."),
    (93, "MICRO-FORMACIÓN", "Cómo evaluar eficiencia", "Divide ETc aplicada entre ETc teórica. Si es menor del 85 %, hay margen de mejora."),
    (94, "MICRO-FORMACIÓN", "Qué es la curva de avance del frente húmedo", "Muestra cómo se distribuye el agua en el perfil. Ayuda a ajustar tiempos."),
    (95, "MICRO-FORMACIÓN", "Por qué medir antes de actuar", "Cada decisión basada en datos reduce errores y mejora resultados."),
    (96, "MICRO-FORMACIÓN", "Qué es la tensión del suelo", "Es la fuerza con que el suelo retiene el agua. Se mide con tensiómetros o sensores capacitivos."),
    (97, "MICRO-FORMACIÓN", "Qué significa caudal específico", "Es el volumen aplicado por hora y hectárea. Base de cualquier planificación de turnos."),
    (98, "MICRO-FORMACIÓN", "Qué es la evapotranspiración potencial", "Es la ETo corregida por disponibilidad de agua y desarrollo vegetal."),
    (99, "MICRO-FORMACIÓN", "Qué es el balance energético", "El sol aporta energía, parte se usa en evaporar agua. Relacionarlo con ETo explica mucho del consumo."),
    (100,"MICRO-FORMACIÓN", "Qué es la eficiencia energética en riego", "Cuanta menos energía uses por m³ aplicado, mejor tu gestión. Optimiza presiones y horarios."),
]

def _notif_block_line(block:str, title:str) -> str:
    return f"🧩 <b>{title}</b>\n<i>{block}</i>"

def _ids_habitos(): return list(range(1, 51))
def _ids_micro():   return list(range(51, 101))

# En "mixto": alterna automáticamente día par → hábitos, día impar → micro
def _allowed_ids(kind:str) -> list[int]:
    k = (kind or "mixto").strip().lower()
    # compat: 'riego' == micro-formación, 'mantenimiento' == hábitos
    if k in ("micro", "riego"):
        return _ids_micro()
    if k in ("habitos", "mantenimiento"):
        return _ids_habitos()
    # mixto → alterna automáticamente día par/impar
    parity = datetime.now().toordinal() % 2  # 0 par → hábitos, 1 impar → micro
    return _ids_habitos() if parity == 0 else _ids_micro()

def _day_based_start(allowed:list[int]) -> int:
    if not allowed: return 1
    idx = (datetime.now().toordinal() - 1) % len(allowed)
    return allowed[idx]

def _next_index_for(uid:int) -> int:
    s = get_settings(uid)
    last = int(s.get("notif_last_idx", -1))
    allowed = _allowed_ids(s.get("notify_kind"))
    if not allowed:
        return 1
    if last in allowed:
        pos = allowed.index(last)
        return allowed[(pos + 1) % len(allowed)]
    return _day_based_start(allowed)

def _save_last_idx(uid:int, idx:int):
    conn = db()
    conn.execute("UPDATE user_settings SET notif_last_idx=? WHERE user_id=?", (idx, uid))
    conn.commit(); conn.close()

def _notif_build_more_kb(nid:int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Ver más", callback_data=f"n:more:{nid}")]])

def _notif_panel_keyboard(uid:int) -> InlineKeyboardMarkup:
    s = get_settings(uid)
    enabled = bool(s["notify_enabled"])
    onoff_text = ("🟢 Activar" if not enabled else "🔴 Desactivar")

    b_onoff = InlineKeyboardButton(onoff_text, callback_data="notif_toggle")

    h = s["notify_time"]
    b07 = InlineKeyboardButton(_mark("🕗 07:00", h=="07:00"), callback_data="notif_time:07:00")
    b08 = InlineKeyboardButton(_mark("08:00",  h=="08:00"),  callback_data="notif_time:08:00")
    b20 = InlineKeyboardButton(_mark("20:00",  h=="20:00"),  callback_data="notif_time:20:00")

    k = (s["notify_kind"] or "mixto").lower()
    # compat en selección visual
    t1 = InlineKeyboardButton(_mark("📗 Hábitos",          k in ("habitos","mantenimiento")), callback_data="notif_kind:habitos")
    t2 = InlineKeyboardButton(_mark("📘 Micro-formación",  k in ("micro","riego")),           callback_data="notif_kind:micro")
    t3 = InlineKeyboardButton(_mark("🔁 Mixto",            k=="mixto"),                       callback_data="notif_kind:mixto")

    f = (s["notify_freq"] or "diaria").lower()
    f1 = InlineKeyboardButton(_mark("📅 Diaria",  f=="diaria"),  callback_data="notif_freq:diaria")
    f2 = InlineKeyboardButton(_mark("📆 Semanal", f=="semanal"), callback_data="notif_freq:semanal")

    test = InlineKeyboardButton("▶️ Enviar una ahora", callback_data="notif_test_now")
    ok   = InlineKeyboardButton("✅ OK", callback_data="notif_ok")

    return InlineKeyboardMarkup([
        [b_onoff],
        [b07, b08, b20],
        [t1, t2, t3],
        [f1, f2],
        [test],
        [ok]
    ])

async def notificaciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    await update.message.reply_text(
        notif_status_text(uid),
        reply_markup=_notif_panel_keyboard(uid)
    )

async def _send_enriched_notification(context: ContextTypes.DEFAULT_TYPE, uid:int):
    s = get_settings(uid)
    if not s or not s.get("notify_enabled"):
        return
    idx = _next_index_for(uid)
    tup = next((t for t in NOTIFICATIONS if t[0]==idx), None)
    if not tup:
        return
    nid, block, title, body = tup
    head = _notif_block_line(block, title)
    await context.bot.send_message(
        chat_id=uid,
        text=head,
        parse_mode="HTML",
        reply_markup=_notif_build_more_kb(nid)
    )
    _save_last_idx(uid, idx)

async def notify_callback(context: ContextTypes.DEFAULT_TYPE):
    uid = context.job.chat_id
    await _send_enriched_notification(context, uid)

def schedule_user_notifications(app, uid:int):
    # Limpia previas
    for job in app.job_queue.get_jobs_by_name(f"notif_{uid}"):
        job.schedule_removal()
    s = get_settings(uid)
    if not s or not s.get("notify_enabled"):
        return
    t = parse_hhmm(s.get("notify_time") or "08:00")
    name = f"notif_{uid}"
    freq = (s.get("notify_freq") or "diaria").lower()
    if freq == "diaria":
        app.job_queue.run_daily(notify_callback, time=t, chat_id=uid, name=name)
    else:
        # semanal en el día actual de la semana
        weekday = datetime.now().weekday()
        app.job_queue.run_daily(notify_callback, time=t, days=(weekday,), chat_id=uid, name=name)

async def _refresh_notif_panel(q, context, uid: int):
    try:
        await q.edit_message_text(
            notif_status_text(uid),
            reply_markup=_notif_panel_keyboard(uid)
        )
    except Exception:
        try:
            await q.delete_message()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text=notif_status_text(uid),
            reply_markup=_notif_panel_keyboard(uid)
        )

async def notif_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()
    data = q.data or ""

    if data == "notif_toggle":
        s = get_settings(uid)
        save_settings(uid, notify_enabled=0 if s["notify_enabled"] else 1)
        schedule_user_notifications(context.application, uid)
        await _refresh_notif_panel(q, context, uid); return

    if data.startswith("notif_time:"):
        hhmm = data.split(":", 1)[1]
        save_settings(uid, notify_time=hhmm)
        schedule_user_notifications(context.application, uid)
        await _refresh_notif_panel(q, context, uid); return

    if data.startswith("notif_kind:"):
        kind = data.split(":", 1)[1]
        save_settings(uid, notify_kind=kind)
        schedule_user_notifications(context.application, uid)
        await _refresh_notif_panel(q, context, uid); return

    if data.startswith("notif_freq:"):
        freq = data.split(":", 1)[1]
        save_settings(uid, notify_freq=freq)
        schedule_user_notifications(context.application, uid)
        await _refresh_notif_panel(q, context, uid); return

    if data == "notif_test_now":
        await _send_enriched_notification(context, uid)
        await q.answer("Enviada una notificación de prueba.", show_alert=False)
        return

    if data == "notif_ok":
        try:
            await q.delete_message()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=q.message.chat_id,
            text="Volvemos al menú principal:",
            reply_markup=kb_main()
        )
        return

async def notif_more_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data  # n:more:<id>
    try:
        nid = int(data.split(":")[2])
    except Exception:
        return
    tup = next((t for t in NOTIFICATIONS if t[0]==nid), None)
    if not tup:
        return
    _id, block, title, body = tup
    full = f"{_notif_block_line(block, title)}\n\n{body}"
    try:
        await q.edit_message_text(full, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=full, parse_mode="HTML")

# =========================
# EXPORTAR SISTEMA TXT
# =========================
import io  # Asegúrate de tener este import arriba del todo

async def exportar_sistema_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    conn = db()

    est = conn.execute("""
        SELECT fecha, presion, filtros, COALESCE(valvulas,''), COALESCE(goteros,''), COALESCE(nota,'')
          FROM sys_estado
         WHERE user_id=?
         ORDER BY id DESC LIMIT 10
    """, (uid,)).fetchall()

    mant = conn.execute("""
        SELECT fecha, tarea, COALESCE(comentario,'')
          FROM sys_mant
         WHERE user_id=?
         ORDER BY id DESC LIMIT 10
    """, (uid,)).fetchall()

    alr = conn.execute("""
        SELECT fecha, descripcion, COALESCE(sector,''), COALESCE(resuelta,0)
          FROM sys_alerta
         WHERE user_id=?
         ORDER BY id DESC LIMIT 10
    """, (uid,)).fetchall()

    conn.close()

    # Construir contenido
    lines = []
    lines.append("📊 EXPORTACIÓN DE SISTEMA — AgriWise Bot")
    lines.append(f"Usuario: {uid}")
    lines.append(f"Fecha de exportación: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    lines.append("🔎 ESTADO (últimos 10)")
    if not est:
        lines.append("— Sin registros")
    else:
        for (f, p, fi, va, go, n) in est[::-1]:  # del más antiguo al más reciente
            extra = f" · Nota: {n.strip()}" if n else ""
            lines.append(f"{f} | {p} · {fi} · {va} · {go}{extra}")
    lines.append("")

    lines.append("🛠️ MANTENIMIENTO (últimos 10)")
    if not mant:
        lines.append("— Sin registros")
    else:
        for (f, t, cmt) in mant[::-1]:
            extra = f" · {cmt.strip()}" if cmt else ""
            lines.append(f"{f} | {t}{extra}")
    lines.append("")

    lines.append("⚠️ ALERTAS (últimos 10)")
    if not alr:
        lines.append("— Sin registros")
    else:
        for (f, d, s, r) in alr[::-1]:
            status = "resuelta" if int(r) else "abierta"
            sector = f" · Sector: {s.strip()}" if s else ""
            lines.append(f"{f} | {d}{sector} · {status}")
    lines.append("")

    content = "\n".join(lines) + "\n"
    buf = io.BytesIO(content.encode("utf-8"))
    buf.name = f"export_sistema_{uid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

    await update.message.reply_document(document=buf, caption="📄 Exportación básica (TXT) — estado sistema de riego (últimos 10)")


# =========================
# DESCARGAS
# =========================
async def descargas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "📤 *DESCARGAS Y EXPORTACIONES*\n"
        "Gestiona tus datos sin depender de nadie.\n"
        "Privado, simple y bajo tu control.\n\n"
        "• /exportar_txt → Últimos riegos en formato TXT\n"
        "• /exportar_sistema_txt → Checklist, mantenimiento y alertas\n"
        "\n"
        "Cada archivo se genera al momento y solo tú puedes verlo. 🌱"
    )
    await update.message.reply_text(txt, reply_markup=kb_main())

# =========================
# RESET DE DATOS (por usuario)
# =========================
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

async def reset_datos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    txt = (
        "⚠️ *Reiniciar datos*\n"
        "Elige qué quieres borrar para este usuario.\n\n"
        "• 🧹 *Solo registros*: Riegos + Estado + Mantenimiento + Alertas\n"
        "• 🧨 *Todo*: Lo anterior *+ Perfil + Ajustes*\n\n"
        "_Acción irreversible._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Solo registros", callback_data="reset_do:reg")],
        [InlineKeyboardButton("🧨 Todo (incluye perfil y ajustes)", callback_data="reset_do:all")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="reset_cancel")],
    ])
    await update.message.reply_text(txt, reply_markup=kb, parse_mode="Markdown")

async def reset_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""

    if data == "reset_cancel":
        try:
            await q.delete_message()
        except Exception:
            pass
        await context.bot.send_message(chat_id=q.message.chat_id, text="Operación cancelada.", reply_markup=kb_main())
        return

    # Ejecutar borrado
    conn = db()
    try:
        if data == "reset_do:reg":
            conn.execute("DELETE FROM logs WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM sys_estado WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM sys_mant WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM sys_alerta WHERE user_id=?", (uid,))
            conn.commit()
            msg = "🧹 Listo. Se han borrado *riegos* y *registros de sistema*."
        elif data == "reset_do:all":
            conn.execute("DELETE FROM logs WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM sys_estado WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM sys_mant WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM sys_alerta WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM profiles WHERE user_id=?", (uid,))
            conn.execute("DELETE FROM user_settings WHERE user_id=?", (uid,))
            conn.commit()
            msg = "🧨 Reinicio completo. Se han borrado *registros*, *perfil* y *ajustes*."
        else:
            msg = "Nada que hacer."
    except Exception as e:
        conn.rollback()
        msg = f"❌ Error al borrar: {e}"
    finally:
        conn.close()

    try:
        await q.edit_message_text(msg, parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(chat_id=q.message.chat_id, text=msg)

    # Vuelve al menú principal sin reimprimir el banner
    await context.bot.send_message(chat_id=q.message.chat_id, text="Volvemos al menú principal:", reply_markup=kb_main())

# =========================
# App
# =========================
def build_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Menús secciones
    app.add_handler(CommandHandler(["start"], start))
    app.add_handler(CommandHandler(["ayuda"], ayuda))
    app.add_handler(CommandHandler(["menu_finca"], finca_menu))
    app.add_handler(CommandHandler(["menu_riego"], riego_menu))
    app.add_handler(CommandHandler(["menu_sistema"], sistema_menu))
    app.add_handler(CommandHandler(["menu_ajustes_acercade"], ajustes_acercade_menu))
    app.add_handler(CommandHandler(["menu_ajustes"], ajustes_menu))               # alias
    app.add_handler(CommandHandler(["acerca_de_ajustes"], acerca_de_ajustes))     # alias
    app.add_handler(CommandHandler("notificaciones", notificaciones))
    app.add_handler(CommandHandler("exportar_txt", exportar_txt))
    app.add_handler(CommandHandler("exportar_sistema_txt", exportar_sistema_txt))
    app.add_handler(CommandHandler("cancelar", cancelar))
    app.add_handler(CommandHandler("descargas", descargas))
    # Reset de datos
    app.add_handler(CommandHandler("reset", reset_datos))
    app.add_handler(CallbackQueryHandler(reset_cb, pattern=r"^(reset_do:(reg|all)|reset_cancel)$"))

    # FINCA
    perfil_conv = ConversationHandler(
        entry_points=[CommandHandler("perfil", perfil)],
        states={
            PERFIL_CULTIVO:   [MessageHandler(filters.TEXT & ~filters.COMMAND, perfil_cultivo)],
            PERFIL_SUELO:     [MessageHandler(filters.TEXT & ~filters.COMMAND, perfil_suelo)],
            PERFIL_CUBIERTA:  [MessageHandler(filters.TEXT & ~filters.COMMAND, perfil_cubierta)],
            PERFIL_EFICIENCIA:[MessageHandler(filters.TEXT & ~filters.COMMAND, perfil_eficiencia)],
            PERFIL_CAUDAL:    [MessageHandler(filters.TEXT & ~filters.COMMAND, perfil_caudal)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(perfil_conv)

    perfil_avz_conv = ConversationHandler(
        entry_points=[CommandHandler(["perfil_avanzado","avanzado"], perfil_avanzado)],
        states={
            PERFIL_CANOPY: [MessageHandler(filters.TEXT & ~filters.COMMAND, perfil_canopy)],
            PERFIL_MARCO_X:[MessageHandler(filters.TEXT & ~filters.COMMAND, perfil_marco_x)],
            PERFIL_MARCO_Y:[MessageHandler(filters.TEXT & ~filters.COMMAND, perfil_marco_y)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(perfil_avz_conv)

    app.add_handler(CommandHandler("perfil_ver", perfil_ver))

    # RIEGO
    riego_conv = ConversationHandler(
        entry_points=[CommandHandler("riego", riego_cmd)],
        states={
            RIEGO_ETO: [
                MessageHandler(filters.Regex(r"^\d+([.,]\d+)?$") & ~filters.COMMAND, riego_eto),
                MessageHandler(filters.TEXT & ~filters.COMMAND, riego_eto),
            ],
            RIEGO_STRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, riego_calc)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(riego_conv)

    eto_rapida_conv = ConversationHandler(
        entry_points=[CommandHandler("eto_rapida", eto_rapida)],
        states={
            ETO_RAPIDA_VALOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, eto_rapida_valor)],
            RIEGO_STRESS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, riego_calc)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(eto_rapida_conv)

    # Registro de riegos (SIN cultivo)
    reg_conv = ConversationHandler(
        entry_points=[CommandHandler("registrar", registrar)],
        states={
            REG_SECTOR:  [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_sector)],
            REG_FECHA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_fecha)],
            REG_HORAS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_horas)],
            REG_NOTA:    [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_nota)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(reg_conv)

    app.add_handler(CommandHandler("historial", historial))

    # SISTEMA
    estado_conv = ConversationHandler(
        entry_points=[CommandHandler("estado", estado)],
        states={
            ESTADO_PRESION:   [MessageHandler(filters.TEXT & ~filters.COMMAND, estado_presion)],
            ESTADO_FILTROS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, estado_filtros)],
            ESTADO_VALVULAS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, estado_valvulas)],
            ESTADO_GOTEROS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, estado_goteros)],
            ESTADO_NOTA:      [MessageHandler(filters.TEXT & ~filters.COMMAND, estado_nota)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(estado_conv)

    mant_conv = ConversationHandler(
        entry_points=[CommandHandler("mantenimiento", mantenimiento)],
        states={
            MANT_TAREA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, mant_tarea)],
            MANT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, mant_confirm)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(mant_conv)

    alerta_conv = ConversationHandler(
        entry_points=[CommandHandler("alerta", alerta)],
        states={
            ALERTA_DESC:   [MessageHandler(filters.TEXT & ~filters.COMMAND, alerta_desc)],
            ALERTA_SECTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, alerta_sector)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(alerta_conv)

    # Resumen
    app.add_handler(CommandHandler("resumen", resumen))

    # Ajustes/Acerca de y otros
    app.add_handler(CommandHandler(["agriwisepro","agriwisePRO"], agriwisePRO))
    app.add_handler(CommandHandler("proposito", proposito))

    # Mi Agua + Ajustes Agua
    app.add_handler(CommandHandler("mi_agua", mi_agua))
    ajustes_agua_conv = ConversationHandler(
        entry_points=[CommandHandler("ajustes_agua", ajustes_agua_start)],
        states={
            AJUAGUA_OBJ:    [MessageHandler(filters.TEXT & ~filters.COMMAND, ajustes_agua_obj)],
            AJUAGUA_PRECIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ajustes_agua_precio)],
        },
        fallbacks=[CommandHandler("cancelar", cancelar)],
        allow_reentry=True,
    )
    app.add_handler(ajustes_agua_conv)

    # Callbacks
    app.add_handler(CallbackQueryHandler(waitlist_pro, pattern=r"^waitlist_pro$"))
    app.add_handler(CallbackQueryHandler(notif_cb, pattern=r"^notif_(toggle|time:.*|kind:.*|freq:.*|test_now|ok)$"))
    app.add_handler(CallbackQueryHandler(notif_more_cb, pattern=r"^n:more:\d+$"))

    # Reprogramar notificaciones activas al arrancar
    try:
        conn = db()
        uids = [r[0] for r in conn.execute("SELECT user_id FROM user_settings WHERE notify_enabled=1").fetchall()]
        conn.close()
        for uid in uids:
            schedule_user_notifications(app, uid)
    except Exception as e:
        print("[WARN] No se pudieron programar notificaciones al inicio:", e)

    return app

# --- AgriWise: registro remoto en tu WordPress ---
import os, json, requests

WEB_ENDPOINT = os.getenv("WEB_ENDPOINT", "https://domiperez.com/wp-json/agriwise/v1/log")
API_KEY      = os.getenv("API_KEY", "AGRIWISE_3kF2p9L0_2025")

def log_event(user_id, event, payload=None):
    try:
        data = {
            "user_id": str(user_id),
            "event": str(event),
            "payload": payload or {}
        }
        headers = {"X-AgriWise-Key": API_KEY, "Content-Type": "application/json"}
        r = requests.post(WEB_ENDPOINT, headers=headers, data=json.dumps(data), timeout=6)
        r.raise_for_status()
        return True
    except Exception as e:
        # Si falla el registro remoto, no paramos el bot
        print(f"[logger] fallo enviando evento: {e}")
        return False

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN en .env")
    app = build_app()
    print("AgriWise Bot arrancando…")
    app.run_polling()
