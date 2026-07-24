import json
import os
import re
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore, storage
from flask import Flask, jsonify, flash, redirect, render_template, request, send_file, session, url_for
from google.cloud.firestore_v1.base_query import FieldFilter
import numpy as np
import pandas as pd
import pandas_ta_classic as ta
from twilio.rest import Client
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
import yfinance as yf

# Carga las variables desde el archivo .env si existe
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'tu_llave_secreta_aqui')

# 🔒 Configuración para HTTPS detrás del Proxy de Render
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Asegurar las cookies de sesión bajo HTTPS en producción
if not app.debug:
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['REMOTE_ADDR_HEADER'] = 'HTTP_X_FORWARDED_FOR'
    app.config['SESSION_COOKIE_SECURE'] = False

# --- CONFIGURACIÓN DE CARPETAS ---
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
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

@app.route('/auth', methods=['GET', 'POST']) # <--- Paso 1: Permitir ambos
def auth():
    if request.method == 'POST': # <--- Paso 2: Solo procesar si es envío de formulario
        user = request.form.get('user')
        pw = request.form.get('password')
        if user == 'admin' and pw == '1234': 
            session['user'] = user
            return redirect(url_for('dashboard'))
        
        flash("Usuario o contraseña incorrectos", "danger")
        return redirect(url_for('login_page'))
    
    # Si alguien intenta entrar a /auth escribiendo la URL o por error:
    return redirect(url_for('login_page')) # <--- Paso 3: Mandarlo de vuelta al login

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
    sort_by = request.args.get('sort', 'fecha_inicio')
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


@app.route('/save', methods=['POST'])
def save():
    if 'user' not in session: 
        return redirect(url_for('login_page'))
    
    d = request.form
    emp_id = d.get('id')

    # --- 0. PROCESAMIENTO DE LA FOTO DE LA PROPIEDAD ---
    # Foto por defecto inicial
    url_imagen = '/static/uploads/propiedad_defecto.jpg'

    # Si es una edición, traemos la foto actual de Firestore para no sobrescribirla
    if emp_id:
        doc_existente = db.collection('Empleados').document(emp_id).get()
        if doc_existente.exists:
            url_imagen = doc_existente.to_dict().get('url_imagen_propiedad', url_imagen)

    # Verificar si el usuario subió un archivo nuevo desde el input 'foto_propiedad'
    if 'foto_propiedad' in request.files:
        file = request.files['foto_propiedad']
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            
            # Garantizamos la carpeta de destino
            upload_folder = os.path.join(app.root_path, 'static', 'uploads')
            os.makedirs(upload_folder, exist_ok=True)
            
            # Nombre de archivo único
            nombre_archivo = f"propiedad_{emp_id or 'nueva'}_{filename}"
            filepath = os.path.join(upload_folder, nombre_archivo)
            
            # Guardar el archivo físicamente
            file.save(filepath)
            
            # Guardar la ruta relativa accesible por Flask
            url_imagen = f"/static/uploads/{nombre_archivo}"

    # --- 1. PROCESAMIENTO SEGURO DE FECHA DE REGISTRO ---
    fecha_reg_raw = d.get('fecha', '').strip()
    
    if not fecha_reg_raw:
        fecha_dt = datetime.now()
    else:
        try:
            fecha_dt = datetime.strptime(fecha_reg_raw, '%Y-%m-%d')
        except ValueError:
            fecha_dt = datetime.now()

    # --- 2. GENERACIÓN DE NÚMERO DE CONTRATO ---
    num_contrato = d.get('num_contrato', '').strip()
    if not num_contrato:
        mes_c = fecha_dt.strftime('%m')
        anio_c = fecha_dt.strftime('%Y')
        try:
            todos = db.collection('Empleados').get()
            consecutivo = len(todos) + 1
        except:
            consecutivo = 1
        num_contrato = f"{mes_c}{anio_c}-{consecutivo:03d}"

    # --- 3. PROCESAMIENTO DE DINERO ---
    deposito_valor = safe_float(d.get('deposito'))
    limite_meses = int(d.get('meses_contrato', 12)) 
    mensualidad_base = (safe_float(d.get('internet')) + safe_float(d.get('agua')) + 
                        safe_float(d.get('luz')) + safe_float(d.get('canon')) + 
                        safe_float(d.get('equipo')))

    # --- 4. PREPARACIÓN DE DATOS ---
    f_inicio_raw = d.get('fecha_inicio', '').strip()
    f_fin_raw = d.get('fecha_fin', '').strip()

    f_inicio_db = f_inicio_raw if f_inicio_raw else datetime.now().strftime('%Y-%m-%d')
    f_fin_db = f_fin_raw if f_fin_raw else datetime.now().strftime('%Y-%m-%d')
    enviar_copia = request.form.get('enviar_copia_inquilino') == 'true'
    enviar_encuesta = request.form.get('enviar_encuesta_inquilino') == 'true' or request.form.get('enviar_encuesta_inquilino') == 'on'

    datos = {
        'nombre': d.get('nombre'),
        'apellido': d.get('apellido'),
        'cedula': d.get('cedula'),
        'telefono': d.get('telefono'),
        'num_contrato': num_contrato,
        'direccion': d.get('direccion'),
        'estado': d.get('estado', 'Pendiente'),
        'fecha_inicio': f_inicio_db,
        'fecha_fin': f_fin_db,
        'internet': safe_float(d.get('internet')),
        'agua': safe_float(d.get('agua')),
        'luz': safe_float(d.get('luz')),
        'canon': safe_float(d.get('canon')),
        'equipo': safe_float(d.get('equipo')),
        'deposito': safe_float(d.get('deposito')),
        'total_pagar': mensualidad_base,
        'fecha': fecha_dt.strftime('%Y-%m-%d'),
        'meses_contrato': limite_meses,
        'enviar_copia_inquilino': enviar_copia,
        'enviar_encuesta_inquilino': enviar_encuesta,
        'url_imagen_propiedad': url_imagen,  # 👈 Guardamos la URL procesada de la foto
    }

    # --- 5. GUARDADO Y PAGOS ---
    if emp_id:
        deposito_nuevo = safe_float(d.get('deposito', 0.0))
        datos['deposito'] = deposito_nuevo
        
        # Actualizamos el documento principal en Firestore
        db.collection('Empleados').document(emp_id).update(datos)
        
        try:
            pagos_viejos = db.collection('Empleados').document(emp_id).collection('Pagos')\
                             .order_by('fecha_vencimiento').get()
            
            try:
                fecha_secuencial = datetime.strptime(f_inicio_db, '%Y-%m-%d')
            except:
                fecha_secuencial = datetime.now()

            for indice, p in enumerate(pagos_viejos):
                p_data = p.to_dict()
                pago_update = {}
                
                if p_data.get('estado', 'Pendiente') == 'Pendiente':
                    pago_update.update({
                        'monto': mensualidad_base,
                        'fecha_vencimiento': fecha_secuencial.strftime('%Y-%m-%d'),
                        'mes_anio': fecha_secuencial.strftime('%B %Y')
                    })
                    
                    mov_actual = safe_float(p_data.get('deposito_movimiento', 0.0))
                    if mov_actual != 0.0:
                        signo = -1.0 if mov_actual < 0 else 1.0
                        pago_update['deposito_movimiento'] = deposito_nuevo * signo
                
                if indice == 0:
                    pago_update['nota'] = f"Depósito Inicial: C$ {deposito_nuevo:,.2f}"
                
                if pago_update:
                    db.collection('Empleados').document(emp_id).collection('Pagos').document(p.id).update(pago_update)
                
                fecha_secuencial += relativedelta(months=1)
                
        except Exception as err_pagos:
            print(f"⚠️ Error actualizando cuotas: {err_pagos}")

        flash("Registro, foto y movimientos de depósito actualizados con éxito", "success")
    else:
        nuevo_doc = db.collection('Empleados').add(datos)
        new_id = nuevo_doc[1].id 

        try:
            fecha_venc = datetime.strptime(f_inicio_db, '%Y-%m-%d')
        except:
            fecha_venc = datetime.now()

        for i in range(limite_meses):
            pago_doc = {
                'mes_anio': fecha_venc.strftime('%B %Y'),
                'fecha_vencimiento': fecha_venc.strftime('%Y-%m-%d'),
                'monto': mensualidad_base,
                'estado': 'Pendiente',
                'nota': "Depósito Inicial" if i == 0 else ""
            }
            db.collection('Empleados').document(new_id).collection('Pagos').add(pago_doc)
            fecha_venc += relativedelta(months=1)
        
        flash(f"Contrato {num_contrato} creado con éxito", "success")

    return redirect(url_for('dashboard'))
