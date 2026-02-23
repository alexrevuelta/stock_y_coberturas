import os
import json
import requests
import smtplib
import ssl
import urllib3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- DESACTIVAR ALERTAS DE CERTIFICADO SSL ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# --- FUNCIÓN PARA OBTENER DATOS DE UNA MARCA ---
def get_odoo_data_for_brand(marca, config):
    url = os.environ.get("ODOO_URL")
    db = os.environ.get("ODOO_DB")
    uid = os.environ.get("ODOO_UID")
    token = os.environ.get("ODOO_TOKEN")
    rpc_url = f"{url}/jsonrpc"

    today = datetime.now()
    # Ventas de los últimos 15 días
    date_limit = (today - timedelta(days=15)).strftime('%Y-%m-%d')

    log(f"🔎 Consultando Odoo para {marca} (Periodo: 15 días)...")

    try:
        # 1. Buscar Productos
        payload_prod = {
            "jsonrpc": "2.0", "method": "call", "params": {
                "service": "object", "method": "execute_kw", "args": [
                    db, int(uid), token, "product.product", "search_read",
                    [[["name", "ilike", marca]]],
                    {"fields": ["id", "default_code", "name", "qty_available", "incoming_qty"]}
                ]
            }
        }
        products = requests.post(rpc_url, json=payload_prod, timeout=30, verify=False).json().get('result', [])
        if not products: return []

        products_to_process = []
        
        # --- LÓGICA ESPECIAL PARA EL OSO PARDO ---
        if marca.upper() == "EL OSO PARDO":
            exploded_items = {}
            SKUS_EXCLUIDOS = ["EOPQUESYSOB1", "EOPQUESYSOB2"]

            for p in products:
                payload_bom = {"jsonrpc":"2.0","method":"call","params":{"service":"object","method":"execute_kw","args":[db,int(uid),token,"mrp.bom","search_read",[[["product_id","=",p['id']]]],{"fields":["bom_line_ids"]}]}}
                boms = requests.post(rpc_url, json=payload_bom, timeout=20, verify=False).json().get('result', [])
                
                if boms and boms[0].get('bom_line_ids'):
                    payload_lines = {"jsonrpc":"2.0","method":"call","params":{"service":"object","method":"execute_kw","args":[db,int(uid),token,"mrp.bom.line","read",[boms[0]['bom_line_ids']],{"fields":["product_id","product_qty"]}]}}
                    lines = requests.post(rpc_url, json=payload_lines, timeout=20, verify=False).json().get('result', [])
                    for line in lines:
                        comp_id = line['product_id'][0]
                        if comp_id not in exploded_items:
                            payload_c = {"jsonrpc":"2.0","method":"call","params":{"service":"object","method":"execute_kw","args":[db,int(uid),token,"product.product","read",[[comp_id]],{"fields":["default_code","name","qty_available","incoming_qty"]}]}}
                            c = requests.post(rpc_url, json=payload_c, timeout=20, verify=False).json().get('result', [{}])[0]
                            exploded_items[comp_id] = {'id':comp_id, 'sku':c.get('default_code','-'), 'name':c.get('name','-'), 'stock':c.get('qty_available',0), 'pendiente':c.get('incoming_qty',0)}
                else:
                    exploded_items[p['id']] = {'id': p['id'], 'sku': p['default_code'], 'name': p['name'], 'stock': p['qty_available'], 'pendiente': p['incoming_qty']}
            
            products_to_process = [i for i in exploded_items.values() if not (str(i['sku']).upper().startswith("PACK") or str(i['name']).upper().startswith("PACK") or str(i['sku']).upper().strip() in SKUS_EXCLUIDOS)]
        else:
            products_to_process = [{'id': p['id'], 'sku': p['default_code'], 'name': p['name'], 'stock': p['qty_available'], 'pendiente': p['incoming_qty']} for p in products]

        # 2. Ventas 15 días
        p_ids = [p['id'] for p in products_to_process]
        if not p_ids: return []
        sales_lines = requests.post(rpc_url, json={"jsonrpc":"2.0","method":"call","params":{"service":"object","method":"execute_kw","args":[db,int(uid),token,"sale.order.line","search_read",[[["product_id","in",p_ids],["state","in",["sale","done"]],["create_date",">=",date_limit]]],{"fields":["product_id","product_uom_qty"]}]}}, timeout=30, verify=False).json().get('result', [])

        # 3. Cálculo final
        report = []
        for p in products_to_process:
            v_15d = sum(line['product_uom_qty'] for line in sales_lines if line['product_id'][0] == p['id'])
            v_diaria = v_15d / 15
            
            # Nueva lógica de cobertura:
            if v_15d > 0:
                cobertura = p['stock'] / v_diaria
            else:
                cobertura = -1 # Marcador para "Sin ventas"

            report.append({'sku':p['sku'], 'name':p['name'], 'stock':p['stock'], 'pendiente':p['pendiente'], 'v_15d':v_15d, 'cobertura':cobertura})

        # Ordenar por ventas de mayor a menor
        report.sort(key=lambda x: x['v_15d'], reverse=True)
        return report

    except Exception as e:
        log(f"❌ Error {marca}: {e}"); return []

