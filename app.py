import os
import re
import tempfile
import logging
from functools import wraps
from datetime import timedelta, datetime
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import Header
from email.utils import formataddr
import pdfplumber

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, abort, send_file, make_response
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from PyPDF2 import PdfReader, PdfWriter
from dotenv import load_dotenv
import pandas as pd
import psycopg2
import psycopg2.extras # Importante para obtener resultados como diccionarios

# Cargar variables de entorno desde .env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'tu_clave_secreta_aqui_12345_cambiala')

# Filtro Jinja2 para formatear montos: 5943.77 → 5,943.77
@app.template_filter('monto')
def format_monto(value):
    try:
        return f"{float(value):,.2f}"
    except (ValueError, TypeError):
        return '0.00'

# Configuración de sesión
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)

# Configuración de logging
logging.basicConfig(level=logging.INFO)
handler = logging.FileHandler('app.log')
app.logger.addHandler(handler)

# Configuración
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'temp_uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Configuración de correo
SMTP_SERVER = os.getenv('SMTP_SERVER')
SMTP_PORT = os.getenv('SMTP_PORT')
EMAIL_USER = os.getenv('EMAIL_USER')
EMAIL_PASS = os.getenv('EMAIL_PASS')

# --- Funciones de base de datos (PostgreSQL) ---
def get_db():
    """Establece una conexión con la base de datos PostgreSQL."""
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST'),
        dbname=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASS')
    )
    return conn

def init_db():
    """Inicializa el esquema de la base de datos en PostgreSQL."""

    tablas = [
        ("clientes", '''CREATE TABLE IF NOT EXISTS clientes (
                            rif TEXT PRIMARY KEY,
                            nombre TEXT,
                            correo TEXT,
                            actualizado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'''),
        ("usuarios", '''CREATE TABLE IF NOT EXISTS usuarios (
                            id SERIAL PRIMARY KEY,
                            username TEXT UNIQUE NOT NULL,
                            password TEXT NOT NULL,
                            nombre_completo TEXT NOT NULL,
                            email TEXT NOT NULL,
                            rol TEXT DEFAULT 'usuario',
                            creado TIMESTAMP DEFAULT CURRENT_TIMESTAMP)'''),
        ("historial_envios", '''CREATE TABLE IF NOT EXISTS historial_envios (
                            id SERIAL PRIMARY KEY,
                            fecha_envio TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            tipo_comprobante TEXT NOT NULL,
                            numero_comprobante TEXT,
                            numero_factura TEXT,
                            cliente_rif TEXT NOT NULL,
                            cliente_nombre TEXT,
                            cliente_correo TEXT,
                            usuario_id INTEGER,
                            usuario_nombre TEXT,
                            agente_retencion TEXT,
                            monto_retenido REAL,
                            estado TEXT)'''),
        ("fix_fk_usuario_set_null", """
            ALTER TABLE historial_envios
            DROP CONSTRAINT IF EXISTS historial_envios_usuario_id_fkey;
            ALTER TABLE historial_envios
            ADD CONSTRAINT historial_envios_usuario_id_fkey
            FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE SET NULL
        """),
        ("fix_cliente_correo_nullable",
         "ALTER TABLE historial_envios ALTER COLUMN cliente_correo DROP NOT NULL"),
        ("configuracion", '''CREATE TABLE IF NOT EXISTS configuracion (
                            clave VARCHAR(100) PRIMARY KEY,
                            valor TEXT,
                            descripcion TEXT,
                            actualizado TIMESTAMP DEFAULT NOW())'''),
    ]

    # Crear cada tabla en su propia transacción
    for nombre_tabla, sql in tablas:
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(sql)
            conn.commit()
            cur.close()
        except Exception as e:
            app.logger.error(f"Error creando tabla {nombre_tabla}: {e}")
            if conn: conn.rollback()
        finally:
            if conn: conn.close()

    # Insertar usuario admin por defecto
    try:
        admin = db_query("SELECT id FROM usuarios WHERE username = %s", ('admin',), fetchone=True)
        if not admin:
            password_hash = generate_password_hash('admin123')
            db_query(
                "INSERT INTO usuarios (username, password, nombre_completo, email, rol) VALUES (%s, %s, %s, %s, %s)",
                ('admin', password_hash, 'Administrador', 'admin@empresa.com', 'admin'),
                commit=True
            )
    except Exception as e:
        app.logger.error(f"Error creando usuario admin: {e}")

    # Insertar configuración por defecto
    defaults = [
        ('smtp_server',          os.getenv('SMTP_SERVER', 'mail.fmcenter.com.ve'),    'Servidor SMTP'),
        ('smtp_port',            os.getenv('SMTP_PORT', '587'),                        'Puerto SMTP'),
        ('email_user',           os.getenv('EMAIL_USER', 'impuestos@fmcenter.com.ve'), 'Correo remitente'),
        ('email_pass',           os.getenv('EMAIL_PASS', ''),                          'Contraseña del correo'),
        ('email_nombre',         'FM Center — Comprobantes',                            'Nombre visible del remitente'),
        ('plantilla_asunto_iva',
         'Comprobante de Retención de IVA N° {numero_comprobante}',
         'Asunto del correo para IVA'),
        ('plantilla_cuerpo_iva',
         'Estimado/a {nombre_cliente},\n\nAdjunto encontrará su Comprobante de Retención de IVA N° {numero_comprobante}.\n\nSaludos cordiales,\n{agente}',
         'Cuerpo del correo para IVA'),
        ('plantilla_asunto_islr',
         'Comprobante de Retención de ISLR - {agente}',
         'Asunto del correo para ISLR'),
        ('plantilla_cuerpo_islr',
         'Estimado/a {nombre_cliente},\n\nAdjunto encontrará su Comprobante de Retención de ISLR.\n\nSaludos cordiales,\n{agente}',
         'Cuerpo del correo para ISLR'),
    ]
    for clave, valor, desc in defaults:
        try:
            db_query("""
                INSERT INTO configuracion (clave, valor, descripcion)
                VALUES (%s, %s, %s)
                ON CONFLICT (clave) DO UPDATE
                    SET valor = EXCLUDED.valor, descripcion = EXCLUDED.descripcion
                WHERE configuracion.valor IS NULL OR configuracion.valor = ''
            """, (clave, valor, desc), commit=True)
        except Exception as e:
            app.logger.error(f"Error insertando config '{clave}': {e}")


        # ── Tablas del módulo Extractor de Plantillas ───────────────────────────
        extractor_tablas = [
            ("plantillas_ext", """CREATE TABLE IF NOT EXISTS plantillas_ext (
                id SERIAL PRIMARY KEY,
                nombre TEXT NOT NULL,
                descripcion TEXT,
                tipo_documento TEXT DEFAULT 'generico',
                activa BOOLEAN DEFAULT true,
                muestra_path TEXT,
                creado TIMESTAMP DEFAULT NOW(),
                actualizado TIMESTAMP DEFAULT NOW())"""),
            ("plantilla_campos", """CREATE TABLE IF NOT EXISTS plantilla_campos (
                id SERIAL PRIMARY KEY,
                plantilla_id INTEGER REFERENCES plantillas_ext(id) ON DELETE CASCADE,
                nombre_campo TEXT NOT NULL,
                etiqueta TEXT NOT NULL,
                tipo_campo TEXT DEFAULT 'texto',
                pagina INTEGER DEFAULT 1,
                x0 REAL, y0 REAL, x1 REAL, y1 REAL,
                es_tabla BOOLEAN DEFAULT false,
                patron_validacion TEXT,
                post_proceso TEXT,
                orden INTEGER DEFAULT 0)"""),
            ("extracciones_ext", """CREATE TABLE IF NOT EXISTS extracciones_ext (
                id SERIAL PRIMARY KEY,
                plantilla_id INTEGER REFERENCES plantillas_ext(id),
                archivo_nombre TEXT,
                fecha TIMESTAMP DEFAULT NOW(),
                usuario_id INTEGER,
                usuario_nombre TEXT,
                datos JSONB,
                estado TEXT DEFAULT 'exitoso')"""),
        ]
        for nombre_t, sql_t in extractor_tablas:
            try:
                conn_t = get_db()
                cur_t = conn_t.cursor()
                cur_t.execute(sql_t)
                conn_t.commit()
                cur_t.close()
                conn_t.close()
            except Exception as e:
                app.logger.error(f"Error creando tabla extractor {nombre_t}: {e}")
    app.logger.info("Base de datos inicializada.")

def db_query(query, params=None, fetchone=False, fetchall=False, commit=False):
    """Función genérica para ejecutar consultas y manejar la conexión."""
    conn = None
    try:
        conn = get_db()
        # Usar DictCursor para obtener resultados como diccionarios
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(query, params or ())
        
        if commit:
            conn.commit()
            return None # No hay nada que devolver en operaciones de escritura
        
        if fetchone:
            return cur.fetchone()
        
        if fetchall:
            return cur.fetchall()

    except Exception as e:
        app.logger.error(f"Error en la consulta a la base de datos: {e}")
        if conn:
            conn.rollback() # Revertir cambios en caso de error
        return None
    finally:
        if conn:
            cur.close()
            conn.close()

# --- Funciones CRUD adaptadas ---
def save_cliente(rif, nombre, correo):
    query = "INSERT INTO clientes (rif, nombre, correo) VALUES (%s, %s, %s) ON CONFLICT (rif) DO UPDATE SET nombre = EXCLUDED.nombre, correo = EXCLUDED.correo"
    db_query(query, (rif, nombre, correo), commit=True)

def get_cliente(rif):
    return db_query("SELECT * FROM clientes WHERE rif = %s", (rif,), fetchone=True)

def get_cliente_by_name(nombre):
    return db_query("SELECT * FROM clientes WHERE nombre LIKE %s", (f"%{nombre.strip()}%",), fetchone=True)

def get_all_clientes():
    return db_query("SELECT * FROM clientes ORDER BY nombre", fetchall=True)

def get_config(clave, default=''):
    """Obtiene un valor de configuración de la BD."""
    row = db_query("SELECT valor FROM configuracion WHERE clave = %s", (clave,), fetchone=True)
    return row['valor'] if row and row['valor'] is not None else default

def get_all_config():
    """Retorna todas las claves de configuración como diccionario."""
    rows = db_query("SELECT clave, valor FROM configuracion", fetchall=True) or []
    return {r['clave']: r['valor'] for r in rows}

def set_config(clave, valor):
    db_query("""
        INSERT INTO configuracion (clave, valor) VALUES (%s, %s)
        ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor, actualizado = NOW()
    """, (clave, valor), commit=True)

def create_user(username, password, nombre_completo, email, rol='usuario'):
    password_hash = generate_password_hash(password)
    try:
        query = "INSERT INTO usuarios (username, password, nombre_completo, email, rol) VALUES (%s, %s, %s, %s, %s)"
        db_query(query, (username, password_hash, nombre_completo, email, rol), commit=True)
        return True
    except psycopg2.IntegrityError: # Error específico para duplicados
        return False

def get_user_by_username(username):
    return db_query("SELECT * FROM usuarios WHERE username = %s", (username,), fetchone=True)

def verify_user(username, password):
    user = get_user_by_username(username)
    if user and check_password_hash(user['password'], password):
        return user
    return None

def get_user_by_id(user_id):
    return db_query("SELECT * FROM usuarios WHERE id = %s", (user_id,), fetchone=True)

def get_all_usuarios():
    return db_query("SELECT id, username, password, nombre_completo, email, rol, creado FROM usuarios ORDER BY username", fetchall=True)

