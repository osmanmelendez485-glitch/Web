from flask import Flask, render_template, request, redirect, url_for, session, send_file
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json
import pandas as pd
import io 
from werkzeug.utils import secure_filename
from datetime import datetime
from google.cloud.firestore_v1.base_query import FieldFilter
# Importaciones para estilos, colores y alineación
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
    
    sort_by = request.args.get('sort', 'fecha')
    direction = firestore.Query.DESCENDING if 'fecha' in sort_by or 'pago' in sort_by else firestore.Query.ASCENDING
    field = sort_by.split(' ')[0] 

    try:
        docs = db.collection('Empleados').order_by(field, direction=direction).stream()
        datos = []
        for doc in docs:
            item = doc.to_dict()
            item['id'] = doc.id
            datos.append(item)
        return render_template('index.html', empleados=datos)
    except Exception as e:
        return f"Error de Firebase: {e}"

@app.route('/add', methods=['POST'])
def add():
    if 'user' not in session: return redirect(url_for('login'))
    d = request.form
    cedula = d.get('cedula', '').strip()
    
    nuevo_empleado = {
        'fecha': d.get('fecha'),
        'nombre': d.get('nombre', '').strip(),
        'apellido': d.get('apellido', ''),
        'cedula': cedula,
        'num_contrato': d.get('num_contrato', ''),
        'direccion': d.get('direccion', 'N/A'),
        'pago': float(d.get('pago') or 0.0),
        'equipo': float(d.get('equipo') or 0.0),
        'deposito': float(d.get('deposito') or 0.0),
        'internet': float(d.get('internet') or 0.0),
        'sexo': 'M',
        'escolaridad': 'N/A',
        'ano_escolaridad': 0,
        'ciudad': 'Chinandega',
        'dependientes': 0,
        'contrato_file': ""
    }

    file_contrato = request.files.get('contrato')
    if file_contrato and file_contrato.filename != '':
        nom_contrato = secure_filename(file_contrato.filename)
        file_contrato.save(os.path.join(app.config['UPLOAD_FOLDER'], nom_contrato))
        nuevo_empleado['contrato_file'] = nom_contrato

    try:
        query = db.collection('Empleados').where(filter=FieldFilter('cedula', '==', cedula)).limit(1).get()
        if query:
            db.collection('Empleados').document(query[0].id).update(nuevo_empleado)
        else:
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
        df = df.fillna(0)

        for _, row in df.iterrows():
            cedula = str(row.get('cedula', '')).strip()
            
            datos_pago = {
                'fecha': str(row.get('fecha')),
                'nombre': str(row.get('nombre_empleado')),
                'apellido': str(row.get('apellido_empleado', '')),
                'cedula': cedula,
                'num_contrato': str(row.get('num_contrato', '')),
                'internet': float(row.get('internet', 0)),
                'pago': float(row.get('pago', 0)),
                'equipo': float(row.get('equipo', 0)),
                'deposito': float(row.get('deposito', 0)),
                'direccion': str(row.get('direccion', 'General'))
            }

            if cedula:
                docs = db.collection('Empleados').where(filter=FieldFilter('cedula', '==', cedula)).limit(1).get()
                if docs:
                    db.collection('Empleados').document(docs[0].id).update(datos_pago)
                    continue
            
            db.collection('Empleados').add(datos_pago)

        return redirect(url_for('dashboard'))
    except Exception as e:
        return f"Error procesando la subida masiva: {e}"

# RUTA ACTUALIZADA: FILTROS SELECTIVOS Y COLORES EN ENCABEZADOS
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
            df.to_excel(writer, index=False, sheet_name='Reporte Pagos')
            
            worksheet = writer.sheets['Reporte Pagos']
            
            # 1. FILTRO SELECTIVO: Solo de Fecha (A) hasta Dirección (E)
            worksheet.auto_filter.ref = "A1:E1"

            # 2. ESTILO DE COLORES PARA ENCABEZADOS
            # Azul (4F81BD) con texto blanco (FFFFFF)
            azul_fill = PatternFill(start_color="4F81BD", end_color="4F81BD", fill_type="solid")
            fuente_blanca = Font(bold=True, color="FFFFFF")

            # 3. APLICAR ESTILOS Y AJUSTE DE ANCHO
            for col in worksheet.columns:
                max_length = 0
                column_letter = col[0].column_letter
                
                # Aplicar color y fuente a la celda del encabezado (fila 1)
                header_cell = worksheet[f"{column_letter}1"]
                header_cell.fill = azul_fill
                header_cell.font = fuente_blanca
                header_cell.alignment = Alignment(horizontal="center")

                for cell in col:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except: pass
                # Margen de +5 para amplitud
                worksheet.column_dimensions[column_letter].width = max_length + 5

        output.seek(0)

        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'Reporte_Pagos_{datetime.now().strftime("%Y%m%d")}.xlsx'
        )
    except Exception as e:
        return f"Error al generar Excel: {e}"

@app.route('/reporte', methods=['GET', 'POST'])
def reporte():
    if 'user' not in session: return redirect(url_for('login'))
    
    direcciones = ["Chinandega", "Miguel Jarquín", "Módulo 1", "Módulo 2"]
    anios = ["2024", "2025", "2026"]
    
    empleados = []
    totales = {'pago': 0.0, 'equipo': 0.0, 'deposito': 0.0, 'internet': 0.0}
    
    seleccionada = request.form.get('direccion')
    anio_sel = request.form.get('anio')
    contrato_filtro = request.form.get('num_contrato', '').strip()

    if request.method == 'POST':
        try:
            ref = db.collection('Empleados')
            if seleccionada:
                ref = ref.where(filter=FieldFilter('direccion', '==', seleccionada))
            
            docs = ref.stream()
            
            for doc in docs:
                emp = doc.to_dict()
                fecha_str = emp.get('fecha', '')
                num_con_emp = str(emp.get('num_contrato', ''))
                
                cumple_anio = not anio_sel or fecha_str.startswith(anio_sel)
                cumple_contrato = not contrato_filtro or contrato_filtro in num_con_emp
                
                if cumple_anio and cumple_contrato:
                    emp['id'] = doc.id
                    empleados.append(emp)
                    totales['pago'] += float(emp.get('pago') or 0)
                    totales['equipo'] += float(emp.get('equipo') or 0)
                    totales['deposito'] += float(emp.get('deposito') or 0)
                    totales['internet'] += float(emp.get('internet') or 0)
                    
        except Exception as e:
            print(f"Error en reporte: {e}")

    return render_template('reporte.html', 
                            direcciones=direcciones, 
                            anios=anios, 
                            empleados=empleados, 
                            totales=totales, 
                            seleccionada=seleccionada, 
                            anio_sel=anio_sel,
                            contrato_sel=contrato_filtro)

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