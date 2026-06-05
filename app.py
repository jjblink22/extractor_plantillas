import os, sys, json, re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.header import Header
from email.utils import formataddr

# Parchear encoding ANTES de importar psycopg2 para evitar error con PostgreSQL en español en Windows
os.environ['PGCLIENTENCODING'] = 'UTF8'
os.environ['LC_ALL'] = 'C'

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, session, make_response, send_file)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import psycopg2
import psycopg2.extras
import pdfplumber

# Cargar .env manualmente (compatible con cualquier codificación Windows)
def cargar_env(path='.env'):
    try:
        with open(path, 'r', encoding='latin-1') as f:
            for linea in f:
                linea = linea.strip()
                if linea and '=' in linea and not linea.startswith('#'):
                    clave, valor = linea.split('=', 1)
                    os.environ.setdefault(clave.strip(), valor.strip())
    except FileNotFoundError:
        pass

cargar_env()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'extractor_secret_2024')

# ── Configuración ────────────────────────────────────────────────────────────
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB

DB_CONFIG = {
    'host':     os.getenv('DB_HOST', 'localhost'),
    'database': os.getenv('DB_NAME', 'extraccion_db'),
    'user':     os.getenv('DB_USER', 'extraccion_user'),
    'password': os.getenv('DB_PASS', ''),
}

# ── Base de datos ────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(**DB_CONFIG)

def db_query(query, params=None, fetchone=False, fetchall=False, commit=False):
    conn = None
    cur  = None
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(query, params)
        if commit: conn.commit()
        if fetchone:  return cur.fetchone()
        if fetchall:  return cur.fetchall()
        return True
    except Exception as e:
        app.logger.error(f"Error en la consulta a la base de datos: {e}")
        if conn: conn.rollback()
        return None
    finally:
        if cur:  cur.close()
        if conn: conn.close()

def init_db():
    tablas = [
        ("usuarios", '''CREATE TABLE IF NOT EXISTS usuarios (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            nombre TEXT NOT NULL,
            rol TEXT DEFAULT 'usuario',
            creado TIMESTAMP DEFAULT NOW())'''),

        ("plantillas", '''CREATE TABLE IF NOT EXISTS plantillas (
            id SERIAL PRIMARY KEY,
            nombre TEXT NOT NULL,
            descripcion TEXT,
            tipo_documento TEXT DEFAULT 'generico',
            activa BOOLEAN DEFAULT true,
            muestra_path TEXT,
            creado TIMESTAMP DEFAULT NOW(),
            actualizado TIMESTAMP DEFAULT NOW())'''),

        ("plantilla_campos", '''CREATE TABLE IF NOT EXISTS plantilla_campos (
            id SERIAL PRIMARY KEY,
            plantilla_id INTEGER REFERENCES plantillas(id) ON DELETE CASCADE,
            nombre_campo TEXT NOT NULL,
            etiqueta TEXT NOT NULL,
            tipo_campo TEXT DEFAULT 'texto',
            pagina INTEGER DEFAULT 1,
            x0 REAL, y0 REAL, x1 REAL, y1 REAL,
            es_tabla BOOLEAN DEFAULT false,
            config_tabla JSONB,
            patron_validacion TEXT,
            post_proceso TEXT,
            orden INTEGER DEFAULT 0)'''),

        ("configuracion", '''CREATE TABLE IF NOT EXISTS configuracion (
            clave VARCHAR(100) PRIMARY KEY,
            valor TEXT,
            actualizado TIMESTAMP DEFAULT NOW())'''),

        ("extracciones", '''CREATE TABLE IF NOT EXISTS extracciones (
            id SERIAL PRIMARY KEY,
            plantilla_id INTEGER REFERENCES plantillas(id),
            archivo_nombre TEXT,
            fecha TIMESTAMP DEFAULT NOW(),
            usuario_id INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            usuario_nombre TEXT,
            datos JSONB,
            estado TEXT DEFAULT 'exitoso')'''),
    ]

    for nombre, sql in tablas:
        conn = None
        try:
            conn = get_db()
            cur  = conn.cursor()
            cur.execute(sql)
            conn.commit()
            cur.close()
        except Exception as e:
            app.logger.error(f"Error creando tabla {nombre}: {e}")
            if conn: conn.rollback()
        finally:
            if conn: conn.close()

    # Usuario admin por defecto
    try:
        if not db_query("SELECT id FROM usuarios WHERE username='admin'", fetchone=True):
            db_query("INSERT INTO usuarios (username,password,nombre,rol) VALUES (%s,%s,%s,%s)",
                     ('admin', generate_password_hash('admin123'), 'Administrador', 'admin'),
                     commit=True)
    except Exception as e:
        app.logger.error(f"Error creando admin: {e}")

    # Valores por defecto de configuración
    config_defaults = [
        ('smtp_server',  os.getenv('SMTP_SERVER', '')),
        ('smtp_port',    os.getenv('SMTP_PORT', '587')),
        ('email_user',   os.getenv('EMAIL_USER', '')),
        ('email_pass',   os.getenv('EMAIL_PASS', '')),
        ('email_nombre',   os.getenv('EMAIL_NOMBRE', 'Extractor de Documentos')),
        # ── Identidad de marca (marca blanca) ──
        ('brand_name',     'Extractor de Plantillas'),
        ('brand_sub',      'Extracción inteligente de documentos'),
        ('brand_logo_url', ''),
        ('brand_theme',    'default'),
        ('brand_primary',  '#0f2744'),
        ('brand_accent',   '#1c5fa5'),
        ('brand_highlight','#3b9eff'),
        ('api_key',        ''),   # API REST — configurar en Configuración
    ]
    for clave, valor in config_defaults:
        try:
            db_query("""INSERT INTO configuracion (clave, valor) VALUES (%s,%s)
                        ON CONFLICT (clave) DO NOTHING""",
                     (clave, valor), commit=True)
        except: pass

    app.logger.info("Base de datos inicializada.")