def registrar_envio(datos_envio):
    tipo_doc = datos_envio.get('tipo_documento')
    num_comp = datos_envio.get('numero_comprobante') if tipo_doc == 'IVA' else None
    num_factura = None

    if tipo_doc == 'IVA':
        num_factura = datos_envio.get('numero_factura')
    elif tipo_doc == 'ISLR':
        facturas = datos_envio.get('facturas', [])
        num_factura = 'Varias' if len(facturas) > 1 else (facturas[0].get('numero') if facturas else None)

    query = """
        INSERT INTO historial_envios (
            tipo_comprobante, numero_comprobante, numero_factura, cliente_rif, 
            cliente_nombre, cliente_correo, usuario_id, 
            usuario_nombre, monto_retenido, agente_retencion, estado
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    params = (
        tipo_doc, num_comp, num_factura,
        datos_envio.get('rif_sujeto'), datos_envio.get('sujeto_retenido'), datos_envio.get('correo'),
        session.get('user_id'), session.get('nombre'),
        datos_envio.get('iva_retenido') or datos_envio.get('monto_total_retenido'),
        datos_envio.get('agente_retencion'), datos_envio.get('estado')
    )
    db_query(query, params, commit=True)

def get_historial(filtros=None):
    query = "SELECT * FROM historial_envios WHERE 1=1"
    params = []

    if filtros:
        if filtros.get('fecha_inicio'):
            query += " AND date(fecha_envio) >= %s"
            params.append(filtros['fecha_inicio'])
        if filtros.get('fecha_fin'):
            query += " AND date(fecha_envio) <= %s"
            params.append(filtros['fecha_fin'])
        if filtros.get('cliente_rif'):
            query += " AND cliente_rif = %s"
            params.append(filtros['cliente_rif'])
        if filtros.get('usuario_id'):
            query += " AND usuario_id = %s"
            params.append(filtros['usuario_id'])
        if filtros.get('agente_retencion'):
            query += " AND agente_retencion = %s"
            params.append(filtros['agente_retencion'])
        if filtros.get('tipo_comprobante'):
            query += " AND tipo_comprobante = %s"
            params.append(filtros['tipo_comprobante'])

    query += " ORDER BY fecha_envio DESC"
    return db_query(query, params, fetchall=True)

def update_user(user_id, nombre_completo, email, rol, new_password=None):
    try:
        user_id = int(user_id)
        if session.get('user_id') == user_id and rol != 'admin':
            flash('No puede quitarse sus propios privilegios de administrador', 'error')
            return False

        existing_email = db_query("SELECT id FROM usuarios WHERE email = %s AND id != %s", (email, user_id), fetchone=True)
        if existing_email:
            flash('El email ya está registrado por otro usuario', 'error')
            return False
        
        if new_password:
            password_hash = generate_password_hash(new_password)
            query = "UPDATE usuarios SET nombre_completo=%s, email=%s, rol=%s, password=%s WHERE id=%s"
            params = (nombre_completo, email, rol, password_hash, user_id)
        else:
            query = "UPDATE usuarios SET nombre_completo=%s, email=%s, rol=%s WHERE id=%s"
            params = (nombre_completo, email, rol, user_id)
        
        db_query(query, params, commit=True)
        return True
    except (ValueError, psycopg2.Error) as e:
        app.logger.error(f"Error actualizando usuario: {e}")
        return False

def delete_user(user_id):
    try:
        user_id = int(user_id)
        if session.get('user_id') == user_id:
            return False, "No puede eliminarse a sí mismo"
        
        admin_count = db_query("SELECT COUNT(*) FROM usuarios WHERE rol = 'admin'", fetchone=True)[0]
        user = get_user_by_id(user_id)
        if user and user['rol'] == 'admin' and admin_count == 1:
            return False, "No puede eliminar el único administrador"
        
        db_query("DELETE FROM usuarios WHERE id = %s", (user_id,), commit=True)
        return True, "Usuario eliminado correctamente"
    except (ValueError, psycopg2.Error) as e:
        app.logger.error(f"Error eliminando usuario: {e}")
        return False, f"Error al eliminar usuario: {e}"

# --- Decorador para proteger rutas (sin cambios) ---
def login_required(roles=None):
    def login_decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'user_id' not in session:
                flash('Debe iniciar sesión para acceder a esta página', 'error')
                return redirect(url_for('login'))
            if roles:
                user = get_user_by_id(session['user_id'])
                if not user or user['rol'] not in roles:
                    flash('No tiene permiso para acceder a esta página', 'error')
                    return redirect(url_for('upload'))
            return f(*args, **kwargs)
        return decorated_function
    return login_decorator

# --- El resto de la lógica de la aplicación (PDF, Email, Rutas) no necesita cambios ---
# Se mantiene igual ya que las funciones de base de datos ahora abstraen la conexión.

# --- Procesamiento de PDF ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() == 'pdf'

def extract_data_islr(text, page_num):
    data = {
        'agente_retencion': None, 'rif_agente': None, 'sujeto_retenido': None, 'rif_sujeto': None,
        'numero_control': None, 'fecha_comprobante': None, 'facturas': [], 'monto_total_bruto': 0.0,
        'monto_total_gravable': 0.0, 'monto_total_retenido': 0.0, 'porcentaje_retencion': 0.0,
        'tipo_persona': None, 'codigo_retencion': None, 'pagina': page_num, 'correo': None,
        'pdf_path': None, 'tipo_documento': 'ISLR'
    }
    agente_match = re.search(r"Agente retención:\s*([^\n]+?)\s+\d{5}\s*([JV]\d{8,9})\s+RIF:", text, re.IGNORECASE)
    if agente_match:
        data['agente_retencion'] = agente_match.group(1).strip()
        data['rif_agente'] = agente_match.group(2).strip()
    sujeto_match = re.search(r"([JV]\d{8,9})\s+RIF:\s+\d+\s+Prove:\s*([^\r\n]+)", text, re.IGNORECASE)
    if sujeto_match:
        data['rif_sujeto'] = sujeto_match.group(1).strip()
        data['sujeto_retenido'] = sujeto_match.group(2).strip().rstrip(',').strip()
    facturas_matches = re.finditer(r"([A-Z]{0,3}\d{3,9})\s+([A-Z]{3})\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+)\s+[\d,\.]+\s+([\d,\.]+)\s+[\d,\.]+\s+(\d{2}/\d{2}/\d{2,4})(?:\s+(\d{2}-\d{3,7}|\d{4,10}))?", text)
    for match in facturas_matches:
        factura = {
            'numero': match.group(1), 'codigo_retencion': match.group(2),
            'monto_retenido': float(match.group(3).replace(',', '').replace('.', '.')),
            'monto_bruto': float(match.group(4).replace(',', '').replace('.', '.')),
            'monto_gravable': float(match.group(5).replace(',', '').replace('.', '.')),
            'porcentaje_retencion': float(match.group(6).replace(',', '.')),
            'fecha': match.group(7), 'numero_control': match.group(8) if match.group(8) else None
        }
        data['facturas'].append(factura)
        data['monto_total_bruto'] += factura['monto_bruto']
        data['monto_total_gravable'] += factura['monto_gravable']
        data['monto_total_retenido'] += factura['monto_retenido']
        if data['porcentaje_retencion'] == 0.0: data['porcentaje_retencion'] = factura['porcentaje_retencion']
        if factura['numero_control'] and not data['numero_control']: data['numero_control'] = factura['numero_control']
    if not data['facturas']:
        match = re.search(r"(\d{3,9})\s+([A-Z]{3})\s+([\d,\.]+)\s+([\d,\.]+)\s+([\d,\.]+)", text)
        if match:
            factura = {
                'numero': match.group(1), 'codigo_retencion': match.group(2),
                'monto_retenido': float(match.group(3).replace('.', '').replace(',', '.')),
                'monto_bruto': float(match.group(4).replace('.', '').replace(',', '.')),
                'monto_gravable': float(match.group(5).replace('.', '').replace(',', '.')),
                'porcentaje_retencion': data['porcentaje_retencion'], 'fecha': data['fecha_comprobante'], 'numero_control': data['numero_control']
            }
            data['facturas'].append(factura)
            data['monto_total_bruto'] = factura['monto_bruto']
            data['monto_total_gravable'] = factura['monto_gravable']
            data['monto_total_retenido'] = factura['monto_retenido']
    return data

def extract_data(text, page_num):
    data = {
        'agente_retencion': None,
        'rif_agente': None,
        'sujeto_retenido': None,
        'rif_sujeto': None,
        'numero_comprobante': None,
        'fecha_comprobante': None,
        'numero_factura': None,
        'base_imponible': 0.0,
        'iva': 0.0,
        'iva_retenido': 0.0,
        'total': 0.0,
        'pagina': page_num,
        'correo': None,
        'pdf_path': None,
        'tipo_documento': 'IVA'
    }

    def parse_amount(amount_str):
        """Convierte string de monto a float manejando diferentes formatos"""
        if not amount_str:
            return 0.0
        amount_str = amount_str.strip()
        
        # Manejar formato con puntos como separadores de miles y coma como decimal
        if ',' in amount_str and '.' in amount_str:
            if amount_str.rfind(',') > amount_str.rfind('.'):
                # Formato: "1.200,20" -> quitar puntos, cambiar coma por punto
                amount_str = amount_str.replace('.', '').replace(',', '.')
            else:
                # Formato: "1,200.20" -> quitar comas
                amount_str = amount_str.replace(',', '')
        elif ',' in amount_str:
            if len(amount_str.split(',')[-1]) == 2:
                # Formato: "1200,20"
                amount_str = amount_str.replace(',', '.')
            else:
                # Formato: "1,200"
                amount_str = amount_str.replace(',', '')
        # Manejar formato como "2.254.46" (puntos de miles, sin decimal)
        elif '.' in amount_str and len(amount_str.split('.')[-1]) != 2:
             amount_str = amount_str.replace('.', '')
             
        try:
            return float(amount_str)
        except ValueError:
            return 0.0

    # Extraer información del agente de retención
    agente_match = re.search(r"Registro de información fiscal agente de retención.*?\n.*?[JV]\d{8,9}\s+(.*?)\s*\n", text, re.IGNORECASE)
    if agente_match:
        data['agente_retencion'] = agente_match.group(1).strip()

    # Extraer RIF del agente
    rif_match = re.search(r"Registro de información fiscal agente de retención.*?\n.*?([JV]\d{8,9})", text, re.IGNORECASE)
    if rif_match:
        data['rif_agente'] = rif_match.group(1).strip()

    # Extraer sujeto retenido
    sujeto_match = re.search(r"Nombre o razón social sujeto retenido(?:.*[\r\n]+)\s*(.*?)\s+[JV]\d+", text, re.IGNORECASE)
    if sujeto_match:
        data['sujeto_retenido'] = sujeto_match.group(1).strip()

    # Extraer RIF del sujeto retenido
    rif_sujeto_match = re.search(r"Registro de información fiscal sujeto retenido.*?\n.*?([JV]\d{8,9})", text, re.IGNORECASE | re.DOTALL)
    if rif_sujeto_match:
        data['rif_sujeto'] = rif_sujeto_match.group(1).strip()

    # --- INICIO DE LÓGICA RESTAURADA (TU CÓDIGO ORIGINAL) ---
    
    # Extraer número de comprobante
    # Esta lógica está adaptada al OCR (Año: 10 Mes: 2025)
    num_comprobante_match = re.search(r"Número de comprobante.*?\n.*?(\d{10})", text, re.IGNORECASE)
    fecha_match = re.search(r"Año:\s*(\d{2})\s*Mes:\s*(\d{4})", text, re.IGNORECASE)
    
    if num_comprobante_match and fecha_match:
        numero_comprobante = num_comprobante_match.group(1).strip()[:8] # Toma '50060361' de '5006036110'
        mes, anio = fecha_match.group(1), fecha_match.group(2) # mes=10, anio=2025
        data['numero_comprobante'] = anio + mes + numero_comprobante # 2025 + 10 + 50060361
        print(f"DEBUG: Nro Comprobante (Restaurado): {data['numero_comprobante']}")
    else:
        print("DEBUG: ✗ No se encontró 'Nro Comprobante' o 'Año/Mes' (Lógica original)")

    # Extraer fecha de comprobante
    fecha_comp_match = re.search(r"\b\d\s+(\d{2}/\d{2}/\d{2})\s", text, re.IGNORECASE)
    if not fecha_comp_match:
        fecha_comp_match = re.search(r"\b\d\s+(\d{2}/\d{2}/\d{4})\s", text, re.IGNORECASE)
    
    # Respaldo para el formato de OCR (Línea 2: ... 2025 27/10/25)
    if not fecha_comp_match:
         fecha_comp_match = re.search(r'\d{4}\s+(\d{2}/\d{2}/\d{2,4})', text)

    if fecha_comp_match:
        data['fecha_comprobante'] = fecha_comp_match.group(1).strip()
        print(f"DEBUG: Fecha Comprobante (Restaurado): {data['fecha_comprobante']}")
    else:
        print("DEBUG: ✗ No se encontró 'Fecha Comprobante' (Lógica original)")

    # --- FIN DE LÓGICA RESTAURADA ---

    print(f"DEBUG: Analizando texto para valores monetarios...")
    
    # DEBUG: Mostrar las primeras líneas del texto para entender la estructura
    lines = text.split('\n')[:15]  # Primeras 15 líneas
    print("DEBUG: Primeras líneas del texto:")
    for i, line in enumerate(lines):
        print(f"  {i}: {line}")
    
    # PASO 1: IDENTIFICAR TIPO DE TRANSACCIÓN
    tipo_transaccion = None
    
    # Buscar patrones de tipo de transacción más amplios
    tipo_patterns = [
        r'(\d{2}-comp)',
        r'(\d{2}-reg)',
        r'(\d{2}-anul)',
        r'(01-reg)',
        r'(02-comp)',
        r'(03-anul)'
    ]
    
    for pattern in tipo_patterns:
        tipo_match = re.search(pattern, text, re.IGNORECASE)
        if tipo_match:
            tipo_transaccion = tipo_match.group(1)
            print(f"DEBUG: Tipo de transacción encontrado: {tipo_transaccion}")
            break
    
    if not tipo_transaccion:
        print("DEBUG: No se pudo identificar el tipo de transacción")
        # Buscar cualquier línea que contenga patrones similares
        for i, line in enumerate(text.split('\n')):
            if any(x in line.lower() for x in ['comp', 'reg', 'anul']):
                print(f"DEBUG: Línea {i} con patrón similar: {line}")
    
    data_line = None 

    # --- PASO 2: EXTRAER NÚMERO DE FACTURA ---
    
    if tipo_transaccion:
        print(f"DEBUG: Procesando tipo {tipo_transaccion}")

        if "02-comp" in tipo_transaccion.lower():  # Nota de débito
            # Buscar ND\d+ directamente (captura prefijo completo)
            nd_match = re.search(r'\b(ND\d+)\b', text, re.IGNORECASE)
            if nd_match:
                data['numero_factura'] = nd_match.group(1).upper()
            else:
                # Fallback: cualquier [A-Z]{1,3}\d+ antes de 02-comp
                factura_match = re.search(
                    r'([A-Z]{1,3}\d+)\s+[\d,\.]+\s+02-comp',
                    text, re.IGNORECASE
                )
                if factura_match:
                    data['numero_factura'] = factura_match.group(1).upper()

        elif "03-anul" in tipo_transaccion.lower():  # Nota de crédito
            # Buscar NC\d+ directamente
            nc_match = re.search(r'\b(NC\d+)\b', text, re.IGNORECASE)
            if nc_match:
                data['numero_factura'] = nc_match.group(1).upper()
            else:
                factura_match = re.search(
                    r'([A-Z]{1,3}\d+)\s+[\d,\.]+\s+03-anul',
                    text, re.IGNORECASE
                )
                if factura_match:
                    data['numero_factura'] = factura_match.group(1).upper()
        
        elif "01-reg" in tipo_transaccion.lower():  # Factura normal
            print("DEBUG: Es una factura normal (01-reg), buscando número de factura.")
            
            # ESTRATEGIA PRINCIPAL: Buscar en la estructura de tabla
            # Formato: Fecha | IVA_Retenido | Numero_Factura | No_Control | IVA | Alicuota | Base | 01-reg | Total
            
            # Patrón 1: Buscar número de factura ANTES del número de control (que tiene formato XX-XXXXXX o XXXXXXXXXX)
            # Captura: (numero_factura) (numero_control_con_guiones_o_largo)
            factura_control_match = re.search(
                r'(\d{3,8})\s+([A-Z0-9\-]{5,12})\s+.*?(01-reg)',
                text,
                re.IGNORECASE
            )
            
            if factura_control_match:
                num_factura = factura_control_match.group(1)
                num_control = factura_control_match.group(2)
                
                # Verificar que el número de control sea diferente y más largo o tenga guiones
                if len(num_control) > len(num_factura) or '-' in num_control:
                    data['numero_factura'] = num_factura
                    print(f"DEBUG: ✓ Número de factura extraído (antes de control {num_control}): {data['numero_factura']}")
                else:
                    # Si no, tomar el número de control como factura
                    data['numero_factura'] = num_control
                    print(f"DEBUG: ✓ Número de factura = control: {data['numero_factura']}")
            
            # Patrón 2: Buscar después de fecha y antes de 01-reg
            if not data['numero_factura']:
                fecha_pattern = re.search(
                    r'(\d{2}/\d{2}/\d{2,4})\s+[\d,]+\.[\d]{2}\s*(\d{4,10})\s+',
                    text
                )
                if fecha_pattern:
                    data['numero_factura'] = fecha_pattern.group(2)
                    print(f"DEBUG: ✓ Número de factura extraído después de fecha: {data['numero_factura']}")

    # MÉTODO DE RESPALDO: Si aún no se pudo extraer
    if not data['numero_factura']:
        print("DEBUG: Usando método de respaldo para extraer número de factura...")
        
        # Respaldo 1: Buscar formatos alfanuméricos — captura prefijo de 1-3 letras + dígitos
        backup_alphanumeric = [
            r'(\d{2}-[A-Z]+-\d+)',          # XX-YYY-ZZZZ
            r'(?<![JV])([A-Z]{1,3}\d{5,})', # ND001826, NC12345, A12345 (no RIFs)
        ]
        
        for pattern in backup_alphanumeric:
            backup_match = re.search(pattern, text)
            if backup_match:
                potential = backup_match.group(1)
                if potential[0] not in ['J', 'V']:
                    data['numero_factura'] = potential
                    print(f"DEBUG: ✓ Número de factura ALFANUMÉRICO extraído con respaldo: {data['numero_factura']}")
                    break
        
        # Respaldo 2: Buscar cualquier número de 6-8 dígitos que no sea monto
        if not data['numero_factura']:
            backup_patterns = [
                r'(\d{8})\s+[A-Z0-9\-]+',  # 8 dígitos seguidos de control
                r'(\d{7})\s+[A-Z0-9\-]+',  # 7 dígitos seguidos de control  
                r'(\d{6})\s+[A-Z0-9\-]+',  # 6 dígitos seguidos de control
            ]
            
            for pattern in backup_patterns:
                backup_match = re.search(pattern, text)
                if backup_match:
                    potential_factura = backup_match.group(1)
                    # EVITAR CAPTURAR NÚMEROS DE COMPROBANTE
                    if data['numero_comprobante'] and potential_factura in data['numero_comprobante']:
                         print(f"DEBUG: ✗ Descartado Nro de Comprobante: {potential_factura}")
                         continue
                         
                    data['numero_factura'] = potential_factura
                    print(f"DEBUG: ✓ Número de factura NUMÉRICO extraído con respaldo: {data['numero_factura']}")
                    break

    # EXTRAER VALORES MONETARIOS (mejorado)
    money_pattern = r"([\d.,]+\d{2})"
    
    totals_line = re.search(
        rf"{money_pattern}\s+{money_pattern}\s+{money_pattern}\s+{money_pattern}\s*Totales", 
        text, 
        re.IGNORECASE
    )
    
    if not totals_line:
         totals_line = re.search(
            rf".*?(01-reg|02-comp|03-anul)\s+.*?\s+{money_pattern}\s+{money_pattern}\s+{money_pattern}\s+{money_pattern}",
            text,
            re.IGNORECASE | re.DOTALL
        )
         if totals_line:
            totals_line = re.search(
                rf"({money_pattern})\s+({money_pattern})\s+({money_pattern})\s+({money_pattern})", 
                totals_line.group(0) # Buscar solo en la línea encontrada
            )

    if totals_line:
        print(f"DEBUG: Encontrado línea de valores: {totals_line.group(0)}")
        try:
            data['total'] = parse_amount(totals_line.group(1))
            data['base_imponible'] = parse_amount(totals_line.group(2))
            data['iva'] = parse_amount(totals_line.group(3))
            data['iva_retenido'] = parse_amount(totals_line.group(4))
            print(f"DEBUG: Valores extraídos - Total: {data['total']}, Base: {data['base_imponible']}, IVA: {data['iva']}, IVA Ret: {data['iva_retenido']}")
        except (ValueError, AttributeError) as e:
            print(f"DEBUG: Error parseando totales: {e}")

    # Calcular total si es posible
    if data['base_imponible'] > 0 and data['iva'] > 0 and data['total'] == 0.0:
        data['total'] = data['base_imponible'] + data['iva']

    print(f"DEBUG: Valores finales - Total: {data['total']}, Base: {data['base_imponible']}, IVA: {data['iva']}, IVA Ret: {data['iva_retenido']}")
    print(f"DEBUG: Número de factura final: {data['numero_factura']}")
    print(f"DEBUG: Número de comprobante final: {data['numero_comprobante']}")
    print(f"DEBUG: Fecha de comprobante final: {data['fecha_comprobante']}")
    
    return data

def split_pdf_by_pages(filepath):
    temp_files = []
    with open(filepath, 'rb') as f:
        reader = PdfReader(f)
        for i in range(len(reader.pages)):
            writer, temp_file = PdfWriter(), tempfile.NamedTemporaryFile(suffix='.pdf', dir=app.config['UPLOAD_FOLDER'], delete=False)
            writer.add_page(reader.pages[i])
            temp_files.append(temp_file.name)
            with open(temp_file.name, 'wb') as out_pdf: writer.write(out_pdf)
    return temp_files

def process_pdf(filepath, tipo_seleccionado, archivo_origen=None):
    comprobantes = []
    nombre_archivo = archivo_origen or os.path.basename(filepath)
    temp_individual_pdfs = split_pdf_by_pages(filepath)
    total_paginas = len(temp_individual_pdfs)
    for i, temp_file_path in enumerate(temp_individual_pdfs):
        try:
            with open(temp_file_path, 'rb') as f:
                reader = PdfReader(f)
                text = reader.pages[0].extract_text() if reader.pages else ""
                tipo_encontrado = 'ISLR' if "Comprobante de Retención de ISLR" in text else 'IVA'

                if tipo_seleccionado.upper() != 'AUTO' and tipo_seleccionado.upper() != tipo_encontrado:
                    os.unlink(temp_file_path)
                    continue

                data = extract_data_islr(text, i + 1) if tipo_encontrado == 'ISLR' else extract_data(text, i + 1)

                if data.get('sujeto_retenido') or data.get('rif_sujeto'):
                    data['pdf_path'] = temp_file_path
                    data['archivo_origen'] = nombre_archivo
                    data['pagina_en_archivo'] = i + 1
                    data['total_paginas_archivo'] = total_paginas
                    cliente = get_cliente(data['rif_sujeto']) if data.get('rif_sujeto') else (get_cliente_by_name(data['sujeto_retenido']) if data.get('sujeto_retenido') else None)
                    if cliente:
                        data['correo'] = cliente['correo']
                        if not data.get('sujeto_retenido'): data['sujeto_retenido'] = cliente['nombre']
                        if not data.get('rif_sujeto'): data['rif_sujeto'] = cliente['rif']
                    comprobantes.append(data)
                else: os.unlink(temp_file_path)
        except Exception as e:
            app.logger.error(f"Error procesando página {i+1}: {e}")
            if os.path.exists(temp_file_path): os.unlink(temp_file_path)
            
    totales = {}
    if comprobantes:
        tipo_doc = comprobantes[0].get('tipo_documento')
        if tipo_doc == 'ISLR':
            totales = {
                'monto_bruto': sum(c.get('monto_total_bruto', 0) for c in comprobantes),
                'monto_gravable': sum(c.get('monto_total_gravable', 0) for c in comprobantes),
                'monto_retenido': sum(c.get('monto_total_retenido', 0) for c in comprobantes),
                'porcentaje_retencion': comprobantes[0].get('porcentaje_retencion', 0)
            }
        else:
            totales = {
                'base': sum(c.get('base_imponible', 0) for c in comprobantes),
                'iva': sum(c.get('iva', 0) for c in comprobantes),
                'retenido': sum(c.get('iva_retenido', 0) for c in comprobantes),
                'total': sum(c.get('total', 0) for c in comprobantes)
            }
    return comprobantes, totales

def send_email(to_email, subject, body, pdf_path, pdf_filename_display):
    smtp_server = get_config('smtp_server') or SMTP_SERVER
    smtp_port   = get_config('smtp_port')   or SMTP_PORT
    email_user  = get_config('email_user')  or EMAIL_USER
    email_pass  = get_config('email_pass')  or EMAIL_PASS
    email_nombre= get_config('email_nombre', 'Sistema de Comprobantes')

    if not all([smtp_server, smtp_port, email_user, email_pass]):
        app.logger.error("Configuración de correo incompleta.")
        return False
    try:
        msg = MIMEMultipart()
        msg['From']    = formataddr((str(Header(email_nombre, 'utf-8')), email_user))
        msg['To']      = to_email
        msg['Subject'] = Header(subject, 'utf-8')
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        if pdf_path and os.path.exists(pdf_path):
            with open(pdf_path, "rb") as attachment:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(attachment.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{secure_filename(pdf_filename_display)}"')
                msg.attach(part)
        server = smtplib.SMTP(smtp_server, int(smtp_port))
        server.starttls()
        server.login(email_user, email_pass)
        server.send_message(msg)
        server.quit()
        app.logger.info(f"Correo enviado exitosamente a {to_email}")
        return True
    except Exception as e:
        app.logger.error(f"Error enviando correo a {to_email}: {e}")
        return False

def process_single_page_pdf(pdf_path, page_num=1):
    try:
        with open(pdf_path, 'rb') as f:
            reader = PdfReader(f)
            if not reader.pages: return None
            text = reader.pages[0].extract_text()
            data = extract_data_islr(text, page_num) if "Comprobante de Retención de ISLR" in text else extract_data(text, page_num)
            if data.get('sujeto_retenido') or data.get('rif_sujeto'):
                data['pdf_path'] = pdf_path
                cliente = get_cliente(data['rif_sujeto']) if data.get('rif_sujeto') else (get_cliente_by_name(data['sujeto_retenido']) if data.get('sujeto_retenido') else None)
                if cliente:
                    data['correo'] = cliente['correo']
                    if not data.get('sujeto_retenido'): data['sujeto_retenido'] = cliente['nombre']
                    if not data.get('rif_sujeto'): data['rif_sujeto'] = cliente['rif']
                return data
    except Exception as e:
        app.logger.error(f"Error procesando la página individual {pdf_path}: {e}")
    return None

# --- Rutas de la aplicación (sin cambios, ya que usan las funciones de DB abstraídas) ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('upload'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = verify_user(username, password)
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['nombre'] = user['nombre_completo']
            session['rol'] = user['rol']
            return redirect(url_for('upload'))
        else:
            flash('Usuario o contraseña incorrectos', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/', methods=['GET', 'POST'])
@login_required()
def upload():
    if request.method == 'POST':
        tipo_seleccionado = request.form.get('tipo_comprobante', 'iva')
        modo_procesamiento = request.form.get('modo_procesamiento', 'individual')
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

        files = request.files.getlist('files') or request.files.getlist('file')
        files = [f for f in files if f and f.filename != '' and allowed_file(f.filename)]

        if not files:
            if is_ajax:
                return jsonify(success=False, message='No se seleccionó ningún archivo PDF válido.')
            flash('No se seleccionó ningún archivo PDF válido.', 'error')
            return redirect(request.url)

        todos_comprobantes = []
        todos_totales = {}
        errores = []

        for file in files:
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
            file.save(filepath)
            try:
                comprobantes, totales = process_pdf(filepath, tipo_seleccionado, archivo_origen=file.filename)
                todos_comprobantes.extend(comprobantes)
                for k, v in totales.items():
                    todos_totales[k] = todos_totales.get(k, 0) + v
                if not comprobantes:
                    tipo_opuesto = 'ISLR' if tipo_seleccionado.upper() == 'IVA' else 'IVA'
                    comp_opuesto, _ = process_pdf(filepath, tipo_opuesto, archivo_origen=file.filename)
                    if comp_opuesto:
                        errores.append(f"'{file.filename}' es un comprobante de <strong>{tipo_opuesto}</strong>, pero seleccionaste <strong>{tipo_seleccionado.upper()}</strong>")
                        for c in comp_opuesto:
                            if os.path.exists(c.get('pdf_path', '')):
                                os.remove(c['pdf_path'])
                    else:
                        errores.append(f"'{file.filename}' no contiene comprobantes válidos")
            except Exception as e:
                errores.append(file.filename)
                app.logger.error(f"Error procesando {file.filename}: {e}", exc_info=True)
            finally:
                if os.path.exists(filepath):
                    os.remove(filepath)

        if not todos_comprobantes:
            msg = '<br>'.join(errores) if errores else 'No se encontraron comprobantes válidos.'
            if is_ajax:
                return jsonify(success=False, message=msg, tipo_error=any('comprobante de' in e for e in errores))
            for err in errores:
                flash(err, 'tipo_error' if 'es un comprobante de' in err else 'warning')
            return render_template('upload.html')

        for err in errores:
            flash(err, 'tipo_error' if 'es un comprobante de' in err else 'warning')

        if modo_procesamiento == 'individual':
            session['comprobantes'] = todos_comprobantes
            session['totales'] = todos_totales
            redirect_url = url_for('confirmar_envio')
        else:
            session['comprobantes_paths'] = [comp['pdf_path'] for comp in todos_comprobantes]
            session['indice_comprobante_actual'] = 0
            redirect_url = url_for('revision_individual_page')

        if is_ajax:
            return jsonify(success=True, redirect=redirect_url)
        return redirect(redirect_url)

    return render_template('upload.html')

@app.route('/confirmar_envio')
@login_required()
def confirmar_envio():
    comprobantes = session.get('comprobantes', [])
    totales = session.get('totales', {})
    if not comprobantes:
        flash('No hay comprobantes pendientes. Por favor carga los archivos nuevamente.', 'warning')
        return redirect(url_for('upload'))
    agente_info = {'nombre': comprobantes[0].get('agente_retencion'), 'rif': comprobantes[0].get('rif_agente')} if comprobantes else {}
    response = make_response(render_template('confirmar.html', comprobantes=comprobantes, totales=totales, agente_info=agente_info))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response

@app.route('/enviar_correo_uno', methods=['POST'])
@login_required()
def enviar_correo_uno():
    """Envía el correo de UN solo comprobante por índice. Usado para progreso en tiempo real."""
    data      = request.get_json()
    idx       = data.get('index', -1)
    correo_override = data.get('correo', '').strip()

    comprobantes = session.get('comprobantes', [])
    if idx < 0 or idx >= len(comprobantes):
        return jsonify(status='error', message='Índice inválido')

    comp          = comprobantes[idx]
    correo        = correo_override or comp.get('correo', '')
    pdf_path      = comp.get('pdf_path', '')
    tipo_doc      = comp.get('tipo_documento', 'IVA')
    nombre_cliente= comp.get('sujeto_retenido', 'Cliente')
    agente        = comp.get('agente_retencion', '')
    rif           = comp.get('rif_sujeto', '')
    num_comp      = comp.get('numero_comprobante', 'S/N')
    periodo       = comp.get('periodo', '')

    if not correo:
        db_query("""
            INSERT INTO historial_envios
                (tipo_comprobante, numero_comprobante, numero_factura, cliente_rif,
                 cliente_nombre, cliente_correo, usuario_id, usuario_nombre,
                 agente_retencion, monto_retenido, estado)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (tipo_doc, num_comp, comp.get('numero_factura',''), rif,
              nombre_cliente, None,
              session.get('user_id'), session.get('nombre'),
              agente, comp.get('monto_total_retenido') or comp.get('iva_retenido'),
              'omitido'), commit=True)
        return jsonify(status='sin_correo', message='Sin correo', cliente=nombre_cliente)

    vars_tpl = {
        'nombre_cliente': nombre_cliente, 'agente': agente,
        'numero_comprobante': num_comp, 'periodo': periodo, 'rif': rif,
    }
    if tipo_doc == 'ISLR':
        asunto = get_config('plantilla_asunto_islr', 'Comprobante ISLR - {agente}').format(**vars_tpl)
        cuerpo = get_config('plantilla_cuerpo_islr', 'Estimado/a {nombre_cliente},\n\nSaludos,\n{agente}').format(**vars_tpl)
        pdf_filename = f"Comprobante_ISLR_{rif}.pdf"
    else:
        asunto = get_config('plantilla_asunto_iva', 'Comprobante IVA N° {numero_comprobante}').format(**vars_tpl)
        cuerpo = get_config('plantilla_cuerpo_iva', 'Estimado/a {nombre_cliente},\n\nSaludos,\n{agente}').format(**vars_tpl)
        pdf_filename = f"Comprobante_IVA_{num_comp}.pdf"

    ok = send_email(correo, asunto, cuerpo, pdf_path, pdf_filename)
    estado = 'enviado' if ok else 'fallido'

    db_query("""
        INSERT INTO historial_envios
            (tipo_comprobante, numero_comprobante, numero_factura, cliente_rif,
             cliente_nombre, cliente_correo, usuario_id, usuario_nombre,
             agente_retencion, monto_retenido, estado)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (tipo_doc, num_comp, comp.get('numero_factura',''), rif,
          nombre_cliente, correo,
          session.get('user_id'), session.get('nombre'),
          agente, comp.get('monto_total_retenido') or comp.get('iva_retenido'),
          estado), commit=True)

    return jsonify(status=estado, cliente=nombre_cliente, correo=correo)



@login_required()
def enviar_correos_route():
    data = request.get_json()
    comprobantes_a_enviar = data.get('comprobantes', [])
    resultados_envio = []
    enviados, fallidos, omitidos = 0, 0, 0

    for comp_data in comprobantes_a_enviar:
        correo, pdf_path = comp_data.get('correo'), comp_data.get('pdf_path')
        nombre_cliente = comp_data.get('sujeto_retenido', 'Cliente')
        agente = comp_data.get('agente_retencion', 'Su Agente de Retención')
        tipo_doc = comp_data.get('tipo_documento', 'IVA')
        status_actual = ''

        if correo and pdf_path and os.path.exists(pdf_path):
            num_comp = comp_data.get('numero_comprobante', 'S/N')
            periodo  = comp_data.get('periodo', '')
            vars_tpl = {
                'nombre_cliente':      nombre_cliente,
                'agente':              agente,
                'numero_comprobante':  num_comp,
                'periodo':             periodo,
                'rif':                 comp_data.get('rif_sujeto', ''),
            }
            if tipo_doc == 'ISLR':
                asunto = get_config('plantilla_asunto_islr', 'Comprobante ISLR - {agente}').format(**vars_tpl)
                cuerpo = get_config('plantilla_cuerpo_islr',  'Estimado/a {nombre_cliente},\n\nAdjunto su comprobante ISLR.\n\nSaludos,\n{agente}').format(**vars_tpl)
                pdf_filename = f"Comprobante_ISLR_{comp_data.get('rif_sujeto', '')}.pdf"
            else:
                asunto = get_config('plantilla_asunto_iva', 'Comprobante de Retención de IVA N° {numero_comprobante}').format(**vars_tpl)
                cuerpo = get_config('plantilla_cuerpo_iva',  'Estimado/a {nombre_cliente},\n\nAdjunto su comprobante IVA N° {numero_comprobante}.\n\nSaludos,\n{agente}').format(**vars_tpl)
                pdf_filename = f"Comprobante_IVA_{num_comp}.pdf"
            
            if send_email(correo, asunto, cuerpo, pdf_path, pdf_filename):
                enviados += 1
                status_actual = 'enviado con éxito'
                comp_data['estado'] = 'enviado'
            else:
                fallidos += 1
                status_actual = 'fallido'
                comp_data['estado'] = 'fallido'
            registrar_envio(comp_data)
        else:
            omitidos += 1
            status_actual = 'omitido (sin correo o PDF)'
        
        num_comp_display = comp_data.get('numero_comprobante') or comp_data.get('rif_sujeto') or f"Pág {comp_data.get('pagina')}"
        resultados_envio.append({'comprobante': num_comp_display, 'cliente': nombre_cliente, 'correo': correo, 'status': status_actual})

    mensaje = f"Proceso completado. Enviados: {enviados}, Fallidos: {fallidos}, Omitidos: {omitidos}."
    status_general = 'error' if fallidos > 0 and enviados == 0 else ('partial_error' if fallidos > 0 else 'success')
    return jsonify({'message': mensaje, 'status': status_general, 'resultados': resultados_envio})

@app.route('/admin/clientes', methods=['GET', 'POST'])
@login_required()
def admin_clientes():
    if request.method == 'POST':
        rif = request.form.get('rif', '').strip().upper().replace('-', '')
        nombre, correo = request.form.get('nombre', '').strip(), request.form.get('correo', '').strip().lower()
        if not all([rif, nombre, correo]): flash('Todos los campos son requeridos.', 'error')
        elif not re.match(r"^[JVGE]\d{8,9}$", rif): flash('Formato de RIF inválido.', 'error')
        elif "@" not in correo or "." not in correo.split('@')[-1]: flash('Formato de correo inválido.', 'error')
        else:
            save_cliente(rif, nombre, correo)
            flash('Cliente actualizado correctamente.', 'success')
        return redirect(url_for('admin_clientes'))
    clientes = get_all_clientes()
    return render_template('admin_clientes.html', clientes=clientes)

@app.route('/historial')
@login_required()
def historial():
    # --- CAMBIO 2: LEER EL NUEVO FILTRO DE LA URL ---
    filtros = {
        key: request.args.get(key) for key in [
            'fecha_inicio', 'fecha_fin', 'cliente_rif', 
            'agente_retencion', 'usuario_id', 'tipo_comprobante'
        ]
    }
    historial_data = get_historial(filtros)
    clientes = get_all_clientes()
    usuarios = get_all_usuarios() if session.get('rol') == 'admin' else []
    with get_db() as conn:
        agentes = db_query("SELECT DISTINCT agente_retencion FROM historial_envios WHERE agente_retencion IS NOT NULL ORDER BY agente_retencion", fetchall=True)
        agentes_retencion = [agente['agente_retencion'] for agente in agentes] if agentes else []
    return render_template('historial.html', historial=historial_data, clientes=clientes, usuarios=usuarios, filtros=filtros, agentes_retencion=agentes_retencion)
    
@app.route('/admin/usuarios', methods=['GET', 'POST'])
@login_required(roles=['admin'])
def admin_usuarios():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'create':
            if create_user(request.form.get('username'), request.form.get('password'), request.form.get('nombre_completo'), request.form.get('email'), request.form.get('rol')):
                flash('Usuario creado exitosamente', 'success')
            else:
                flash('Error: El nombre de usuario ya existe', 'error')
        elif action == 'update':
            if update_user(request.form.get('user_id'), request.form.get('nombre_completo'), request.form.get('email'), request.form.get('rol'), request.form.get('new_password')):
                flash('Usuario actualizado correctamente', 'success')
        elif action == 'delete':
            success, message = delete_user(request.form.get('user_id'))
            flash(message, 'success' if success else 'error')
        return redirect(url_for('admin_usuarios'))
    usuarios = get_all_usuarios()
    return render_template('admin_usuarios.html', usuarios=usuarios)

@app.route('/perfil')
@login_required()
def perfil():
    user = get_user_by_id(session['user_id'])
    return render_template('perfil.html', user=user)

@app.route('/procesar_carpeta', methods=['POST'])
@login_required()
def procesar_carpeta_route():
    try:
        files = request.files.getlist("files[]")
        all_comprobantes_paths = []
        if not files: return jsonify({'success': False, 'message': 'No se seleccionaron archivos.'}), 400

        for file in files:
            if file and allowed_file(file.filename):
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
                file.save(filepath)
                try:
                    comprobantes_from_file, _ = process_pdf(filepath, 'AUTO', archivo_origen=file.filename)
                    if comprobantes_from_file:
                        all_comprobantes_paths.extend([comp['pdf_path'] for comp in comprobantes_from_file])
                except Exception as e:
                    app.logger.error(f"Error procesando archivo local {file.filename}: {e}")
                finally:
                    if os.path.exists(filepath): os.remove(filepath)

        if not all_comprobantes_paths:
            return jsonify({'success': False, 'message': 'No se encontraron comprobantes válidos.'}), 404
        
        session['comprobantes_paths'] = all_comprobantes_paths
        session['indice_comprobante_actual'] = 0
        return jsonify({'success': True, 'redirect_url': url_for('revision_individual_page'), 'message': f'Se procesaron {len(all_comprobantes_paths)} comprobantes.'})
    except Exception as e:
        app.logger.error(f"Error crítico en /procesar_carpeta: {e}")
        return jsonify({'success': False, 'message': 'Ocurrió un error inesperado.'}), 500

@app.route('/revision_individual')
@login_required()
def revision_individual_page():
    paths = session.get('comprobantes_paths', [])
    if not paths: return redirect(url_for('upload'))
    
    indice = session.get('indice_comprobante_actual', 0)
    if indice >= len(paths): return redirect(url_for('finalizar_proceso_individual_page'))
    
    comprobante_actual = process_single_page_pdf(paths[indice], page_num=indice + 1)
    if not comprobante_actual:
        flash(f'Error al leer el comprobante {indice + 1}. Omitiendo.', 'error')
        session['indice_comprobante_actual'] = indice + 1
        return redirect(url_for('revision_individual_page'))

    response = make_response(render_template('revision_individual.html', comprobante=comprobante_actual, indice_actual=indice, total=len(paths)))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    return response

@app.route('/enviar_individual', methods=['POST'])
@login_required()
def enviar_individual_handler():
    data = request.json
    comp = data['comprobante']
    comp['correo'] = data.get('correo', comp.get('correo', ''))
    if not comp['correo']: return jsonify(success=False, message="Debe especificar un correo electrónico")
    
    nombre_cliente = comp.get('sujeto_retenido', 'Cliente')
    agente = comp.get('agente_retencion', 'Su Agente de Retención')
    tipo_doc = comp.get('tipo_documento', 'IVA')
    
    num_comp = comp.get('numero_comprobante', 'S/N')
    periodo  = comp.get('periodo', '')
    vars_tpl = {
        'nombre_cliente':     nombre_cliente,
        'agente':             agente,
        'numero_comprobante': num_comp,
        'periodo':            periodo,
        'rif':                comp.get('rif_sujeto', ''),
    }
    if tipo_doc == 'ISLR':
        asunto = get_config('plantilla_asunto_islr', 'Comprobante ISLR - {agente}').format(**vars_tpl)
        cuerpo = get_config('plantilla_cuerpo_islr',  'Estimado/a {nombre_cliente},\n\nAdjunto su comprobante ISLR.\n\nSaludos,\n{agente}').format(**vars_tpl)
        pdf_filename = f"Comprobante_ISLR_{comp.get('rif_sujeto', '')}.pdf"
    else:
        asunto = get_config('plantilla_asunto_iva', 'Comprobante de Retención de IVA N° {numero_comprobante}').format(**vars_tpl)
        cuerpo = get_config('plantilla_cuerpo_iva',  'Estimado/a {nombre_cliente},\n\nAdjunto su comprobante IVA N° {numero_comprobante}.\n\nSaludos,\n{agente}').format(**vars_tpl)
        pdf_filename = f"Comprobante_IVA_{num_comp}.pdf"

    if send_email(comp['correo'], asunto, cuerpo, comp['pdf_path'], pdf_filename):
        comp['estado'] = 'enviado'
        registrar_envio(comp)
        resumen = session.get('resumen_individual', {'enviados': 0, 'omitidos': 0, 'fallidos': 0})
        resumen['enviados'] += 1
        session['resumen_individual'] = resumen
        return jsonify(success=True)
    else:
        comp['estado'] = 'fallido'
        registrar_envio(comp)
        resumen = session.get('resumen_individual', {'enviados': 0, 'omitidos': 0, 'fallidos': 0})
        resumen['fallidos'] += 1
        session['resumen_individual'] = resumen
        return jsonify(success=False, message="Error al enviar el correo")

@app.route('/siguiente_comprobante')
@login_required()
def siguiente_comprobante_handler():
    paths = session.get('comprobantes_paths', [])
    idx   = session.get('indice_comprobante_actual', 0)
    if idx < len(paths):
        resumen = session.get('resumen_individual', {'enviados': 0, 'omitidos': 0, 'fallidos': 0})
        resumen['omitidos'] += 1
        session['resumen_individual'] = resumen
        # Guardar omitido en historial para que aparezca en el resumen final
        try:
            comp = process_single_page_pdf(paths[idx], page_num=idx + 1)
            if comp:
                db_query("""
                    INSERT INTO historial_envios
                        (tipo_comprobante, numero_comprobante, cliente_rif, cliente_nombre,
                         cliente_correo, usuario_nombre, agente_retencion, estado)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    comp.get('tipo_documento', 'IVA'),
                    comp.get('numero_comprobante', ''),
                    comp.get('rif_sujeto', ''),
                    comp.get('sujeto_retenido', ''),
                    comp.get('correo', '') or None,
                    session.get('nombre'),
                    comp.get('agente_retencion', ''),
                    'omitido'
                ), commit=True)
        except Exception as e:
            app.logger.error(f"Error guardando omitido en historial: {e}")
    session['indice_comprobante_actual'] = idx + 1
    return redirect(url_for('revision_individual_page'))

