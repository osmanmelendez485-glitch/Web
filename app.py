import os
import re
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
cred = credentials.Certificate("serviceAccountKey.json")
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()

# --- AYUDANTES ---
def clean_val(val, default=""):
    if val is None or (isinstance(val, float) and np.isnan(val)) or str(val).lower() == 'nan':
        return default
    return val

def safe_float(val):
    try: 
        if not val or str(val).lower() == 'nan': return 0.0
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
            item['internet'] = safe_float(item.get('internet'))
            item['agua'] = safe_float(item.get('agua'))
            item['luz'] = safe_float(item.get('luz'))

            if item['estado'] == 'Cancelado':
                total_recaudado += pago_val
            else:
                total_pendiente += pago_val

            nombre_completo = f"{item.get('nombre','')} {item.get('apellido','')}".lower()
            if not search_query or (search_query in nombre_completo or 
                                    search_query in str(item.get('cedula','')).lower() or
                                    search_query in str(item.get('num_contrato','')).lower()):
                empleados.append(item)
                
        return render_template('index.html', empleados=empleados, search_query=search_query, 
                               sort_by=sort_by, direction=direction,
                               total_recaudado=total_recaudado, total_pendiente=total_pendiente)
    except Exception as e:
        return f"Error en Dashboard: {e}"

# --- REPORTE (CON TODOS LOS FILTROS Y AÑO CORREGIDO) ---
@app.route('/reporte', methods=['GET', 'POST'])
def reporte():
    if 'user' not in session: return redirect(url_for('login_page'))
    try:
        docs_all = db.collection('Empleados').stream()
        direcciones = set()
        anios_disponibles = set()
        todos_datos = []
        
        for doc in docs_all:
            item = doc.to_dict()
            item['id'] = doc.id
            todos_datos.append(item)
            
            # 1. Obtener direcciones
            dir_val = clean_val(item.get('direccion'))
            if dir_val: direcciones.add(dir_val)
            
            # 2. EXTRAER AÑO DE FORMA LIMPIA (SOLO 4 DÍGITOS)
            fecha_str = str(item.get('fecha', ''))
            anio_match = re.search(r'(\d{4})', fecha_str)
            if anio_match:
                anios_disponibles.add(anio_match.group(1))

        # Capturar filtros del formulario
        anio_sel = request.form.get('anio', '')
        dir_sel = request.form.get('direccion', '')
        cont_sel = request.form.get('num_contrato', '')
        est_sel = request.form.get('estado', '')
        
        filtrados = []
        totales = {'internet': 0.0, 'pago': 0.0, 'agua': 0.0, 'luz': 0.0}

        for emp in todos_datos:
            fecha_emp = str(emp.get('fecha', ''))
            
            # Lógica de filtrado corregida para el año
            match_anio = not anio_sel or anio_sel in fecha_emp
            match_dir = not dir_sel or str(emp.get('direccion')) == dir_sel
            match_cont = not cont_sel or cont_sel.lower() in str(emp.get('num_contrato', '')).lower()
            match_est = not est_sel or emp.get('estado') == est_sel
            
            if match_anio and match_dir and match_cont and match_est:
                for k in totales:
                    val = safe_float(emp.get(k))
                    emp[k] = val
                    totales[k] += val
                filtrados.append(emp)

        anios_lista = sorted(list(anios_disponibles), reverse=True)
        if not anios_lista: anios_lista = [str(datetime.now().year)]

        return render_template('reporte.html', 
                               empleados=filtrados, 
                               direcciones=sorted(list(direcciones)), 
                               totales=totales, 
                               seleccionada=dir_sel, 
                               contrato_sel=cont_sel, 
                               estado_sel=est_sel, 
                               anios=anios_lista, 
                               anio_sel=anio_sel)
    except Exception as e:
        return f"Error en reporte: {str(e)}"

