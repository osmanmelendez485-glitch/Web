import os
import re
import json
import pandas as pd
import numpy as np
from datetime import datetime
from dateutil.relativedelta import relativedelta
from io import BytesIO
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash, send_file
import firebase_admin
from firebase_admin import credentials, firestore
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'tu_llave_secreta_aqui'

# --- CONFIGURACIÓN DE CARPETAS ---
UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# --- CONFIGURACIÓN DE FIREBASE ---
firebase_json = os.environ.get('FIREBASE_JSON')
try:
    if firebase_json:
        cred_info = json.loads(firebase_json)
        cred = credentials.Certificate(cred_info)
    else:
        cred = credentials.Certificate("serviceAccountKey.json")

    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
except Exception as e:
    print(f"⚠️ Error al conectar Firebase: {e}")
    db = None

# --- AYUDANTES ---
def clean_val(val, default=""):
    if val is None or (isinstance(val, float) and np.isnan(val)) or str(val).lower() == 'nan':
        return default
    return val

def safe_float(val):
    try: 
        if val is None or str(val).lower() == 'nan' or str(val).strip() == '': return 0.0
        return float(str(val).replace(',', '.'))
    except: return 0.0

@app.context_processor
def inject_now():
    return {'now': datetime.now(), 'datetime': datetime}

# --- RUTAS DE AUTENTICACIÓN ---
@app.route('/')
def login_page():
    if 'user' in session: return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/auth', methods=['POST'])
def auth():
    user = request.form.get('user')
    pw = request.form.get('password')
    if user == 'admin' and pw == '1234': 
        session['user'] = user
        return redirect(url_for('dashboard'))
    flash("Usuario o contraseña incorrectos", "danger")
    return redirect(url_for('login_page'))

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login_page'))

# --- DASHBOARD ---
@app.route('/dashboard')
def dashboard():
    if 'user' not in session: return redirect(url_for('login_page'))
    if not db: return "Error: No hay conexión con la base de datos."
    
    search_query = request.args.get('search', '').lower()
    sort_by = request.args.get('sort', 'fecha')
    direction = request.args.get('direction', 'desc')
    
    try:
        order_dir = firestore.Query.DESCENDING if direction == 'desc' else firestore.Query.ASCENDING
        docs = db.collection('Empleados').order_by(sort_by, direction=order_dir).stream()
        
        empleados = []
        total_recaudado = 0.0
        total_pendiente = 0.0

        for doc in docs:
            item = doc.to_dict()
            item['id'] = doc.id
            item['estado'] = clean_val(item.get('estado'), 'Pendiente')
            canon_val = safe_float(item.get('canon'))
            item['canon'] = canon_val
            
            nombre_completo = f"{item.get('nombre','')} {item.get('apellido','')}".lower()
            cedula = str(item.get('cedula','')).lower()
            num_con = str(item.get('num_contrato','')).lower()

            if not search_query or (search_query in nombre_completo or search_query in cedula or search_query in num_con):
                if item['estado'] == 'Cancelado':
                    total_recaudado += canon_val
                else:
                    total_pendiente += canon_val
                empleados.append(item)
                
        return render_template('index.html', empleados=empleados, search_query=search_query, 
                               sort_by=sort_by, direction=direction,
                               total_recaudado=total_recaudado, total_pendiente=total_pendiente)
    except Exception as e:
        return f"Error en Dashboard: {e}"

# --- GUARDAR / EDITAR ---
@app.route('/save', methods=['POST'])
def save():
    if 'user' not in session: return redirect(url_for('login_page'))
    
    d = request.form
    emp_id = d.get('id')
    
    datos = {
        'nombre': d.get('nombre'),
        'apellido': d.get('apellido'),
        'cedula': d.get('cedula'),
        'num_contrato': d.get('num_contrato'),
        'direccion': d.get('direccion'),
        'estado': d.get('estado', 'Pendiente'),
        'internet': safe_float(d.get('internet')),
        'agua': safe_float(d.get('agua')),
        'luz': safe_float(d.get('luz')),
        'canon': safe_float(d.get('canon')),
        'equipo': safe_float(d.get('equipo')),
        'deposito': safe_float(d.get('deposito')),
        'total_pagar': safe_float(d.get('internet')) + safe_float(d.get('agua')) + safe_float(d.get('luz')) + safe_float(d.get('canon')) + safe_float(d.get('equipo'))
    }

    if 'imagen_archivo' in request.files:
        file = request.files['imagen_archivo']
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            upload_path = os.path.join(app.root_path, 'static', 'uploads')
            if not os.path.exists(upload_path): os.makedirs(upload_path)
            file.save(os.path.join(upload_path, filename))
            datos['foto_url'] = f'/static/uploads/{filename}'

    if emp_id:
        db.collection('Empleados').document(emp_id).update(datos)
        flash("Registro actualizado correctamente", "success")
    else:
        # Lógica simplificada para nuevo registro (faltaría tu bucle de pagos)
        db.collection('Empleados').add(datos)
        flash("Contrato creado", "success")

    return redirect(url_for('dashboard'))

# --- GESTIÓN DE PAGOS ---
@app.route('/ver_pagos/<id>')
def ver_pagos(id):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    doc_ref = db.collection('Empleados').document(id)
    contrato = doc_ref.get().to_dict()
    if not contrato: return redirect(url_for('dashboard'))
    contrato['id'] = id

    pagos_query = doc_ref.collection('Pagos').order_by('fecha_vencimiento').stream()
    
    pagos = []
    recaudado = 0.0
    pendiente = 0.0
    
    for p in pagos_query:
        p_data = p.to_dict()
        p_data['id'] = p.id
        monto = safe_float(p_data.get('monto', 0))
        
        if p_data.get('estado') == 'Cancelado':
            recaudado += monto
        elif p_data.get('estado') == 'Pendiente':
            pendiente += monto
            
        pagos.append(p_data)

    return render_template('pagos.html', pagos=pagos, contrato=contrato, id=id, 
                           total_recaudado=recaudado, total_pendiente=pendiente)

@app.route('/toggle_pago/<e_id>/<p_id>/<nuevo_estado>')
def toggle_pago(e_id, p_id, nuevo_estado):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    db.collection('Empleados').document(e_id).collection('Pagos').document(p_id).update({
        'estado': nuevo_estado
    })
    
    flash(f"Estado actualizado a {nuevo_estado}", "success")
    return redirect(url_for('ver_pagos', id=e_id))

@app.route('/suspend/<e_id>/<p_id>')
def suspend_payment(e_id, p_id):
    # Reutiliza toggle_pago internamente
    return toggle_pago(e_id, p_id, 'Suspensión')

# --- OTROS ---
@app.route('/delete/<id>')
def delete(id):
    if 'user' not in session: return redirect(url_for('login_page'))
    db.collection('Empleados').document(id).delete()
    flash("Registro eliminado", "warning")
    return redirect(url_for('dashboard'))

@app.route('/propiedades')
def propiedades():
    if 'user' not in session: return redirect(url_for('login_page'))
    docs = db.collection('Empleados').stream()
    propiedades_dict = {}
    for doc in docs:
        item = doc.to_dict()
        item['id'] = doc.id
        dir_name = item.get('direccion', 'Sin Dirección')
        if dir_name not in propiedades_dict or item.get('fecha', '') > propiedades_dict[dir_name].get('fecha', ''):
            propiedades_dict[dir_name] = item
    return render_template('propiedades.html', propiedades=propiedades_dict)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)