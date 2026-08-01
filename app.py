import json
import os
import re
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import BytesIO
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore, storage
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from google.cloud.firestore_v1.base_query import FieldFilter
import numpy as np
import pandas as pd
import pandas_ta_classic as ta
from twilio.rest import Client
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
import yfinance as yf
import socket
import requests


# ---------------------------------------------------------
# CARGA DE VARIABLES DE ENTORNO (.env y TW.env)
# ---------------------------------------------------------
load_dotenv()
if os.path.exists("TW.env"):
  load_dotenv("TW.env")

# Variables Globales de Twilio obtenidas de TW.env
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()

TWILIO_WHATSAPP_NUMBER = os.environ.get(
    'TWILIO_WHATSAPP_NUMBER', 'whatsapp:+14155238886'
)
TWILIO_SMS_NUMBER = os.environ.get('TWILIO_SMS_NUMBER', '+14155238886')

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
  if (
      val is None
      or (isinstance(val, float) and np.isnan(val))
      or str(val).lower() == 'nan'
  ):
    return default
  return val


def safe_float(val):
  try:
    if val is None or str(val).lower() == 'nan' or str(val).strip() == '':
      return 0.0
    return float(str(val).replace(',', '.'))
  except:
    return 0.0


@app.context_processor
def inject_now():
  return {'now': datetime.now(), 'datetime': datetime}


# --- RUTAS DE AUTENTICACIÓN ---
@app.route('/')
def login_page():
  if 'user' in session:
    return redirect(url_for('dashboard'))
  return render_template('login.html')


@app.route('/auth', methods=['GET', 'POST'])
def auth():
  if request.method == 'POST':
    user = request.form.get('user')
    pw = request.form.get('password')
    if user == 'admin' and pw == '1234':
      session['user'] = user
      return redirect(url_for('dashboard'))

    flash("Usuario o contraseña incorrectos", "danger")
    return redirect(url_for('login_page'))

  return redirect(url_for('login_page'))


@app.route('/logout')
def logout():
  session.pop('user', None)
  return redirect(url_for('login_page'))


# --- DASHBOARD ---


@app.route('/dashboard')
def dashboard():
  if 'user' not in session:
    return redirect(url_for('login_page'))
  if not db:
    return "Error: No hay conexión con la base de datos."

  search_query = request.args.get('search', '').lower()
  sort_by = request.args.get('sort', 'fecha_inicio')
  direction = request.args.get('direction', 'desc')

  try:
    order_dir = (
        firestore.Query.DESCENDING
        if direction == 'desc'
        else firestore.Query.ASCENDING
    )
    docs = (
        db.collection('Empleados')
        .order_by(sort_by, direction=order_dir)
        .stream()
    )

    empleados = []
    total_recaudado = 0.0
    total_pendiente = 0.0

    for doc in docs:
      item = doc.to_dict()
      item['id'] = doc.id
      item['estado'] = clean_val(item.get('estado'), 'Pendiente')
      canon_val = safe_float(item.get('canon'))
      item['canon'] = canon_val

      nombre_completo = (
          f"{item.get('nombre','')} {item.get('apellido','')}".lower()
      )
      cedula = str(item.get('cedula', '')).lower()
      num_con = str(item.get('num_contrato', '')).lower()

      if not search_query or (
          search_query in nombre_completo
          or search_query in cedula
          or search_query in num_con
      ):
        if item['estado'] == 'Cancelado':
          total_recaudado += canon_val
        else:
          total_pendiente += canon_val
        empleados.append(item)

    return render_template(
        'index.html',
        empleados=empleados,
        search_query=search_query,
        sort_by=sort_by,
        direction=direction,
        total_recaudado=total_recaudado,
        total_pendiente=total_pendiente,
    )
  except Exception as e:
    return f"Error en Dashboard: {e}"


@app.route('/save', methods=['POST'])
def save():
  if 'user' not in session:
    return redirect(url_for('login_page'))

  d = request.form
  emp_id = d.get('id')

  url_imagen = '/static/uploads/propiedad_defecto.jpg'

  if emp_id:
    doc_existente = db.collection('Empleados').document(emp_id).get()
    if doc_existente.exists:
      url_imagen = doc_existente.to_dict().get(
          'url_imagen_propiedad', url_imagen
      )

  if 'foto_propiedad' in request.files:
    file = request.files['foto_propiedad']
    if file and file.filename != '':
      filename = secure_filename(file.filename)
      upload_folder = os.path.join(app.root_path, 'static', 'uploads')
      os.makedirs(upload_folder, exist_ok=True)

      nombre_archivo = f"propiedad_{emp_id or 'nueva'}_{filename}"
      filepath = os.path.join(upload_folder, nombre_archivo)
      file.save(filepath)
      url_imagen = f"/static/uploads/{nombre_archivo}"

  fecha_reg_raw = d.get('fecha', '').strip()

  if not fecha_reg_raw:
    fecha_dt = datetime.now()
  else:
    try:
      fecha_dt = datetime.strptime(fecha_reg_raw, '%Y-%m-%d')
    except ValueError:
      fecha_dt = datetime.now()

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

  deposito_valor = safe_float(d.get('deposito'))
  limite_meses = int(d.get('meses_contrato', 12))
  mensualidad_base = (
      safe_float(d.get('internet'))
      + safe_float(d.get('agua'))
      + safe_float(d.get('luz'))
      + safe_float(d.get('canon'))
      + safe_float(d.get('equipo'))
  )

  f_inicio_raw = d.get('fecha_inicio', '').strip()
  f_fin_raw = d.get('fecha_fin', '').strip()

  f_inicio_db = (
      f_inicio_raw if f_inicio_raw else datetime.now().strftime('%Y-%m-%d')
  )
  f_fin_db = f_fin_raw if f_fin_raw else datetime.now().strftime('%Y-%m-%d')
  enviar_copia = request.form.get('enviar_copia_inquilino') == 'true'
  enviar_encuesta = (
      request.form.get('enviar_encuesta_inquilino') == 'true'
      or request.form.get('enviar_encuesta_inquilino') == 'on'
  )

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
      'url_imagen_propiedad': url_imagen,
  }

  if emp_id:
    deposito_nuevo = safe_float(d.get('deposito', 0.0))
    datos['deposito'] = deposito_nuevo
    db.collection('Empleados').document(emp_id).update(datos)

    try:
      pagos_viejos = (
          db.collection('Empleados')
          .document(emp_id)
          .collection('Pagos')
          .order_by('fecha_vencimiento')
          .get()
      )

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
              'mes_anio': fecha_secuencial.strftime('%B %Y'),
          })

          mov_actual = safe_float(p_data.get('deposito_movimiento', 0.0))
          if mov_actual != 0.0:
            signo = -1.0 if mov_actual < 0 else 1.0
            pago_update['deposito_movimiento'] = deposito_nuevo * signo

        if indice == 0:
          pago_update['nota'] = f'Depósito Inicial: C$ {deposito_nuevo:,.2f}'

        if pago_update:
          db.collection('Empleados').document(emp_id).collection(
              'Pagos'
          ).document(p.id).update(pago_update)

        fecha_secuencial += relativedelta(months=1)

    except Exception as err_pagos:
      print(f'⚠️ Error actualizando cuotas: {err_pagos}')

    flash(
        'Registro, foto y movimientos de depósito actualizados con éxito',
        'success',
    )
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
          'nota': 'Depósito Inicial' if i == 0 else '',
      }
      db.collection('Empleados').document(new_id).collection('Pagos').add(
          pago_doc
      )
      fecha_venc += relativedelta(months=1)

    flash(f'Contrato {num_contrato} creado con éxito', 'success')

  return redirect(url_for('dashboard'))