#Boveda de contratos

@app.route('/boveda_contratos')  # <--- Mira que no tenga espacios al final
def boveda_contratos():
    if 'user' not in session: return redirect(url_for('login_page'))
    
    docs = db.collection('Empleados').order_by('nombre').stream()
    lista_documentos = []
    for d in docs:
        item = d.to_dict()
        if item.get('url_contrato_pdf'):
            item['id'] = d.id
            lista_documentos.append(item)
            
    return render_template('boveda.html', documentos=lista_documentos)

#-----------
####Gestio de contratos

@app.route('/detalle_contrato/<id>')  # Recibe el ID
def ver_contrato(id):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    # Buscamos directamente el documento por su ID único
    doc_ref = db.collection('Empleados').document(id).get()
    
    if doc_ref.exists:
        contrato = doc_ref.to_dict()
        # Pasamos los datos del contrato y el ID del documento
        return render_template('detalle_contrato.html', c=contrato, id=doc_ref.id)
    else:
        flash("Contrato no encontrado", "danger")
        return redirect('/')

#Editar adjunto contrato

@app.route('/vincular_drive_pdf', methods=['POST'])
def vincular_drive_pdf():
    if 'user' not in session: return redirect(url_for('login_page'))
    
    emp_id = request.form.get('id')
    link_drive = request.form.get('link_pdf')

    if emp_id and link_drive:
        try:
            # .update() SOLO modifica el campo url_contrato_pdf
            # Mantiene intactos: nombre, cedula, canon, agua, luz, etc.
            db.collection('Empleados').document(emp_id).update({
                'url_contrato_pdf': link_drive
            })
            flash("Enlace de Google Drive vinculado con éxito", "success")
        except Exception as e:
            flash(f"Error al vincular: {e}", "danger")
    
    return redirect(url_for('ver_contrato', id=emp_id))


@app.route('/gestionar_deposito/<e_id>/<p_id>/<accion>')
def gestionar_deposito(e_id, p_id, accion):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    emp_ref = db.collection('Empleados').document(e_id)
    pago_ref = emp_ref.collection('Pagos').document(p_id)
    
    contrato = emp_ref.get().to_dict()
    pago = pago_ref.get().to_dict()
    
    # 1. Recuperar el depósito real establecido en el contrato
    deposito = safe_float(contrato.get('deposito', 0))
    
    # 2. Reconstruir la mensualidad base real recalculada (Canon + Servicios contratados)
    mensualidad_base = (safe_float(contrato.get('internet')) + safe_float(contrato.get('agua')) + 
                        safe_float(contrato.get('luz')) + safe_float(contrato.get('canon')) + 
                        safe_float(contrato.get('equipo')))

    # 3. Aplicar la lógica según la acción seleccionada sin arrastrar basura previa
    if accion == 'toma':
        nuevo_mov = deposito
        nuevo_monto_total = mensualidad_base + deposito  # Al inicio se SUMA el depósito
        nota = f"Cobro de Depósito Inicial: C$ {deposito:,.2f}"
    elif accion == 'retorna':
        nuevo_mov = -deposito
        # Si el depósito cubre el último mes, el monto a cobrar en caja baja (se le resta el crédito del depósito)
        nuevo_monto_total = mensualidad_base - deposito  
        nota = f"Retorno de Depósito Aplicado a Cuota: C$ {deposito:,.2f}"
    else: # ninguno
        nuevo_mov = 0
        nuevo_monto_total = mensualidad_base
        nota = ""

    # 4. Calcular el saldo pendiente real restando lo que ya se haya abonado físicamente
    monto_pagado_ya = safe_float(pago.get('monto_pagado', 0.0))
    saldo_pendiente = nuevo_monto_total - monto_pagado_ya

    # Determinamos el estado lógico de la fila tras el movimiento
    if saldo_pendiente <= 0:
        nuevo_estado = "Cancelado" if monto_pagado_ya > 0 or accion == 'retorna' else "Pendiente"
    else:
        nuevo_estado = "Parcial" if monto_pagado_ya > 0 else "Pendiente"

    # 5. Guardar la actualización limpia en Firestore
    pago_ref.update({
        'monto': nuevo_monto_total,
        'deposito_movimiento': nuevo_mov,
        'saldo_pendiente': max(0.0, saldo_pendiente),
        'estado': nuevo_estado,
        'nota': nota
    })
    
    flash("Movimiento de depósito sincronizado con éxito", "success")
    return redirect(url_for('ver_pagos', id=e_id))


@app.route('/deshacer_deposito/<e_id>/<p_id>')
def deshacer_deposito(e_id, p_id):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    pago_ref = db.collection('Empleados').document(e_id).collection('Pagos').document(p_id)
    pago = pago_ref.get().to_dict()
    
    # 1. Recuperamos los valores actuales
    movimiento = safe_float(pago.get('deposito_movimiento', 0))
    monto_actual = safe_float(pago.get('monto'))

    # 2. Revertimos el monto total del mes a su estado original (ej: vuelve a C$ 3,000)
    nuevo_monto_total = monto_actual - movimiento

    # 3. CRUCIAL: Al deshacer la toma, el 'monto_pagado' debe volver a 0 
    # porque ya no cuenta con el respaldo del depósito tomado.
    monto_abonado_limpio = 0.0  

    # 4. Recalculamos saldos y estados basados en el reinicio total
    saldo_pendiente = nuevo_monto_total
    saldo_a_favor = 0.0
    nuevo_estado = "Pendiente" # Al no haber abono real, vuelve a estar pendiente

    # 5. Guardamos la corrección absoluta en Firestore
    pago_ref.update({
        'monto': nuevo_monto_total,
        'monto_pagado': monto_abonado_limpio, # <--- Esto limpia el abono fantasma
        'deposito_movimiento': 0,
        'estado': nuevo_estado,
        'saldo_pendiente': saldo_pendiente,
        'saldo_a_favor': saldo_a_favor,
        'nota': "" 
    })
    
    flash("Movimiento de depósito deshecho. La cuota ha vuelto a su canon original.", "secondary")
    return redirect(url_for('ver_pagos', id=e_id))

