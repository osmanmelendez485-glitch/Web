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
from firebase_admin import storage
from google.cloud.firestore_v1.base_query import FieldFilter
from twilio.rest import Client
from dotenv import load_dotenv
import threading
from datetime import datetime, timezone, timedelta

# Carga las variables desde el archivo .env si existe
load_dotenv()

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
    if 'user' not in session: return redirect(url_for('login_page'))
    
    d = request.form
    emp_id = d.get('id')
    


    # --- 1. PROCESAMIENTO SEGURO DE FECHA DE REGISTRO ---
    # Capturamos el dato y quitamos espacios
    fecha_reg_raw = d.get('fecha', '').strip()
    
    # Si está vacío o hay error, usamos 'now'
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
        # Contamos registros para el consecutivo
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

    # --- 4. PREPARACIÓN DE DATOS (Validando fechas de inicio/fin) ---
    f_inicio_raw = d.get('fecha_inicio', '').strip()
    f_fin_raw = d.get('fecha_fin', '').strip()

    # Si vienen vacías, asignamos hoy en formato texto para la DB
    f_inicio_db = f_inicio_raw if f_inicio_raw else datetime.now().strftime('%Y-%m-%d')
    f_fin_db = f_fin_raw if f_fin_raw else datetime.now().strftime('%Y-%m-%d')

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
        'deposito': deposito_valor,
        'total_pagar': mensualidad_base,
        'fecha': fecha_dt.strftime('%Y-%m-%d')
    }

    # --- 5. GUARDADO Y PAGOS ---
    
    if emp_id:
        # 1. Actualizar los datos del documento principal del inquilino
        db.collection('Empleados').document(emp_id).update(datos)
        
        try:
            # 2. Buscar todos los pagos asociados a este inquilino
            pagos_viejos = db.collection('Empleados').document(emp_id).collection('Pagos').stream()
            
            for p in pagos_viejos:
                p_data = p.to_dict()
                # 3. SOLO modificamos los meses que el usuario aún NO ha pagado
                if p_data.get('estado', 'Pendiente') == 'Pendiente':
                    db.collection('Empleados').document(emp_id).collection('Pagos').document(p.id).update({
                        'monto': mensualidad_base  # Le asignamos la nueva suma recalculada
                    })
        except Exception as err_pagos:
            print(f"⚠️ No se pudieron actualizar las cuotas pendientes: {err_pagos}")

        flash(f"Registro y cuotas pendientes de {num_contrato} actualizados", "success")


    else:
        nuevo_doc = db.collection('Empleados').add(datos)
        new_id = nuevo_doc[1].id 

        # Generador de pagos con protección extra
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

            # Al final de tu función def save():
        #flash(f"Contrato {num_contrato} procesado", "success")
    #return redirect(url_for('ver_contrato', num_contrato=num_contrato))
        
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
    
    db.collection('Empleados').document(e_id).collection('Pagos').document(p_id).update({
        'estado': nuevo_estado
    })
    
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

        if acumulado_propiedad > 0: 
            resumen.append({
                'direccion': e_data.get('direccion', 'Sin Dirección'),
                'inquilino': f"{e_data.get('nombre', '')} {e_data.get('apellido', '')}",
                'total': acumulado_propiedad,
                'recaudado': recaudado_propiedad,
                'pendiente': pendiente_propiedad
            })

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
    # 1. Credenciales reales y validadas al 100%
    real_sid = 'AC55a32288ebca14e7286265bd207bd593'
    real_token = '125ad0c17e47e6a45bed4f733636c375'
    
    # 2. Inicialización limpia compatible con tu versión de Twilio
    # Pasamos las credenciales de forma posicional directa para ignorar el entorno de Render
    client = Client(real_sid, real_token)

    # Concatena todos los meses vencidos en líneas individuales
    bloque_detalles = "\n".join(detalles_pagos)

    mensaje = (
        f"📋 *ESTADO DE CUENTA CONSOLIDADO* 📋\n\n"
        f"Hola *{empleado.get('nombre', '')} {empleado.get('apellido', '')}*,\n"
        f"Te saludamos para recordarte los saldos pendientes asociados a tu contrato *{empleado.get('num_contrato', 'N/A')}*:\n\n"
        f"{bloque_detalles}\n\n"
        f"💰 *TOTAL A PAGAR*: *C$ {monto_total:,.2f}*\n\n"
        f"Por favor omitir este mensaje si ya has realizado tu depósito o transferencia. ¡Muchas gracias!"
    )

    try:
        # Envío utilizando el cliente
        message = client.messages.create(
            from_='whatsapp:+14155238886',  # Sandbox oficial de Twilio
            body=mensaje,
            to='whatsapp:+50589475863'     # Tu número móvil de pruebas fijo
        )
        print(f"✅ WhatsApp Consolidado enviado con SID: {message.sid}")
        return True
    except Exception as e:
        print(f"❌ Error detallado en Twilio API: {str(e)}")
        raise e

