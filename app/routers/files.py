"""
Files router — file upload, download, and configuration endpoints.

Handles report downloads, ticket uploads, file uploads for AI analysis,
and directory configuration.
Extracted from api.py (Fase 4 router split, Task 5).

Endpoints:
    GET  /api/reports/excel
    GET  /api/reports/download/{filename}
    POST /api/tickets/upload
    GET  /api/files/config
    POST /api/files/config
    POST /api/files/upload
    GET  /api/files/list
"""
from fastapi import APIRouter, UploadFile, File, Query
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
import connector
import reports
import os
import time
import io
import logging
import pandas as pd

logger = logging.getLogger(__name__)
router = APIRouter()

REPORTS_DIR = os.path.abspath("reports")
UPLOADS_DIR = os.path.abspath("uploads")


class DirectoryConfig(BaseModel):
    uploads_dir: Optional[str] = None
    reports_dir: Optional[str] = None


@router.get("/api/reports/excel")
def download_excel_report():
    try:
        data_dict = reports.get_financial_summary_data()
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            for sheet_name, df in data_dict.items():
                df.to_excel(writer, index=False, sheet_name=sheet_name)
        output.seek(0)

        headers = {
            'Content-Disposition': 'attachment; filename="holded_connector_report.xlsx"'
        }
        return StreamingResponse(output, headers=headers, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        logger.error(f"Excel API Error: {e}", exc_info=True)
        return {"status": "error", "message": "Failed to generate Excel report"}


@router.get("/api/reports/download/{filename}")
async def download_report_file(filename: str):
    if not filename.endswith(".pdf"):
        return {"status": "error", "message": "Invalid file type"}

    safe_name = os.path.basename(filename)
    file_path = os.path.join(REPORTS_DIR, safe_name)
    if not os.path.abspath(file_path).startswith(REPORTS_DIR):
        return {"status": "error", "message": "Invalid file path"}

    if os.path.exists(file_path):
        return FileResponse(file_path, filename=safe_name)
    return {"status": "error", "message": "File not found"}


@router.post("/api/tickets/upload")
async def upload_ticket(file: UploadFile = File(...)):
    from fastapi import HTTPException
    # Validate file type
    allowed_exts = {".jpg", ".jpeg", ".png", ".pdf", ".csv", ".xlsx", ".xls"}
    file_ext = os.path.splitext(file.filename or "")[1].lower()
    if file_ext not in allowed_exts:
        raise HTTPException(status_code=400, detail=f"File type not allowed: {file_ext}")

    os.makedirs(UPLOADS_DIR, exist_ok=True)
    safe_name = f"{int(time.time())}_{os.path.basename(file.filename or 'upload')}"
    file_path = os.path.join(UPLOADS_DIR, safe_name)
    if not os.path.abspath(file_path).startswith(UPLOADS_DIR):
        raise HTTPException(status_code=400, detail="Invalid filename")

    content = await file.read()
    if len(content) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(status_code=413, detail="File too large (max 50MB)")

    with open(file_path, "wb") as buffer:
        buffer.write(content)

    return {
        "status": "success",
        "filename": safe_name,
        "message": "Ticket subido correctamente. En la siguiente versión implementaremos el reconocimiento automático."
    }


@router.get("/api/files/config")
async def get_file_config():
    """Get current uploads and reports directory configuration."""
    return {
        "uploads_dir": connector.get_uploads_dir(),
        "reports_dir": connector.get_reports_dir()
    }


@router.post("/api/files/config")
async def set_file_config(body: DirectoryConfig):
    """Update uploads/reports directory paths."""
    results = {}

    if body.uploads_dir:
        result = connector.set_uploads_dir(body.uploads_dir)
        results["uploads"] = result

    if body.reports_dir:
        result = connector.set_reports_dir(body.reports_dir)
        results["reports"] = result

    return results if results else {"error": "No paths provided"}


@router.post("/api/files/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload a file for AI analysis (CSV/Excel only)."""
    from fastapi import HTTPException

    try:
        uploads_dir = connector.get_uploads_dir()
        logger.info(f"Upload directory: {uploads_dir}")

        # Create directory if it doesn't exist
        os.makedirs(uploads_dir, exist_ok=True)
        logger.info(f"Upload directory created/verified: {uploads_dir}")

        # Validate file type
        allowed_exts = {".csv", ".xlsx", ".xls"}
        file_ext = os.path.splitext(file.filename or "")[1].lower()

        if file_ext not in allowed_exts:
            raise HTTPException(status_code=400, detail=f"File type not allowed: {file_ext}. Only CSV/Excel allowed.")

        # Validate file size (max 50MB)
        try:
            content = await file.read()
            if len(content) > 50 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="File too large (max 50MB)")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"File read error: {str(e)}")
            raise HTTPException(status_code=400, detail="File read error")

        # Save file with timestamp prefix (unique names) + path traversal guard
        safe_name = f"{int(time.time())}_{os.path.basename(file.filename or 'upload')}"
        filepath = os.path.join(uploads_dir, safe_name)
        if not os.path.abspath(filepath).startswith(os.path.abspath(uploads_dir)):
            raise HTTPException(status_code=400, detail="Invalid filename")
        logger.info(f"Saving file to: {filepath}")

        with open(filepath, "wb") as f:
            f.write(content)

        logger.info(f"File uploaded successfully: {filepath}")
        return {
            "success": True,
            "filename": safe_name,
            "original_name": file.filename,
            "size": len(content)
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Upload failed")


@router.get("/api/files/list")
async def list_files(directory: str = "uploads", limit: int = Query(20, ge=1, le=200)):
    """List files in uploads or reports directory."""
    try:
        if directory == "uploads":
            files = connector.list_uploaded_files(limit)
        elif directory == "reports":
            reports_dir = connector.get_reports_dir()
            os.makedirs(reports_dir, exist_ok=True)
            files = []
            for f in os.listdir(reports_dir)[:limit]:
                fpath = os.path.join(reports_dir, f)
                if os.path.isfile(fpath):
                    files.append({
                        "name": f,
                        "size": os.path.getsize(fpath),
                        "type": f.split(".")[-1] if "." in f else "unknown"
                    })
            files = sorted(files, key=lambda x: x["name"], reverse=True)
        else:
            return {"error": "Invalid directory (must be 'uploads' or 'reports')"}

        return {"files": files, "count": len(files)}
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return {"error": "Failed to list files"}