@app.route('/ver_pagos/<id>')
def ver_pagos(id):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    # 1. Obtener datos del empleado/contrato
    doc_ref = db.collection('Empleados').document(id)
    emp_doc = doc_ref.get()
    
    if not emp_doc.exists: 
        return redirect(url_for('dashboard'))
    
    empleado = emp_doc.to_dict()
    empleado['id'] = id
    
    # Obtener límites del contrato para filtrar visualmente
    f_inicio = empleado.get('fecha_inicio', '1900-01-01')
    f_fin = empleado.get('fecha_fin', '2099-12-31')

    # 2. Consultar pagos ordenados
    pagos_query = doc_ref.collection('Pagos').order_by('fecha_vencimiento').stream()
    
    pagos = []
    recaudado = 0.0
    pendiente = 0.0
    adelantos_totales = 0.0  # <- Inicializada correctamente aquí

    for p in pagos_query:
        p_data = p.to_dict()
        p_data['id'] = p.id
        fecha_v = p_data.get('fecha_vencimiento', '')

        # FILTRO DE SEGURIDAD: Solo procesar pagos dentro del rango del contrato
        if f_inicio <= fecha_v <= f_fin:
            monto_mes = safe_float(p_data.get('monto', 0))
            estado_pago = p_data.get('estado', 'Pendiente')
            
            # --- PROTECCIÓN PARA REGISTROS VIEJOS ---
            # Si no existen los campos nuevos en Firestore, los inicializamos en cero para evitar errores
            if 'monto_pagado' not in p_data:
                p_data['monto_pagado'] = monto_mes if estado_pago == 'Cancelado' else 0.0
            if 'saldo_pendiente' not in p_data:
                p_data['saldo_pendiente'] = monto_mes if estado_pago == 'Pendiente' else 0.0
            if 'saldo_a_favor' not in p_data:
                p_data['saldo_a_favor'] = 0.0

            monto_pagado = safe_float(p_data.get('monto_pagado'))
            s_pend = safe_float(p_data.get('saldo_pendiente'))
            s_favor = safe_float(p_data.get('saldo_a_favor'))

            # --- ACUMULACIÓN DE TOTALES ---
            if estado_pago == 'Cancelado':
                recaudado += monto_pagado
                adelantos_totales += s_favor
            elif estado_pago == 'Parcial':
                recaudado += monto_pagado
                pendiente += s_pend
            elif estado_pago == 'Pendiente':
                pendiente += monto_mes
                
            pagos.append(p_data)

    # 3. Renderizar enviando también los adelantos totales calculados
    return render_template('pagos.html', 
                           pagos=pagos, 
                           empleado=empleado, 
                           id=id, 
                           total_recaudado=recaudado, 
                           total_pendiente=pendiente,
                           total_adelantos=adelantos_totales)  # <- Enviado al HTML

@app.route('/toggle_pago/<e_id>/<p_id>/<nuevo_estado>')
def toggle_pago(e_id, p_id, nuevo_estado):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    pago_ref = db.collection('Empleados').document(e_id).collection('Pagos').document(p_id)
    
    payload = {'estado': nuevo_estado}
    
    # Si se fuerza a Pendiente o Suspensión, reseteamos los montos abonados
    if nuevo_estado in ['Pendiente', 'Suspensión']:
        payload.update({
            'monto_pagado': 0.0,
            'saldo_a_favor': 0.0,
            'saldo_pendiente': 0.0
        })

    pago_ref.update(payload)
    
    flash(f"Estado actualizado a {nuevo_estado}", "success")
    return redirect(url_for('ver_pagos', id=e_id))



@app.route('/suspend/<e_id>/<p_id>')
def suspend_payment(e_id, p_id):
    # Reutiliza toggle_pago internamente
    return toggle_pago(e_id, p_id, 'Suspensión')


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
    
    # 1. Capturar los datos del formulario
    fecha_desde = request.form.get('desde', datetime.now().strftime('%Y-%m-01'))
    fecha_hasta = request.form.get('hasta', datetime.now().strftime('%Y-%m-%d'))
    direccion_sel = request.form.get('direccion', '')

    # 2. Filtrar Empleados usando .where() en lugar de .filter()
    query_empleados = db.collection('Empleados')
    
    if direccion_sel and direccion_sel != "":
        # Filtro clásico compatible
        docs_empleados = query_empleados.where('direccion', '==', direccion_sel).stream()
    else:
        docs_empleados = query_empleados.stream()

    resumen = []

    for emp in docs_empleados:
        e_data = emp.to_dict()
        e_id = emp.id
        
        # 3. Consultar subcolección de pagos usando .where()
        pagos_query = db.collection('Empleados').document(e_id).collection('Pagos')\
            .where(filter=FieldFilter('fecha_vencimiento', '>=', fecha_desde))\
            .where(filter=FieldFilter('fecha_vencimiento', '<=', fecha_hasta)).stream()
            
# ... (Tu código anterior de la ruta se mantiene igual) ...
        acumulado_propiedad = 0.0
        recaudado_propiedad = 0.0
        pendiente_propiedad = 0.0
        
        for p in pagos_query:
            p_data = p.to_dict()
            monto_base = safe_float(p_data.get('monto', 0))
            estado = p_data.get('estado', '').strip().lower()
            
            # 1. Extraemos los valores reales usando las variables del sistema de abonos
            monto_pagado = safe_float(p_data.get('monto_pagado', 0.0))
            saldo_pendiente = safe_float(p_data.get('saldo_pendiente', 0.0))
            
            # 2. Si es un registro viejo que no tiene los nuevos campos de abonos, los calculamos al vuelo
            if 'monto_pagado' not in p_data:
                monto_pagado = monto_base if estado == 'cancelado' else 0.0
            if 'saldo_pendiente' not in p_data:
                saldo_pendiente = monto_base if estado == 'pendiente' else 0.0

            # 3. Distribución matemática exacta según el estado del recibo
            if estado == 'cancelado':
                recaudado_propiedad += monto_pagado
            elif estado == 'pendiente':
                pendiente_propiedad += monto_base
            elif estado == 'parcial':
                recaudado_propiedad += monto_pagado
                # Si es parcial, sumamos su saldo pendiente real restante
                pendiente_propiedad += saldo_pendiente
            
            # El total acumulado del mes es la suma de lo cobrado más lo que falta por cobrar
            acumulado_propiedad += (monto_pagado + saldo_pendiente) if estado == 'parcial' else monto_base

        if acumulado_propiedad > 0: 
            resumen.append({
                'direccion': e_data.get('direccion', 'Sin Dirección'),
                'inquilino': f"{e_data.get('nombre', '')} {e_data.get('apellido', '')}",
                'total': acumulado_propiedad,
                'recaudado': recaudado_propiedad,
                'pendiente': pendiente_propiedad
            })
        # ... (El resto de la ruta se mantiene igual) ...

    return render_template('resumen_acumulado.html', 
                           resumen=resumen, 
                           desde=fecha_desde, 
                           hasta=fecha_hasta,
                           direccion_sel=direccion_sel)


