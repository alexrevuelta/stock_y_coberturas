import os
import json
import requests
import smtplib
import ssl
import urllib3
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- DESACTIVAR ALERTAS DE CERTIFICADO SSL ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# --- FUNCIÓN PARA OBTENER DATOS DE UNA MARCA ESPECÍFICA ---
def get_odoo_data_for_brand(marca, config):
    url = os.environ.get("ODOO_URL")
    db = os.environ.get("ODOO_DB")
    uid = os.environ.get("ODOO_UID")
    token = os.environ.get("ODOO_TOKEN")
    rpc_url = f"{url}/jsonrpc"

    today = datetime.now()
    # Periodo de 15 días solicitado
    date_limit = (today - timedelta(days=15)).strftime('%Y-%m-%d')
    first_day_month = today.replace(day=1).strftime('%Y-%m-%d')

    log(f"🔎 Consultando Odoo para la marca: {marca} (Periodo: 15 días)...")

    try:
        # 1. Buscar Productos de la marca
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
        
        # --- LÓGICA ESPECIAL PARA EL OSO PARDO (DESGLOSE DE PACKS) ---
        if marca.upper() == "EL OSO PARDO":
            log("📦 Marca EL OSO PARDO detectada: Desglosando packs y filtrando referencias...")
            exploded_items = {}
            for p in products:
                # Consultar si tiene Lista de Materiales (Pack)
                payload_bom = {"jsonrpc":"2.0","method":"call","params":{"service":"object","method":"execute_kw","args":[db,int(uid),token,"mrp.bom","search_read",[[["product_id","=",p['id']]]],{"fields":["bom_line_ids"]}]}}
                boms = requests.post(rpc_url, json=payload_bom, timeout=20, verify=False).json().get('result', [])
                
                if boms and boms[0].get('bom_line_ids'):
                    # Es un pack: buscamos sus componentes
                    payload_lines = {"jsonrpc":"2.0","method":"call","params":{"service":"object","method":"execute_kw","args":[db,int(uid),token,"mrp.bom.line","read",[boms[0]['bom_line_ids']],{"fields":["product_id","product_qty"]}]}}
                    lines = requests.post(rpc_url, json=payload_lines, timeout=20, verify=False).json().get('result', [])
                    for line in lines:
                        comp_id = line['product_id'][0]
                        if comp_id not in exploded_items:
                            # Datos del componente unitario
                            payload_c = {"jsonrpc":"2.0","method":"call","params":{"service":"object","method":"execute_kw","args":[db,int(uid),token,"product.product","read",[[comp_id]],{"fields":["default_code","name","qty_available","incoming_qty"]}]}}
                            c = requests.post(rpc_url, json=payload_c, timeout=20, verify=False).json().get('result', [{}])[0]
                            exploded_items[comp_id] = {
                                'id': comp_id, 
                                'sku': c.get('default_code','-'), 
                                'name': c.get('name','-'), 
                                'stock': c.get('qty_available',0), 
                                'pendiente': c.get('incoming_qty',0)
                            }
                else:
                    # Producto individual (no pack)
                    exploded_items[p['id']] = {
                        'id': p['id'], 
                        'sku': p['default_code'], 
                        'name': p['name'], 
                        'stock': p['qty_available'], 
                        'pendiente': p['incoming_qty']
                    }
            
            # --- FILTRADO FINAL: Eliminar referencias que empiecen por "PACK" ---
            filtered_list = []
            for item in exploded_items.values():
                sku_upper = str(item['sku']).upper()
                name_upper = str(item['name']).upper()
                if not (sku_upper.startswith("PACK") or name_upper.startswith("PACK")):
                    filtered_list.append(item)
            
            products_to_process = filtered_list
        else:
            # Proceso estándar para el resto de marcas
            products_to_process = [{'id': p['id'], 'sku': p['default_code'], 'name': p['name'], 'stock': p['qty_available'], 'pendiente': p['incoming_qty']} for p in products]

        # 2. Buscar Ventas (15 días)
        p_ids = [p['id'] for p in products_to_process]
        if not p_ids: return []
        
        payload_sales = {
            "jsonrpc": "2.0", "method": "call", "params": {
                "service": "object", "method": "execute_kw", "args": [
                    db, int(uid), token, "sale.order.line", "search_read",
                    [[["product_id", "in", p_ids], ["state", "in", ["sale", "done"]], ["create_date", ">=", date_limit]]],
                    {"fields": ["product_id", "product_uom_qty", "create_date"]}
                ]
            }
        }
        sales_lines = requests.post(rpc_url, json=payload_sales, timeout=30, verify=False).json().get('result', [])

        # 3. Procesar resultados finales
        report = []
        for p in products_to_process:
            v_15d = sum(line['product_uom_qty'] for line in sales_lines if line['product_id'][0] == p['id'])
            v_diaria = v_15d / 15
            cobertura = (p['stock'] / v_diaria) if v_diaria > 0 else (999 if p['stock'] > 0 else 0)

            report.append({
                'sku': p['sku'], 
                'name': p['name'], 
                'stock': p['stock'],
                'pendiente': p['pendiente'], 
                'v_15d': v_15d, 
                'cobertura': cobertura
            })

        # Ordenar por el más vendido
        report.sort(key=lambda x: x['v_15d'], reverse=True)
        return report[:10]

    except Exception as e:
        log(f"❌ Error Odoo ({marca}): {e}")
        return []