@app.route('/boveda_contratos')
def boveda_contratos():
  if 'user' not in session:
    return redirect(url_for('login_page'))

  docs = db.collection('Empleados').order_by('nombre').stream()
  lista_documentos = []
  for d in docs:
    item = d.to_dict()
    if item.get('url_contrato_pdf'):
      item['id'] = d.id
      lista_documentos.append(item)

  return render_template('boveda.html', documentos=lista_documentos)


@app.route('/detalle_contrato/<id>')
def ver_contrato(id):
  if 'user' not in session:
    return redirect(url_for('login_page'))

  doc_ref = db.collection('Empleados').document(id).get()

  if doc_ref.exists:
    contrato = doc_ref.to_dict()
    return render_template('detalle_contrato.html', c=contrato, id=doc_ref.id)
  else:
    flash('Contrato no encontrado', 'danger')
    return redirect('/')


@app.route('/vincular_drive_pdf', methods=['POST'])
def vincular_drive_pdf():
  if 'user' not in session:
    return redirect(url_for('login_page'))

  emp_id = request.form.get('id')
  link_drive = request.form.get('link_pdf')

  if emp_id and link_drive:
    try:
      db.collection('Empleados').document(emp_id).update(
          {'url_contrato_pdf': link_drive}
      )
      flash('Enlace de Google Drive vinculado con éxito', 'success')
    except Exception as e:
      flash(f'Error al vincular: {e}', 'danger')

  return redirect(url_for('ver_contrato', id=emp_id))


@app.route('/gestionar_deposito/<e_id>/<p_id>/<accion>')
def gestionar_deposito(e_id, p_id, accion):
  if 'user' not in session:
    return redirect(url_for('login_page'))

  emp_ref = db.collection('Empleados').document(e_id)
  pago_ref = emp_ref.collection('Pagos').document(p_id)

  contrato = emp_ref.get().to_dict()
  pago = pago_ref.get().to_dict()

  deposito = safe_float(contrato.get('deposito', 0))
  mensualidad_base = (
      safe_float(contrato.get('internet'))
      + safe_float(contrato.get('agua'))
      + safe_float(contrato.get('luz'))
      + safe_float(contrato.get('canon'))
      + safe_float(contrato.get('equipo'))
  )

  if accion == 'toma':
    nuevo_mov = deposito
    nuevo_monto_total = mensualidad_base + deposito
    nota = f'Cobro de Depósito Inicial: C$ {deposito:,.2f}'
  elif accion == 'retorna':
    nuevo_mov = -deposito
    nuevo_monto_total = mensualidad_base - deposito
    nota = f'Retorno de Depósito Aplicado a Cuota: C$ {deposito:,.2f}'
  else:
    nuevo_mov = 0
    nuevo_monto_total = mensualidad_base
    nota = ''

  monto_pagado_ya = safe_float(pago.get('monto_pagado', 0.0))
  saldo_pendiente = nuevo_monto_total - monto_pagado_ya

  if saldo_pendiente <= 0:
    nuevo_estado = (
        'Cancelado' if monto_pagado_ya > 0 or accion == 'retorna' else 'Pendiente'
    )
  else:
    nuevo_estado = 'Parcial' if monto_pagado_ya > 0 else 'Pendiente'

  pago_ref.update({
      'monto': nuevo_monto_total,
      'deposito_movimiento': nuevo_mov,
      'saldo_pendiente': max(0.0, saldo_pendiente),
      'estado': nuevo_estado,
      'nota': nota,
  })

  flash('Movimiento de depósito sincronizado con éxito', 'success')
  return redirect(url_for('ver_pagos', id=e_id))


@app.route('/deshacer_deposito/<e_id>/<p_id>')
def deshacer_deposito(e_id, p_id):
  if 'user' not in session:
    return redirect(url_for('login_page'))

  pago_ref = (
      db.collection('Empleados')
      .document(e_id)
      .collection('Pagos')
      .document(p_id)
  )
  pago = pago_ref.get().to_dict()

  movimiento = safe_float(pago.get('deposito_movimiento', 0))
  monto_actual = safe_float(pago.get('monto'))

  nuevo_monto_total = monto_actual - movimiento
  monto_abonado_limpio = 0.0

  saldo_pendiente = nuevo_monto_total
  saldo_a_favor = 0.0
  nuevo_estado = 'Pendiente'

  pago_ref.update({
      'monto': nuevo_monto_total,
      'monto_pagado': monto_abonado_limpio,
      'deposito_movimiento': 0,
      'estado': nuevo_estado,
      'saldo_pendiente': saldo_pendiente,
      'saldo_a_favor': saldo_a_favor,
      'nota': '',
  })

  flash(
      'Movimiento de depósito deshecho. La cuota ha vuelto a su canon'
      ' original.',
      'secondary',
  )
  return redirect(url_for('ver_pagos', id=e_id))