# --- CRUD ---
@app.route('/add', methods=['POST'])
def add():
    if 'user' not in session: return redirect(url_for('login_page'))
    d = request.form
    id_registro = d.get('id')
    file = request.files.get('comprobante')
    filename = d.get('archivo_actual', '')
    
    if file and file.filename != '':
        filename = secure_filename(f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}")
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    
    datos = {
        'fecha': d.get('fecha') or datetime.now().strftime('%Y-%m-%d'),
        'nombre': clean_val(d.get('nombre')),
        'apellido': clean_val(d.get('apellido')),
        'cedula': clean_val(d.get('cedula')),
        'num_contrato': clean_val(d.get('num_contrato')),
        'direccion': d.get('direccion', ''),
        'fecha_inicio': d.get('fecha_inicio', ''),
        'fecha_fin': d.get('fecha_fin', ''),
        'estado': d.get('estado', 'Pendiente'),
        'internet': safe_float(d.get('internet')),
        'pago': safe_float(d.get('pago')),
        'agua': safe_float(d.get('agua')),
        'luz': safe_float(d.get('luz')),
        'archivo_url': filename
    }

    if id_registro:
        db.collection('Empleados').document(id_registro).update(datos)
        flash("Registro actualizado correctamente", "success")
    else:
        db.collection('Empleados').add(datos)
        flash("Nuevo registro creado", "success")
    return redirect(url_for('dashboard'))

@app.route('/delete/<id>')
def delete(id):
    if 'user' not in session: return redirect(url_for('login_page'))
    db.collection('Empleados').document(id).delete()
    flash("Registro eliminado", "warning")
    return redirect(url_for('dashboard'))

@app.route('/delete_multiple', methods=['POST'])
def delete_multiple():
    if 'user' not in session: return jsonify({"status": "error"}), 403
    data = request.get_json()
    ids = data.get('ids', [])
    batch = db.batch()
    for doc_id in ids:
        doc_ref = db.collection('Empleados').document(doc_id)
        batch.delete(doc_ref)
    batch.commit()
    flash(f"{len(ids)} registros eliminados", "warning")
    return jsonify({"status": "success"})

# --- CARGA MASIVA Y EXPORTACIÓN ---
@app.route('/upload_masivo', methods=['POST'])
def upload_masivo():
    file = request.files.get('archivo')
    if file:
        try:
            df = pd.read_excel(file).replace({np.nan: None})
            batch = db.batch()
            for _, row in df.iterrows():
                new_doc = db.collection('Empleados').document()
                batch.set(new_doc, {
                    'fecha': str(row.get('Fecha') or datetime.now().strftime('%Y-%m-%d')),
                    'nombre': clean_val(row.get('Nombre')),
                    'apellido': clean_val(row.get('Apellido')),
                    'cedula': clean_val(row.get('Cédula')),
                    'num_contrato': clean_val(row.get('Contrato')),
                    'direccion': clean_val(row.get('Dirección')),
                    'fecha_inicio': clean_val(row.get('Inicio')),
                    'fecha_fin': clean_val(row.get('Fin')),
                    'estado': clean_val(row.get('Estado'), 'Pendiente'),
                    'internet': safe_float(row.get('Internet')),
                    'pago': safe_float(row.get('Pago')),
                    'agua': safe_float(row.get('Agua')),
                    'luz': safe_float(row.get('Luz'))
                })
            batch.commit()
            flash("Carga masiva exitosa", "success")
        except Exception as e:
            flash(f"Error en Excel: {e}", "danger")
    return redirect(url_for('dashboard'))

@app.route('/exportar_excel')
def exportar_excel():
    if 'user' not in session: return redirect(url_for('login_page'))
    
    anio_sel = request.args.get('anio', '')
    docs = db.collection('Empleados').stream()
    data = []
    for doc in docs:
        item = doc.to_dict()
        fecha_emp = str(item.get('fecha', ''))
        
        if anio_sel and anio_sel not in fecha_emp:
            continue

        data.append({
            'Fecha': clean_val(item.get('fecha')),
            'Nombre': clean_val(item.get('nombre')),
            'Apellido': clean_val(item.get('apellido')),
            'Cédula': clean_val(item.get('cedula')),
            'Contrato': clean_val(item.get('num_contrato')),
            'Dirección': clean_val(item.get('direccion')),
            'Inicio': clean_val(item.get('fecha_inicio')),
            'Fin': clean_val(item.get('fecha_fin')),
            'Estado': clean_val(item.get('estado'), 'Pendiente'),
            'Internet': safe_float(item.get('internet')),
            'Pago': safe_float(item.get('pago')),
            'Agua': safe_float(item.get('agua')),
            'Luz': safe_float(item.get('luz'))
        })
    
    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Empleados')
    output.seek(0)
    
    return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name=f'Reporte_Contratos_{anio_sel or "Gral"}.xlsx')

if __name__ == '__main__':
    app.run(debug=True, port=5000)