VERSION = "1.2.0" # Cambia esto cada vez que hagas un hito importante

@app.context_processor
def inject_version():
    # Esto permite que {{ app_version }} funcione en TODOS tus HTML
    return dict(app_version=VERSION)

def enviar_whatsapp_consolidado(empleado, detalles_pagos, monto_total):
    # 1. Credenciales de Twilio
    real_sid = 'AC55a32288ebca14e7286265bd207bd593'
    real_token = '9ead4c07b599ae86f5118122bbc004f9'
    
    # 2. Limpieza de variables del sistema en Render para evitar Error 401
    if 'TWILIO_ACCOUNT_SID' in os.environ:
        del os.environ['TWILIO_ACCOUNT_SID']
    if 'TWILIO_AUTH_TOKEN' in os.environ:
        del os.environ['TWILIO_AUTH_TOKEN']
        
    os.environ['TWILIO_ACCOUNT_SID'] = ""
    os.environ['TWILIO_AUTH_TOKEN'] = ""
    
    # 3. Inicialización del cliente
    client = Client(real_sid, real_token)
    
    # Concatena todos los meses vencidos o parciales
    bloque_detalles = "\n".join(detalles_pagos)

    mensaje = (
        f"📋 *ESTADO DE CUENTA CONSOLIDADO* 📋\n\n"
        f"Hola *{empleado.get('nombre', '')} {empleado.get('apellido', '')}*,\n"
        f"Te saluda Osman Meléndez para recordarte los saldos pendientes asociados a tu contrato *{empleado.get('num_contrato', 'N/A')}*:\n\n"
        f"{bloque_detalles}\n\n"
        f"💰 *TOTAL NETO PENDIENTE*: *C$ {monto_total:,.2f}*\n\n"
        f"Por favor omitir este mensaje si ya has realizado tu depósito o transferencia correspondiente. ¡Muchas gracias!"
    )

    nom_cli = f"{empleado.get('nombre', '')} {empleado.get('apellido', '')}"

    try:
        # Intento de envío del mensaje de cobro
        # Reemplazar 'whatsapp:+50589475863' por f"whatsapp:{empleado.get('telefono')}" en producción
        message = client.messages.create(
            from_='whatsapp:+14155238886',  # Sandbox oficial de Twilio
            body=mensaje,
            to='whatsapp:+50589475863'
        )

        print(f"✅ WhatsApp Consolidado enviado a {nom_cli} | SID: {message.sid}")
        flash(f"Notificación de cobro enviada con éxito a {nom_cli}.", "success")
        return True

    except Exception as error_twilio:
        print(f"❌ Error en Twilio API para {nom_cli}: {error_twilio}")
        flash(f"Se procesó el cobro de {nom_cli}, pero falló el envío de WhatsApp: {error_twilio}", "warning")
        return False

# --- RUTA AUTOMÁTICA GATILLADA POR CRON-JOB (ENVÍO ÚNICO DIARIO DIARIO COMPLETO) ---

# --- RUTA AUTOMÁTICA GATILLADA POR CRON-JOB (ENVÍO ÚNICO DIARIO COMPLETO) ---
@app.route('/ejecutar_envio_automatico_secreto_123')
def ejecutar_envio_automatico():
    diagnostico = []
    mensajes_enviados = 0
    errores_totales = 0  # 👈 Control de errores reales
    
    try:
        zona_ni = timezone(timedelta(hours=-6))
        fecha_actual = datetime.now(zona_ni)
        hoy_str = fecha_actual.strftime('%Y-%m-%d')
        
        config_ref = db.collection('Configuracion_Cron').document('control_envios')
        config_doc = config_ref.get()
        
        if config_doc.exists:
            ultima_fecha_envio = config_doc.to_dict().get('ultima_fecha_exitosa', '')
            if ultima_fecha_envio == hoy_str:
                return jsonify({
                    "status": "skipped",
                    "mensaje": f"Los recordatorios del día de hoy ({hoy_str}) ya fueron enviados en un ciclo previo.",
                    "fecha_servidor_managua": hoy_str
                }), 200

        inquilinos = db.collection('Empleados').stream()
        total_inquilinos = 0
        total_pagos_revisados = 0
        hubo_envios_hoy = False
        
        for doc in inquilinos:
            total_inquilinos += 1
            empleado = doc.to_dict()
            e_id = doc.id
            nombre_completo = f"{empleado.get('nombre', '')} {empleado.get('apellido', '')}"
            
            detalles_pagos_inquilino = []
            monto_total_inquilino = 0.0
            
            pagos_query = db.collection('Empleados').document(e_id).collection('Pagos').stream()
            
            for p in pagos_query:
                total_pagos_revisados += 1
                pago = p.to_dict()
                estado_pago = pago.get('estado', '').strip().lower()
                fecha_venc_str = pago.get('fecha_vencimiento', '')
                
                if (estado_pago == 'pendiente' or estado_pago == 'parcial') and fecha_venc_str:
                    if fecha_venc_str <= hoy_str:
                        try:
                            deuda_recibo = float(pago.get('saldo_pendiente', 0.0)) if estado_pago == 'parcial' else float(pago.get('monto', 0.0))
                        except (ValueError, TypeError):
                            deuda_recibo = 0.0
                            
                        if deuda_recibo <= 0:
                            continue
                            
                        monto_total_inquilino += deuda_recibo
                        tipo_deuda = "Pendiente" if estado_pago == 'pendiente' else "Saldo Parcial"
                        linea_detalle = f"▪️ *Mes*: {pago.get('mes_anio', 'N/A')} | *{tipo_deuda}*: C$ {deuda_recibo:,.2f} (Venció: {fecha_venc_str})"
                        detalles_pagos_inquilino.append(linea_detalle)
            
            # Intento de envío
            if len(detalles_pagos_inquilino) > 0:
                # 🟢 Evaluamos si la función realmente logró enviar el WhatsApp
                exito = enviar_whatsapp_consolidado(empleado, detalles_pagos_inquilino, monto_total_inquilino)
                
                if exito:
                    mensajes_enviados += 1
                    hubo_envios_hoy = True
                    diagnostico.append(f"✅ Consolidado Enviado: {nombre_completo} (C$ {monto_total_inquilino:,.2f})")
                else:
                    errores_totales += 1
                    diagnostico.append(f"❌ Falló el envío en Twilio para {nombre_completo}")
        
        # Solo guardamos el bloqueo diario si AL MENOS UN MENSAJE fue exitoso
        if hubo_envios_hoy:
            config_ref.set({'ultima_fecha_exitosa': hoy_str}, merge=True)
            diagnostico.append(f"💾 Control guardado en Firebase para el día {hoy_str}.")
        
        # 🟢 Determinar el status real de la respuesta
        status_respuesta = "success" if errores_totales == 0 and mensajes_enviados > 0 else "warning_with_errors"
        
        return jsonify({
            "status": status_respuesta,
            "mensaje": f"Escaneo completado. Exitosos: {mensajes_enviados} | Fallidos por Twilio: {errores_totales}",
            "fecha_servidor_managua": hoy_str,
            "total_inquilinos_escaneados": total_inquilinos,
            "mensajes_enviados_con_exito": mensajes_enviados,
            "envios_fallidos": errores_totales,
            "detalles_del_proceso": diagnostico
        }), 200 if status_respuesta == "success" else 400

    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500

    