@app.route('/ver_pagos/<id>')
def ver_pagos(id):
  if 'user' not in session:
    return redirect(url_for('login_page'))

  doc_ref = db.collection('Empleados').document(id)
  emp_doc = doc_ref.get()

  if not emp_doc.exists:
    return redirect(url_for('dashboard'))

  empleado = emp_doc.to_dict()
  empleado['id'] = id

  f_inicio = empleado.get('fecha_inicio', '1900-01-01')
  f_fin = empleado.get('fecha_fin', '2099-12-31')

  pagos_query = doc_ref.collection('Pagos').order_by('fecha_vencimiento').stream()

  pagos = []
  recaudado = 0.0
  pendiente = 0.0
  adelantos_totales = 0.0

  for p in pagos_query:
    p_data = p.to_dict()
    p_data['id'] = p.id
    fecha_v = p_data.get('fecha_vencimiento', '')

    if f_inicio <= fecha_v <= f_fin:
      monto_mes = safe_float(p_data.get('monto', 0))
      estado_pago = p_data.get('estado', 'Pendiente')

      if 'monto_pagado' not in p_data:
        p_data['monto_pagado'] = (
            monto_mes if estado_pago == 'Cancelado' else 0.0
        )
      if 'saldo_pendiente' not in p_data:
        p_data['saldo_pendiente'] = (
            monto_mes if estado_pago == 'Pendiente' else 0.0
        )
      if 'saldo_a_favor' not in p_data:
        p_data['saldo_a_favor'] = 0.0

      monto_pagado = safe_float(p_data.get('monto_pagado'))
      s_pend = safe_float(p_data.get('saldo_pendiente'))
      s_favor = safe_float(p_data.get('saldo_a_favor'))

      if estado_pago == 'Cancelado':
        recaudado += monto_pagado
        adelantos_totales += s_favor
      elif estado_pago == 'Parcial':
        recaudado += monto_pagado
        pendiente += s_pend
      elif estado_pago == 'Pendiente':
        pendiente += monto_mes

      pagos.append(p_data)

  return render_template(
      'pagos.html',
      pagos=pagos,
      empleado=empleado,
      id=id,
      total_recaudado=recaudado,
      total_pendiente=pendiente,
      total_adelantos=adelantos_totales,
  )


@app.route('/toggle_pago/<e_id>/<p_id>/<nuevo_estado>')
def toggle_pago(e_id, p_id, nuevo_estado):
  if 'user' not in session:
    return redirect(url_for('login_page'))

  pago_ref = (
      db.collection('Empleados')
      .document(e_id)
      .collection('Pagos')
      .document(p_id)
  )

  payload = {'estado': nuevo_estado}

  if nuevo_estado in ['Pendiente', 'Suspensión']:
    payload.update(
        {'monto_pagado': 0.0, 'saldo_a_favor': 0.0, 'saldo_pendiente': 0.0}
    )

  pago_ref.update(payload)

  flash(f'Estado actualizado a {nuevo_estado}', 'success')
  return redirect(url_for('ver_pagos', id=e_id))


@app.route('/suspend/<e_id>/<p_id>')
def suspend_payment(e_id, p_id):
  return toggle_pago(e_id, p_id, 'Suspensión')


@app.route('/delete/<id>')
def delete(id):
  if 'user' not in session:
    return redirect(url_for('login_page'))
  db.collection('Empleados').document(id).delete()
  flash('Registro eliminado', 'warning')
  return redirect(url_for('dashboard'))


@app.route('/propiedades')
def propiedades():
  if 'user' not in session:
    return redirect(url_for('login_page'))
  docs = db.collection('Empleados').stream()
  propiedades_dict = {}
  for doc in docs:
    item = doc.to_dict()
    item['id'] = doc.id
    dir_name = item.get('direccion', 'Sin Dirección')
    if dir_name not in propiedades_dict or item.get(
        'fecha', ''
    ) > propiedades_dict[dir_name].get('fecha', ''):
      propiedades_dict[dir_name] = item
  return render_template('propiedades.html', propiedades=propiedades_dict)


@app.route('/resumen_propiedades', methods=['GET', 'POST'])
def resumen_propiedades():
  if 'user' not in session:
    return redirect(url_for('login_page'))

  fecha_desde = request.form.get('desde', datetime.now().strftime('%Y-%m-01'))
  fecha_hasta = request.form.get('hasta', datetime.now().strftime('%Y-%m-%d'))
  direccion_sel = request.form.get('direccion', '')

  query_empleados = db.collection('Empleados')

  if direccion_sel and direccion_sel != '':
    docs_empleados = query_empleados.where(
        'direccion', '==', direccion_sel
    ).stream()
  else:
    docs_empleados = query_empleados.stream()

  resumen = []

  for emp in docs_empleados:
    e_data = emp.to_dict()
    e_id = emp.id

    pagos_query = (
        db.collection('Empleados')
        .document(e_id)
        .collection('Pagos')
        .where(filter=FieldFilter('fecha_vencimiento', '>=', fecha_desde))
        .where(filter=FieldFilter('fecha_vencimiento', '<=', fecha_hasta))
        .stream()
    )

    acumulado_propiedad = 0.0
    recaudado_propiedad = 0.0
    pendiente_propiedad = 0.0

    for p in pagos_query:
      p_data = p.to_dict()
      monto_base = safe_float(p_data.get('monto', 0))
      estado = p_data.get('estado', '').strip().lower()

      monto_pagado = safe_float(p_data.get('monto_pagado', 0.0))
      saldo_pendiente = safe_float(p_data.get('saldo_pendiente', 0.0))

      if 'monto_pagado' not in p_data:
        monto_pagado = monto_base if estado == 'cancelado' else 0.0
      if 'saldo_pendiente' not in p_data:
        saldo_pendiente = monto_base if estado == 'pendiente' else 0.0

      if estado == 'cancelado':
        recaudado_propiedad += monto_pagado
      elif estado == 'pendiente':
        pendiente_propiedad += monto_base
      elif estado == 'parcial':
        recaudado_propiedad += monto_pagado
        pendiente_propiedad += saldo_pendiente

      acumulado_propiedad += (
          (monto_pagado + saldo_pendiente)
          if estado == 'parcial'
          else monto_base
      )

    if acumulado_propiedad > 0:
      resumen.append({
          'direccion': e_data.get('direccion', 'Sin Dirección'),
          'inquilino': (
              f"{e_data.get('nombre', '')} {e_data.get('apellido', '')}"
          ),
          'total': acumulado_propiedad,
          'recaudado': recaudado_propiedad,
          'pendiente': pendiente_propiedad,
      })

  return render_template(
      'resumen_acumulado.html',
      resumen=resumen,
      desde=fecha_desde,
      hasta=fecha_hasta,
      direccion_sel=direccion_sel,
  )


