"""AI agent tool definitions (JSON schemas for Claude function calling)."""
import connector


def _month_fmt(col="date"):
    """SQL expression for YYYY-MM month grouping (SQLite/PostgreSQL compatible)."""
    if connector._USE_SQLITE:
        return f"strftime('%Y-%m', datetime({col}, 'unixepoch'))"
    return f"to_char(to_timestamp({col}), 'YYYY-MM')"


WRITE_TOOLS = {"create_estimate", "create_invoice", "send_document", "create_contact", "update_invoice_status", "upload_file", "convert_estimate_to_invoice"}


def get_tools_for_role(role: str = "admin") -> list:
    """Filter AI tool definitions by user role.
    Accountants get read-only tools (WRITE_TOOLS excluded).
    Admin and operator get all tools."""
    if role == "accountant":
        return [t for t in TOOL_DEFINITIONS if t["name"] not in WRITE_TOOLS]
    return TOOL_DEFINITIONS


TOOL_DEFINITIONS = [
    {
        "name": "query_database",
        "description": "Execute a read-only SQL SELECT query against the local financial database. Tables: invoices, purchase_invoices, estimates (columns: id, contact_id, contact_name, desc, date[unix epoch], amount[EUR], status[int]). Line items: invoice_items(invoice_id, product_id, name, sku, units, price, subtotal, discount, tax, retention, account), purchase_items(purchase_id, ...), estimate_items(estimate_id, ...). Also: contacts(id, name, email, type, code, vat, phone, mobile), products(id, name, desc, price, stock, sku), payments(id, document_id, amount, date, method, type), projects(id, name, desc, status, customer_id, budget), ledger_accounts(id, name, num). Status codes for invoices/purchases: 0=draft, 1=issued, 2=partial payment, 3=paid, 4=overdue, 5=cancelled. Estimates: 0=draft, 1=pending, 2=accepted, 3=rejected, 4=invoiced. Use strftime('%Y-%m', datetime(date, 'unixepoch')) for month grouping.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "A SELECT query. Only SELECT is allowed."},
                "explanation": {"type": "string", "description": "Brief explanation of what this query does."}
            },
            "required": ["sql", "explanation"]
        }
    },
    {
        "name": "get_contact_details",
        "description": "Look up a contact by name (fuzzy search) or exact ID. Returns contact info and optionally their transaction history summary (invoice/purchase counts and totals).",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Contact name (partial match) or exact Holded ID"},
                "include_history": {"type": "boolean", "description": "Include transaction summary", "default": False}
            },
            "required": ["search"]
        }
    },
    {
        "name": "get_product_pricing",
        "description": "Look up product catalog prices and compare with actual historical sale/purchase prices. Returns catalog price, avg/min/max sale and purchase prices, margin analysis, and pack/component info (if the product is a pack or belongs to packs).",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Product name (partial match) or SKU"},
                "include_history": {"type": "boolean", "description": "Include historical price analysis", "default": True}
            },
            "required": ["search"]
        }
    },
    {
        "name": "get_financial_summary",
        "description": "Get a high-level financial summary for a time period: total income, expenses, net balance, top clients, and monthly trend.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "Start date YYYY-MM-DD (defaults to 12 months ago)"},
                "end_date": {"type": "string", "description": "End date YYYY-MM-DD (defaults to today)"}
            }
        }
    },
    {
        "name": "get_document_details",
        "description": "Get full details of a specific invoice, purchase, or estimate including all line items.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["invoice", "purchase", "estimate"]},
                "doc_id": {"type": "string", "description": "The document ID"}
            },
            "required": ["doc_type", "doc_id"]
        }
    },
    {
        "name": "create_estimate",
        "description": "Create a new estimate/presupuesto in Holded as a draft. Requires contact_id and line items. In safe mode, simulates without writing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string", "description": "Holded contact ID"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "units": {"type": "number"},
                            "price": {"type": "number"},
                            "tax": {"type": "number", "description": "Tax % (e.g. 21 for IVA)"},
                            "retention": {"type": "number", "description": "IRPF retention %, 0 if N/A"}
                        },
                        "required": ["name", "units", "price"]
                    }
                },
                "desc": {"type": "string", "description": "Description/notes"},
                "date": {"type": "string", "description": "Date YYYY-MM-DD, defaults to today"}
            },
            "required": ["contact_id", "items"]
        }
    },
    {
        "name": "create_invoice",
        "description": "Create a new sales invoice in Holded. Requires contact_id and line items. In safe mode, simulates without writing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "units": {"type": "number"},
                            "price": {"type": "number"},
                            "tax": {"type": "number"},
                            "retention": {"type": "number"}
                        },
                        "required": ["name", "units", "price"]
                    }
                },
                "desc": {"type": "string"}
            },
            "required": ["contact_id", "items"]
        }
    },
    {
        "name": "send_document",
        "description": "Send a document (invoice, estimate, purchase) via email through Holded's email system. The PDF is attached automatically. In safe mode, simulates without sending.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["invoice", "estimate", "purchase"]},
                "doc_id": {"type": "string"},
                "emails": {"type": "array", "items": {"type": "string"}, "description": "Email addresses. If omitted, sends to contact's email on file."},
                "subject": {"type": "string", "description": "Email subject (optional)"},
                "body": {"type": "string", "description": "Email body text (optional)"}
            },
            "required": ["doc_type", "doc_id"]
        }
    },
    {
        "name": "generate_report",
        "description": "Generate a downloadable PDF report from analysis text. Returns a download URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string", "description": "Report text content"}
            },
            "required": ["title", "content"]
        }
    },
    {
        "name": "create_contact",
        "description": "Create a new contact (client or supplier) in Holded. In safe mode, simulates without writing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "type": {"type": "string", "enum": ["client", "supplier", "creditor", "debtor"]},
                "vat": {"type": "string", "description": "Tax ID / NIF / CIF"},
                "phone": {"type": "string"},
                "code": {"type": "string"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "get_overdue_invoices",
        "description": "Get all overdue invoices (status=4) or invoices past their due date. Returns a list sorted by amount descending.",
        "input_schema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["receivable", "payable", "both"], "description": "receivable=sales invoices, payable=purchase invoices, both=all. Default: both"},
                "min_amount": {"type": "number", "description": "Minimum amount filter"}
            }
        }
    },
    {
        "name": "get_upcoming_payments",
        "description": "Get recent and upcoming payments, optionally filtered by date range. Shows payment method, amounts, and linked documents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "description": "Number of days to look ahead from today (default: 30)"},
                "type": {"type": "string", "enum": ["income", "expense", "both"], "description": "Filter by payment type. Default: both"}
            }
        }
    },
    {
        "name": "compare_periods",
        "description": "Compare financial performance between two time periods. Returns income, expenses, balance, and percentage changes for each period.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period1_start": {"type": "string", "description": "Start date YYYY-MM-DD for first period"},
                "period1_end": {"type": "string", "description": "End date YYYY-MM-DD for first period"},
                "period2_start": {"type": "string", "description": "Start date YYYY-MM-DD for second period"},
                "period2_end": {"type": "string", "description": "End date YYYY-MM-DD for second period"}
            },
            "required": ["period1_start", "period1_end", "period2_start", "period2_end"]
        }
    },
    {
        "name": "update_invoice_status",
        "description": "Update the status of an invoice or purchase invoice in Holded. CRITICAL: Changing from borrador(0) to aprobada(1) submits to Hacienda (SII) — IRREVERSIBLE. NEVER approve without explicit user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": ["invoice", "purchase"]},
                "doc_id": {"type": "string", "description": "The document ID"},
                "status": {"type": "integer", "description": "New status: 0=borrador, 1=aprobada (HACIENDA!), 2=partial, 3=paid, 4=overdue, 5=cancelled"}
            },
            "required": ["doc_type", "doc_id", "status"]
        }
    },
    {
        "name": "render_chart",
        "description": "Render an inline chart in the chat response. Use this when the user asks for visual data or graphs. The chart will be rendered using Chart.js in the chat panel.",
        "input_schema": {
            "type": "object",
            "properties": {
                "chart_type": {"type": "string", "enum": ["bar", "line", "doughnut", "pie"], "description": "Type of chart"},
                "title": {"type": "string", "description": "Chart title"},
                "labels": {"type": "array", "items": {"type": "string"}, "description": "Labels for each data point (x-axis or segments)"},
                "datasets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "data": {"type": "array", "items": {"type": "number"}}
                        },
                        "required": ["label", "data"]
                    },
                    "description": "One or more data series"
                }
            },
            "required": ["chart_type", "title", "labels", "datasets"]
        }
    },
    {
        "name": "get_amortization_status",
        "description": "Get amortization (ROI) tracking for rental products. Returns each product's purchase cost, total revenue (direct + pack-attributed), profit, and ROI%. Pack revenue is automatically attributed to component products proportionally. Also includes a global summary. Use when the user asks about amortization, ROI, investment recovery, or how much a product has earned.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_name": {"type": "string", "description": "Optional: filter by product name (partial match). Leave empty for all products."}
            }
        }
    },
    {
        "name": "analyze_file",
        "description": "Analyze an uploaded CSV or Excel file. Returns data summary, columns, data types, and statistical analysis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Name of the uploaded file (e.g., 'sales.csv' or '1234567890_data.xlsx')"},
                "analysis_type": {"type": "string", "enum": ["summary", "trends", "anomalies"], "description": "Type of analysis to perform", "default": "summary"}
            },
            "required": ["filename"]
        }
    },
    {
        "name": "list_files",
        "description": "List recently uploaded files or generated reports. Useful for referencing files by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {"type": "string", "enum": ["uploads", "reports"], "description": "Which directory to list"},
                "limit": {"type": "integer", "description": "Maximum files to return (default: 20)", "default": 20}
            },
            "required": ["directory"]
        }
    },
    {
        "name": "upload_file",
        "description": "Register an uploaded file for processing. This is a write operation that requires user confirmation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Name of the file that was uploaded"},
                "description": {"type": "string", "description": "Optional description of file contents"}
            },
            "required": ["filename"]
        }
    },
    {
        "name": "convert_estimate_to_invoice",
        "description": "Convert an estimate (presupuesto/quote) to an invoice (factura). Creates a new invoice as BORRADOR with the same items, and marks the estimate as invoiced. The estimate is automatically approved (safe — quotes don't go to Hacienda).",
        "input_schema": {
            "type": "object",
            "properties": {
                "estimate_id": {"type": "string", "description": "The Holded ID of the estimate to convert"}
            },
            "required": ["estimate_id"]
        }
    }
]
