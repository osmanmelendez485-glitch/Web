from flask import Flask, render_template, request, redirect, url_for, session
import firebase_admin
from firebase_admin import credentials, firestore
import os
from werkzeug.utils import secure_filename
from datetime import datetime


import os
import json
import firebase_admin
from firebase_admin import credentials, firestore

# Intentar leer la variable de entorno para producción
firebase_json = os.environ.get('FIREBASE_JSON')

if firebase_json:
    # Si estamos en Render/Railway, usamos la variable de entorno
    cred_dict = json.loads(firebase_json)
    cred = credentials.Certificate(cred_dict)
else:
    # Si estamos en tu PC local, usamos el archivo físico
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred)
db = firestore.client()
app = Flask(__name__)
app.secret_key = 'tu_llave_secreta_segura'

# --- CONFIGURACIÓN DE FIREBASE ---
# Asegúrate de que el archivo JSON de tu cuenta de servicio esté en la carpeta del proyecto
cred = credentials.Certificate("serviceAccountKey.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- RUTAS ---

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
    
    # En Firestore, el ordenamiento funciona de forma similar
    sort_by = request.args.get('sort', 'fecha')
    direction = firestore.Query.DESCENDING if 'fecha' in sort_by or 'pago' in sort_by else firestore.Query.ASCENDING
    
    field = sort_by.split(' ')[0] # Limpiamos el string del request

    try:
        # Consulta a la colección 'Empleados'
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
    
    # Preparar el diccionario de datos
    nuevo_empleado = {
        'fecha': d.get('fecha'),
        'nombre': d.get('nombre', '').strip(),
        'apellido': d.get('apellido', ''),
        'cedula': d.get('cedula', '').strip(),
        'direccion': d.get('direccion', 'N/A'),
        'pago': float(d.get('pago') or 0.0),
        'equipo': float(d.get('equipo') or 0.0),
        'deposito': float(d.get('deposito') or 0.0),
        'sexo': 'M',
        'contrato': ""
    }

    # Manejo de archivo
    file_contrato = request.files.get('contrato')
    if file_contrato and file_contrato.filename != '':
        nom_contrato = secure_filename(file_contrato.filename)
        file_contrato.save(os.path.join(app.config['UPLOAD_FOLDER'], nom_contrato))
        nuevo_empleado['contrato'] = nom_contrato

    try:
        # En Firestore no hay "IntegrityError" por duplicados de la misma forma,
        # pero podemos buscar si la cédula existe:
        query = db.collection('Empleados').where('cedula', '==', nuevo_empleado['cedula']).limit(1).get()
        
        if query:
            # UPDATE si existe
            doc_id = query[0].id
            db.collection('Empleados').document(doc_id).update(nuevo_empleado)
        else:
            # INSERT si no existe
            db.collection('Empleados').add(nuevo_empleado)
            
    except Exception as e:
        return f"Error al guardar: {e}"

    return redirect(url_for('dashboard'))

@app.route('/update', methods=['POST'])
def update():
    if 'user' not in session: return redirect(url_for('login'))
    d = request.form
    id_reg = d.get('id') # El ID alfanumérico de Firestore
    
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

@app.route('/delete/<id>') # Quitamos el 'int:' porque Firestore usa strings
def delete(id):
    if 'user' not in session: return redirect(url_for('login'))
    db.collection('Empleados').document(id).delete()
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)