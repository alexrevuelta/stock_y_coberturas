name: Ejecutar Reporte Diario

on:
  schedule:
    # 09:00 UTC es 10:00 AM en España (invierno)
    - cron: '0 9 * * *'
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v3

      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'

      - name: Install dependencies
        run: pip install requests

      - name: Run script
        env:
          ODOO_URL: ${{ secrets.ODOO_URL }}
          ODOO_DB: ${{ secrets.ODOO_DB }}
          ODOO_UID: ${{ secrets.ODOO_UID }}
          ODOO_TOKEN: ${{ secrets.ODOO_TOKEN }}
          EMAIL_PASSWORD: ${{ secrets.EMAIL_PASSWORD }}
        run: python main.py
