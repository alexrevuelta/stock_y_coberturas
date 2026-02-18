import os
import json
import requests
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

# --- FUNCIONES DE LOG Y ODOO ---
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def get_stock_and_coverage(config):
    url = os.environ.get("ODOO_URL")
    db = os.environ.get("ODOO_DB")
    uid = os.environ.get("ODOO_UID")
    token = os.environ.get("ODOO_TOKEN")
    rpc_url = f"{url}/jsonrpc"

    today = datetime.now()
    first_day_month = today.replace(day=1).strftime('%Y-%m-%d')
    date_limit = (today - timedelta(days=30)).strftime('%Y-%m-%d')

    # Filtro dinámico: busca por la marca configurada en config.json
    marca = config.get("marca_a_analizar", "MELCHIONI")
    log(f"🔎 Buscando productos de la marca: {marca}...")
    
    filtro_productos = [
        "|", 
        ["name", "ilike", marca], 
        ["default_code", "ilike", marca]
    ]

    try:
        # 1. Obtener Productos y Stock Actual
        payload_products = {
            "jsonrpc": "2.0", "method": "call", "params": {
                "service": "object", "method": "execute_kw", "args": [
                    db, int(uid), token, "product.product", "search_read",
                    [filtro_productos],
                    {"fields": ["id", "default_code", "barcode", "name", "qty_available", "incoming_qty"]}
                ]
            }
        }
        res_prod = requests.post(rpc_url, json=payload_products, timeout=30).json()
        products = res_prod.get('result', [])

        if not products:
            log("⚠️ No se encontraron productos.")
            return []

        p_ids = [p['id'] for p in products]

        # 2. Obtener Ventas de los últimos 30 días
        payload_sales = {
            "jsonrpc": "2.0", "method": "call", "params": {
                "service": "object", "method": "execute_kw", "args": [
                    db, int(uid), token, "sale.order.line", "search_read",
                    [[["product_id", "in", p_ids], ["state", "in", ["sale", "done"]], ["create_date", ">=", date_limit]]],
                    {"fields": ["product_id", "product_uom_qty", "create_date"]}
                ]
            }
        }
        res_sales = requests.post(rpc_url, json=payload_sales, timeout=30).json()
        sales_lines = res_sales.get('result', [])

        # 3. Procesar datos
        report = []
        for p in products:
            p_id = p['id']
            v_30d = sum(line['product_uom_qty'] for line in sales_lines if line['product_id'][0] == p_id)
            v_mes = sum(line['product_uom_qty'] for line in sales_lines if line['product_id'][0] == p_id and line['create_date'] >= first_day_month)
            
            # Cálculo de Cobertura
            venta_diaria = v_30d / 30
            stock = p.get('qty_available', 0)
            # Si no hay ventas, pero hay stock, ponemos 999 (cobertura infinita)
            cobertura = (stock / venta_diaria) if venta_diaria > 0 else (999 if stock > 0 else 0)

            report.append({
                'sku': p.get('default_code', 'S/N'),
                'ean': p.get('barcode', '-'),
                'name': p.get('name', '-'),
                'stock': stock,
                'pendiente': p.get('incoming_qty', 0),
                'v_mes': v_mes,
                'v_30d': v_30d,
                'cobertura': cobertura
            })

        # Ordenar por el más vendido (v_30d) y devolver el Top 10
        report.sort(key=lambda x: x['v_30d'], reverse=True)
        return report[:10]

    except Exception as e:
        log(f"❌ Error Odoo: {e}")
        return []

def send_email(data, config):
    if not data: return
    marca = config.get("marca_a_analizar", "OPPO")
    
    html = f"""
    <html><head><style>
        body {{ font-family: sans-serif; font-size: 13px; color: #333; }}
        table {{ border-collapse: collapse; width: 100%; border: 1px solid #ddd; }}
        th, td {{ padding: 10px; border: 1px solid #ddd; text-align: left; }}
        th {{ background-color: #f4f4f4; font-weight: bold; }}
        .center {{ text-align: center; }}
        .critico {{ color: #d9534f; font-weight: bold; }} /* Rojo */
        .bajo {{ color: #f0ad4e; font-weight: bold; }}    /* Naranja */
        .ok {{ color: #5cb85c; }}                         /* Verde */
        h2 {{ color: #2c3e50; border-bottom: 2px solid #eee; }}
    </style></head><body>
    <h2>📊 Informe de Stock y Cobertura: {marca}</h2>
    <p>Top 10 productos más vendidos en los últimos 30 días.</p>
    <table>
        <thead>
            <tr>
                <th>SKU</th><th>EAN</th><th>Nombre del Producto</th>
                <th class="center">Stock Act.</th><th class="center">Pendiente</th>
                <th class="center">Ventas Mes</th><th class="center">Ventas 30d</th>
                <th class="center">Días Cobertura</th>
            </tr>
        </thead>
        <tbody>"""

    for item in data:
        # Lógica de colores para cobertura
        cob = item['cobertura']
        clase_cob = "ok"
        if cob < 7: clase_cob = "critico"
        elif cob < 15: clase_cob = "bajo"
        
        txt_cob = f"{cob:.0f} días" if cob < 999 else "Sin ventas"

        html += f"""
            <tr>
                <td>{item['sku']}</td>
                <td>{item['ean']}</td>
                <td>{item['name']}</td>
                <td class="center">{item['stock']:.0f}</td>
                <td class="center" style="color: #3498db;">{item['pendiente']:.0f}</td>
                <td class="center">{item['v_mes']:.0f}</td>
                <td class="center"><b>{item['v_30d']:.0f}</b></td>
                <td class="center {clase_cob}">{txt_cob}</td>
            </tr>"""
            
    html += "</tbody></table><p><small>Generado automáticamente desde Odoo.</small></p></body></html>"

    msg = MIMEMultipart()
    msg['Subject'] = f"📈 STOCK Y COBERTURA: {marca} - {datetime.now().strftime('%d/%m/%Y')}"
    msg['From'] = config['email_sender']
    msg['To'] = ", ".join(config['recipients'])
    msg.attach(MIMEText(html, 'html'))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config['smtp_server'], config['smtp_port'], context=context) as server:
            server.login(config['email_sender'], os.environ.get("EMAIL_PASSWORD"))
            server.sendmail(config['email_sender'], config['recipients'], msg.as_string())
        log("✅ Email de cobertura enviado.")
    except Exception as e:
        log(f"❌ Error email: {e}")

if __name__ == "__main__":
    with open("config.json", encoding="utf-8") as f:
        config_data = json.load(f)
    
    resultados = get_stock_and_coverage(config_data)
    if resultados:
        send_email(resultados, config_data)
    else:
        log("No hay datos para enviar.")