def get_config(clave, default=''):
    row = db_query("SELECT valor FROM configuracion WHERE clave=%s", (clave,), fetchone=True)
    return row['valor'] if row and row['valor'] else default

def set_config(clave, valor):
    db_query("""INSERT INTO configuracion (clave, valor) VALUES (%s,%s)
                ON CONFLICT (clave) DO UPDATE SET valor=EXCLUDED.valor, actualizado=NOW()""",
             (clave, valor), commit=True)

# ── Context Processor: inyecta marca en TODOS los templates automáticamente ──
@app.context_processor
def inject_brand():
    """
    Variables de marca disponibles en cualquier template sin pasarlas manualmente.
    Para personalizar una empresa: cambiar estos valores en Configuración → Marca.
    """
    try:
        return dict(
            brand_name    = get_config('brand_name',    'Extractor de Plantillas'),
            brand_sub     = get_config('brand_sub',     'Extracción inteligente de documentos'),
            brand_logo_url= get_config('brand_logo_url', ''),
            brand_theme   = get_config('brand_theme',   'default'),
            brand_primary = get_config('brand_primary',  '#0f2744'),
            brand_accent  = get_config('brand_accent',   '#1c5fa5'),
            brand_highlight=get_config('brand_highlight','#3b9eff'),
        )
    except Exception:
        return dict(brand_name='Extractor', brand_sub='', brand_logo_url='',
                    brand_theme='default', brand_primary='#0f2744',
                    brand_accent='#1c5fa5', brand_highlight='#3b9eff')

# ── Auth ─────────────────────────────────────────────────────────────────────
def login_required(roles=None):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if roles and session.get('rol') not in roles:
                flash('No tienes permisos para acceder a esta sección.', 'danger')
                return redirect(url_for('index'))
            return f(*args, **kwargs)
        return wrapper
    return decorator