VERSION = '1.2.0'


@app.context_processor
def inject_version():
  return dict(app_version=VERSION)


def enviar_whatsapp_consolidado(empleado, detalles_pagos, monto_total):
  if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    print('❌ Error: Credenciales de Twilio no configuradas en entorno.')
    return False

  client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

  bloque_detalles = '\n'.join(detalles_pagos)

  mensaje = (
      '📋 *ESTADO DE CUENTA CONSOLIDADO* 📋\n\n'
      f"Hola *{empleado.get('nombre', '')} {empleado.get('apellido', '')}*,\n"
      'Te saluda Osman Meléndez para recordarte los saldos pendientes'
      f" asociados a tu contrato *{empleado.get('direccion', 'N/A')}*:\n\n"
      f'{bloque_detalles}\n\n'
      f'💰 *TOTAL NETO PENDIENTE*: *C$ {monto_total:,.2f}*\n\n'
      'Por favor omitir este mensaje si ya has realizado tu depósito o'
      ' transferencia correspondiente. ¡Muchas gracias!'
  )

  nom_cli = f"{empleado.get('nombre', '')} {empleado.get('apellido', '')}"

  try:
    message = client.messages.create(
        from_=TWILIO_WHATSAPP_NUMBER, body=mensaje, to='whatsapp:+50589475863'
    )

    print(
        f'✅ WhatsApp Consolidado enviado a {nom_cli} | SID: {message.sid}'
    )
    flash(f'Notificación de cobro enviada con éxito a {nom_cli}.', 'success')
    return True

  except Exception as error_twilio:
    print(f'❌ Error en Twilio API para {nom_cli}: {error_twilio}')
    flash(
        f'Se procesó el cobro de {nom_cli}, pero falló el envío de WhatsApp:'
        f' {error_twilio}',
        'warning',
    )
    return False


@app.route('/ejecutar_envio_automatico_secreto_123')
def ejecutar_envio_automatico():
  diagnostico = []
  mensajes_enviados = 0
  errores_totales = 0

  try:
    zona_ni = timezone(timedelta(hours=-6))
    fecha_actual = datetime.now(zona_ni)
    hoy_str = fecha_actual.strftime('%Y-%m-%d')

    config_ref = db.collection('Configuracion_Cron').document('control_envios')
    config_doc = config_ref.get()

    if config_doc.exists:
      ultima_fecha_envio = config_doc.to_dict().get(
          'ultima_fecha_exitosa', ''
      )
      if ultima_fecha_envio == hoy_str:
        return (
            jsonify({
                'status': 'skipped',
                'mensaje': (
                    f'Los recordatorios del día de hoy ({hoy_str}) ya fueron'
                    ' enviados en un ciclo previo.'
                ),
                'fecha_servidor_managua': hoy_str,
            }),
            200,
        )

    inquilinos = db.collection('Empleados').stream()
    total_inquilinos = 0
    total_pagos_revisados = 0
    hubo_envios_hoy = False

    for doc in inquilinos:
      total_inquilinos += 1
      empleado = doc.to_dict()
      e_id = doc.id
      nombre_completo = (
          f"{empleado.get('nombre', '')} {empleado.get('apellido', '')}"
      )

      detalles_pagos_inquilino = []
      monto_total_inquilino = 0.0

      pagos_query = (
          db.collection('Empleados').document(e_id).collection('Pagos').stream()
      )

      for p in pagos_query:
        total_pagos_revisados += 1
        pago = p.to_dict()
        estado_pago = pago.get('estado', '').strip().lower()
        fecha_venc_str = pago.get('fecha_vencimiento', '')

        if (
            estado_pago == 'pendiente' or estado_pago == 'parcial'
        ) and fecha_venc_str:
          if fecha_venc_str <= hoy_str:
            try:
              deuda_recibo = (
                  float(pago.get('saldo_pendiente', 0.0))
                  if estado_pago == 'parcial'
                  else float(pago.get('monto', 0.0))
              )
            except (ValueError, TypeError):
              deuda_recibo = 0.0

            if deuda_recibo <= 0:
              continue

            monto_total_inquilino += deuda_recibo
            tipo_deuda = (
                'Pendiente' if estado_pago == 'pendiente' else 'Saldo Parcial'
            )
            linea_detalle = (
                f"▪️ *Mes*: {pago.get('mes_anio', 'N/A')} | *{tipo_deuda}*: C$"
                f' {deuda_recibo:,.2f} (Venció: {fecha_venc_str})'
            )
            detalles_pagos_inquilino.append(linea_detalle)

      if len(detalles_pagos_inquilino) > 0:
        exito = enviar_whatsapp_consolidado(
            empleado, detalles_pagos_inquilino, monto_total_inquilino
        )

        if exito:
          mensajes_enviados += 1
          hubo_envios_hoy = True
          diagnostico.append(
              f'✅ Consolidado Enviado: {nombre_completo} (C$'
              f' {monto_total_inquilino:,.2f})'
          )
        else:
          errores_totales += 1
          diagnostico.append(
              f'❌ Falló el envío en Twilio para {nombre_completo}'
          )

    if hubo_envios_hoy:
      config_ref.set({'ultima_fecha_exitosa': hoy_str}, merge=True)
      diagnostico.append(
          f'💾 Control guardado en Firebase para el día {hoy_str}.'
      )

    status_respuesta = (
        'success'
        if errores_totales == 0 and mensajes_enviados > 0
        else 'warning_with_errors'
    )

    return (
        jsonify({
            'status': status_respuesta,
            'mensaje': (
                f'Escaneo completado. Exitosos: {mensajes_enviados} | Fallidos'
                f' por Twilio: {errores_totales}'
            ),
            'fecha_servidor_managua': hoy_str,
            'total_inquilinos_escaneados': total_inquilinos,
            'mensajes_enviados_con_exito': mensajes_enviados,
            'envios_fallidos': errores_totales,
            'detalles_del_proceso': diagnostico,
        }),
        200 if status_respuesta == 'success' else 400,
    )

  except Exception as e:
    return jsonify({'status': 'error', 'detalle': str(e)}), 500


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
      flash('El pago no existe.', 'danger')
      return redirect(url_for('ver_pagos', id=e_id))

    pago_data = pago_doc.to_dict()
    monto_total_mes = safe_float(pago_data.get('monto', 0.0))

    comprobante_url = pago_data.get('comprobante_url', None)

    if 'comprobante' in request.files:
      file = request.files['comprobante']
      if file and file.filename != '':
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        nombre_archivo = f'recibo_{p_id}_{timestamp}_{filename}'

        upload_folder = app.config['UPLOAD_FOLDER']
        os.makedirs(upload_folder, exist_ok=True)

        filepath = os.path.join(upload_folder, nombre_archivo)
        file.save(filepath)
        comprobante_url = f'/static/uploads/{nombre_archivo}'

    saldo_pendiente = 0.0
    saldo_a_favor = 0.0
    nuevo_estado = 'Cancelado'

    if monto_abonado < monto_total_mes:
      saldo_pendiente = monto_total_mes - monto_abonado
      nuevo_estado = 'Parcial' if monto_abonado > 0 else 'Pendiente'
    elif monto_abonado > monto_total_mes:
      saldo_a_favor = monto_abonado - monto_total_mes
      nuevo_estado = 'Cancelado'

    if nota_opcional:
      texto_nota = f'Abono C$ {monto_abonado:,.2f} - {nota_opcional}'
    else:
      texto_nota = (
          f'Abono C$ {monto_abonado:,.2f}' if nuevo_estado != 'Cancelado' else ''
      )

    update_payload = {
        'estado': nuevo_estado,
        'monto_pagado': monto_abonado,
        'saldo_pendiente': saldo_pendiente,
        'saldo_a_favor': saldo_a_favor,
        'nota': texto_nota,
    }

    if comprobante_url:
      update_payload['comprobante_url'] = comprobante_url

    pago_ref.update(update_payload)

    flash('Abono grabado con éxito.', 'success')

  except Exception as e:
    flash(f'Error al procesar el abono: {e}', 'danger')

  return redirect(url_for('ver_pagos', id=e_id))