# --- RUTA AUTOMÁTICA GATILLADA POR CRON-JOB (PROCESO LINEAL DIRECTO) ---
@app.route('/ejecutar_envio_automatico_secreto_123')
def ejecutar_envio_automatico():
    diagnostico = []
    mensajes_enviados = 0
    try:
        # Configurar la zona horaria nativa de Nicaragua (UTC -6)
        zona_ni = timezone(timedelta(hours=-6))
        fecha_actual = datetime.now(zona_ni)
        hoy_str = fecha_actual.strftime('%Y-%m-%d')
        
        print(f"🤖 Ejecutando análisis de cobros. Fecha (Nicaragua): {hoy_str}")
        
        # 1. REVISAR EN FIREBASE SI YA SE HIZO EL ENVÍO HOY
        config_ref = db.collection('Configuracion_Cron').document('control_envios')
        config_doc = config_ref.get()
        
        if config_doc.exists:
            ultima_fecha_envio = config_doc.to_dict().get('ultima_fecha_exitosa', '')
            if ultima_fecha_envio == hoy_str:
                return jsonify({
                    "status": "skipped",
                    "mensaje": f"Los recordatorios del dia de hoy ({hoy_str}) ya fueron enviados en un ciclo previo.",
                    "fecha_servidor_managua": hoy_str
                }), 200

        # 2. PROCESAR Y ESCANEAR CUENTAS SI ES LA PRIMERA VEZ DEL ĐÍA
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
                estado_pago = pago.get('estado', '')
                fecha_venc_str = pago.get('fecha_vencimiento', '')
                
                if estado_pago.strip().lower() == 'pendiente' and fecha_venc_str:
                    if fecha_venc_str <= hoy_str:
                        try:
                            monto_recibo = float(pago.get('monto', 0.0))
                        except (ValueError, TypeError):
                            monto_recibo = 0.0
                            
                        monto_total_inquilino += monto_recibo
                        linea_detalle = f"▪️ *Mes*: {pago.get('mes_anio', 'N/A')} | *Monto*: C$ {monto_recibo:,.2f} (Vence: {fecha_venc_str})"
                        detalles_pagos_inquilino.append(linea_detalle)
            
            # Si el inquilino acumuló saldos pendientes, se le envía UN SOLO mensaje consolidado
            if len(detalles_pagos_inquilino) > 0:
                try:
                    enviar_whatsapp_consolidado(empleado, detalles_pagos_inquilino, monto_total_inquilino)
                    mensajes_enviados += 1
                    hubo_envios_hoy = True
                    diagnostico.append(f"✅ Consolidado Enviado: {nombre_completo} (Total: C$ {monto_total_inquilino:,.2f})")
                except Exception as error_twilio:
                    diagnostico.append(f"❌ Error en Twilio para {nombre_completo}: {str(error_twilio)}")
        
        # 3. SI SE LOGRARON ENVIAR MENSAJES CON ÉXITO, GUARDAR LA FECHA EN FIREBASE PARA BLOQUEAR EL RESTO DEL DÍA
        if hubo_envios_hoy:
            config_ref.set({'ultima_fecha_exitosa': hoy_str}, merge=True)
            diagnostico.append(f"💾 Control guardado en Firebase para el dia {hoy_str}.")
                        
        return jsonify({
            "status": "success",
            "mensaje": "Proceso completado exitosamente de forma lineal.",
            "fecha_servidor_managua": hoy_str,
            "total_inquilinos_escaneados": total_inquilinos,
            "total_pagos_totales_leidos": total_pagos_revisados,
            "mensajes_enviados_con_exito": mensajes_enviados,
            "detalles_del_proceso": diagnostico
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500

@app.route('/registrar_abono/<e_id>/<p_id>', methods=['POST'])
def registrar_abono(e_id, p_id):
    if 'user' not in session: return redirect(url_for('login_page'))
    
    # Capturamos el dinero que ingreses en la casilla de la web
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
        
        saldo_pendiente = 0.0
        saldo_a_favor = 0.0
        nuevo_estado = "Cancelado"
        
        # Lógica matemática de saldos
        if monto_abonado < monto_total_mes:
            saldo_pendiente = monto_total_mes - monto_abonado
            nuevo_estado = "Parcial" if monto_abonado > 0 else "Pendiente"
        elif monto_abonado > monto_total_mes:
            saldo_a_favor = monto_abonado - monto_total_mes
            nuevo_estado = "Cancelado"
            
        # Guardamos los nuevos campos en Firestore
        pago_ref.update({
            'estado': nuevo_estado,
            'monto_pagado': monto_abonado,
            'saldo_pendiente': saldo_pendiente,
            'saldo_a_favor': saldo_a_favor,
            'nota': f"{pago_data.get('nota', '')} | Abono: C$ {monto_abonado}. {nota_opcional}".strip(" | ")
        })
        
        flash("Abono/Adelanto grabado con éxito.", "success")
        
    except Exception as e:
        flash(f"Error al procesar el abono: {e}", "danger")
        
    return redirect(url_for('ver_pagos', id=e_id))

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