# --- GENERAR HTML DEL EMAIL ---
def generate_email_html(marca, data):
    html = f"""<html><body style="font-family: sans-serif; padding: 10px;">
        <h2 style="color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px;">📊 Reporte Stock y Cobertura: {marca}</h2>
        <p style="font-size: 12px; color: #666;">Cálculo basado en ventas de los últimos 15 días.</p>
        <table style="border-collapse: collapse; width: 100%; font-size: 12px; border: 1px solid #ddd;">
            <thead>
                <tr style="background-color: #f8f9fa;">
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">SKU</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Producto</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Stock Actual</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">En camino</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Ventas 15d</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Cobertura</th>
                </tr>
            </thead>
            <tbody>"""
    
    for item in data:
        # Estilo de fila: Rojo si el stock es 0 o menos
        row_style = 'style="background-color: #f8d7da; color: #721c24;"' if item['stock'] <= 0 else ""
        
        # Lógica de texto y color de cobertura
        if item['v_15d'] <= 0:
            txt_cob = "Sin ventas"
            cob_style = "color: #999;" # Gris si no hay ventas
        else:
            # Si hay ventas, calculamos los días
            if item['cobertura'] > 365:
                txt_cob = "+365 días"
                cob_style = "color: #5cb85c;" # Verde (Mucha cobertura)
            else:
                txt_cob = f"{item['cobertura']:.0f} días"
                # Colores de alerta por días
                if item['cobertura'] < 7: cob_style = "color: #d9534f; font-weight: bold;"
                elif item['cobertura'] < 15: cob_style = "color: #f0ad4e; font-weight: bold;"
                else: cob_style = "color: #5cb85c;"

        html += f"""
        <tr {row_style}>
            <td style="border: 1px solid #ddd; padding: 8px;">{item['sku']}</td>
            <td style="border: 1px solid #ddd; padding: 8px;">{item['name']}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center;"><b>{item['stock']:.0f}</b></td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center; color: #3498db;">{item['pendiente']:.0f}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center;">{item['v_15d']:.0f}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center; {cob_style}">{txt_cob}</td>
        </tr>"""
    
    return html + "</tbody></table></body></html>"

# --- ENVÍO INDIVIDUAL POR MARCA ---
def send_brand_email(marca, html_content, config):
    msg = MIMEMultipart()
    msg['Subject'] = f"📈 STOCK {marca} - {datetime.now().strftime('%d/%m/%Y')}"
    msg['From'] = config['email_sender']
    msg['To'] = ", ".join(config['recipients'])
    msg.attach(MIMEText(html_content, 'html'))

    context = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL(config['smtp_server'], config['smtp_port'], context=context) as server:
            server.login(config['email_sender'], os.environ.get("EMAIL_PASSWORD"))
            server.sendmail(config['email_sender'], config['recipients'], msg.as_string())
        log(f"✅ Email enviado: {marca}")
    except Exception as e:
        log(f"❌ Error enviando {marca}: {e}")

if __name__ == "__main__":
    with open("config.json", encoding="utf-8") as f:
        config = json.load(f)

    for marca in config.get("marcas", []):
        data = get_odoo_data_for_brand(marca, config)
        if data:
            html = generate_email_html(marca, data)
            send_brand_email(marca, html, config)
        else:
            log(f"⚠️ Sin datos para {marca}")
