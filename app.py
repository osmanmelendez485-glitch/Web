from flask import Flask, render_template, request, redirect, url_for, session
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
from werkzeug.utils import secure_filename
from datetime import datetime

# ==========================================
# 1. CONFIGURACIÓN DE FIREBASE (CORREGIDA)
# ==========================================
firebase_json = os.environ.get('FIREBASE_JSON')

if firebase_json:
    # Caso: Producción (Render) - Lee desde la Variable de Entorno
    cred_dict = json.loads(firebase_json)
    cred = credentials.Certificate(cred_dict)
else:
    # Caso: Desarrollo Local (Tu PC) - Lee desde el archivo .json
    # Asegúrate de que este archivo exista en tu carpeta local
    cred = credentials.Certificate("serviceAccountKey.json")

# Inicializar Firebase solo una vez
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================
# 2. CONFIGURACIÓN DE FLASK
# ==========================================
app = Flask(__name__)
app.secret_key = 'tu_llave_secreta_segura'

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# ==========================================
# 3. RUTAS DE LA APLICACIÓN
# ==========================================

@app.route('/')
def login():
    return render_template('login.html')

@app.route('/auth', methods=['POST'])
def auth():
    user = request.form.get('username')
    pwd = request.form.get('password')
    if user == 'admin' and pwd == '1234':
        session['user'] = user
        return redirect(url_for('dashboard'))
    return "Acceso denegado. <a href='/'>Volver</a>"

@app.route('/dashboard')
def dashboard():
    if 'user' not in session: return redirect(url_for('login'))
    
    sort_by = request.args.get('sort', 'fecha')
    # Ajuste de dirección de ordenamiento para Firestore
    direction = firestore.Query.DESCENDING if 'fecha' in sort_by or 'pago' in sort_by else firestore.Query.ASCENDING
    field = sort_by.split(' ')[0] 

    try:
        # Consulta a Firestore
        empleados_ref = db.collection('Empleados').order_by(field, direction=direction)
        docs = empleados_ref.stream()
        
        datos = []
        for doc in docs:
            item = doc.to_dict()
            item['id'] = doc.id  # Firestore usa IDs alfanuméricos
            datos.append(item)
            
        return render_template('index.html', empleados=datos)
    except Exception as e:
        return f"Error de Firebase: {e}"

@app.route('/add', methods=['POST'])
def add():
    if 'user' not in session: return redirect(url_for('login'))
    d = request.form
    cedula = d.get('cedula', '').strip()
    nombre = d.get('nombre', '').strip()
    
    nuevo_empleado = {
        'fecha': d.get('fecha'),
        'nombre': nombre,
        'apellido': d.get('apellido', ''),
        'cedula': cedula,
        'direccion': d.get('direccion', 'N/A'),
        'pago': float(d.get('pago') or 0.0),
        'equipo': float(d.get('equipo') or 0.0),
        'deposito': float(d.get('deposito') or 0.0),
        'sexo': 'M',
        'escolaridad': 'N/A',
        'ano_escolaridad': 0,
        'ciudad': 'Chinandega',
        'dependientes': 0,
        'contrato': ""
    }

    # Manejo de archivo (contrato)
    file_contrato = request.files.get('contrato')
    if file_contrato and file_contrato.filename != '':
        nom_contrato = secure_filename(file_contrato.filename)
        file_contrato.save(os.path.join(app.config['UPLOAD_FOLDER'], nom_contrato))
        nuevo_empleado['contrato'] = nom_contrato

    try:
        # Lógica de Upsert (Actualizar si existe la cédula, si no Crear)
        query = db.collection('Empleados').where('cedula', '==', cedula).limit(1).get()
        
        if query:
            doc_id = query[0].id
            db.collection('Empleados').document(doc_id).update(nuevo_empleado)
        else:
            db.collection('Empleados').add(nuevo_empleado)
            
    except Exception as e:
        return f"Error al guardar: {e}"

    return redirect(url_for('dashboard'))

@app.route('/update', methods=['POST'])
def update():
    if 'user' not in session: return redirect(url_for('login'))
    d = request.form
    id_reg = d.get('id')
    
    datos_update = {
        'fecha': d.get('fecha'),
        'nombre': d.get('nombre'),
        'apellido': d.get('apellido'),
        'cedula': d.get('cedula'),
        'direccion': d.get('direccion'),
        'equipo': float(d.get('equipo') or 0),
        'pago': float(d.get('pago') or 0),
        'deposito': float(d.get('deposito') or 0)
    }
    
    try:
        db.collection('Empleados').document(id_reg).update(datos_update)
    except Exception as e:
        return f"Error: {e}"
    return redirect(url_for('dashboard'))

@app.route('/delete/<id>')
def delete(id):
    if 'user' not in session: return redirect(url_for('login'))
    try:
        db.collection('Empleados').document(id).delete()
    except Exception as e:
        return f"Error al eliminar: {e}"
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    # Puerto dinámico para Render, por defecto 5000 para local
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)