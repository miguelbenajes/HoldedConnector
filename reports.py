import pandas as pd
from fpdf import FPDF
import os
import logging
import connector

logger = logging.getLogger(__name__)

def generate_excel_report(data_list, filename="report.xlsx"):
    """
    Generates an Excel file from a list of dictionaries.
    """
    df = pd.DataFrame(data_list)
    # Ensure directory exists (if using a specific path)
    df.to_excel(filename, index=False)
    return filename

class PDF(FPDF):
    def header(self):
        self.set_font('Arial', 'B', 15)
        self.cell(80)
        self.cell(30, 10, 'Financial Analysis Report', 0, 0, 'C')
        self.ln(20)

    def footer(self):
        self.set_y(-15)
        self.set_font('Arial', 'I', 8)
        self.cell(0, 10, 'Page ' + str(self.page_no()) + '/{nb}', 0, 0, 'C')

def generate_pdf_report(content, filename="analysis.pdf"):
    """
    Generates a PDF from a text string (AI output).
    """
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    filepath = os.path.join(reports_dir, os.path.basename(filename))

    clean_content = content.encode('latin-1', 'replace').decode('latin-1').replace('?', ' ')

    pdf = PDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font('Helvetica', '', 12)
    pdf.multi_cell(0, 10, clean_content)
    pdf.output(filepath)
    return filepath

def get_financial_summary_data():
    """
    Gathers a flat list of financial records for Excel export.
    """
    import sqlite3
    # Use the same DB path constant as connector
    conn = sqlite3.connect(connector.DB_NAME)
    try:
        # Get invoices and purchase invoices
        df_invoices = pd.read_sql_query("SELECT id, contact_name, date, amount, status FROM invoices", conn)
        df_purchases = pd.read_sql_query("SELECT id, contact_name, date, amount, status FROM purchase_invoices", conn)
        conn.close()
        return {"Invoices": df_invoices, "Purchases": df_purchases}
    except Exception as e:
        conn.close()
        logger.error(f"Excel data gather error: {e}")
        return {}