@app.route('/registrar_abono/<e_id>/<p_id>', methods=['POST'])
def registrar_abono(e_id, p_id):
    if 'user' not in session: 
        return redirect(url_for('login_page'))
    
    monto_abonado = safe_float(request.form.get('monto_abonado'))
    nota_opcional = request.form.get('nota', '').strip()
    
    try:
        emp_ref = db.collection('Empleados').document(e_id)
        pago_ref = emp_ref.collection('Pagos').document(p_id)
        
        pago_doc = pago_ref.get()
        if not pago_doc.exists:
            flash("El pago no existe.", "danger")
            return redirect(url_for('ver_pagos', id=e_id))
            
        pago_data = pago_doc.to_dict()
        monto_total_mes = safe_float(pago_data.get('monto', 0.0))
        
        # --- 1. PROCESAMIENTO DE COMPROBANTE / FOTO ---
        comprobante_url = pago_data.get('comprobante_url', None)

        if 'comprobante' in request.files:
            file = request.files['comprobante']
            if file and file.filename != '':
                filename = secure_filename(file.filename)
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
                nombre_archivo = f"recibo_{p_id}_{timestamp}_{filename}"
                
                upload_folder = app.config['UPLOAD_FOLDER']
                os.makedirs(upload_folder, exist_ok=True)
                
                filepath = os.path.join(upload_folder, nombre_archivo)
                file.save(filepath)
                comprobante_url = f"/static/uploads/{nombre_archivo}"

        # --- 2. LÓGICA DE SALDOS ---
        saldo_pendiente = 0.0
        saldo_a_favor = 0.0
        nuevo_estado = "Cancelado"
        
        if monto_abonado < monto_total_mes:
            saldo_pendiente = monto_total_mes - monto_abonado
            nuevo_estado = "Parcial" if monto_abonado > 0 else "Pendiente"
        elif monto_abonado > monto_total_mes:
            saldo_a_favor = monto_abonado - monto_total_mes
            nuevo_estado = "Cancelado"

        # --- 3. MANEJO LIMPIO DE LA NOTA (SIN ACUMULACIÓN REPETITIVA) ---
        # Si el usuario escribió algo en el campo Nota, usamos ese texto.
        # Si lo dejó vacío, guardamos solo una referencia limpia del abono sin concatenar basura vieja.
        if nota_opcional:
            texto_nota = f"Abono C$ {monto_abonado:,.2f} - {nota_opcional}"
        else:
            texto_nota = f"Abono C$ {monto_abonado:,.2f}" if nuevo_estado != "Cancelado" else ""

        # --- 4. ACTUALIZACIÓN EN FIRESTORE ---
        update_payload = {
            'estado': nuevo_estado,
            'monto_pagado': monto_abonado,
            'saldo_pendiente': saldo_pendiente,
            'saldo_a_favor': saldo_a_favor,
            'nota': texto_nota  # 👈 Asignación directa, ya no se concatena a sí misma
        }

        if comprobante_url:
            update_payload['comprobante_url'] = comprobante_url

        pago_ref.update(update_payload)
        
        flash("Abono grabado con éxito.", "success")
        
    except Exception as e:
        flash(f"Error al procesar el abono: {e}", "danger")
        
    return redirect(url_for('ver_pagos', id=e_id))



@app.route('/encuesta/<id>')
def abrir_encuesta(id):
    if not db: 
        return "Error: No hay conexión con la base de datos."
        
    # CORREGIDO: Usar 'Empleados' con E mayúscula para que coincida con el dashboard
    doc_ref = db.collection('Empleados').document(id)
    doc = doc_ref.get()
    if not doc.exists:
        flash("Inquilino no encontrado", "danger")
        return redirect(url_for('login_page'))
    
    emp = doc.to_dict()
    emp['id'] = doc.id
    return render_template('encuesta.html', emp=emp)

@app.route('/guardar_encuesta', methods=['POST'])
def guardar_encuesta():
    if not db: 
        return "Error: No hay conexión con la base de datos."

    # 1. Inicializar diccionario con los datos base de control
    datos_encuesta = {
        "inquilino_id": request.form.get("inquilino_id"),
        "nombre_inquilino": request.form.get("nombre_inquilino"),
        "propiedad": request.form.get("propiedad"),
        "fecha_respuesta": datetime.now(ZoneInfo("America/Managua")).strftime('%Y-%m-%d %H:%M:%S'),
    }
    
    # 2. Capturar respuestas (p1-p10) y comentarios (comentario_p1 a comentario_p10) dinámicamente
    for i in range(1, 11):
        datos_encuesta[f"p{i}"] = request.form.get(f"p{i}")
        # Capturamos el texto quitándole espacios extra en blanco. Si está vacío, guarda un string vacío "" o puedes cambiarlo por None.
        datos_encuesta[f"comentario_p{i}"] = request.form.get(f"comentario_p{i}", "").strip()
    
    # 3. Procesamiento y guardado seguro de archivos multimedia en la carpeta local
    campos_foto = ['foto_p1', 'foto_p2', 'foto_p4', 'foto_p6']
    for campo in campos_foto:
        if campo in request.files:
            file = request.files[campo]
            if file and file.filename != '':
                filename = secure_filename(file.filename)
                # Añadimos marca de tiempo al nombre del archivo para que no se sobreescriban
                timestamp = datetime.now().strftime('%Y%m%d%H%M%S_')
                filename_final = timestamp + filename
                
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename_final)
                file.save(filepath)
                datos_encuesta[campo] = '/' + filepath
            else:
                datos_encuesta[campo] = None
        else:
            datos_encuesta[campo] = None

    # 4. Persistencia en la subcolección global de Firestore
    db.collection('encuestas_respuestas').add(datos_encuesta)
    
    return "<h3>Encuesta Enviada Exitosamente. Muchas gracias por tu apoyo. Puedes cerrar esta ventana.</h3>"


