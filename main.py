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

# --- CONFIGURACIÓN DE LOGS ---
def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

# --- CONEXIÓN CON ODOO (CON PARCHE SSL) ---
def get_odoo_data(config):
    url = os.environ.get("ODOO_URL")
    db = os.environ.get("ODOO_DB")
    uid = os.environ.get("ODOO_UID")
    token = os.environ.get("ODOO_TOKEN")
    rpc_url = f"{url}/jsonrpc"

    today = datetime.now()
    first_day_month = today.replace(day=1).strftime('%Y-%m-%d')
    date_limit = (today - timedelta(days=30)).strftime('%Y-%m-%d')

    lista_marcas = config.get("marcas", ["OPPO"])
    log(f"🔎 Analizando stock para: {', '.join(lista_marcas)}...")

    # Construir filtro dinámico para múltiples marcas (Operador OR '|')
    filtro_marcas = ["|"] * (len(lista_marcas) - 1)
    for marca in lista_marcas:
        filtro_marcas.append(["name", "ilike", marca])

    try:
        # 1. Buscar Productos (verify=False para evitar error SSL)
        payload_prod = {
            "jsonrpc": "2.0", "method": "call", "params": {
                "service": "object", "method": "execute_kw", "args": [
                    db, int(uid), token, "product.product", "search_read",
                    [filtro_marcas],
                    {"fields": ["id", "default_code", "barcode", "name", "qty_available", "incoming_qty"]}
                ]
            }
        }
        res_prod = requests.post(rpc_url, json=payload_prod, timeout=30, verify=False).json()
        products = res_prod.get('result', [])
        if not products: 
            log("⚠️ No se encontraron productos con esos nombres.")
            return []

        p_ids = [p['id'] for p in products]

        # 2. Buscar Ventas (últimos 30 días)
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

        # 3. Procesar y calcular cobertura
        final_report = []
        for p in products:
            p_id = p['id']
            # Sumar cantidades vendidas
            v_30d = sum(line['product_uom_qty'] for line in sales_lines if line['product_id'][0] == p_id)
            v_mes = sum(line['product_uom_qty'] for line in sales_lines if line['product_id'][0] == p_id and line['create_date'] >= first_day_month)
            
            stock = p.get('qty_available', 0)
            v_diaria = v_30d / 30
            
            # Lógica de cobertura: 999 si hay stock pero no hay ventas
            if v_diaria > 0:
                cobertura = stock / v_diaria
            else:
                cobertura = 999 if stock > 0 else 0

            final_report.append({
                'sku': p.get('default_code', '-'),
                'ean': p.get('barcode', '-'),
                'name': p.get('name', '-'),
                'stock': stock,
                'pendiente': p.get('incoming_qty', 0),
                'v_mes': v_mes,
                'v_30d': v_30d,
                'cobertura': cobertura
            })

        # Ordenar por ventas de últimos 30 días y devolver Top 10
        final_report.sort(key=lambda x: x['v_30d'], reverse=True)
        return final_report[:10]

    except Exception as e:
        log(f"❌ Error en la conexión u obtención de datos: {e}")
        return []

# --- GENERAR CONCLUSIONES CON IA ---
def get_ai_analysis(data, marcas):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key: 
        return "Análisis de IA no disponible (falta OPENAI_API_KEY en los secretos)."

    client = OpenAI(api_key=api_key)
    
    # Resumen simplificado para la IA
    resumen = "\n".join([
        f"- {i['name']} ({i['sku']}): Stock {i['stock']:.0f}, Ventas30d {i['v_30d']:.0f}, Cobertura {i['cobertura']:.0f} días, Pendiente {i['pendiente']:.0f}." 
        for i in data
    ])

    prompt = f"""
    Actúa como Director de Logística. Analiza estos datos de las marcas {', '.join(marcas)}:
    {resumen}

    Escribe 3 párrafos cortos:
    1. Alertas de rotura: ¿Qué modelos se agotarán pronto?
    2. Comportamiento: ¿Qué marca o modelo destaca en ventas?
    3. Acción: ¿Qué pedido debemos priorizar o qué stock debemos mover?
    
    Tono profesional y directo. Usa negritas en los nombres de productos.
    """

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        return response.choices[0].message.content.replace("\n", "<br>")
    except Exception as e:
        return f"Error al consultar OpenAI: {e}"

