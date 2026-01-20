from flask import Flask, render_template, request, redirect, url_for, session, send_file, jsonify
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
import pandas as pd
import io 
import unicodedata
from werkzeug.utils import secure_filename
from datetime import datetime
from google.cloud.firestore_v1.base_query import FieldFilter
from openpyxl.styles import Font, Alignment, PatternFill

# ==========================================
# 1. CONFIGURACIÓN DE FIREBASE
# ==========================================
firebase_json = os.environ.get('FIREBASE_JSON')
if firebase_json:
    cred_dict = json.loads(firebase_json)
    cred = credentials.Certificate(cred_dict)
else:
    cred = credentials.Certificate("serviceAccountKey.json")

if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ==========================================
# 2. CONFIGURACIÓN DE FLASK
# ==========================================
app = Flask(__name__)
app.secret_key = 'tu_llave_secreta_segura'
app.config['UPLOAD_FOLDER'] = 'static/uploads'

if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# --- Función auxiliar para normalizar texto (quitar acentos) ---
def normalizar(texto):
    if not texto: return ""
    return "".join(c for c in unicodedata.normalize('NFD', str(texto)) 
                   if unicodedata.category(c) != 'Mn').lower().strip()

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
    
    # Parámetros de orden y búsqueda
    sort_by = request.args.get('sort', 'fecha')
    search_query = request.args.get('search', '').strip().lower()
    
    direction = firestore.Query.DESCENDING if 'fecha' in sort_by or 'pago' in sort_by else firestore.Query.ASCENDING
    field = sort_by.split(' ')[0] 

    try:
        docs = db.collection('Empleados').order_by(field, direction=direction).stream()
        datos = []
        
        for doc in docs:
            item = doc.to_dict()
            item['id'] = doc.id
            
            # Lógica de búsqueda global
            if search_query:
                # Concatenamos campos para buscar en todos a la vez
                full_text = f"{item.get('nombre','')} {item.get('cedula','')} {item.get('num_contrato','')} {item.get('direccion','')}".lower()
                if search_query in full_text:
                    datos.append(item)
            else:
                datos.append(item)
                
        return render_template('index.html', empleados=datos, search_query=search_query)
    except Exception as e:
        return f"Error de Firebase: {e}"

@app.route('/add', methods=['POST'])
def add():
    if 'user' not in session: return redirect(url_for('login'))
    d = request.form
    cedula_raw = d.get('cedula', '').strip()
    cedula_final = cedula_raw if cedula_raw and cedula_raw != '0' else "Sin Ingresar"
    
    nuevo_empleado = {
        'fecha': d.get('fecha'),
        'nombre': d.get('nombre', '').strip(),
        'apellido': d.get('apellido', ''),
        'cedula': cedula_final,
        'num_contrato': d.get('num_contrato', ''),
        'direccion': d.get('direccion', 'N/A'),
        'pago': float(d.get('pago') or 0.0),
        'equipo': float(d.get('equipo') or 0.0),
        'deposito': float(d.get('deposito') or 0.0),
        'internet': float(d.get('internet') or 0.0),
        'sexo': 'M', 'ciudad': 'Chinandega'
    }

    try:
        if cedula_final != "Sin Ingresar":
            query = db.collection('Empleados').where(filter=FieldFilter('cedula', '==', cedula_final)).limit(1).get()
            if query:
                db.collection('Empleados').document(query[0].id).update(nuevo_empleado)
                return redirect(url_for('dashboard'))
        
        db.collection('Empleados').add(nuevo_empleado)
    except Exception as e:
        return f"Error al guardar: {e}"

    return redirect(url_for('dashboard'))