@app.route('/resumen_encuestas')
def resumen_encuestas():
    if 'user' not in session: 
        return redirect(url_for('login_page'))
    if not db: 
        return "Error: No hay conexión con la base de datos."
        
    # Recuperamos todas las respuestas enviadas por los inquilinos
    docs = db.collection('encuestas_respuestas').stream()
    lista_encuestas = []
    for doc in docs:
        d = doc.to_dict()
        d['id'] = doc.id
        lista_encuestas.append(d)
        
    return render_template('resumen_encuestas.html', encuestas=lista_encuestas)

@app.route('/ejecutar_envio_encuestas_secreto_123')
def ejecutar_envio_encuestas_automatico():
    if 'user' not in session: 
        return redirect(url_for('login_page'))
    if not db: 
        return "Error: No hay conexión con la base de datos."
        
    try:
        # 1. Recuperar los contratos desde Firestore
        docs = db.collection('Empleados').stream()
        
        # 2. Credenciales fijas y verificadas de Twilio (Copiadas estrictamente de tu otra función)
        real_sid = 'AC55a32288ebca14e7286265bd207bd593'
        real_token = '9ead4c07b599ae86f5118122bbc004f9'
        
        # 3. Limpieza estricta de variables del sistema en Render para evitar fallos de autenticación
        if 'TWILIO_ACCOUNT_SID' in os.environ:
            del os.environ['TWILIO_ACCOUNT_SID']
        if 'TWILIO_AUTH_TOKEN' in os.environ:
            del os.environ['TWILIO_AUTH_TOKEN']
            
        os.environ['TWILIO_ACCOUNT_SID'] = ""
        os.environ['TWILIO_AUTH_TOKEN'] = ""
        
        # 4. Inicialización directa del cliente de Twilio
        client = Client(real_sid, real_token)
        contador_envios = 0
        errores = []
        
        for doc in docs:
            emp = doc.to_dict()
            emp['id'] = doc.id
            
            telefono = emp.get('telefono')
            
            # Validación del nuevo switch independiente para encuestas
            encuesta_activa = emp.get('enviar_encuesta_inquilino') == True or emp.get('enviar_encuesta_inquilino') == 'true'
            
            if telefono and encuesta_activa:
                # Construcción del link dinámico seguro
                # ✅ CAMBIO RECOMENDADO: Forzar HTTPS o detectar el protocolo del proxy de Render
                scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
                if scheme == 'http' and not app.debug:
                    scheme = 'https'  # Forzar https en producción

                host = request.host
                link_encuesta = f"{scheme}://{host}/encuesta/{emp['id']}"
                mensaje = (
                    f"📋 *ENCUESTA DE CONTROL INTERNO* 📋\n\n"
                    f"Hola *{emp.get('nombre', '')} {emp.get('apellido', '')} {emp.get('cedula', '')}*,\n"
                    f"Te Saluda Osman Melébdes y para compartir el enlace de la encuesta de control para propiedad {emp.get('propiedad', '')}*,\n"
                    f"ubicada en: *{emp.get('direccion', 'N/A')}*.\n\n"
                    f"Agradecemos enormemente tu valioso apoyo completándola a través del siguiente link:\n"
                    f"🔗 {link_encuesta}\n\n"
                    f"¡Muchas gracias por tu colaboración!"
                )
                
                try:
                    # Ajusta el 'to' con el teléfono real del inquilino cuando pases a producción
                    client.messages.create(
                        from_='whatsapp:+14155238886',
                        body=mensaje,
                        to='whatsapp:+50589475863'  # 👈 O f"whatsapp:{telefono}" si usas números dinámicos
                    )
                    contador_envios += 1
                    time.sleep(2)

                except Exception as error_twilio:
                    nom_cli = f"{emp.get('nombre', '')} {emp.get('apellido', '')}"
                    print(f"❌ Error enviando a {nom_cli}: {error_twilio}")
                    errores.append(f"{nom_cli} ({error_twilio})")
        
        if errores:
            flash(f"Se enviaron {contador_envios} encuestas. Hubo fallas con: {', '.join(errores)}", "warning")
        else:
            flash(f"Proceso completado. Se enviaron {contador_envios} encuestas vía WhatsApp con éxito.", "success")
            
        return redirect(url_for('dashboard'))

    except Exception as e:
        flash(f"Error crítico en el servidor: {str(e)}", "danger")
        return redirect(url_for('dashboard'))

@app.route('/imprimir_encuesta/<encuesta_id>')
def imprimir_encuesta_individual(encuesta_id):
    if not db:
        return "Error: No hay conexión con la base de datos."
    
    # Buscar el documento específico de la encuesta en Firestore
    doc_ref = db.collection('encuestas_respuestas').document(encuesta_id)
    doc = doc_ref.get()
    
    if not doc.exists:
        return "<h3>Error: La encuesta solicitada no existe.</h3>", 404
        
    encuesta_data = doc.to_dict()
    
    # Pasamos los datos del documento a la plantilla de impresión individual
    return render_template('imprimir_encuesta.html', enc=encuesta_data)


from datetime import datetime
import os
from flask import Flask, render_template, request, redirect, url_for, flash, session
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime
import os
from flask import Flask, render_template, request, redirect, url_for, flash, session
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------
# CREDENCIALES DE TWILIO (Ingresadas directamente)
# ---------------------------------------------------------
ACCOUNT_SID = "AC55a32288ebca14e7286265bd207bd593"
AUTH_TOKEN = "9ead4c07b599ae86f5118122bbc004f9"

# Inicializar cliente de Twilio
twilio_client = Client(ACCOUNT_SID, AUTH_TOKEN)

TWILIO_WHATSAPP_NUMBER = 'whatsapp:+14155238886' # Sandbox Twilio
TWILIO_SMS_NUMBER = '+14155238886' # Reemplazar con tu número SMS de Twilio si aplica

# ---------------------------------------------------------
# TAREA EN SEGUNDO PLANO (SCHEDULER

def procesar_mensajes_programados():
    with app.app_context():
        ahora = datetime.now()
        ahora_str = ahora.strftime('%Y-%m-%dT%H:%M')
        
        docs = db.collection('MensajesProgramados').where('estado', '==', 'Pendiente').stream()
        
        for doc in docs:
            data = doc.to_dict()
            fecha_proxima = data.get('fecha_hora_inicio', '')
            fecha_limite_str = data.get('fecha_fin', '') # Formato: YYYY-MM-DD
            
            if fecha_proxima and fecha_proxima <= ahora_str:
                telefonos = [data.get('telefono_1'), data.get('telefono_2')]
                canal = data.get('canal', 'whatsapp')
                mensaje_texto = data.get('mensaje', '')
                
                # 1. Enviar el mensaje a los teléfonos
                for tel in telefonos:
                    if not tel:
                        continue
                    try:
                        if canal == 'whatsapp':
                            destinatario = tel if tel.startswith('whatsapp:') else f'whatsapp:{tel}'
                            twilio_client.messages.create(
                                body=mensaje_texto,
                                from_=TWILIO_WHATSAPP_NUMBER,
                                to=destinatario
                            )
                        else:
                            twilio_client.messages.create(
                                body=mensaje_texto,
                                from_=TWILIO_SMS_NUMBER,
                                to=tel
                            )
                        print(f"✅ Mensaje diario enviado a {tel}")
                    except Exception as e:
                        print(f"❌ Error al enviar a {tel}: {e}")

                # 2. Calcular la fecha y hora del siguiente día (+24h)
                dt_actual = datetime.strptime(fecha_proxima, '%Y-%m-%dT%H:%M')
                dt_siguiente = dt_actual + timedelta(days=1)
                siguiente_fecha_str = dt_siguiente.strftime('%Y-%m-%dT%H:%M')
                
                # Convertir fecha_fin a datetime para comparar (23:59 del último día)
                dt_limite = datetime.strptime(f"{fecha_limite_str}T23:59", '%Y-%m-%dT%H:%M')
                
                # 3. Validar si el siguiente envío entra en el rango
                if dt_siguiente <= dt_limite:
                    db.collection('MensajesProgramados').document(doc.id).update({
                        'fecha_hora_inicio': siguiente_fecha_str,
                        'estado': 'Pendiente'
                    })
                    print(f"🔄 Mensaje programado para el día siguiente: {siguiente_fecha_str}")
                else:
                    # Se superó el rango de fechas programado
                    db.collection('MensajesProgramados').document(doc.id).update({
                        'estado': 'Completado'
                    })
                    print("🏁 Rango de fechas finalizado. Marcado como Completado.")
