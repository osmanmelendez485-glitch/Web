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
    
    # --- PROCESAMIENTO DE DINERO ---
    deposito_valor = safe_float(d.get('deposito'))
    # Si no especificas meses, por defecto son 12
    limite_meses = int(d.get('meses_contrato', 12)) 
    
    mensualidad_base = (safe_float(d.get('internet')) + safe_float(d.get('agua')) + 
                        safe_float(d.get('luz')) + safe_float(d.get('canon')) + 
                        safe_float(d.get('equipo')))

    datos = {
        'nombre': d.get('nombre'),
        'apellido': d.get('apellido'),
        'cedula': d.get('cedula'),
        'num_contrato': d.get('num_contrato'),
        'direccion': d.get('direccion'),
        'estado': d.get('estado', 'Pendiente'),
        'fecha_inicio': d.get('fecha_inicio', datetime.now().strftime('%Y-%m-%d')),
        'fecha_fin': d.get('fecha_fin', datetime.now().strftime('%Y-%m-%d')),
        'internet': safe_float(d.get('internet')),
        'agua': safe_float(d.get('agua')),
        'luz': safe_float(d.get('luz')),
        'canon': safe_float(d.get('canon')),
        'equipo': safe_float(d.get('equipo')),
        'deposito': deposito_valor,
        'total_pagar': mensualidad_base, # Guardamos el total mensual base en el contrato
        'fecha': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    if emp_id:
        db.collection('Empleados').document(emp_id).update(datos)
        flash("Datos actualizados", "success")
    else:
        # --- CREACIÓN DE NUEVO CONTRATO Y PAGOS ---
        nuevo_doc = db.collection('Empleados').add(datos)
        new_id = nuevo_doc[1].id 

        try:
            fecha_venc = datetime.strptime(datos['fecha_inicio'], '%Y-%m-%d')
        except:
            fecha_venc = datetime.now()

        for i in range(limite_meses):
            monto_final = mensualidad_base
            nota_info = ""

            # 1. SUMAR DEPÓSITO AL PRIMER MES
            if i == 0:
                monto_final = mensualidad_base #+ deposito_valor
                nota_info = "Cobro de Depósito Inicial"
            
            # 2. RETORNAR DEPÓSITO AL ÚLTIMO MES (DINÁMICO)
            elif i == limite_meses - 1:
                monto_final = mensualidad_base #- deposito_valor
                nota_info = "Retorno de Depósito 206"

            pago_doc = {
                'mes_anio': fecha_venc.strftime('%B %Y'),
                'fecha_vencimiento': fecha_venc.strftime('%Y-%m-%d'),
                'monto': monto_final,
                'estado': 'Pendiente',
                'nota': nota_info
            }
            
            db.collection('Empleados').document(new_id).collection('Pagos').add(pago_doc)
            fecha_venc += relativedelta(months=1)
        
        flash(f"Contrato creado con {limite_meses} meses de pagos.", "success")

    num_contrato = request.form.get('num_contrato')
    fecha_registro = request.form.get('fecha') # Formato YYYY-MM-DD
    
    # Si el número de contrato viene vacío, lo generamos
    if not num_contrato or num_contrato.strip() == "":
        fecha_dt = datetime.strptime(fecha_registro, '%Y-%m-%d')
        mes = fecha_dt.strftime('%m')
        anio = fecha_dt.strftime('%Y')
        
        # 1. Consultar cuántos contratos hay en Firebase para este año
        # Esto depende de cómo tengas estructurada tu DB. 
        # Ejemplo si usas Firestore:
        todos = db.collection('empleados').get()
        consecutivo = len(todos) + 1 
        
        # Formato: MMYYYY-001
        num_contrato = f"{mes}{anio}-{consecutivo:03d}"
    
    # Guardar en la base de datos con num_contrato generado..

    return redirect(url_for('dashboard'))
###procesar el retorno de depósito de forma individual

@app.route('/gestionar_deposito/<e_id>/<p_id>/<accion>')
def gestionar_deposito(e_id, p_id, accion):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    emp_ref = db.collection('Empleados').document(e_id)
    pago_ref = emp_ref.collection('Pagos').document(p_id)
    
    contrato = emp_ref.get().to_dict()
    pago = pago_ref.get().to_dict()
    
    deposito = safe_float(contrato.get('deposito', 0))
    # Calculamos el monto base quitando cualquier movimiento previo
    mov_previo = safe_float(pago.get('deposito_movimiento', 0))
    monto_base = safe_float(pago.get('monto')) - mov_previo

    if accion == 'toma':
        nuevo_mov = deposito
        nota = "Depósito Tomado"
    elif accion == 'retorna':
        nuevo_mov = -deposito
        nota = "Retorno de Depósito 2026"
    else: # ninguno
        nuevo_mov = 0
        nota = ""

    pago_ref.update({
        'monto': monto_base + nuevo_mov,
        'deposito_movimiento': nuevo_mov,
        'nota': nota
    })
    return redirect(url_for('ver_pagos', id=e_id))

@app.route('/deshacer_deposito/<e_id>/<p_id>')
def deshacer_deposito(e_id, p_id):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    pago_ref = db.collection('Empleados').document(e_id).collection('Pagos').document(p_id)
    pago = pago_ref.get().to_dict()
    
    # Obtenemos el movimiento que hubo (positivo o negativo)
    movimiento = safe_float(pago.get('deposito_movimiento', 0))
    monto_actual = safe_float(pago.get('monto'))

    # Revertimos el monto al estado original
    pago_ref.update({
        'monto': monto_actual - movimiento,
        'deposito_movimiento': 0, # Limpiamos el rastro
        'nota': ""
    })
    
    flash("Movimiento de depósito revertido", "secondary")
    return redirect(url_for('ver_pagos', id=e_id))


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


@app.route('/resumen_propiedades', methods=['GET', 'POST'])
def resumen_propiedades():
    if 'user' not in session: return redirect(url_for('login_page'))
    
    # Obtener fechas del filtro (por defecto el mes actual)
    fecha_desde = request.form.get('desde', datetime.now().strftime('%Y-%m-01'))
    fecha_hasta = request.form.get('hasta', datetime.now().strftime('%Y-%m-%d'))
    
    docs_empleados = db.collection('Empleados').stream()
    resumen = []

    for emp in docs_empleados:
        e_data = emp.to_dict()
        e_id = emp.id
        
        # Consultar subcolección de pagos filtrada por fecha
        # Nota: La comparación de strings funciona bien con formato YYYY-MM-DD
        pagos_query = db.collection('Empleados').document(e_id).collection('Pagos')\
            .where('fecha_vencimiento', '>=', fecha_desde)\
            .where('fecha_vencimiento', '<=', fecha_hasta).stream()
            
        acumulado_propiedad = 0.0
        recaudado_propiedad = 0.0
        pendiente_propiedad = 0.0
        
        for p in pagos_query:
            p_data = p.to_dict()
            monto = safe_float(p_data.get('monto', 0))
            estado = p_data.get('estado', 'Pendiente')
            
            if estado == 'Cancelado':
                recaudado_propiedad += monto
            elif estado == 'Pendiente':
                pendiente_propiedad += monto
            
            acumulado_propiedad += monto

        if acumulado_propiedad > 0: # Solo mostrar si hay pagos en ese rango
            resumen.append({
                'direccion': e_data.get('direccion', 'Sin Dirección'),
                'inquilino': f"{e_data.get('nombre')} {e_data.get('apellido')}",
                'total': acumulado_propiedad,
                'recaudado': recaudado_propiedad,
                'pendiente': pendiente_propiedad
            })

    return render_template('resumen_acumulado.html', 
                           resumen=resumen, 
                           desde=fecha_desde, 
                           hasta=fecha_hasta)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True)