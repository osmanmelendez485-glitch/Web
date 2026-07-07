import firebase_admin
from firebase_admin import credentials, firestore

# 1. Configuración (Usa tu ruta de archivo .json)
if not firebase_admin._apps:
    cred = credentials.Certificate('serviceAccountKey.json')
    firebase_admin.initialize_app(cred)

db = firestore.client()

def visualizar_datos():
    print("\n" + "="*100)
    # Encabezados de las columnas
    print(f"{'ID':<20} | {'NOMBRE':<15} | {'CONTRATO':<12} | {'ESTADO':<10} | {'TOTAL CANON':<12}")
    print("-" * 100)

    # Obtenemos los documentos
    docs = db.collection('Empleados').stream()

    for d in docs:
        item = d.to_dict()
        doc_id = d.id[:18] # Recortamos el ID largo para que quepa
        nombre = f"{item.get('nombre', '')} {item.get('apellido', '')}"[:15]
        num_con = item.get('num_contrato', 'S/N')
        estado = item.get('estado', 'Pendiente')
        
        # Calculamos el total de esa fila para mostrarlo
        try:
            total = (float(item.get('internet', 0)) + 
                     float(item.get('agua', 0)) + 
                     float(item.get('luz', 0)) + 
                     float(item.get('canon', 0)))
        except:
            total = 0.0

        # Imprimimos la fila con formato de columnas fijas
        print(f"{doc_id:<20} | {nombre:<15} | {num_con:<12} | {estado:<10} | C$ {total:>10,.2f}")

    print("="*100 + "\n")

if __name__ == "__main__":
    visualizar_datos()