# ---------------------------------------------------------
# INICIALIZACIÓN DEL SCHEDULER
# ---------------------------------------------------------
scheduler = BackgroundScheduler()
# Ejecuta la función de verificación cada 60 segundos
scheduler.add_job(func=procesar_mensajes_programados, trigger="interval", seconds=60)
scheduler.start()

# ---------------------------------------------------------
# RUTAS DE FLASK
# ---------------------------------------------------------
@app.route('/mensajes')
def vista_mensajes():
    if 'user' not in session: 
        return redirect(url_for('login_page'))
    
    programaciones_ref = db.collection('MensajesProgramados').stream()
    programaciones = [dict(doc.to_dict(), id=doc.id) for doc in programaciones_ref]
    return render_template('mensajes.html', programaciones=programaciones)

@app.route('/guardar_programacion_mensaje', methods=['POST'])
def guardar_programacion_mensaje():
    if 'user' not in session: 
        return redirect(url_for('login_page'))
    
    payload = {
        'telefono_1': request.form.get('telefono_1', '').strip(),
        'telefono_2': request.form.get('telefono_2', '').strip(),
        'fecha_hora_inicio': request.form.get('fecha_hora_inicio'), # Ej: 2026-07-23T09:00
        'fecha_fin': request.form.get('fecha_fin'),                 # Ej: 2026-07-30
        'canal': request.form.get('canal'),
        'mensaje': request.form.get('mensaje'),
        'estado': 'Pendiente'
    }
    
    db.collection('MensajesProgramados').add(payload)
    flash("Programación guardada exitosamente.", "success")
    return redirect(url_for('vista_mensajes'))

@app.route('/eliminar_programacion/<id_prog>')
def eliminar_programacion(id_prog):
    if 'user' not in session: 
        return redirect(url_for('login_page'))
    
    db.collection('MensajesProgramados').document(id_prog).delete()
    flash("Programación eliminada.", "info")
    return redirect(url_for('vista_mensajes'))
#TRADING

import os
import smtplib
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage

import pandas as pd
import numpy as np
import yfinance as yf
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, session
from apscheduler.schedulers.background import BackgroundScheduler


app.secret_key = os.getenv('SECRET_KEY', 'tu_clave_secreta_aqui')

# ---------------------------------------------------------
# CARGA DE VARIABLES DE ENTORNO SMTP
# ---------------------------------------------------------
load_dotenv()
if os.path.exists("TW.env"):
    load_dotenv("TW.env")

SMTP_SERVER = os.getenv('SMTP_SERVER', 'smtp.gmail.com').strip()
SMTP_PORT = int(os.getenv('SMTP_PORT', 465))  # Recomendado SSL en puerto 465

raw_user = os.getenv('SMTP_USER', '')
SMTP_USER = raw_user.strip().strip('"').strip("'") if raw_user else None

raw_pass = os.getenv('SMTP_PASSWORD', '')
SMTP_PASSWORD = raw_pass.strip().replace(" ", "").strip('"').strip("'") if raw_pass else None

raw_dest = os.getenv('EMAIL_DESTINO_DEFAULT', '')
EMAIL_DESTINO_DEFAULT = raw_dest.strip().strip('"').strip("'") if raw_dest else SMTP_USER

# ---------------------------------------------------------
# DICCIONARIO GLOBAL DE INSTRUMENTOS
# ---------------------------------------------------------
DICCIONARIO_NOMBRES = {
    'GC=F': 'Oro (Refugio)', 
    'SI=F': 'Plata (Refugio)', 
    'HG=F': 'Cobre', 
    'CL=F': 'Petroleo WTI', 
    'ES=F': 'S&P 500'
}

# ---------------------------------------------------------
# INDICADORES TÉCNICOS
# ---------------------------------------------------------
def calcular_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calcular_adx(df, period=14):
    high = df['High']
    low = df['Low']
    close = df['Close']
    
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()

    up_move = high - high.shift(1)
    down_move = low.shift(1) - low

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(window=period).mean() / atr)
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(window=period).mean() / atr)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(window=period).mean()

# =========================================================
# MÓDULO DE TRADING OPTIMIZADO (DESCARGA EN PARALELO)
# =========================================================
def procesar_lote_trading(
    seleccionados, intervalo, periodo, puntos_min, email_destino
):
  """Descarga todos los tickers juntos de una sola vez para evitar timeouts."""
  try:
    print(f'🚀 Iniciando escaneo en paralelo para: {seleccionados}')

    # 1. Descargar TODOS los activos seleccionados en UNA sola llamada
    datos_mkt = yf.download(
        tickers=seleccionados,
        period=periodo,
        interval=intervalo,
        group_by='ticker',
        progress=False,
        auto_adjust=True,
        timeout=10,
    )

    alertas_enviadas = 0

    for ticker in seleccionados:
      nombre = DICCIONARIO_NOMBRES.get(ticker, ticker)

      # Extraer DataFrame del ticker
      if len(seleccionados) > 1:
        if ticker not in datos_mkt:
          continue
        df = datos_mkt[ticker].copy()
      else:
        df = datos_mkt.copy()

      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

      df = df.ffill().dropna()
      if df.empty or len(df) < 50:
        continue

      precio_actual = float(df['Close'].iloc[-1])

      # Indicadores
      adx_serie = calcular_adx(df, period=14)
      if (
          adx_serie is None
          or adx_serie.empty
          or pd.isna(adx_serie.iloc[-1])
      ):
        continue

      valor_adx_num = round(float(adx_serie.iloc[-1]), 2)
      if valor_adx_num < 25:
        continue

      puntos_long = 3
      puntos_short = 3

      sma50_serie = df['Close'].rolling(window=50).mean()
      if sma50_serie is not None and not sma50_serie.empty:
        sma50 = sma50_serie.iloc[-1]
        if pd.notna(sma50):
          if precio_actual > sma50:
            puntos_long += 2
          else:
            puntos_short += 2

      rsi_serie = calcular_rsi(df['Close'], period=14)
      if rsi_serie is not None and not rsi_serie.empty:
        rsi = rsi_serie.iloc[-1]
        if pd.notna(rsi) and (40 < rsi < 60):
          puntos_long += 2
          puntos_short += 2

      puntos_finales = max(puntos_long, puntos_short)
      accion = 'COMPRA/LONG' if puntos_long >= puntos_short else 'VENTA/SHORT'

      if puntos_finales >= puntos_min:
        orden = {
            'ticker': ticker,
            'nombre': nombre,
            'precio': round(precio_actual, 4),
            'puntos': puntos_finales,
            'adx_valor': valor_adx_num,
            'accion': accion,
        }
        if enviar_alerta_trading_email(
            orden, email_destino, puntos_min
        ):
          alertas_enviadas += 1

    print(
        f'🏁 Escaneo completado. Se enviaron {alertas_enviadas} alertas por'
        ' correo.'
    )

  except Exception as e:
    print(f'❌ Error procesando lote de trading: {e}')