@app.route('/encuesta/<id>')
def abrir_encuesta(id):
  if not db:
    return "Error: No hay conexión con la base de datos."

  doc_ref = db.collection('Empleados').document(id)
  doc = doc_ref.get()
  if not doc.exists:
    flash('Inquilino no encontrado', 'danger')
    return redirect(url_for('login_page'))

  emp = doc.to_dict()
  emp['id'] = doc.id
  return render_template('encuesta.html', emp=emp)


@app.route('/guardar_encuesta', methods=['POST'])
def guardar_encuesta():
  if not db:
    return "Error: No hay conexión con la base de datos."

  datos_encuesta = {
      'inquilino_id': request.form.get('inquilino_id'),
      'nombre_inquilino': request.form.get('nombre_inquilino'),
      'propiedad': request.form.get('propiedad'),
      'fecha_respuesta': datetime.now(
          ZoneInfo('America/Managua')
      ).strftime('%Y-%m-%d %H:%M:%S'),
  }

  for i in range(1, 11):
    datos_encuesta[f'p{i}'] = request.form.get(f'p{i}')
    datos_encuesta[f'comentario_p{i}'] = request.form.get(
        f'comentario_p{i}', ''
    ).strip()

  campos_foto = ['foto_p1', 'foto_p2', 'foto_p4', 'foto_p6']
  for campo in campos_foto:
    if campo in request.files:
      file = request.files[campo]
      if file and file.filename != '':
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S_')
        filename_final = timestamp + filename

        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename_final)
        file.save(filepath)
        datos_encuesta[campo] = '/' + filepath
      else:
        datos_encuesta[campo] = None
    else:
      datos_encuesta[campo] = None

  db.collection('encuestas_respuestas').add(datos_encuesta)

  return (
      '<h3>Encuesta Enviada Exitosamente. Muchas gracias por tu apoyo. Puedes'
      ' cerrar esta ventana.</h3>'
  )


