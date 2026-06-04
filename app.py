import os, json, re
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

    app.logger.info("Base de datos inicializada.")

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
    total_extracciones = (db_query("SELECT COUNT(*) as n FROM extracciones", fetchone=True) or {}).get('n', 0)
    return render_template('index.html', plantillas=plantillas, total_extracciones=total_extracciones)

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
              fetchone=True)
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
                fetchone=True)
        return jsonify(success=True, id=row['id'] if row else None)

@app.route('/plantillas/<int:plantilla_id>/campos/<int:campo_id>', methods=['DELETE'])
@login_required()
def eliminar_campo(plantilla_id, campo_id):
    db_query("DELETE FROM plantilla_campos WHERE id=%s AND plantilla_id=%s",
             (campo_id, plantilla_id), commit=True)
    return jsonify(success=True)

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
                                   'confianza': confianza, 'estado': estado})
            except Exception as e:
                app.logger.error(f"Error extrayendo {archivo.filename}: {e}")
                resultados.append({'archivo': archivo.filename, 'error': str(e), 'estado': 'error'})
            finally:
                if os.path.exists(path): os.remove(path)
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

# ── Historial ────────────────────────────────────────────────────────────────
@app.route('/historial')
@login_required()
def historial():
    filas = db_query("""
        SELECT e.*, p.nombre as plantilla_nombre
        FROM extracciones e
        LEFT JOIN plantillas p ON p.id = e.plantilla_id
        ORDER BY e.fecha DESC LIMIT 200
    """, fetchall=True) or []
    return render_template('historial.html', extracciones=filas)

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

if __name__ == '__main__':
    init_db()
    from waitress import serve
    port = int(os.getenv('PORT', 8081))
    print(f"Extractor de Plantillas iniciando en http://0.0.0.0:{port}")
    serve(app, host='0.0.0.0', port=port, threads=8)
