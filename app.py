import os
import re
import json
import pandas as pd
import numpy as np
from datetime import datetime
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
            pago_val = safe_float(item.get('pago'))
            item['pago'] = pago_val
            
            # Formateo para búsqueda
            nombre_completo = f"{item.get('nombre','')} {item.get('apellido','')}".lower()
            cedula = str(item.get('cedula','')).lower()
            num_con = str(item.get('num_contrato','')).lower()

            if not search_query or (search_query in nombre_completo or search_query in cedula or search_query in num_con):
                if item['estado'] == 'Cancelado':
                    total_recaudado += pago_val
                else:
                    total_pendiente += pago_val
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
        'fecha': d.get('fecha'),
        'nombre': d.get('nombre'),
        'apellido': d.get('apellido'),
        'cedula': d.get('cedula'),
        'num_contrato': d.get('num_contrato'),
        'direccion': d.get('direccion'),
        'fecha_inicio': d.get('fecha_inicio'),
        'fecha_fin': d.get('fecha_fin'),
        'estado': d.get('estado', 'Pendiente'),
        'internet': safe_float(d.get('internet')),
        'agua': safe_float(d.get('agua')),
        'luz': safe_float(d.get('luz')),
        'pago': safe_float(d.get('pago'))
    }
    
    try:
        if emp_id:
            db.collection('Empleados').document(emp_id).update(datos)
            flash("Registro actualizado correctamente", "success")
        else:
            db.collection('Empleados').add(datos)
            flash("Registro creado con éxito", "success")
    except Exception as e:
        flash(f"Error al procesar: {e}", "danger")
        
    return redirect(url_for('dashboard'))

# --- ELIMINACIÓN ---
@app.route('/delete/<id>')
def delete(id):
    if 'user' not in session: return redirect(url_for('login_page'))
    db.collection('Empleados').document(id).delete()
    flash("Registro eliminado", "warning")
    return redirect(url_for('dashboard'))

@app.route('/delete_multiple', methods=['POST'])
def delete_multiple():
    if 'user' not in session: return jsonify({'status': 'error'}), 401
    ids = request.json.get('ids', [])
    batch = db.batch()
    for doc_id in ids:
        doc_ref = db.collection('Empleados').document(doc_id)
        batch.delete(doc_ref)
    batch.commit()
    return jsonify({'status': 'success'})

# --- EXCEL ---
@app.route('/upload_masivo', methods=['POST'])
def upload_masivo():
    file = request.files.get('archivo')
    if file:
        df = pd.read_excel(file).replace({np.nan: None})
        batch = db.batch()
        for _, row in df.iterrows():
            new_doc = db.collection('Empleados').document()
            batch.set(new_doc, {
                'fecha': str(row.get('Fecha', datetime.now().strftime('%Y-%m-%d'))),
                'nombre': str(row.get('Nombre', '')),
                'apellido': str(row.get('Apellido', '')),
                'pago': safe_float(row.get('Pago Total')),
                'estado': 'Pendiente'
            })
        batch.commit()
        flash("Importación exitosa", "success")
    return redirect(url_for('dashboard'))

@app.route('/exportar_excel')
def exportar_excel():
    docs = db.collection('Empleados').stream()
    data = [doc.to_dict() for doc in docs]
    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'Reporte_{datetime.now().strftime("%Y%m%d")}.xlsx')



@app.route('/reporte', methods=['GET', 'POST'])
def reporte():
    if 'user' not in session: return redirect(url_for('login_page'))
    
    # Obtener todos los datos para filtros
    docs_all = db.collection('Empleados').stream()
    todos = [d.to_dict() for d in docs_all]
    
    # Extraer listas para los selectores del filtro
    anios = sorted(list(set([re.search(r'\d{4}', d.get('fecha', '')).group() for d in todos if re.search(r'\d{4}', d.get('fecha', ''))])), reverse=True)
    direcciones = sorted(list(set([d.get('direccion') for d in todos if d.get('direccion')])))

    # Capturar filtros del formulario
    anio_sel = request.form.get('anio', '')
    dir_sel = request.form.get('direccion', '')
    cont_sel = request.form.get('num_contrato', '')
    est_sel = request.form.get('estado', '')

    empleados_filtrados = []
    totales = {'internet': 0.0, 'agua': 0.0, 'luz': 0.0, 'pago': 0.0}

    for item in todos:
        # Lógica de filtrado
        match_anio = not anio_sel or anio_sel in item.get('fecha', '')
        match_dir = not dir_sel or dir_sel == item.get('direccion')
        match_cont = not cont_sel or cont_sel.lower() in str(item.get('num_contrato', '')).lower()
        match_est = not est_sel or est_sel == item.get('estado')

        if match_anio and match_dir and match_cont and match_est:
            # Limpieza de valores para suma
            val_int = safe_float(item.get('internet'))
            val_agua = safe_float(item.get('agua'))
            val_luz = safe_float(item.get('luz'))
            val_pago = safe_float(item.get('pago'))
            
            item['internet'] = val_int
            item['agua'] = val_agua
            item['luz'] = val_luz
            item['pago'] = val_pago
            
            totales['internet'] += val_int
            totales['agua'] += val_agua
            totales['luz'] += val_luz
            totales['pago'] += val_pago
            
            empleados_filtrados.append(item)

    return render_template('reporte.html', 
                           empleados=empleados_filtrados, 
                           totales=totales, 
                           anios=anios, 
                           direcciones=direcciones,
                           anio_sel=anio_sel, 
                           seleccionada=dir_sel, 
                           contrato_sel=cont_sel, 
                           estado_sel=est_sel)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