@app.route('/resumen_encuestas')
def resumen_encuestas():
  if 'user' not in session:
    return redirect(url_for('login_page'))
  if not db:
    return "Error: No hay conexión con la base de datos."

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
    docs = db.collection('Empleados').stream()

    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
      flash('Error: Faltan credenciales de Twilio.', 'danger')
      return redirect(url_for('dashboard'))

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    contador_envios = 0
    errores = []

    for doc in docs:
      emp = doc.to_dict()
      emp['id'] = doc.id

      telefono = emp.get('telefono')

      encuesta_activa = (
          emp.get('enviar_encuesta_inquilino') == True
          or emp.get('enviar_encuesta_inquilino') == 'true'
      )

      if telefono and encuesta_activa:
        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
        if scheme == 'http' and not app.debug:
          scheme = 'https'

        host = request.host
        link_encuesta = f"{scheme}://{host}/encuesta/{emp['id']}"
        mensaje = (
            '📋 *ENCUESTA DE CONTROL INTERNO* 📋\n\n'
            f"Hola *{emp.get('nombre', '')} {emp.get('apellido', '')}"
            f" {emp.get('cedula', '')}*,\n"
            'Te Saluda Osman Meléndez y para compartir el enlace de la'
            f" encuesta de control para propiedad {emp.get('propiedad', '')}*,\n"
            f"ubicada en: *{emp.get('direccion', 'N/A')}*.\n\n"
            'Agradecemos enormemente tu valioso apoyo completándola a través'
            ' del siguiente link:\n'
            f'🔗 {link_encuesta}\n\n'
            '¡Muchas gracias por tu colaboración!'
        )

        try:
          client.messages.create(
              from_=TWILIO_WHATSAPP_NUMBER,
              body=mensaje,
              to='whatsapp:+50589475863',
          )
          contador_envios += 1
          time.sleep(2)

        except Exception as error_twilio:
          nom_cli = f"{emp.get('nombre', '')} {emp.get('apellido', '')}"
          print(f'❌ Error enviando a {nom_cli}: {error_twilio}')
          errores.append(f'{nom_cli} ({error_twilio})')

    if errores:
      flash(
          f"Se enviaron {contador_envios} encuestas. Hubo fallas con:"
          f" {', '.join(errores)}",
          'warning',
      )
    else:
      flash(
          f'Proceso completado. Se enviaron {contador_envios} encuestas vía'
          ' WhatsApp con éxito.',
          'success',
      )

    return redirect(url_for('dashboard'))

  except Exception as e:
    flash(f'Error crítico en el servidor: {str(e)}', 'danger')
    return redirect(url_for('dashboard'))


@app.route('/imprimir_encuesta/<encuesta_id>')
def imprimir_encuesta_individual(encuesta_id):
  if not db:
    return "Error: No hay conexión con la base de datos."

  doc_ref = db.collection('encuestas_respuestas').document(encuesta_id)
  doc = doc_ref.get()

  if not doc.exists:
    return "<h3>Error: La encuesta solicitada no existe.</h3>", 404

  encuesta_data = doc.to_dict()
  return render_template('imprimir_encuesta.html', enc=encuesta_data)


# ---------------------------------------------------------
# CLIENTE TWILIO PARA TAREAS PROGRAMADAS
# ---------------------------------------------------------
twilio_client = (
    Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)
    else None
)


def procesar_mensajes_programados():
  if not twilio_client:
    print('⚠️ Cliente Twilio no configurado. Omitiendo tarea programada.')
    return

  with app.app_context():
    ahora = datetime.now()
    ahora_str = ahora.strftime('%Y-%m-%dT%H:%M')

    docs = (
        db.collection('MensajesProgramados')
        .where('estado', '==', 'Pendiente')
        .stream()
    )

    for doc in docs:
      data = doc.to_dict()
      fecha_proxima = data.get('fecha_hora_inicio', '')
      fecha_limite_str = data.get('fecha_fin', '')

      if fecha_proxima and fecha_proxima <= ahora_str:
        telefonos = [data.get('telefono_1'), data.get('telefono_2')]
        canal = data.get('canal', 'whatsapp')
        mensaje_texto = data.get('mensaje', '')

        for tel in telefonos:
          if not tel:
            continue
          try:
            if canal == 'whatsapp':
              destinatario = (
                  tel if tel.startswith('whatsapp:') else f'whatsapp:{tel}'
              )
              twilio_client.messages.create(
                  body=mensaje_texto,
                  from_=TWILIO_WHATSAPP_NUMBER,
                  to=destinatario,
              )
            else:
              twilio_client.messages.create(
                  body=mensaje_texto, from_=TWILIO_SMS_NUMBER, to=tel
              )
            print(f'✅ Mensaje diario enviado a {tel}')
          except Exception as e:
            print(f'❌ Error al enviar a {tel}: {e}')

        dt_actual = datetime.strptime(fecha_proxima, '%Y-%m-%dT%H:%M')
        dt_siguiente = dt_actual + timedelta(days=1)
        siguiente_fecha_str = dt_siguiente.strftime('%Y-%m-%dT%H:%M')

        dt_limite = datetime.strptime(
            f'{fecha_limite_str}T23:59', '%Y-%m-%dT%H:%M'
        )

        if dt_siguiente <= dt_limite:
          db.collection('MensajesProgramados').document(doc.id).update({
              'fecha_hora_inicio': siguiente_fecha_str,
              'estado': 'Pendiente',
          })
          print(
              '🔄 Mensaje programado para el día siguiente:'
              f' {siguiente_fecha_str}'
          )
        else:
          db.collection('MensajesProgramados').document(doc.id).update(
              {'estado': 'Completado'}
          )
          print('🏁 Rango de fechas finalizado. Marcado como Completado.')

@app.route('/mensajes')
def vista_mensajes():
  if 'user' not in session:
    return redirect(url_for('login_page'))

  programaciones_ref = db.collection('MensajesProgramados').stream()
  programaciones = [
      dict(doc.to_dict(), id=doc.id) for doc in programaciones_ref
  ]
  return render_template('mensajes.html', programaciones=programaciones)

@app.route('/guardar_programacion_mensaje', methods=['POST'])
def guardar_programacion_mensaje():
  if 'user' not in session:
    return redirect(url_for('login_page'))

  payload = {
      'telefono_1': request.form.get('telefono_1', '').strip(),
      'telefono_2': request.form.get('telefono_2', '').strip(),
      'fecha_hora_inicio': request.form.get('fecha_hora_inicio'),
      'fecha_fin': request.form.get('fecha_fin'),
      'canal': request.form.get('canal'),
      'mensaje': request.form.get('mensaje'),
      'estado': 'Pendiente',
  }

  db.collection('MensajesProgramados').add(payload)
  flash('Programación guardada exitosamente.', 'success')
  return redirect(url_for('vista_mensajes'))

@app.route('/eliminar_programacion/<id_prog>')
def eliminar_programacion(id_prog):
  if 'user' not in session:
    return redirect(url_for('login_page'))

  db.collection('MensajesProgramados').document(id_prog).delete()
  flash('Programación eliminada.', 'info')
  return redirect(url_for('vista_mensajes'))

# --- TRADING ---


DICCIONARIO_NOMBRES = {
    'GC=F': 'Oro (Refugio)',
    'SI=F': 'Plata (Refugio)',
    'HG=F': 'Cobre',
    'CL=F': 'Petroleo WTI',
    'ES=F': 'S&P 500',
    'BTC-USD': 'BITCOIN',
}