@app.route('/finalizar_proceso_individual')
@login_required()
def finalizar_proceso_individual_page():
    for path in session.get('comprobantes_paths', []):
        if path and os.path.exists(path):
            try: os.unlink(path)
            except Exception as e: app.logger.error(f"Error eliminando archivo temporal: {e}")
    session.pop('comprobantes_paths', None)
    session.pop('indice_comprobante_actual', None)
    session.pop('resumen_individual', None)
    session.pop('revision_batch_id', None)
    return render_template('finalizado_individual.html')

@app.route('/ver_pdf')
@login_required()
def ver_pdf():
    path = request.args.get('path')
    if not path or not os.path.exists(path): abort(404)
    return send_file(path, mimetype='application/pdf')
    
@app.route('/exportar_historial')
@login_required()
def exportar_historial():
    try:
        # --- CAMBIO 3: LEER EL NUEVO FILTRO PARA LA EXPORTACIÓN ---
        filtros = {
            'fecha_inicio': request.args.get('fecha_inicio'),
            'fecha_fin': request.args.get('fecha_fin'),
            'cliente_rif': request.args.get('cliente_rif'),
            'agente_retencion': request.args.get('agente_retencion'),
            'usuario_id': request.args.get('usuario_id'),
            'tipo_comprobante': request.args.get('tipo_comprobante')
        }
        
        historial_data = get_historial(filtros)
        
        if not historial_data:
            flash('No hay datos para exportar con los filtros seleccionados', 'warning')
            return redirect(url_for('historial'))
        
        datos_excel = []
        for registro in historial_data:
            num_comprobante = registro['numero_comprobante'] if registro['tipo_comprobante'] == 'IVA' else ''
            num_factura = registro['numero_factura'] or ''

            datos_excel.append({
                'Fecha': registro['fecha_envio'],
                'Usuario': registro['usuario_nombre'],
                'Agente Retención': registro['agente_retencion'],
                'Cliente': registro['cliente_nombre'],
                'RIF Cliente': registro['cliente_rif'],
                'Correo': registro['cliente_correo'],
                'Tipo': registro['tipo_comprobante'],
                'N° Comprobante': num_comprobante,
                'N° Factura': num_factura,
                'Monto Retenido': registro['monto_retenido'],
                'Estado': registro['estado']
            })
        
        df = pd.DataFrame(datos_excel)
        
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        
        with pd.ExcelWriter(temp_file.name, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Historial Envíos', index=False)
            
            worksheet = writer.sheets['Historial Envíos']
            
            for column in worksheet.columns:
                max_length = 0
                column_letter = column[0].column_letter
                for cell in column:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 50)
                worksheet.column_dimensions[column_letter].width = adjusted_width
            
            from openpyxl.styles import Font, PatternFill
            header_font = Font(bold=True, color="FFFFFF")
            header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
            
            for cell in worksheet[1]:
                cell.font = header_font
                cell.fill = header_fill
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"historial_envios_{timestamp}.xlsx"
        
        return send_file(
            temp_file.name,
            as_attachment=True,
            download_name=filename,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        
    except Exception as e:
        app.logger.error(f"Error exportando historial: {str(e)}")
        flash('Error al generar el archivo Excel', 'error')
        return redirect(url_for('historial'))

@app.route('/admin/clientes/plantilla')
@login_required()
def descargar_plantilla_clientes():
    """Genera y descarga una plantilla Excel para importar clientes."""
    import tempfile
    df = pd.DataFrame(columns=['RIF', 'Nombre', 'Correo'])
    # Filas de ejemplo
    df.loc[0] = ['J123456789', 'Empresa Ejemplo C.A.', 'empresa@correo.com']
    df.loc[1] = ['V987654321', 'Juan Pérez', 'juan@correo.com']

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
    with pd.ExcelWriter(tmp.name, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Clientes')
        ws = writer.sheets['Clientes']
        from openpyxl.styles import Font, PatternFill
        for cell in ws[1]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill(start_color='4E73DF', end_color='4E73DF', fill_type='solid')
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = 35
    return send_file(tmp.name, as_attachment=True, download_name='plantilla_clientes.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/admin/clientes/preview_importar', methods=['POST'])
@login_required()
def preview_importar_clientes():
    """Lee el Excel y devuelve preview de lo que se importará."""
    import re as re_mod
    archivo = request.files.get('excel_file')
    if not archivo:
        return jsonify(success=False, message='No se recibió ningún archivo.')
    try:
        df = pd.read_excel(archivo, dtype=str).fillna('')
        df.columns = [c.strip().lower() for c in df.columns]

        # Aceptar variantes de nombres de columna
        col_map = {}
        for c in df.columns:
            if 'rif' in c: col_map['rif'] = c
            elif 'nombre' in c or 'name' in c: col_map['nombre'] = c
            elif 'correo' in c or 'email' in c or 'mail' in c: col_map['correo'] = c

        if len(col_map) < 3:
            return jsonify(success=False, message='El archivo debe tener columnas RIF, Nombre y Correo.')

        # Obtener RIFs existentes para saber cuáles son nuevos vs actualizar
        existentes = {c['rif'] for c in get_all_clientes()}

        filas = []
        for _, row in df.iterrows():
            rif = row[col_map['rif']].strip().upper().replace('-', '').replace(' ', '')
            nombre = row[col_map['nombre']].strip()
            correo = row[col_map['correo']].strip().lower()

            estado, error = 'nuevo', None
            if not rif or not nombre or not correo:
                estado, error = 'error', 'Campos vacíos'
            elif not re_mod.match(r'^[JVGE]\d{8,9}$', rif):
                estado, error = 'error', f'RIF inválido: {rif}'
            elif '@' not in correo or '.' not in correo.split('@')[-1]:
                estado, error = 'error', 'Correo inválido'
            elif rif in existentes:
                estado = 'actualizar'

            filas.append({'rif': rif, 'nombre': nombre, 'correo': correo,
                          'estado': estado, 'error': error or ''})

        return jsonify(success=True, filas=filas)
    except Exception as e:
        app.logger.error(f'Error leyendo Excel para importar: {e}')
        return jsonify(success=False, message=f'Error al leer el archivo: {e}')


@app.route('/admin/clientes/confirmar_importar', methods=['POST'])
@login_required()
def confirmar_importar_clientes():
    """Recibe el JSON de filas validadas y las inserta/actualiza en la BD."""
    data = request.get_json()
    filas = data.get('filas', [])
    if not filas:
        return jsonify(success=False, message='No hay datos para importar.')
    try:
        for fila in filas:
            if fila.get('estado') != 'error':
                save_cliente(fila['rif'], fila['nombre'], fila['correo'])
        app.logger.info(f"Importación masiva: {len(filas)} clientes por usuario {session.get('username')}")
        return jsonify(success=True, message=f'{len(filas)} clientes importados correctamente.')
    except Exception as e:
        app.logger.error(f'Error en importación masiva: {e}')
        return jsonify(success=False, message=str(e))


@app.route('/configuracion', methods=['GET', 'POST'])
@login_required(roles=['admin'])
def configuracion():
    if request.method == 'POST':
        seccion = request.form.get('seccion')
        if seccion == 'correo':
            set_config('smtp_server',  request.form.get('smtp_server', '').strip())
            set_config('smtp_port',    request.form.get('smtp_port', '587').strip())
            set_config('email_user',   request.form.get('email_user', '').strip())
            set_config('email_nombre', request.form.get('email_nombre', '').strip())
            # Solo actualizar contraseña si se ingresó una nueva
            if request.form.get('email_pass', '').strip():
                set_config('email_pass', request.form.get('email_pass').strip())
            flash('Configuración de correo guardada correctamente.', 'success')
        elif seccion == 'plantillas':
            set_config('plantilla_asunto_iva',  request.form.get('plantilla_asunto_iva', '').strip())
            set_config('plantilla_cuerpo_iva',  request.form.get('plantilla_cuerpo_iva', '').strip())
            set_config('plantilla_asunto_islr', request.form.get('plantilla_asunto_islr', '').strip())
            set_config('plantilla_cuerpo_islr', request.form.get('plantilla_cuerpo_islr', '').strip())
            flash('Plantillas de correo guardadas correctamente.', 'success')
        return redirect(url_for('configuracion'))

    cfg = get_all_config()
    return render_template('configuracion.html', cfg=cfg)


@app.route('/configuracion/crear_tabla')
@login_required(roles=['admin'])
def crear_tabla_configuracion():
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS configuracion (
                        clave VARCHAR(100) PRIMARY KEY,
                        valor TEXT,
                        descripcion TEXT,
                        actualizado TIMESTAMP DEFAULT NOW())''')
        conn.commit()

        defaults = [
            ('smtp_server',          os.getenv('SMTP_SERVER', 'mail.fmcenter.com.ve'),    'Servidor SMTP'),
            ('smtp_port',            os.getenv('SMTP_PORT', '587'),                        'Puerto SMTP'),
            ('email_user',           os.getenv('EMAIL_USER', 'impuestos@fmcenter.com.ve'), 'Correo remitente'),
            ('email_pass',           os.getenv('EMAIL_PASS', ''),                          'Contraseña'),
            ('email_nombre',         'FM Center — Comprobantes',                            'Nombre remitente'),
            ('plantilla_asunto_iva',
             'Comprobante de Retención de IVA N° {numero_comprobante}',
             'Asunto email IVA'),
            ('plantilla_cuerpo_iva',
             'Estimado/a {nombre_cliente},\n\nAdjunto encontrará su Comprobante de Retención de IVA N° {numero_comprobante}.\n\nSaludos cordiales,\n{agente}',
             'Cuerpo email IVA'),
            ('plantilla_asunto_islr',
             'Comprobante de Retención de ISLR - {agente}',
             'Asunto email ISLR'),
            ('plantilla_cuerpo_islr',
             'Estimado/a {nombre_cliente},\n\nAdjunto encontrará su Comprobante de Retención de ISLR.\n\nSaludos cordiales,\n{agente}',
             'Cuerpo email ISLR'),
        ]
        for clave, valor, desc in defaults:
            cur.execute("""
                INSERT INTO configuracion (clave, valor, descripcion)
                VALUES (%s, %s, %s)
                ON CONFLICT (clave) DO UPDATE
                SET valor = EXCLUDED.valor, descripcion = EXCLUDED.descripcion
                WHERE configuracion.valor IS NULL OR configuracion.valor = ''
            """, (clave, valor, desc))
        conn.commit()
        cur.close()
        app.logger.info("Tabla configuracion creada y poblada correctamente.")
        flash('Tabla de configuración creada correctamente. Ya puedes usar la página de configuración.', 'success')
    except Exception as e:
        if conn: conn.rollback()
        app.logger.error(f"Error creando tabla configuracion: {e}")
        flash(f'Error: {e}', 'danger')
    finally:
        if conn: conn.close()
    return redirect(url_for('configuracion'))


@app.route('/configuracion/debug_cfg')
@login_required(roles=['admin'])
def debug_cfg():
    info = db_query("SELECT current_database(), current_schema(), current_user", fetchone=True)
    tablas = db_query("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_name
    """, fetchall=True) or []
    rows = db_query("SELECT clave, valor FROM configuracion ORDER BY clave", fetchall=True) or []
    return jsonify(
        conexion={
            'database': info['current_database'] if info else '?',
            'schema':   info['current_schema']   if info else '?',
            'user':     info['current_user']      if info else '?',
        },
        tablas_publicas=[t['table_name'] for t in tablas],
        total_filas_configuracion=len(rows),
        datos={r['clave']: r['valor'] for r in rows}
    )


@app.route('/configuracion/reset_plantillas', methods=['POST'])
@login_required(roles=['admin'])
def reset_plantillas():
    plantillas = {
        'plantilla_asunto_iva':  'Comprobante de Retención de IVA N° {numero_comprobante}',
        'plantilla_cuerpo_iva':  'Estimado/a {nombre_cliente},\n\nAdjunto encontrará su Comprobante de Retención de IVA N° {numero_comprobante}.\n\nSaludos cordiales,\n{agente}',
        'plantilla_asunto_islr': 'Comprobante de Retención de ISLR - {agente}',
        'plantilla_cuerpo_islr': 'Estimado/a {nombre_cliente},\n\nAdjunto encontrará su Comprobante de Retención de ISLR.\n\nSaludos cordiales,\n{agente}',
    }
    for clave, valor in plantillas.items():
        set_config(clave, valor)
    flash('Plantillas restauradas a los valores por defecto.', 'success')
    return redirect(url_for('configuracion') + '#tab-plantillas')


@app.route('/configuracion/probar_correo', methods=['POST'])
@login_required(roles=['admin'])
def probar_correo():
    correo_prueba = request.json.get('correo_destino', '').strip()
    if not correo_prueba:
        return jsonify(success=False, message='Ingresa un correo de destino.')
    ok = send_email(
        correo_prueba,
        'Prueba de configuración — Sistema de Comprobantes',
        'Este es un correo de prueba enviado desde el Sistema de Comprobantes.\n\nSi ves este mensaje, la configuración SMTP es correcta.',
        None, None
    )
    if ok:
        return jsonify(success=True, message=f'Correo de prueba enviado a {correo_prueba}.')
    return jsonify(success=False, message='Error al enviar. Verifica el servidor SMTP, puerto, usuario y contraseña.')


@app.route('/herramientas/debug_pdf', methods=['GET', 'POST'])
@login_required(roles=['admin'])
def debug_pdf_texto():
    texto_paginas = []
    error = None
    if request.method == 'POST':
        archivo = request.files.get('pdf_file')
        if not archivo or archivo.filename == '':
            error = 'No se seleccionó ningún archivo.'
        else:
            try:
                from PyPDF2 import PdfReader
                import io
                reader = PdfReader(io.BytesIO(archivo.read()))
                for i, page in enumerate(reader.pages):
                    texto_paginas.append({
                        'numero': i + 1,
                        'texto': page.extract_text() or '(página sin texto extraíble)'
                    })
            except Exception as e:
                error = f'Error al leer el PDF: {e}'
    return render_template('debug_pdf.html', texto_paginas=texto_paginas, error=error)




# ═══════════════════════════════════════════════════════════════════
# MÓDULO DE EXTRACCIÓN CON PLANTILLAS VISUALES
# ═══════════════════════════════════════════════════════════════════

def aplicar_plantilla_visual(pdf_path, plantilla_id):
    """Aplica una plantilla visual a un PDF usando coordenadas definidas por el usuario."""
    import json as json_module
    campos = db_query("SELECT * FROM plantilla_campos WHERE plantilla_id=%s ORDER BY orden",
                      (plantilla_id,), fetchall=True) or []
    resultados = {}
    confianza  = {}
    with pdfplumber.open(pdf_path) as pdf:
        for campo in campos:
            try:
                pagina = pdf.pages[campo['pagina'] - 1]
                zona   = pagina.crop((campo['x0'], campo['y0'], campo['x1'], campo['y1']))
                if campo['es_tabla']:
                    tabla = zona.extract_table() or []
                    filas = [row for row in tabla if any(c and str(c).strip() for c in row)]
                    texto = json_module.dumps(filas, ensure_ascii=False)
                    confianza[campo['nombre_campo']] = 'ok' if filas else 'vacio'
                else:
                    texto = (zona.extract_text() or '').strip()
                    if campo['post_proceso'] and texto:
                        m = re.search(campo['post_proceso'], texto)
                        texto = m.group(1) if m else texto
                    if campo['patron_validacion'] and texto:
                        ok = bool(re.search(campo['patron_validacion'], texto))
                        confianza[campo['nombre_campo']] = 'ok' if ok else 'revisar'
                    else:
                        confianza[campo['nombre_campo']] = 'ok' if texto else 'vacio'
                resultados[campo['nombre_campo']] = texto
            except Exception as e:
                resultados[campo['nombre_campo']] = None
                confianza[campo['nombre_campo']] = 'error'
                app.logger.error(f"Error extrayendo {campo['nombre_campo']}: {e}")
    return resultados, confianza

@app.route('/extractor/plantillas')
@login_required()
def lista_plantillas_ext():
    plantillas = db_query("""
        SELECT p.*, COUNT(e.id) as total_usos
        FROM plantillas p
        LEFT JOIN extracciones e ON e.plantilla_id = p.id
        GROUP BY p.id ORDER BY p.nombre
    """, fetchall=True) or []
    return render_template('plantillas.html', plantillas=plantillas)

@app.route('/extractor/plantillas/<int:plantilla_id>/editar', methods=['POST'])
@login_required()
def editar_plantilla_ext(plantilla_id):
    nombre = request.form.get('nombre','').strip()
    if not nombre:
        flash('El nombre es obligatorio.', 'danger')
        return redirect(url_for('lista_plantillas_ext'))
    db_query("UPDATE plantillas SET nombre=%s, descripcion=%s, tipo_documento=%s, actualizado=NOW() WHERE id=%s",
             (nombre, request.form.get('descripcion','').strip(),
              request.form.get('tipo_documento','generico'), plantilla_id), commit=True)
    flash('Plantilla actualizada.', 'success')
    return redirect(url_for('lista_plantillas_ext'))

@app.route('/extractor/plantillas/<int:plantilla_id>/eliminar', methods=['POST'])
@login_required(roles=['admin'])
def eliminar_plantilla_ext(plantilla_id):
    db_query("DELETE FROM plantillas WHERE id=%s", (plantilla_id,), commit=True)
    flash('Plantilla eliminada.', 'success')
    return redirect(url_for('lista_plantillas_ext'))

@app.route('/extractor/plantillas/nueva', methods=['GET', 'POST'])
@login_required()
def nueva_plantilla_ext():
    if request.method == 'POST':
        nombre = request.form.get('nombre', '').strip()
        if not nombre:
            flash('El nombre es obligatorio.', 'danger')
            return redirect(request.url)
        pid = db_query("""
            INSERT INTO plantillas (nombre, descripcion, tipo_documento)
            VALUES (%s, %s, %s) RETURNING id
        """, (nombre,
              request.form.get('descripcion','').strip(),
              request.form.get('tipo_documento','generico')),
              fetchone=True, commit=True)
        if pid:
            flash(f'Plantilla "{nombre}" creada.', 'success')
            return redirect(url_for('editor_plantilla_ext', plantilla_id=pid['id']))
        flash('Error al crear la plantilla.', 'danger')
    return render_template('nueva_plantilla.html')

@app.route('/extractor/plantillas/<int:plantilla_id>/editor')
@login_required()
def editor_plantilla_ext(plantilla_id):
    plantilla = db_query("SELECT * FROM plantillas WHERE id=%s", (plantilla_id,), fetchone=True)
    if not plantilla:
        flash('Plantilla no encontrada.', 'danger')
        return redirect(url_for('lista_plantillas_ext'))
    campos = db_query("SELECT * FROM plantilla_campos WHERE plantilla_id=%s ORDER BY orden",
                      (plantilla_id,), fetchall=True) or []
    response = make_response(render_template('editor.html', plantilla=plantilla, campos=campos))
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/extractor/plantillas/<int:plantilla_id>/subir_muestra', methods=['POST'])
@login_required()
def subir_muestra_ext(plantilla_id):
    archivo = request.files.get('pdf_muestra')
    if not archivo or not archivo.filename.lower().endswith('.pdf'):
        return jsonify(success=False, message='Sube un archivo PDF válido.')
    nombre = f"muestra_{plantilla_id}_{secure_filename(archivo.filename)}"
    path   = os.path.join(UPLOAD_FOLDER, nombre)
    archivo.save(path)
    db_query("UPDATE plantillas SET muestra_path=%s WHERE id=%s",
             (path, plantilla_id), commit=True)
    # Obtener dimensiones de la primera página
    with pdfplumber.open(path) as pdf:
        page   = pdf.pages[0]
        ancho  = float(page.width)
        alto   = float(page.height)
        paginas = len(pdf.pages)
    return jsonify(success=True, ancho=ancho, alto=alto, paginas=paginas,
                   pdf_url=url_for('ver_muestra_pdf_ext', plantilla_id=plantilla_id))

@app.route('/extractor/plantillas/<int:plantilla_id>/muestra_pdf')
@login_required()
def ver_muestra_pdf_ext(plantilla_id):
    plantilla = db_query("SELECT muestra_path FROM plantillas WHERE id=%s",
                         (plantilla_id,), fetchone=True)
    if not plantilla or not plantilla['muestra_path']:
        return 'No hay PDF de muestra', 404
    return send_file(plantilla['muestra_path'], mimetype='application/pdf')

@app.route('/extractor/plantillas/<int:plantilla_id>/campos', methods=['POST'])
@login_required()
def guardar_campo_ext(plantilla_id):
    data = request.get_json()
    if data.get('id'):
        db_query("""UPDATE plantilla_campos
                    SET nombre_campo=%s, etiqueta=%s, tipo_campo=%s,
                        pagina=%s, x0=%s, y0=%s, x1=%s, y1=%s,
                        patron_validacion=%s, post_proceso=%s
                    WHERE id=%s AND plantilla_id=%s""",
                 (data['nombre_campo'], data['etiqueta'], data.get('tipo_campo','texto'),
                  data.get('pagina',1), data['x0'], data['y0'], data['x1'], data['y1'],
                  data.get('patron_validacion'), data.get('post_proceso'),
                  data['id'], plantilla_id), commit=True)
        return jsonify(success=True, id=data['id'])
    else:
        orden = (db_query("SELECT COUNT(*) as n FROM plantilla_campos WHERE plantilla_id=%s",
                          (plantilla_id,), fetchone=True) or {}).get('n', 0)
        row = db_query("""INSERT INTO plantilla_campos
                (plantilla_id, nombre_campo, etiqueta, tipo_campo, es_tabla,
                 pagina, x0, y0, x1, y1, patron_validacion, post_proceso, orden)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (plantilla_id, data['nombre_campo'], data['etiqueta'],
                 data.get('tipo_campo','texto'), data.get('es_tabla', False),
                 data.get('pagina',1),
                 data['x0'], data['y0'], data['x1'], data['y1'],
                 data.get('patron_validacion'), data.get('post_proceso'), orden),
                fetchone=True, commit=True)
        return jsonify(success=True, id=row['id'] if row else None)

@app.route('/extractor/plantillas/<int:plantilla_id>/campos/<int:campo_id>', methods=['DELETE'])
@login_required()
def eliminar_campo_ext(plantilla_id, campo_id):
    db_query("DELETE FROM plantilla_campos WHERE id=%s AND plantilla_id=%s",
             (campo_id, plantilla_id), commit=True)
    return jsonify(success=True)

@app.route('/extractor/plantillas/<int:plantilla_id>/preview_zona', methods=['POST'])
@login_required()
def preview_zona_ext(plantilla_id):
    """Extrae y devuelve el texto de una zona del PDF de muestra."""
    data     = request.get_json()
    plantilla = db_query("SELECT muestra_path FROM plantillas WHERE id=%s",
                         (plantilla_id,), fetchone=True)
    if not plantilla or not plantilla['muestra_path']:
        return jsonify(texto=None, message='Sin PDF de muestra.')
    try:
        with pdfplumber.open(plantilla['muestra_path']) as pdf:
            pagina = data.get('pagina', 1)
            page   = pdf.pages[pagina - 1]
            zona   = page.crop((data['x0'], data['y0'], data['x1'], data['y1']))
            texto  = (zona.extract_text() or '').strip()
        return jsonify(texto=texto or None)
    except Exception as e:
        return jsonify(texto=None, message=str(e))


@app.route('/extractor/plantillas/<int:plantilla_id>/previsualizar', methods=['POST'])
@login_required()
def previsualizar_extraccion_ext(plantilla_id):
    """Prueba la plantilla contra su PDF de muestra."""
    plantilla = db_query("SELECT * FROM plantillas WHERE id=%s", (plantilla_id,), fetchone=True)
    if not plantilla or not plantilla['muestra_path']:
        return jsonify(success=False, message='No hay PDF de muestra cargado.')
    resultado, _ = aplicar_plantilla_visual(plantilla['muestra_path'], plantilla_id)
    return jsonify(success=True, resultado=resultado)

# ── Extracción ───────────────────────────────────────────────────────────────
@app.route('/extractor/extraer', methods=['GET', 'POST'])
@login_required()
def extraer_plantilla():
    plantillas = db_query("SELECT * FROM plantillas WHERE activa=true ORDER BY nombre",
                          fetchall=True) or []
    if request.method == 'POST':
        plantilla_id = request.form.get('plantilla_id')
        archivos = request.files.getlist('pdfs')
        archivos = [f for f in archivos if f and f.filename.lower().endswith('.pdf')]
        if not archivos or not plantilla_id:
            flash('Selecciona una plantilla y al menos un PDF.', 'warning')
            return redirect(request.url)
        resultados = []
        for archivo in archivos:
            path = os.path.join(UPLOAD_FOLDER, secure_filename(archivo.filename))
            archivo.save(path)
            try:
                datos, confianza = aplicar_plantilla_visual(path, int(plantilla_id))
                estado = 'exitoso' if all(v != 'revisar' for v in confianza.values()) else 'revisar'
                db_query("""INSERT INTO extracciones
                    (plantilla_id, archivo_nombre, usuario_id, usuario_nombre, datos, estado)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (plantilla_id, archivo.filename,
                     session.get('user_id'), session.get('nombre'),
                     json.dumps(datos), estado), commit=True)
                resultados.append({'archivo': archivo.filename, 'datos': datos,
                                   'confianza': confianza, 'estado': estado,
                                   'pdf_path': path})
            except Exception as e:
                app.logger.error(f"Error extrayendo {archivo.filename}: {e}")
                resultados.append({'archivo': archivo.filename, 'error': str(e),
                                   'estado': 'error', 'pdf_path': ''})
            # PDF se mantiene en uploads/ para poder enviarlo por correo
        return render_template('resultado_extraccion.html', resultados=resultados,
                               plantilla_id=plantilla_id)
    return render_template('extraer.html', plantillas=plantillas)

def aplicar_plantilla_visual(pdf_path, plantilla_id):
    campos = db_query("SELECT * FROM plantilla_campos WHERE plantilla_id=%s ORDER BY orden",
                      (plantilla_id,), fetchall=True) or []
    resultados = {}
    confianza  = {}
    with pdfplumber.open(pdf_path) as pdf:
        for campo in campos:
            try:
                pagina = pdf.pages[campo['pagina'] - 1]
                zona   = pagina.crop((campo['x0'], campo['y0'], campo['x1'], campo['y1']))
                if campo['es_tabla']:
                    tabla = zona.extract_table() or []
                    filas = [row for row in tabla if any(c and str(c).strip() for c in row)]
                    texto = json.dumps(filas, ensure_ascii=False)
                    confianza[campo['nombre_campo']] = 'ok' if filas else 'vacio'
                else:
                    texto = (zona.extract_text() or '').strip()
                    if campo['post_proceso'] and texto:
                        m = re.search(campo['post_proceso'], texto)
                        texto = m.group(1) if m else texto
                    if campo['patron_validacion'] and texto:
                        ok = bool(re.search(campo['patron_validacion'], texto))
                        confianza[campo['nombre_campo']] = 'ok' if ok else 'revisar'
                    else:
                        confianza[campo['nombre_campo']] = 'ok' if texto else 'vacio'
                resultados[campo['nombre_campo']] = texto
            except Exception as e:
                resultados[campo['nombre_campo']] = None
                confianza[campo['nombre_campo']] = 'error'
                app.logger.error(f"Error extrayendo campo {campo['nombre_campo']}: {e}")
    return resultados, confianza

@app.route('/extractor/enviar', methods=['POST'])
@login_required()
def enviar_extraido():
    """Envía por correo un PDF ya extraído."""
    data         = request.get_json()
    correo_dest  = data.get('correo', '').strip()
    pdf_path     = data.get('pdf_path', '')
    archivo_nombre = data.get('archivo_nombre', 'documento.pdf')
    asunto       = data.get('asunto', 'Documento adjunto')
    cuerpo       = data.get('cuerpo', 'Estimado/a, adjunto encontrará el documento solicitado.')

    if not correo_dest or not pdf_path or not os.path.exists(pdf_path):
        return jsonify(success=False, message='Datos incompletos o archivo no disponible.')

    # Config SMTP desde .env
    smtp_server = get_config('smtp_server')
    smtp_port   = int(get_config('smtp_port', '587'))
    email_user  = get_config('email_user')
    email_pass  = get_config('email_pass')
    email_nombre= get_config('email_nombre', 'Extractor de Documentos')

    if not all([smtp_server, email_user, email_pass]):
        return jsonify(success=False, message='Configura el servidor SMTP en el archivo .env')

    try:
        msg = MIMEMultipart()
        msg['From']    = formataddr((str(Header(email_nombre, 'utf-8')), email_user))
        msg['To']      = correo_dest
        msg['Subject'] = Header(asunto, 'utf-8')
        msg.attach(MIMEText(cuerpo, 'plain', 'utf-8'))
        with open(pdf_path, 'rb') as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f'attachment; filename="{archivo_nombre}"')
            msg.attach(part)
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(email_user, email_pass)
        server.send_message(msg)
        server.quit()
        return jsonify(success=True, message=f'Correo enviado a {correo_dest}')
    except Exception as e:
        app.logger.error(f"Error enviando correo: {e}")
        return jsonify(success=False, message=str(e))



@app.route('/extractor/enviar_masivo', methods=['POST'])
@login_required()
def enviar_masivo():
    """Envía múltiples PDFs extraídos en un solo request."""
    items = request.get_json()
    resultados = []
    for item in items:
        correo = item.get('correo','').strip()
        pdf_path = item.get('pdf_path','')
        archivo  = item.get('archivo_nombre','documento.pdf')
        datos    = item.get('datos', {})

        asunto_tpl = get_config('plantilla_asunto', 'Documento: {archivo}')
        cuerpo_tpl = get_config('plantilla_cuerpo', 'Estimado/a {cliente},\n\nAdjunto el documento.\n\nSaludos.')
        vars_tpl = {
            'archivo': archivo,
            'cliente': datos.get('cliente', datos.get('nombre_cliente', datos.get('razon_social', ''))),
            **{k: v or '' for k,v in datos.items()}
        }
        try:
            asunto = asunto_tpl.format(**vars_tpl)
            cuerpo = cuerpo_tpl.format(**vars_tpl)
        except Exception:
            asunto = asunto_tpl
            cuerpo = cuerpo_tpl

        if not correo or not pdf_path or not os.path.exists(pdf_path):
            resultados.append({'archivo': archivo, 'status': 'sin_correo'})
            continue

        smtp_server = get_config('smtp_server')
        smtp_port   = int(get_config('smtp_port','587'))
        email_user  = get_config('email_user')
        email_pass  = get_config('email_pass')
        email_nombre= get_config('email_nombre','Extractor de Documentos')
        try:
            msg = MIMEMultipart()
            msg['From']    = formataddr((str(Header(email_nombre,'utf-8')), email_user))
            msg['To']      = correo
            msg['Subject'] = Header(asunto,'utf-8')
            msg.attach(MIMEText(cuerpo,'plain','utf-8'))
            with open(pdf_path,'rb') as f:
                part = MIMEBase('application','octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{archivo}"')
                msg.attach(part)
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(email_user, email_pass)
            server.send_message(msg)
            server.quit()
            resultados.append({'archivo': archivo, 'status': 'enviado', 'correo': correo})
        except Exception as e:
            resultados.append({'archivo': archivo, 'status': 'error', 'error': str(e)})
    return jsonify(resultados=resultados)


@app.route('/configuracion/probar_extractor', methods=['POST'])
@login_required(roles=['admin'])
def probar_correo_extractor():
    correo_dest = request.json.get('correo','').strip()
    if not correo_dest:
        return jsonify(success=False, message='Ingresa un correo de destino.')
    smtp_server = get_config('smtp_server')
    smtp_port   = int(get_config('smtp_port', '587'))
    email_user  = get_config('email_user')
    email_pass  = get_config('email_pass')
    email_nombre= get_config('email_nombre', 'Extractor de Documentos')
    if not all([smtp_server, email_user, email_pass]):
        return jsonify(success=False, message='Configura primero el servidor SMTP.')
    try:
        msg = MIMEMultipart()
        msg['From']    = formataddr((str(Header(email_nombre, 'utf-8')), email_user))
        msg['To']      = correo_dest
        msg['Subject'] = Header('Prueba de configuración — Extractor de Documentos', 'utf-8')
        msg.attach(MIMEText('Este es un correo de prueba. La configuración SMTP es correcta.', 'plain', 'utf-8'))
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(email_user, email_pass)
        server.send_message(msg)
        server.quit()
        return jsonify(success=True, message=f'Correo de prueba enviado a {correo_dest}.')
    except Exception as e:
        return jsonify(success=False, message=str(e))


# ── Historial ────────────────────────────────────────────────────────────────
@app.route('/extractor/historial')
@login_required()
def historial_extractor():
    filas = db_query("""
        SELECT e.*, p.nombre as plantilla_nombre
        FROM extracciones e
        LEFT JOIN plantillas p ON p.id = e.plantilla_id
        ORDER BY e.fecha DESC LIMIT 200
    """, fetchall=True) or []
    return render_template('historial_extractor.html', extracciones=filas)

# ── API: extraer coordenadas de texto del PDF ─────────────────────────────
@app.route('/api/palabras_pdf', methods=['POST'])
@login_required()
def palabras_pdf():
    """Devuelve todas las palabras del PDF con sus coordenadas para el editor visual."""
    plantilla_id = request.json.get('plantilla_id')
    pagina_num   = request.json.get('pagina', 1)
    plantilla    = db_query("SELECT muestra_path FROM plantillas WHERE id=%s",
                            (plantilla_id,), fetchone=True)
    if not plantilla or not plantilla['muestra_path']:
        return jsonify(success=False, message='No hay PDF de muestra.')
    palabras = []
    with pdfplumber.open(plantilla['muestra_path']) as pdf:
        if pagina_num <= len(pdf.pages):
            page = pdf.pages[pagina_num - 1]
            for w in (page.extract_words() or []):
                palabras.append({
                    'texto': w['text'],
                    'x0': w['x0'], 'y0': w['top'],
                    'x1': w['x1'], 'y1': w['bottom'],
                })
    return jsonify(success=True, palabras=palabras,
                   ancho=float(pdf.pages[0].width),
                   alto=float(pdf.pages[0].height))


@app.route('/cleanup', methods=['POST'])
def cleanup():
    # Limpiar archivos temporales de la sesión si existen
    for path in session.pop('comprobantes', []):
        if path.get('pdf_path') and os.path.exists(path['pdf_path']):
            try: os.unlink(path['pdf_path'])
            except OSError as e: app.logger.error(f"Error al limpiar archivo: {e}")
    session.pop('totales', None)
    return "Cleanup", 200

if __name__ == '__main__':
    # La siguiente línea es para ejecutar en modo de DESARROLLO (como antes)
    # app.run(debug=True)

    # La siguiente sección es para ejecutar en modo de PRODUCCIÓN con Waitress
    from waitress import serve
    print("Iniciando servidor de producción en http://0.0.0.0:8080")
    serve(app, host='0.0.0.0', port=8081, threads=16, connection_limit=100)