@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('index'))
    if request.method == 'POST':
        user = db_query("SELECT * FROM usuarios WHERE username=%s",
                        (request.form.get('username','').strip(),), fetchone=True)
        if user and check_password_hash(user['password'], request.form.get('password','')):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['nombre']   = user['nombre']
            session['rol']      = user['rol']
            return redirect(url_for('index'))
        flash('Usuario o contraseña incorrectos.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Rutas principales ────────────────────────────────────────────────────────
@app.route('/')
@login_required()
def index():
    plantillas = db_query("SELECT * FROM plantillas WHERE activa=true ORDER BY nombre", fetchall=True) or []
    stats = db_query("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN estado='exitoso' THEN 1 ELSE 0 END) as exitosos,
            SUM(CASE WHEN estado='revisar' THEN 1 ELSE 0 END) as revisar,
            SUM(CASE WHEN estado='error'   THEN 1 ELSE 0 END) as errores,
            SUM(CASE WHEN fecha >= NOW() - INTERVAL '7 days' THEN 1 ELSE 0 END) as esta_semana
        FROM extracciones
    """, fetchone=True) or {}
    ultimas = db_query("""
        SELECT e.archivo_nombre, e.estado, e.fecha, p.nombre as plantilla_nombre
        FROM extracciones e LEFT JOIN plantillas p ON p.id = e.plantilla_id
        ORDER BY e.fecha DESC LIMIT 5
    """, fetchall=True) or []
    return render_template('index.html', plantillas=plantillas, stats=stats, ultimas=ultimas)

# ── Plantillas ───────────────────────────────────────────────────────────────
@app.route('/plantillas')
@login_required()
def lista_plantillas():
    plantillas = db_query("""
        SELECT p.*, COUNT(e.id) as total_usos
        FROM plantillas p
        LEFT JOIN extracciones e ON e.plantilla_id = p.id
        GROUP BY p.id ORDER BY p.nombre
    """, fetchall=True) or []
    return render_template('plantillas.html', plantillas=plantillas)

@app.route('/plantillas/nueva', methods=['GET', 'POST'])
@login_required()
def nueva_plantilla():
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
            return redirect(url_for('editor_plantilla', plantilla_id=pid['id']))
        flash('Error al crear la plantilla.', 'danger')
    return render_template('nueva_plantilla.html')

@app.route('/plantillas/<int:plantilla_id>/editor')
@login_required()
def editor_plantilla(plantilla_id):
    plantilla = db_query("SELECT * FROM plantillas WHERE id=%s", (plantilla_id,), fetchone=True)
    if not plantilla:
        flash('Plantilla no encontrada.', 'danger')
        return redirect(url_for('lista_plantillas'))
    campos = db_query("SELECT * FROM plantilla_campos WHERE plantilla_id=%s ORDER BY orden",
                      (plantilla_id,), fetchall=True) or []
    response = make_response(render_template('editor.html', plantilla=plantilla, campos=campos))
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.route('/plantillas/<int:plantilla_id>/subir_muestra', methods=['POST'])
@login_required()
def subir_muestra(plantilla_id):
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
                   pdf_url=url_for('ver_muestra_pdf', plantilla_id=plantilla_id))

@app.route('/plantillas/<int:plantilla_id>/muestra_pdf')
@login_required()
def ver_muestra_pdf(plantilla_id):
    plantilla = db_query("SELECT muestra_path FROM plantillas WHERE id=%s",
                         (plantilla_id,), fetchone=True)
    if not plantilla or not plantilla['muestra_path']:
        return 'No hay PDF de muestra', 404
    return send_file(plantilla['muestra_path'], mimetype='application/pdf')

@app.route('/plantillas/<int:plantilla_id>/campos', methods=['POST'])
@login_required()
def guardar_campo(plantilla_id):
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
                (plantilla_id, nombre_campo, etiqueta, tipo_campo,
                 pagina, x0, y0, x1, y1, patron_validacion, post_proceso, orden)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (plantilla_id, data['nombre_campo'], data['etiqueta'],
                 data.get('tipo_campo','texto'), data.get('pagina',1),
                 data['x0'], data['y0'], data['x1'], data['y1'],
                 data.get('patron_validacion'), data.get('post_proceso'), orden),
                fetchone=True, commit=True)
        return jsonify(success=True, id=row['id'] if row else None)

@app.route('/plantillas/<int:plantilla_id>/campos/<int:campo_id>', methods=['DELETE'])
@login_required()
def eliminar_campo(plantilla_id, campo_id):
    db_query("DELETE FROM plantilla_campos WHERE id=%s AND plantilla_id=%s",
             (campo_id, plantilla_id), commit=True)
    return jsonify(success=True)

@app.route('/plantillas/<int:plantilla_id>/preview_zona', methods=['POST'])
@login_required()
def preview_zona(plantilla_id):
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


@app.route('/plantillas/<int:plantilla_id>/previsualizar', methods=['POST'])
@login_required()
def previsualizar_extraccion(plantilla_id):
    """Prueba la plantilla contra su PDF de muestra."""
    plantilla = db_query("SELECT * FROM plantillas WHERE id=%s", (plantilla_id,), fetchone=True)
    if not plantilla or not plantilla['muestra_path']:
        return jsonify(success=False, message='No hay PDF de muestra cargado.')
    resultado, _ = aplicar_plantilla(plantilla['muestra_path'], plantilla_id)
    return jsonify(success=True, resultado=resultado)

# ── Extracción ───────────────────────────────────────────────────────────────
@app.route('/extraer', methods=['GET', 'POST'])
@login_required()
def extraer():
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
                datos, confianza = aplicar_plantilla(path, int(plantilla_id))
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

def aplicar_plantilla(pdf_path, plantilla_id):
    campos = db_query("SELECT * FROM plantilla_campos WHERE plantilla_id=%s ORDER BY orden",
                      (plantilla_id,), fetchall=True) or []
    resultados = {}
    confianza  = {}
    with pdfplumber.open(pdf_path) as pdf:
        for campo in campos:
            try:
                pagina = pdf.pages[campo['pagina'] - 1]
                zona   = pagina.crop((campo['x0'], campo['y0'], campo['x1'], campo['y1']))
                texto  = (zona.extract_text() or '').strip()
                if campo['post_proceso']:
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

@app.route('/extraer/enviar', methods=['POST'])
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


@app.route('/configuracion', methods=['GET', 'POST'])
@login_required(roles=['admin'])
def configuracion():
    if request.method == 'POST':
        seccion = request.form.get('seccion', 'smtp')

        if seccion == 'smtp':
            set_config('smtp_server',  request.form.get('smtp_server','').strip())
            set_config('smtp_port',    request.form.get('smtp_port','587').strip())
            set_config('email_user',   request.form.get('email_user','').strip())
            set_config('email_nombre', request.form.get('email_nombre','').strip())
            if request.form.get('email_pass','').strip():
                set_config('email_pass', request.form.get('email_pass').strip())
            if request.form.get('api_key_value','').strip():
                set_config('api_key', request.form.get('api_key_value').strip())
            flash('Configuración SMTP guardada.', 'success')

        elif seccion == 'marca':
            set_config('brand_name',      request.form.get('brand_name','').strip())
            set_config('brand_sub',       request.form.get('brand_sub','').strip())
            set_config('brand_theme',     request.form.get('brand_theme','default').strip())
            set_config('brand_primary',   request.form.get('brand_primary','#0f2744').strip())
            set_config('brand_accent',    request.form.get('brand_accent','#1c5fa5').strip())
            set_config('brand_highlight', request.form.get('brand_highlight','#3b9eff').strip())
            # Logo upload
            logo = request.files.get('brand_logo')
            if logo and logo.filename:
                ext  = os.path.splitext(secure_filename(logo.filename))[1].lower()
                path = os.path.join(app.static_folder, f'brand_logo{ext}')
                logo.save(path)
                set_config('brand_logo_url', url_for('static', filename=f'brand_logo{ext}'))
            flash('Identidad de marca guardada.', 'success')

        return redirect(url_for('configuracion'))

    cfg = {
        'smtp_server':  get_config('smtp_server'),
        'smtp_port':    get_config('smtp_port', '587'),
        'email_user':   get_config('email_user'),
        'email_pass':   get_config('email_pass'),
        'email_nombre': get_config('email_nombre', 'Extractor de Documentos'),
        'brand_name':     get_config('brand_name', 'Extractor de Plantillas'),
        'brand_sub':      get_config('brand_sub', ''),
        'brand_logo_url': get_config('brand_logo_url', ''),
        'brand_theme':    get_config('brand_theme', 'default'),
        'brand_primary':  get_config('brand_primary', '#0f2744'),
        'brand_accent':   get_config('brand_accent', '#1c5fa5'),
        'brand_highlight':get_config('brand_highlight','#3b9eff'),
    }
    # Temas disponibles
    themes_dir = os.path.join(app.static_folder, 'themes')
    temas = []
    if os.path.exists(themes_dir):
        temas = [f.replace('.css','') for f in os.listdir(themes_dir) if f.endswith('.css')]
    return render_template('configuracion.html', cfg=cfg, temas=temas)


# ── Gestión de usuarios ───────────────────────────────────────────────────────
@app.route('/usuarios')
@login_required(roles=['admin'])
def admin_usuarios():
    usuarios = db_query("""
        SELECT id, username, COALESCE(nombre, username) as nombre, rol, creado
        FROM usuarios ORDER BY COALESCE(nombre, username)
    """, fetchall=True) or []
    return render_template('admin_usuarios.html', usuarios=usuarios)

@app.route('/usuarios/crear', methods=['POST'])
@login_required(roles=['admin'])
def crear_usuario():
    username = request.form.get('username','').strip()
    nombre   = request.form.get('nombre','').strip()
    password = request.form.get('password','').strip()
    rol      = request.form.get('rol','usuario')
    if not all([username, nombre, password]):
        flash('Todos los campos son obligatorios.', 'danger')
        return redirect(url_for('admin_usuarios'))
    if db_query("SELECT id FROM usuarios WHERE username=%s", (username,), fetchone=True):
        flash(f'El usuario "{username}" ya existe.', 'danger')
        return redirect(url_for('admin_usuarios'))
    db_query("INSERT INTO usuarios (username, password, nombre, rol) VALUES (%s,%s,%s,%s)",
             (username, generate_password_hash(password), nombre, rol), commit=True)
    flash(f'Usuario "{nombre}" creado correctamente.', 'success')
    return redirect(url_for('admin_usuarios'))

@app.route('/usuarios/<int:user_id>/eliminar', methods=['POST'])
@login_required(roles=['admin'])
def eliminar_usuario(user_id):
    if user_id == session.get('user_id'):
        flash('No puedes eliminar tu propio usuario.', 'danger')
        return redirect(url_for('admin_usuarios'))
    db_query("DELETE FROM usuarios WHERE id=%s", (user_id,), commit=True)
    flash('Usuario eliminado.', 'success')
    return redirect(url_for('admin_usuarios'))

@app.route('/usuarios/<int:user_id>/cambiar_password', methods=['POST'])
@login_required(roles=['admin'])
def cambiar_password(user_id):
    nueva = request.form.get('password','').strip()
    if len(nueva) < 6:
        flash('La contraseña debe tener al menos 6 caracteres.', 'danger')
        return redirect(url_for('admin_usuarios'))
    db_query("UPDATE usuarios SET password=%s WHERE id=%s",
             (generate_password_hash(nueva), user_id), commit=True)
    flash('Contraseña actualizada.', 'success')
    return redirect(url_for('admin_usuarios'))

@app.route('/usuarios/<int:user_id>/cambiar_rol', methods=['POST'])
@login_required(roles=['admin'])
def cambiar_rol(user_id):
    if user_id == session.get('user_id'):
        flash('No puedes cambiar tu propio rol.', 'danger')
        return redirect(url_for('admin_usuarios'))
    db_query("UPDATE usuarios SET rol=%s WHERE id=%s",
             (request.form.get('rol','usuario'), user_id), commit=True)
    flash('Rol actualizado.', 'success')
    return redirect(url_for('admin_usuarios'))


@app.route('/configuracion/probar', methods=['POST'])
@login_required(roles=['admin'])
def probar_correo():
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
@app.route('/historial')
@login_required()
def historial():
    plantilla_id = request.args.get('plantilla_id','')
    estado       = request.args.get('estado','')
    busqueda     = request.args.get('q','')

    filtros = ["1=1"]
    params  = []
    if plantilla_id:
        filtros.append("e.plantilla_id=%s"); params.append(plantilla_id)
    if estado:
        filtros.append("e.estado=%s"); params.append(estado)
    if busqueda:
        filtros.append("e.archivo_nombre ILIKE %s"); params.append(f'%{busqueda}%')

    filas = db_query(f"""
        SELECT e.*, p.nombre as plantilla_nombre
        FROM extracciones e
        LEFT JOIN plantillas p ON p.id = e.plantilla_id
        WHERE {' AND '.join(filtros)}
        ORDER BY e.fecha DESC LIMIT 300
    """, params or None, fetchall=True) or []

    plantillas = db_query("SELECT id,nombre FROM plantillas ORDER BY nombre", fetchall=True) or []
    return render_template('historial.html', extracciones=filas,
                           plantillas=plantillas, filtros={'plantilla_id':plantilla_id,'estado':estado,'q':busqueda})


# ── Duplicar plantilla ─────────────────────────────────────────────────────
@app.route('/plantillas/<int:plantilla_id>/duplicar', methods=['POST'])
@login_required()
def duplicar_plantilla(plantilla_id):
    orig = db_query("SELECT * FROM plantillas WHERE id=%s", (plantilla_id,), fetchone=True)
    if not orig:
        flash('Plantilla no encontrada.', 'danger')
        return redirect(url_for('lista_plantillas'))
    nueva = db_query("""
        INSERT INTO plantillas (nombre, descripcion, tipo_documento)
        VALUES (%s,%s,%s) RETURNING id
    """, (f"{orig['nombre']} (copia)", orig['descripcion'], orig['tipo_documento']),
        fetchone=True, commit=True)
    if nueva:
        campos = db_query("SELECT * FROM plantilla_campos WHERE plantilla_id=%s ORDER BY orden",
                          (plantilla_id,), fetchall=True) or []
        for c in campos:
            db_query("""INSERT INTO plantilla_campos
                (plantilla_id,nombre_campo,etiqueta,tipo_campo,pagina,x0,y0,x1,y1,
                 es_tabla,patron_validacion,post_proceso,orden)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (nueva['id'],c['nombre_campo'],c['etiqueta'],c['tipo_campo'],
                 c['pagina'],c['x0'],c['y0'],c['x1'],c['y1'],
                 c['es_tabla'],c['patron_validacion'],c['post_proceso'],c['orden']),
                commit=True)
        flash(f'Plantilla duplicada como "{orig["nombre"]} (copia)".', 'success')
        return redirect(url_for('editor_plantilla', plantilla_id=nueva['id']))
    flash('Error al duplicar.', 'danger')
    return redirect(url_for('lista_plantillas'))


# ── Exportar historial a Excel con orden personalizado ─────────────────────
@app.route('/historial/exportar')
@login_required()
def exportar_historial():
    import io
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        flash('Instala openpyxl: pip install openpyxl', 'danger')
        return redirect(url_for('historial'))

    plantilla_id = request.args.get('plantilla_id')
    estado       = request.args.get('estado','')

    filtros = ["1=1"]; params = []
    if plantilla_id:
        filtros.append("e.plantilla_id=%s"); params.append(plantilla_id)
    if estado:
        filtros.append("e.estado=%s"); params.append(estado)

    filas = db_query(f"""
        SELECT e.*, p.nombre as plantilla_nombre
        FROM extracciones e LEFT JOIN plantillas p ON p.id = e.plantilla_id
        WHERE {' AND '.join(filtros)} ORDER BY e.fecha DESC LIMIT 5000
    """, params or None, fetchall=True) or []

    # Obtener orden de campos de la plantilla si se filtra por una
    orden_campos = []
    if plantilla_id:
        campos = db_query("""SELECT nombre_campo, etiqueta FROM plantilla_campos
                             WHERE plantilla_id=%s ORDER BY orden""",
                          (plantilla_id,), fetchall=True) or []
        orden_campos = [(c['nombre_campo'], c['etiqueta']) for c in campos]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Extracciones'

    # Estilo cabecera
    hdr_fill = PatternFill("solid", fgColor="0F2744")
    hdr_font = Font(bold=True, color="FFFFFF", size=10)
    hdr_aln  = Alignment(horizontal="center", vertical="center")

    # Columnas base
    base_cols = ['Fecha', 'Archivo', 'Plantilla', 'Estado', 'Usuario']
    # Columnas de datos en el orden definido por la plantilla
    data_cols  = [etiqueta for _, etiqueta in orden_campos] if orden_campos else []
    data_keys  = [key for key, _ in orden_campos] if orden_campos else []

    all_cols = base_cols + data_cols
    ws.append(all_cols)

    for cell in ws[1]:
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = hdr_aln

    # Filas de datos
    for fila in filas:
        datos = fila.get('datos') or {}
        if isinstance(datos, str):
            import json as _json
            try: datos = _json.loads(datos)
            except: datos = {}
        row = [
            fila['fecha'].strftime('%d/%m/%Y %H:%M') if fila.get('fecha') else '',
            fila.get('archivo_nombre',''),
            fila.get('plantilla_nombre',''),
            fila.get('estado',''),
            fila.get('usuario_nombre',''),
        ]
        # Agregar campos en el orden de la plantilla
        if data_keys:
            for key in data_keys:
                row.append(datos.get(key,''))
        else:
            # Sin plantilla específica: agregar todos los campos del JSON
            for k,v in datos.items():
                if k not in data_keys:
                    row.append(f"{k}: {v}")
        ws.append(row)

    # Ajustar anchos
    for col in ws.columns:
        max_w = max((len(str(c.value or '')) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_w + 4, 50)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    from flask import send_file as _sf
    return _sf(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
               as_attachment=True, download_name='extracciones.xlsx')


# ── Reprocesar extracción ──────────────────────────────────────────────────
@app.route('/historial/<int:extraccion_id>/reprocesar', methods=['POST'])
@login_required()
def reprocesar_extraccion(extraccion_id):
    ext = db_query("SELECT * FROM extracciones WHERE id=%s", (extraccion_id,), fetchone=True)
    if not ext:
        return jsonify(success=False, message='Extracción no encontrada.')
    # Guardar PDF temporalmente si está disponible
    flash('Para reprocesar sube nuevamente el PDF en la sección Extraer.', 'info')
    return redirect(url_for('extraer', plantilla_id=ext['plantilla_id']))


# ── Auto-detectar plantilla desde PDF ────────────────────────────────────
@app.route('/api/campos_plantilla/<int:plantilla_id>')
@login_required()
def api_campos_plantilla(plantilla_id):
    campos = db_query("""SELECT nombre_campo, etiqueta, tipo_campo, orden
                         FROM plantilla_campos WHERE plantilla_id=%s ORDER BY orden""",
                      (plantilla_id,), fetchall=True) or []
    return jsonify(campos=[dict(c) for c in campos])


@app.route('/api/autodetectar', methods=['POST'])
@login_required()
def autodetectar_plantilla():
    """Analiza un PDF y sugiere la plantilla más probable."""
    archivo = request.files.get('pdf')
    if not archivo:
        return jsonify(success=False)
    path = os.path.join(UPLOAD_FOLDER, secure_filename(archivo.filename))
    archivo.save(path)
    try:
        with pdfplumber.open(path) as pdf:
            texto = ' '.join(
                (pdf.pages[i].extract_text() or '') for i in range(min(2, len(pdf.pages)))
            ).lower()

        plantillas = db_query("""
            SELECT id, nombre, descripcion, tipo_documento FROM plantillas WHERE activa=true
        """, fetchall=True) or []

        mejor = None; mejor_score = 0
        for p in plantillas:
            keywords = (p['nombre'] + ' ' + (p['descripcion'] or '') + ' ' + p['tipo_documento']).lower()
            score = sum(1 for w in keywords.split() if len(w)>3 and w in texto)
            if score > mejor_score:
                mejor_score = score; mejor = p

        os.remove(path)
        if mejor and mejor_score > 0:
            return jsonify(success=True, plantilla_id=mejor['id'],
                           plantilla_nombre=mejor['nombre'], score=mejor_score)
        return jsonify(success=False, message='No se encontró plantilla compatible.')
    except Exception as e:
        if os.path.exists(path): os.remove(path)
        return jsonify(success=False, message=str(e))


# ── API REST pública ───────────────────────────────────────────────────────
@app.route('/api/v1/extraer', methods=['POST'])
def api_extraer():
    """
    API REST para extracción desde sistemas externos.
    Headers: X-API-Key: <clave>
    Body: multipart/form-data con 'pdf' y 'plantilla_id'
    """
    api_key = request.headers.get('X-API-Key','')
    key_valida = get_config('api_key','')
    if not key_valida or api_key != key_valida:
        return jsonify(error='API key inválida o no configurada.'), 401

    archivo = request.files.get('pdf')
    plantilla_id = request.form.get('plantilla_id')
    if not archivo or not plantilla_id:
        return jsonify(error='Parámetros requeridos: pdf, plantilla_id'), 400

    path = os.path.join(UPLOAD_FOLDER, secure_filename(archivo.filename))
    archivo.save(path)
    try:
        datos, confianza = aplicar_plantilla(path, int(plantilla_id))
        os.remove(path)
        return jsonify(success=True, datos=datos, confianza=confianza,
                       archivo=archivo.filename)
    except Exception as e:
        if os.path.exists(path): os.remove(path)
        return jsonify(error=str(e)), 500


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
                palabras.append({'texto': w['text'],
                    'x0': w['x0'], 'y0': w['top'],
                    'x1': w['x1'], 'y1': w['bottom']})
    return jsonify(success=True, palabras=palabras,
                   ancho=float(pdf.pages[0].width), alto=float(pdf.pages[0].height))

if __name__ == '__main__':
    init_db()
    from waitress import serve
    port = int(os.getenv('PORT', 8081))
    print(f"Extractor de Plantillas iniciando en http://0.0.0.0:{port}")
    serve(app, host='0.0.0.0', port=port, threads=8)
