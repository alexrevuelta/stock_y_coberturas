import os
import json
import requests
import smtplib
import ssl
import urllib3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from openai import OpenAI

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
    first_day_month = today.replace(day=1).strftime('%Y-%m-%d')
    date_limit = (today - timedelta(days=30)).strftime('%Y-%m-%d')

    log(f"🔎 Consultando Odoo para la marca: {marca}...")

    # Filtro para la marca específica
    filtro_odoo = [["name", "ilike", marca]]

    try:
        # 1. Buscar Productos
        payload_prod = {
            "jsonrpc": "2.0", "method": "call", "params": {
                "service": "object", "method": "execute_kw", "args": [
                    db, int(uid), token, "product.product", "search_read",
                    [filtro_odoo],
                    {"fields": ["id", "default_code", "barcode", "name", "qty_available", "incoming_qty"]}
                ]
            }
        }
        res_prod = requests.post(rpc_url, json=payload_prod, timeout=30, verify=False).json()
        products = res_prod.get('result', [])
        if not products: return []

        p_ids = [p['id'] for p in products]

        # 2. Buscar Ventas
        payload_sales = {
            "jsonrpc": "2.0", "method": "call", "params": {
                "service": "object", "method": "execute_kw", "args": [
                    db, int(uid), token, "sale.order.line", "search_read",
                    [[["product_id", "in", p_ids], ["state", "in", ["sale", "done"]], ["create_date", ">=", date_limit]]],
                    {"fields": ["product_id", "product_uom_qty", "create_date"]}
                ]
            }
        }
        res_sales = requests.post(rpc_url, json=payload_sales, timeout=30, verify=False).json()
        sales_lines = res_sales.get('result', [])

        # 3. Procesar
        report = []
        for p in products:
            p_id = p['id']
            v_30d = sum(line['product_uom_qty'] for line in sales_lines if line['product_id'][0] == p_id)
            v_mes = sum(line['product_uom_qty'] for line in sales_lines if line['product_id'][0] == p_id and line['create_date'] >= first_day_month)
            stock = p.get('qty_available', 0)
            v_diaria = v_30d / 30
            cobertura = (stock / v_diaria) if v_diaria > 0 else (999 if stock > 0 else 0)

            report.append({
                'sku': p.get('default_code', '-'),
                'name': p.get('name', '-'),
                'stock': stock,
                'pendiente': p.get('incoming_qty', 0),
                'v_mes': v_mes,
                'v_30d': v_30d,
                'cobertura': cobertura
            })

        report.sort(key=lambda x: x['v_30d'], reverse=True)
        return report[:10] # Top 10 de esta marca
    except Exception as e:
        log(f"❌ Error Odoo ({marca}): {e}")
        return []

# --- FUNCIÓN IA POR MARCA ---
def get_ai_analysis(data, marca):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key: return f"Análisis de IA no disponible para {marca} (falta API KEY)."

    client = OpenAI(api_key=api_key)
    resumen = "\n".join([f"- {i['name']}: Stock {i['stock']:.0f}, Ventas30d {i['v_30d']:.0f}, Cobertura {i['cobertura']:.0f}d." for i in data])

    prompt = f"Eres un experto en compras. Analiza estos 10 productos de la marca {marca}:\n{resumen}\n\nEscribe un párrafo muy corto de conclusiones sobre riesgos de stock y qué pedir con prioridad."
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return response.choices[0].message.content
    except:
        return "Error consultando a OpenAI."

# --- GENERAR BLOQUE HTML POR MARCA ---
def generate_brand_html(marca, data, ai_text):
    html = f"""
    <div style="margin-top: 40px; border-top: 2px solid #eee; padding-top: 20px;">
        <h2 style="color: #2c3e50; margin-bottom: 5px;">📦 Marca: {marca}</h2>
        <div style="background-color: #f0f7ff; border-left: 5px solid #007bff; padding: 12px; margin-bottom: 15px; border-radius: 4px;">
            <strong style="color: #007bff;">🤖 Conclusiones IA ({marca}):</strong><br>
            <span style="font-style: italic; font-size: 13px;">{ai_text}</span>
        </div>
        <table style="border-collapse: collapse; width: 100%; font-size: 12px; border: 1px solid #ddd;">
            <thead>
                <tr style="background-color: #f8f9fa;">
                    <th style="border: 1px solid #ddd; padding: 8px;">SKU</th>
                    <th style="border: 1px solid #ddd; padding: 8px;">Producto</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Stock</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">En camino</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Ventas 30d</th>
                    <th style="border: 1px solid #ddd; padding: 8px; text-align: center;">Cobertura</th>
                </tr>
            </thead>
            <tbody>"""
    
    for item in data:
        clase = "color: #5cb85c;" # Verde
        if item['cobertura'] < 7: clase = "color: #d9534f; font-weight: bold;" # Rojo
        elif item['cobertura'] < 15: clase = "color: #f0ad4e; font-weight: bold;" # Naranja
        
        txt_cob = f"{item['cobertura']:.0f} días" if item['cobertura'] < 999 else "Sin ventas"

        html += f"""
        <tr>
            <td style="border: 1px solid #ddd; padding: 8px;">{item['sku']}</td>
            <td style="border: 1px solid #ddd; padding: 8px;">{item['name']}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center;">{item['stock']:.0f}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center; color: #3498db;">{item['pendiente']:.0f}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center;">{item['v_30d']:.0f}</td>
            <td style="border: 1px solid #ddd; padding: 8px; text-align: center; {clase}">{txt_cob}</td>
        </tr>"""
    
    html += "</tbody></table></div>"
    return html

# --- FLUJO PRINCIPAL ---
if __name__ == "__main__":
    with open("config.json", encoding="utf-8") as f:
        config = json.load(f)

    full_email_body = f"""<html><body style="font-family: sans-serif;">
        <h1 style="color: #333; text-align: center;">📈 Reporte de Stock y Cobertura por Marcas</h1>
        <p style="text-align: center; color: #666;">Fecha: {datetime.now().strftime('%d/%m/%Y')}</p>
    """

    any_data = False
    for marca in config.get("marcas", []):
        data_marca = get_odoo_data_for_brand(marca, config)
        if data_marca:
            any_data = True
            log(f"🤖 Generando análisis IA para {marca}...")
            ai_conclusiones = get_ai_analysis(data_marca, marca)
            full_email_body += generate_brand_html(marca, data_marca, ai_conclusiones)
        else:
            log(f"⚠️ No se encontraron datos para {marca}.")

    full_email_body += "<p style='margin-top: 30px; font-size: 11px; color: #999;'>Generado automáticamente desde Odoo.</p></body></html>"

    if any_data:
        # Enviar el email con todos los bloques acumulados
        msg = MIMEMultipart()
        msg['Subject'] = f"📊 REPORTE STOCK MULTI-MARCA - {datetime.now().strftime('%d/%m/%Y')}"
        msg['From'] = config['email_sender']
        msg['To'] = ", ".join(config['recipients'])
        msg.attach(MIMEText(full_email_body, 'html'))

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(config['smtp_server'], config['smtp_port'], context=context) as server:
                server.login(config['email_sender'], os.environ.get("EMAIL_PASSWORD"))
                server.sendmail(config['email_sender'], config['recipients'], msg.as_string())
            log("✅ Email multi-marca enviado con éxito.")
        except Exception as e:
            log(f"❌ Error enviando email: {e}")
    else:
        log("No hay datos de ninguna marca para enviar.")
