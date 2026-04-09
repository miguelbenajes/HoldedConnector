"""Invoice analysis background job — categorization + inventory matching.

Runs daily at 3 AM via a daemon thread. Categorizes unanalyzed purchase
invoices using keyword rules (with Claude AI fallback) and scans for
inventory matches.
"""

import json
import re
import logging
import threading
from datetime import datetime, timedelta

import connector

logger = logging.getLogger(__name__)

# ── Status tracking (thread-safe) ──────────────────────────────────────────
analysis_status = {
    "running": False,
    "last_run": None,
    "last_result": None,
    "processed": 0,
    "pending_matches": 0,
}
_analysis_lock = threading.Lock()


def run_analysis_job(batch_size: int = 10):
    """Core analysis job:
    1. Categorize up to batch_size unanalyzed purchase invoices (rules -> Claude fallback)
    2. Scan ALL purchase_items for inventory matches and save pending ones"""
    with _analysis_lock:
        if analysis_status["running"]:
            return {"error": "Already running"}
        analysis_status["running"] = True
        analysis_status["last_run"] = datetime.now().isoformat()
    processed = 0
    errors = []

    try:
        # Step 1: Categorize unanalyzed invoices
        invoices = connector.get_unanalyzed_purchases(limit=batch_size)
        logger.info(f"Analysis job: {len(invoices)} invoices to categorize")

        for inv in invoices:
            try:
                result = connector.categorize_by_rules(
                    inv.get('desc') or '',
                    inv.get('contact_name') or '',
                    inv.get('item_names') or []
                )
                if result is None:
                    result = _claude_categorize(inv)

                connector.save_purchase_analysis(
                    purchase_id=inv['id'],
                    category=result.get('category', 'Sin categoría'),
                    subcategory=result.get('subcategory', ''),
                    confidence=result.get('confidence', 'low'),
                    method=result.get('method', 'unknown'),
                    reasoning=result.get('reasoning', '')
                )
                processed += 1
            except Exception as e:
                logger.error(f"Error categorizing {inv['id']}: {e}")
                errors.append(str(e))

        # Step 2: Scan for inventory matches
        matches = connector.find_inventory_in_purchases()
        logger.info(f"Analysis job: {len(matches)} inventory matches found")
        for m in matches:
            try:
                connector.save_inventory_match(
                    m['purchase_id'], m['purchase_item_id'], m['product_id'],
                    m['product_name'], m['matched_price'], m['matched_date'],
                    m['match_method']
                )
            except Exception as e:
                logger.warning(f"Match save error: {e}")

        pending = len(connector.get_pending_matches())
        analysis_status["processed"] = processed
        analysis_status["pending_matches"] = pending
        analysis_status["last_result"] = "success" if not errors else "partial"
        return {"processed": processed, "matches_found": len(matches), "pending": pending}

    except Exception as e:
        logger.error(f"Analysis job failed: {e}", exc_info=True)
        analysis_status["last_result"] = "error"
        return {"error": "Analysis job failed"}
    finally:
        analysis_status["running"] = False


def _claude_categorize(inv: dict) -> dict:
    """Use Claude to categorize a purchase invoice when rules don't match.
    Keeps prompt minimal to save tokens."""
    try:
        import ai_agent
        api_key = ai_agent._get_api_key()
        if not api_key:
            return {"category": "Sin categoría", "subcategory": "", "confidence": "low",
                    "method": "no_key", "reasoning": "No Claude API key configured"}

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        item_list = ", ".join(inv.get('item_names') or []) or "—"
        prompt = (
            f"Clasifica esta factura de gasto empresarial. "
            f"Elige la carpeta contable que mejor encaje.\n"
            f"Proveedor: {inv.get('contact_name','')}\n"
            f"Descripción: {inv.get('desc','')}\n"
            f"Items: {item_list}\n"
            f"Importe: {inv.get('amount',0)}€\n\n"
            f"Carpetas contables disponibles:\n"
            f"- AIRBNB RECIBOS (recibos de alojamiento Airbnb)\n"
            f"- AMAZON (compras en Amazon de cualquier tipo)\n"
            f"- GASTOS LOCAL → subcarpeta: agua y luz | alarma | alquiler\n"
            f"- SEGUROS - SEG SOCIAL → subcarpeta: Seguro | Seg. Social\n"
            f"- SOFTWARE → subcarpeta: adobe | apple | capture one | google | holded | hostalia | spotify | Suscripción\n"
            f"- TELEFONIA E INTERNET → subcarpeta: Digi | finetwork | Telefonía\n"
            f"- TRANSPORTE → subcarpeta: dhl | gasolina | renfe | taxis | uber | vuelos | Mensajería\n"
            f"- A AMORTIZAR (equipamiento de alto valor: cámaras, ordenadores, maquinaria)\n"
            f"- VARIOS → subcarpeta: Restaurante | Formación | Varios\n\n"
            f"Responde SOLO con JSON: "
            f'{{\"category\":\"...\",\"subcategory\":\"...\",\"reasoning\":\"...\"}}'
        )
        msg = client.messages.create(
            model=ai_agent._get_model(),
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            data = json.loads(m.group())
            return {
                "category": data.get("category", "Otros"),
                "subcategory": data.get("subcategory", ""),
                "confidence": "medium",
                "method": "claude",
                "reasoning": data.get("reasoning", "")
            }
    except Exception as e:
        logger.warning(f"Claude categorization failed: {e}")

    return {"category": "Sin categoría", "subcategory": "", "confidence": "low",
            "method": "failed", "reasoning": "Categorization failed"}


# ── Daily scheduler ────────────────────────────────────────────────────────
_scheduler_thread = None
_scheduler_stop = threading.Event()


def _daily_scheduler():
    """Background thread: runs analysis job once per day at 3 AM."""
    while not _scheduler_stop.is_set():
        now = datetime.now()
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        wait_secs = (next_run - now).total_seconds()
        logger.info(f"Analysis scheduler: next run in {wait_secs/3600:.1f}h at {next_run.strftime('%H:%M')}")
        if _scheduler_stop.wait(timeout=wait_secs):
            break  # Shutdown requested
        logger.info("Analysis scheduler: starting daily job")
        try:
            run_analysis_job(batch_size=10)
        except Exception as e:
            logger.error(f"Analysis scheduler: job failed: {e}", exc_info=True)


def start_scheduler():
    """Start the daily analysis scheduler in a daemon thread."""
    global _scheduler_thread
    _scheduler_thread = threading.Thread(target=_daily_scheduler, daemon=True)
    _scheduler_thread.start()
    logger.info("Daily analysis scheduler started")


def stop_scheduler():
    """Signal the scheduler thread to stop gracefully."""
    _scheduler_stop.set()