@app.route('/upload_masivo', methods=['POST'])
def upload_masivo():
    if 'user' not in session: return redirect(url_for('login'))
    file = request.files.get('archivo')
    if not file: return redirect(url_for('dashboard'))

    try:
        df = pd.read_excel(file) if file.filename.endswith('.xlsx') else pd.read_csv(file)
        df.columns = [str(c).strip() for c in df.columns]
        df = df.fillna(0)

        for _, row in df.iterrows():
            cedula_raw = str(row.get('Cédula', row.get('cedula', ''))).strip()
            cedula_final = "Sin Ingresar" if cedula_raw in ['0', '0.0', 'nan', 'NaN', ''] else cedula_raw

            direccion = str(row.get('Dirección', row.get('direccion', row.get('Lugar', 'General')))).strip()
            # Limpiar contrato de decimales (ej: 145.0 -> 145)
            contrato_raw = str(row.get('Contrato', row.get('num_contrato', row.get('Contrato ', 'S/N')))).strip()
            contrato = contrato_raw.split('.')[0]
            
            nombre = str(row.get('Nombre', row.get('nombre_empleado', 'Cliente'))).strip()
            apellido = str(row.get('apellido_empleado', f"Zona {direccion}")).strip()

            datos_pago = {
                'fecha': str(row.get('Fecha', row.get('fecha', datetime.now().strftime('%Y-%m-%d')))),
                'nombre': nombre,
                'apellido': apellido,
                'cedula': cedula_final,
                'num_contrato': contrato,
                'direccion': direccion,
                'internet': float(row.get('Internet', row.get('internet', 0))),
                'pago': float(row.get('Pago', row.get('pago', 0))),
                'equipo': float(row.get('Equipo', row.get('equipo', row.get('Alquiler Equipos', 0)))),
                'deposito': float(row.get('Depósito', row.get('Deposito', row.get('deposito', 0)))),
                'sexo': 'M', 'ciudad': 'Chinandega'
            }

            ref = db.collection('Empleados')
            # Evitar duplicados por contrato y fecha
            query = ref.where(filter=FieldFilter('num_contrato', '==', contrato))\
                       .where(filter=FieldFilter('fecha', '==', datos_pago['fecha'])).limit(1).get()
            
            if query:
                ref.document(query[0].id).update(datos_pago)
            else:
                ref.add(datos_pago)

        return redirect(url_for('dashboard'))
    except Exception as e:
        return f"Error procesando la subida masiva: {e}"

@app.route('/reporte', methods=['GET', 'POST'])
def reporte():
    if 'user' not in session: return redirect(url_for('login'))
    
    # Se incluye Invercasa SAFI
    direcciones = ["Chinandega", "Miguel Jarquín", "Módulo 1", "Módulo 2", "Invercasa SAFI"]
    anios = ["2024", "2025", "2026"]
    
    empleados = []
    totales = {'pago': 0.0, 'equipo': 0.0, 'deposito': 0.0, 'internet': 0.0}
    
    seleccionada = request.form.get('direccion')
    anio_sel = request.form.get('anio')
    contrato_filtro = request.form.get('num_contrato', '').strip()

    if request.method == 'POST':
        try:
            docs = db.collection('Empleados').stream()
            sel_norm = normalizar(seleccionada)

            for doc in docs:
                emp = doc.to_dict()
                dir_db_norm = normalizar(emp.get('direccion', ''))
                fecha_db = str(emp.get('fecha', ''))
                num_con_db = str(emp.get('num_contrato', '')).strip()
                
                cumple_direccion = not seleccionada or sel_norm in dir_db_norm
                cumple_anio = not anio_sel or anio_sel in fecha_db
                cumple_contrato = not contrato_filtro or contrato_filtro.lower() == num_con_db.lower()
                
                if cumple_direccion and cumple_anio and cumple_contrato:
                    emp['id'] = doc.id
                    empleados.append(emp)
                    totales['pago'] += float(emp.get('pago') or 0)
                    totales['equipo'] += float(emp.get('equipo') or 0)
                    totales['deposito'] += float(emp.get('deposito') or 0)
                    totales['internet'] += float(emp.get('internet') or 0)
                    
        except Exception as e:
            print(f"Error en reporte: {e}")

    return render_template('reporte.html', 
                            direcciones=direcciones, anios=anios, 
                            empleados=empleados, totales=totales, 
                            seleccionada=seleccionada, anio_sel=anio_sel,
                            contrato_sel=contrato_filtro)

@app.route('/exportar_excel')
def exportar_excel():
    if 'user' not in session: return redirect(url_for('login'))
    try:
        docs = db.collection('Empleados').stream()
        datos = []
        for doc in docs:
            d = doc.to_dict()
            datos.append({
                'Fecha': d.get('fecha'),
                'Cédula': d.get('cedula'),
                'Contrato': d.get('num_contrato'),
                'Nombre': f"{d.get('nombre')} {d.get('apellido')}",
                'Dirección': d.get('direccion'),
                'Internet': float(d.get('internet') or 0),
                'Pago': float(d.get('pago') or 0),
                'Equipo': float(d.get('equipo') or 0),
                'Depósito': float(d.get('deposito') or 0)
            })
        df = pd.DataFrame(datos)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Reporte')
        output.seek(0)
        return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', as_attachment=True, download_name='Reporte_Pagos.xlsx')
    except Exception as e:
        return f"Error exportando: {e}"

@app.route('/delete_multiple', methods=['POST'])
def delete_multiple():
    if 'user' not in session: return jsonify({'status': 'error', 'message': 'No autorizado'}), 401
    data = request.get_json()
    ids = data.get('ids', [])
    try:
        batch = db.batch()
        for doc_id in ids:
            batch.delete(db.collection('Empleados').document(doc_id))
        batch.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/delete/<id>')
def delete(id):
    if 'user' not in session: return redirect(url_for('login'))
    db.collection('Empleados').document(id).delete()
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=True)