# --- INDICADORES TÉCNICOS OPTIMIZADOS ---
def calcular_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calcular_adx(df, period=14):
    """Calcula el ADX utilizando pandas_ta_classic de forma nativa y robusta."""
    try:
        adx_df = ta.adx(df['High'], df['Low'], df['Close'], length=period)
        if adx_df is not None and not adx_df.empty:
            col_adx = [col for col in adx_df.columns if col.startswith('ADX_')][0]
            return adx_df[col_adx]
    except Exception as e:
        print(f"⚠️ Error calculando ADX con pandas_ta: {e}")
    return None

# --- MÓDULO DE ENVÍO WHATSAPP VÍA TWILIO (REST API HTTPS PUERTO 443) ---
def enviar_whatsapp_twilio(mensaje):
    account_sid = os.getenv('TWILIO_ACCOUNT_SID')
    auth_token = os.getenv('TWILIO_AUTH_TOKEN')
    from_whatsapp = os.getenv('TWILIO_WHATSAPP_NUMBER')  # ej: whatsapp:+14155238886
    to_whatsapp = os.getenv('MI_WHATSAPP_NUMBER')        # ej: whatsapp:+505XXXXXXXX

    if not account_sid or not auth_token or not from_whatsapp or not to_whatsapp:
        print("❌ Error: Variables de entorno de Twilio/WhatsApp no configuradas en el entorno.")
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = {
        'From': from_whatsapp,
        'To': to_whatsapp,
        'Body': mensaje
    }

    try:
        response = requests.post(url, data=payload, auth=(account_sid, auth_token), timeout=12)
        if response.status_code in [200, 201]:
            print("✅ Alerta unificada de WhatsApp enviada exitosamente vía Twilio.")
            return True
        else:
            print(f"❌ Error enviando WhatsApp ({response.status_code}): {response.text}")
            return False
    except Exception as e:
        print(f"❌ Error de conexión al enviar WhatsApp: {e}")
        return False

# --- FUNCIÓN DE ANÁLISIS INDIVIDUAL POR TICKER ---
def analizar_ticker_individual(ticker, intervalo, periodo, puntos_min):
  nombre = DICCIONARIO_NOMBRES.get(ticker, ticker)
  try:
    df = yf.download(
        ticker, period=periodo, interval=intervalo, progress=False
    )
    if df.empty or len(df) < 14:
      return None

    if isinstance(df.columns, list) or getattr(df.columns, 'nlevels', 1) > 1:
      df.columns = [
          col[0] if isinstance(col, tuple) else col for col in df.columns
      ]

    df['ADX'] = calcular_adx(df)
    df['RSI'] = calcular_rsi(df['Close'])

    ultima_vela = df.iloc[-1]
    vela_anterior = df.iloc[-2]

    precio_actual = round(float(ultima_vela['Close']), 2)
    precio_previo = round(float(vela_anterior['Close']), 2)

    variacion_pct = round(
        ((precio_actual - precio_previo) / precio_previo) * 100, 2
    )
    eje_var = f'+{variacion_pct}%' if variacion_pct >= 0 else f'{variacion_pct}%'

    adx_val = (
        round(float(ultima_vela['ADX']), 2)
        if 'ADX' in df.columns and not pd.isna(ultima_vela['ADX'])
        else 0.0
    )
    rsi_val = (
        round(float(ultima_vela['RSI']), 2)
        if 'RSI' in df.columns and not pd.isna(ultima_vela['RSI'])
        else 50.0
    )

    min_reciente = round(float(df['Low'].min()), 2)
    max_reciente = round(float(df['High'].max()), 2)

    puntos = 0
    accion = 'WAIT'

    # --- 1. EVALUACIÓN DE FUERZA (ADX) ---
    if adx_val >= 25:
      puntos += 3
    elif adx_val >= 15:
      puntos += 1

    # --- 2. RANGOS DELIMITADOS DE RSI ---
    if 10.0 <= rsi_val <= 20.0:
      puntos += 3
      accion = '🟢 COMPRA (Zona Clave 10-20)'
    elif 60.0 <= rsi_val <= 70.0:
      puntos += 3
      accion = '🔴 VENTA (Zona Clave 60-70)'
    else:
      # Si el RSI está fuera de las ventanas útiles (<10, 20-60, o >70)
      # se asigna 0 puntos adicionales para descartar la oportunidad.
      puntos += 0

    return {
        'ticker': ticker,
        'nombre': nombre,
        'precio': precio_actual,
        'variacion': eje_var,
        'adx_valor': adx_val,
        'rsi_valor': rsi_val,
        'min_reciente': min_reciente,
        'max_reciente': max_reciente,
        'puntos': puntos,
        'accion': accion,
    }
  except Exception as e:
    print(f'⚠️ Error procesando datos para {ticker}: {e}')
    return None
    