def generate_brand_html(marca, data):
    html = f"""
    <div style="margin-top: 30px; border-top: 1px solid #ddd; padding-top: 15px;">
        <h2 style="color: #2c3e50; margin-bottom: 10px;">📦 Marca: {marca}</h2>
        <table style="border-collapse: collapse; width: 100%; font-size: 12px; border: 1px solid #ddd;">
            <thead>
                <tr style="background-color: #f8f9fa;">
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">SKU</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: left;">Producto</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Stock</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">En camino</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Ventas 15d</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Cobertura</th>
                </tr>
            </thead>
            <tbody>"""
    for item in data:
        clase = "color: #5cb85c;"
        if item['cobertura'] < 7: clase = "color: #d9534f; font-weight: bold;"
        elif item['cobertura'] < 15: clase = "color: #f0ad4e; font-weight: bold;"
        txt_cob = f"{item['cobertura']:.0f} días" if item['cobertura'] < 999 else "Sin ventas"
        html += f"""<tr>
            <td style="border: 1px solid #ddd; padding: 8px;">{item['sku']}</td>
            <td style="border: 1px solid #ddd; padding: 8px;">{item['name']}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center;">{item['stock']:.0f}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center; color: #3498db;">{item['pendiente']:.0f}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center;">{item['v_15d']:.0f}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center; {clase}">{txt_cob}</td>
        </tr>"""
    return html + "</tbody></table></div>"

if __name__ == "__main__":
    if not os.path.exists("config.json"):
        log("❌ Error: No existe config.json")
    else:
        with open("config.json", encoding="utf-8") as f:
            config = json.load(f)
            
        full_email_body = f"""<html><body style="font-family: sans-serif; padding: 20px;">
            <h1 style="color: #333; text-align: center; border-bottom: 2px solid #333; padding-bottom: 10px;">📈 Reporte Stock y Cobertura (Ventas 15 días)</h1>"""
        
        any_data = False
        for marca in config.get("marcas", []):
            data_marca = get_odoo_data_for_brand(marca, config)
            if data_marca:
                any_data = True
                full_email_body += generate_brand_html(marca, data_marca)
        
        if any_data:
            full_email_body += "</body></html>"
            msg = MIMEMultipart()
            msg['Subject'] = f"📊 REPORTE COBERTURA MULTI-MARCA - {datetime.now().strftime('%d/%m/%Y')}"
            msg['From'] = config['email_sender']; msg['To'] = ", ".join(config['recipients'])
            msg.attach(MIMEText(full_email_body, 'html'))
            context = ssl.create_default_context()
            try:
                with smtplib.SMTP_SSL(config['smtp_server'], config['smtp_port'], context=context) as server:
                    server.login(config['email_sender'], os.environ.get("EMAIL_PASSWORD"))
                    server.sendmail(config['email_sender'], config['recipients'], msg.as_string())
                log("✅ Email enviado correctamente.")
            except Exception as e:
                log(f"❌ Error enviando email: {e}")