# --- ENVIAR EMAIL ---
def send_email(data, ai_text, config):
    sender = config.get("email_sender")
    recipients = config.get("recipients", [])
    marcas_str = ", ".join(config.get("marcas", []))
    
    html = f"""
    <html><head><style>
        body {{ font-family: sans-serif; font-size: 13px; color: #333; }}
        .ia-box {{ background-color: #f0f7ff; border-left: 5px solid #007bff; padding: 15px; margin-bottom: 20px; border-radius: 4px; }}
        table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
        th {{ background-color: #f8f9fa; font-weight: bold; }}
        .center {{ text-align: center; }}
        .critico {{ color: #d9534f; font-weight: bold; }}
        .bajo {{ color: #f0ad4e; font-weight: bold; }}
        .ok {{ color: #5cb85c; }}
    </style></head><body>
        <h2 style="color: #2c3e50;">📊 Reporte Stock y Cobertura: {marcas_str}</h2>
        
        <div class="ia-box">
            <h3 style="margin-top:0; color:#007bff;">🤖 Análisis de IA</h3>
            <div style="line-height: 1.5;">{ai_text}</div>
        </div>

        <table><thead><tr>
            <th>SKU</th><th>Nombre del Producto</th><th class="center">Stock</th>
            <th class="center">En camino</th><th class="center">Ventas Mes</th>
            <th class="center">Ventas 30d</th><th class="center">Cobertura</th>
        </tr></thead><tbody>"""

    for item in data:
        cob = item['cobertura']
        clase = "ok"
        if cob < 7: clase = "critico"
        elif cob < 15: clase = "bajo"
        
        txt_cob = f"{cob:.0f} días" if cob < 999 else "Sin ventas"

        html += f"""
        <tr>
            <td>{item['sku']}</td><td>{item['name']}</td>
            <td class="center">{item['stock']:.0f}</td>
            <td class="center" style="color: #3498db;">{item['pendiente']:.0f}</td>
            <td class="center">{item['v_mes']:.0f}</td>
            <td class="center"><b>{item['v_30d']:.0f}</b></td>
            <td class="center {clase}">{txt_cob}</td>
        </tr>"""

    html += "</tbody></table><p style='color: #888; font-size: 11px;'>Reporte generado desde Odoo con soporte de IA.</p></body></html>"

    msg = MIMEMultipart()
    msg['Subject'] = f"📈 STOCK Y COBERTURA: {marcas_str} - {datetime.now().strftime('%d/%m/%Y')}"
    msg['From'] = sender
    msg['To'] = ", ".join(recipients)
    msg.attach(MIMEText(html, 'html'))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(config['smtp_server'], config['smtp_port'], context=context) as server:
            server.login(sender, os.environ.get("EMAIL_PASSWORD"))
            server.sendmail(sender, recipients, msg.as_string())
        log("✅ Email enviado correctamente.")
    except Exception as e:
        log(f"❌ Error enviando email: {e}")

# --- FLUJO PRINCIPAL ---
if __name__ == "__main__":
    # Cargar Configuración
    if not os.path.exists("config.json"):
        log("❌ Error: No existe el archivo config.json")
    else:
        with open("config.json", encoding="utf-8") as f:
            config = json.load(f)

        # 1. Obtener datos de Odoo
        data = get_odoo_data(config)
        
        if data:
            # 2. Obtener análisis de la IA
            log("🤖 Generando conclusiones con OpenAI...")
            conclusiones = get_ai_analysis(data, config.get("marcas", []))
            
            # 3. Enviar Informe
            send_email(data, conclusiones, config)
        else:
            log("⚠️ No se generaron datos. Revisa las marcas en config.json o la conexión a Odoo.")