@app.route('/ejecutar_escaner_trading', methods=['POST'])
def ejecutar_escaner_trading():
  if 'user' not in session:
    return redirect(url_for('login_page'))

  seleccionados = request.form.getlist('instrumentos')
  intervalo = request.form.get('intervalo', '1h')
  puntos_min = int(request.form.get('puntos_maximos', 6))

  email_ingresado = request.form.get('email_destino')
  email_destino = (
      email_ingresado.strip()
      if email_ingresado and email_ingresado.strip()
      else (EMAIL_DESTINO_DEFAULT or '').strip()
  )

  if not email_destino or not seleccionados:
    flash('Revisa la selección de instrumentos y correo de destino.', 'warning')
    return redirect(url_for('vista_trading'))

  periodo_map = {'5m': '5d', '15m': '1mo', '1h': '5d', '1d': '2y'}
  periodo = periodo_map.get(intervalo, '5d')

  # ⚡ EJECUCIÓN EN SEGUNDO PLANO (THREAD) PARA EVITAR TIMEOUT EN RENDER
  threading.Thread(
      target=procesar_lote_trading,
      args=(seleccionados, intervalo, periodo, puntos_min, email_destino),
      daemon=True,
  ).start()

  flash(
      '🚀 Escaneo iniciado en segundo plano. Si hay oportunidades activas,'
      ' recibirás la alerta por correo en unos segundos.',
      'success',
  )
  return redirect(url_for('vista_trading'))
# ---------------------------------------------------------
# FUNCIÓN DE ENVÍO DE EMAIL
# ---------------------------------------------------------
def enviar_email_smtp(asunto, cuerpo, destino=None):
    if not SMTP_USER or not SMTP_PASSWORD:
        print("❌ Error: Credenciales de SMTP no configuradas en el entorno.")
        return False

    destinatario = destino.strip() if destino and destino.strip() else EMAIL_DESTINO_DEFAULT

    if not destinatario:
        print("❌ Error: No se especificó dirección de destino.")
        return False

    msg = EmailMessage()
    msg.set_content(cuerpo)
    msg['Subject'] = asunto
    msg['From'] = SMTP_USER
    msg['To'] = destinatario

    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"❌ Error enviando Email por SMTP: {e}")
        return False

def enviar_alerta_trading_email(orden, destino=None, puntos_max=6):
    asunto = f"🚀 Alerta Trading: {orden['nombre']} ({orden['accion']})"
    cuerpo = (f"🚀 OPORTUNIDAD TRADING 24/7\n\n"
              f"Instrumento: {orden['nombre']} ({orden['ticker']})\n"
              f"Acción: {orden['accion']}\n"
              f"Precio Actual: {orden['precio']}\n"
              f"ADX: {orden['adx_valor']} 🔥\n"
              f"Puntuación: {orden['puntos']}/{puntos_max}")
    
    return enviar_email_smtp(asunto, cuerpo, destino)

# ---------------------------------------------------------
# RUTAS FLASK
# ---------------------------------------------------------
@app.route('/trading')
def vista_trading():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    return render_template('trading.html', instrumentos=DICCIONARIO_NOMBRES)


# ---------------------------------------------------------
# TAREAS EN SEGUNDO PLANO Y SCHEDULER (PREVENCIÓN DE DUPLICADOS)
# ---------------------------------------------------------
ALERTAS_ENVIADAS_CACHE = {}

def tarea_escaneo_automatico_trading():
    with app.app_context():
        zona_ni = ZoneInfo("America/Managua")
        ahora_dt = datetime.now(zona_ni)
        print(f"🚀 [{ahora_dt.strftime('%Y-%m-%d %H:%M:%S')}] Escaneo automático en ejecución...")
        
        intervalo, periodo, puntos_min = "30m", "1mo", 6
        alertas_enviadas = 0

        for ticker, nombre in DICCIONARIO_NOMBRES.items():
            ultimo_envio = ALERTAS_ENVIADAS_CACHE.get(ticker)
            if ultimo_envio and (ahora_dt - ultimo_envio).total_seconds() < 3600:
                continue

            orden = procesar_lote_trading(ticker, nombre, intervalo, periodo, puntos_min)
            if orden and enviar_alerta_trading_email(orden, EMAIL_DESTINO_DEFAULT, puntos_min):
                ALERTAS_ENVIADAS_CACHE[ticker] = ahora_dt
                alertas_enviadas += 1
                print(f"✅ Alerta enviada para {ticker}")

        print(f"🏁 Escaneo finalizado. Alertas enviadas: {alertas_enviadas}")

def enviar_reporte_estado_trading():
    with app.app_context():
        zona_ni = ZoneInfo("America/Managua")
        hora_fmt = datetime.now(zona_ni).strftime("%I:%M %p (%d/%m)")
        asunto = f"🟢 TRADING BOT OPERATIVO ({hora_fmt})"
        mensaje = f"🟢 BOT OPERATIVO 24/7\n\nHora (Nicaragua): {hora_fmt}\nEscáner analizando velas 30m continuamente."
        enviar_email_smtp(asunto, mensaje, EMAIL_DESTINO_DEFAULT)

# Inicialización segura del Scheduler para Gunicorn / Render
scheduler = BackgroundScheduler(daemon=True)

def inicializar_scheduler():
    if not scheduler.running:
        scheduler.add_job(
            func=tarea_escaneo_automatico_trading,
            trigger="interval",
            minutes=5,
            id="job_trading_automatico",
            replace_existing=True
        )
        scheduler.add_job(
            func=enviar_reporte_estado_trading,
            trigger="cron",
            minute=0,
            timezone=ZoneInfo("America/Managua"),
            id="job_status_heartbeat",
            replace_existing=True
        )
        scheduler.start()
        print("🚀 APScheduler iniciado correctamente en Render.")

# Arrancar el scheduler al levantar la app
inicializar_scheduler()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)


