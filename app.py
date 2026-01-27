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
            
            # Formateo para búsqueda
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


import os
from werkzeug.utils import secure_filename

@app.route('/save', methods=['POST'])
def save():
    if 'user' not in session: return redirect(url_for('login_page'))
    
    d = request.form
    emp_id = d.get('id') # ID proveniente del modal de edición
    
    # conversion de valores numericos
    internet = safe_float(d.get('internet'))
    agua = safe_float(d.get('agua'))
    luz = safe_float(d.get('luz'))
    canon = safe_float(d.get('canon'))
    equipo = safe_float(d.get('equipo'))
    deposito = safe_float(d.get('deposito'))

    total_mensual = internet + agua + luz + canon + equipo

    # 1. Preparar Diccionario de Datos base
    datos = {
        'fecha': d.get('fecha'),
        'nombre': d.get('nombre'),
        'apellido': d.get('apellido'),
        'cedula': d.get('cedula'),
        'num_contrato': d.get('num_contrato'),
        'direccion': d.get('direccion'),
        'internet': internet,
        'agua': agua,
        'luz': luz,
        'canon': canon,
        'equipo': equipo,
        'deposito': deposito,
        'total_pagar': total_mensual, # Monto base recurrente
        'fecha_inicio': d.get('fecha_inicio'),
        'fecha_fin': d.get('fecha_fin'),
        'estado': d.get('estado', 'Pendiente')
    }

    # 2. Manejo de Imagen
    if 'imagen_archivo' in request.files:
        file = request.files['imagen_archivo']
        if file.filename != '':
            filename = secure_filename(file.filename)
            upload_path = os.path.join('static', 'uploads')
            if not os.path.exists(upload_path): os.makedirs(upload_path)
            file.save(os.path.join(upload_path, filename))
            datos['foto_url'] = f'/static/uploads/{filename}'

    # 3. LÓGICA DE GUARDADO (Corregida para evitar duplicados)
    if emp_id:
        # --- CASO EDICIÓN ---
        db.collection('Empleados').document(emp_id).update(datos)
        flash("Contrato actualizado correctamente", "success")
    else:
        # --- CASO NUEVO ---
        # Guardamos el contrato principal
        new_doc_ref = db.collection('Empleados').add(datos)
        nuevo_id = new_doc_ref[1].id
        
        # Generar cronograma de pagos
        try:
            f_inicio = datetime.strptime(d.get('fecha_inicio'), '%Y-%m-%d')
            f_fin = datetime.strptime(d.get('fecha_fin'), '%Y-%m-%d')
            
            temp_fecha = f_inicio
            es_primer_pago = True
            
            while temp_fecha <= f_fin:
                # El depósito solo se suma al primer mes
                monto_final = total_mensual + (deposito if es_primer_pago else 0)
                
                pago_mes = {
                    'mes_anio': temp_fecha.strftime('%B %Y'),
                    'fecha_vencimiento': temp_fecha.strftime('%Y-%m-%d'),
                    'monto': monto_final,
                    'estado': 'Pendiente',
                    'nota': 'Incluye Depósito' if es_primer_pago and deposito > 0 else ''
                }
                
                db.collection('Empleados').document(nuevo_id).collection('Pagos').add(pago_mes)
                
                temp_fecha += relativedelta(months=1)
                es_primer_pago = False
                
            flash("Contrato y pagos generados con éxito", "success")
        except Exception as e:
            flash(f"Contrato creado, pero hubo un error en las fechas: {e}", "warning")

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
    totales = {'internet': 0.0, 'agua': 0.0, 'luz': 0.0, 'canon': 0.0}

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
            val_canon = safe_float(item.get('canon'))
            val_equipo = safe_float(item.get('equipo'))
            
            item['internet'] = val_int
            item['agua'] = val_agua
            item['luz'] = val_luz
            item['canon'] = val_canon
            item['equipo'] = val_equipo
            
            totales['internet'] += val_int
            totales['agua'] += val_agua
            totales['luz'] += val_luz
            totales['canon'] += val_canon
            totales['equipo'] += val_equipo
            
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


# --- NUEVA RUTA: VER HOJA DE PAGOS ---
@app.route('/ver_pagos/<id>')
def ver_pagos(id):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    # 1. Obtener datos del contrato (Empleado)
    doc_ref = db.collection('Empleados').document(id)
    contrato = doc_ref.get().to_dict()
    contrato['id'] = id

    # 2. Obtener la sub-colección de pagos
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
        else:
            pendiente += monto
        pagos.append(p_data)

    return render_template('pagos.html', 
                           pagos=pagos, 
                           contrato=contrato, 
                           total_recaudado=recaudado, 
                           total_pendiente=pendiente)

# --- NUEVA RUTA: CAMBIAR ESTADO DE PAGO ---
@app.route('/toggle_pago/<contrato_id>/<pago_id>', methods=['POST'])
def toggle_pago(contrato_id, pago_id):
    if 'user' not in session: return jsonify({'status': 'error'}), 401
    
    pago_ref = db.collection('Empleados').document(contrato_id).collection('Pagos').document(pago_id)
    pago_doc = pago_ref.get().to_dict()
    
    nuevo_estado = 'Cancelado' if pago_doc.get('estado') == 'Pendiente' else 'Pendiente'
    pago_ref.update({'estado': nuevo_estado})
    
    return redirect(url_for('ver_pagos', id=contrato_id))

@app.route('/propiedades') # O puedes ponerlo en @app.route('/') si prefieres que sea lo primero
def propiedades():
    if 'user' not in session: return redirect(url_for('login_page'))
    
    docs = db.collection('Empleados').stream()
    propiedades_dict = {}

    for doc in docs:
        item = doc.to_dict()
        item['id'] = doc.id
        # Usamos la dirección como identificador único de la propiedad
        dir_name = item.get('direccion', 'Sin Dirección')
        
        # Lógica: Si hay varios contratos para una misma dirección, 
        # nos quedamos con el más reciente (Contrato Actual)
        if dir_name not in propiedades_dict:
            propiedades_dict[dir_name] = item
        else:
            # Si el contrato actual es más reciente que el guardado, lo reemplaza
            if item.get('fecha') > propiedades_dict[dir_name].get('fecha'):
                propiedades_dict[dir_name] = item

    return render_template('propiedades.html', propiedades=propiedades_dict)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)