# --- PROCESAMIENTO DE LOTE CON AUDITORÍA EN TERMINAL Y ENVÍO ÚNICO ---
def procesar_lote_trading(instrumentos, intervalo, periodo, puntos_min, destino=None):
    if isinstance(instrumentos, str):
        instrumentos = [instrumentos]

    reporte_lineas = []

    print(f"\n🔍 --- INICIANDO ESCANEO DE {len(instrumentos)} INSTRUMENTOS ---")
    print(f"⚙️ Filtros: Intervalo={intervalo} | Puntos Mínimos Requeridos={puntos_min}")

    for ticker in instrumentos:
        orden = analizar_ticker_individual(ticker, intervalo, periodo, puntos_min)

        if orden:
            puntos_obtenidos = orden['puntos']
            adx_val = orden['adx_valor']
            accion = orden['accion']

            # 📊 MOSTRAR EN CONSOLA / TERMINAL
            print(f"🔹 [{ticker}] {orden['nombre']} | ADX: {adx_val} | RSI: {orden['rsi_valor']} | Puntos: {puntos_obtenidos}/{puntos_min} | Acción: {accion}")

            # Agregar solo si cumple el filtro
            if puntos_obtenidos >= puntos_min:
                print(f"   👉 ¡CUMPLE CRITERIO! Agregado a la lista de notificación.")
                linea = (
                    f"🚀 *{orden['nombre']}* (`{orden['ticker']}`)\n"
                    f"   • *Acción:* {orden['accion']}\n"
                    f"   • *Precio:* ${orden['precio']} ({orden['variacion']})\n"
                    f"   • *ADX:* {orden['adx_valor']} 🔥 | *RSI:* {orden['rsi_valor']}\n"
                    f"   • *Rango:* Mín ${orden['min_reciente']} | Máx ${orden['max_reciente']}\n"
                    f"   • *Puntuación:* {puntos_obtenidos}/{puntos_min}"
                )
                reporte_lineas.append(linea)
            else:
                print(f"   ❌ Omitido: Puntuación insuficiente ({puntos_obtenidos}/{puntos_min}).")
        else:
            print(f"⚠️ [{ticker}] No se pudieron calcular indicadores.")

    print("--------------------------------------------------")

    # ENVÍO CONSOLIDADO A WHATSAPP
    if reporte_lineas:
        print(f"🚀 Enviando mensaje unificado con {len(reporte_lineas)} oportunidad(es) a WhatsApp...")
        encabezado = "📊 *REPORTE CONSOLIDADO TRADING BOT*\n"
        encabezado += "-----------------------------------------\n\n"
        mensaje_unificado = encabezado + "\n\n".join(reporte_lineas)
        enviar_whatsapp_twilio(mensaje_unificado)
    else:
        print("ℹ️ Escaneo completado: Ningún instrumento alcanzó los puntos mínimos para enviar por WhatsApp.\n")

# --- RUTAS FLASK ---
@app.route('/ejecutar_escaner_trading', methods=['GET', 'POST'])
def ejecutar_escaner_trading():
    if 'user' not in session:
        return redirect(url_for('login_page'))

    seleccionados = request.form.getlist('instrumentos')
    intervalo = request.form.get('intervalo', '1h')
    puntos_min = int(request.form.get('puntos_maximos', 6))

    if not seleccionados:
        flash('Revisa la selección de instrumentos.', 'warning')
        return redirect(url_for('vista_trading'))

    periodo_map = {'5m': '5d', '15m': '1mo', '1h': '5d', '1d': '2y'}
    periodo = periodo_map.get(intervalo, '5d')

    threading.Thread(
        target=procesar_lote_trading,
        args=(seleccionados, intervalo, periodo, puntos_min),
        daemon=True
    ).start()

    flash('🚀 Escaneo iniciado en segundo plano. Recibirás un resumen consolidado por WhatsApp.', 'success')
    return redirect(url_for('vista_trading'))

@app.route('/trading')
def vista_trading():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    return render_template('trading.html', instrumentos=DICCIONARIO_NOMBRES)

@app.route('/ejecutar_escaner_directo')
def ejecutar_escaner_directo():
    instrumentos = list(DICCIONARIO_NOMBRES.keys())

    threading.Thread(
        target=procesar_lote_trading,
        args=(instrumentos, '5m', '1d', 6), # Bajado temporalmente a 6 puntos para pruebas
        daemon=True
    ).start()

    return jsonify({
        'status': 'success',
        'mensaje': '🚀 Escaneo manual iniciado. Revisa la terminal para ver el desglose punto por punto.',
        'instrumentos': instrumentos
    }), 200

# --- TAREAS PROGRAMADAS (SCHEDULER) ---
ALERTAS_ENVIADAS_CACHE = {}

def tarea_escaneo_automatico_trading():
    with app.app_context():
        zona_ni = ZoneInfo('America/Managua')
        ahora_dt = datetime.now(zona_ni)
        
        intervalo, periodo, puntos_min = '5m', '1d', 6
        instrumentos_a_procesar = []

        for ticker in DICCIONARIO_NOMBRES.keys():
            ultimo_envio = ALERTAS_ENVIADAS_CACHE.get(ticker)
            if not ultimo_envio or (ahora_dt - ultimo_envio).total_seconds() >= 1800:
                instrumentos_a_procesar.append(ticker)
                ALERTAS_ENVIADAS_CACHE[ticker] = ahora_dt

        if instrumentos_a_procesar:
            procesar_lote_trading(instrumentos_a_procesar, intervalo, periodo, puntos_min)

def enviar_reporte_estado_trading():
    with app.app_context():
        zona_ni = ZoneInfo('America/Managua')
        hora_fmt = datetime.now(zona_ni).strftime('%I:%M %p (%d/%m)')
        
        mensaje = (
            f"🟢 *TRADING BOT OPERATIVO EN RENDER*\n\n"
            f"📍 *Hora (Nicaragua):* {hora_fmt}\n"
            f"El script `Trading_ADX_Pro_V3` está activo y escaneando el mercado."
        )
        enviar_whatsapp_twilio(mensaje)

# --- INICIALIZACIÓN DE BACKGROUND SCHEDULER ---
executors = {'default': ThreadPoolExecutor(max_workers=10)}
job_defaults = {'coalesce': True, 'max_instances': 1}

trading_scheduler = BackgroundScheduler(
    executors=executors,
    job_defaults=job_defaults,
    timezone=ZoneInfo('America/Managua'),
    daemon=True
)

def inicializar_scheduler():
    if not trading_scheduler.running:
        trading_scheduler.add_job(
            func=procesar_mensajes_programados,
            trigger='interval',
            seconds=60,
            id='job_mensajes_programados',
            replace_existing=True
        )

        trading_scheduler.add_job(
            func=tarea_escaneo_automatico_trading,
            trigger='interval',
            minutes=30,
            id='job_trading_automatico',
            replace_existing=True
        )

        trading_scheduler.add_job(
            func=enviar_reporte_estado_trading,
            trigger='cron',
            hour='6-18',
            minute=00,
            timezone=ZoneInfo('America/Managua'),
            id='job_status_heartbeat',
            replace_existing=True
        )

        trading_scheduler.start()
        print('🚀 Scheduler unificado iniciado con éxito.')

inicializar_scheduler()

if __name__ == '__main__':
  app.run(host='0.0.0.0', port=5000)