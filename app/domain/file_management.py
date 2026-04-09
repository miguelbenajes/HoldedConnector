"""File management — uploads and reports directory configuration.

Provides directory path resolution (settings → env → default) and
file listing for the uploads/reports directories.
"""

import os
import logging

from app.db.settings import get_setting, save_setting

logger = logging.getLogger(__name__)


def get_uploads_dir():
    """Return uploads directory path.
    Priority: settings table > env var > default.
    Note: Directory may not exist yet; calling code should create it."""
    saved = get_setting("uploads_dir")
    if saved:
        return saved
    env_path = os.getenv("UPLOADS_DIR", "")
    if env_path:
        return env_path
    return os.path.abspath("uploads")


def get_reports_dir():
    """Return reports directory path.
    Priority: settings table > env var > default.
    Note: Directory may not exist yet; calling code should create it."""
    saved = get_setting("reports_dir")
    if saved:
        return saved
    env_path = os.getenv("REPORTS_DIR", "")
    if env_path:
        return env_path
    return os.path.abspath("reports")


def set_uploads_dir(path):
    """Validate and save uploads directory path.
    Returns: {"success": True, "path": path} or {"error": "..."}"""
    if not path:
        return {"error": "Path cannot be empty"}
    if not os.path.isabs(path):
        return {"error": "Path must be absolute (e.g., /home/user/uploads)"}
    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            return {"error": f"Cannot create directory: {str(e)}"}
    if not os.path.isdir(path):
        return {"error": "Path is not a directory"}
    if not os.access(path, os.W_OK):
        return {"error": "Directory is not writable"}
    save_setting("uploads_dir", path)
    logger.info(f"Uploads directory set to: {path}")
    return {"success": True, "path": path}


def set_reports_dir(path):
    """Validate and save reports directory path.
    Returns: {"success": True, "path": path} or {"error": "..."}"""
    if not path:
        return {"error": "Path cannot be empty"}
    if not os.path.isabs(path):
        return {"error": "Path must be absolute (e.g., /home/user/reports)"}
    if not os.path.exists(path):
        try:
            os.makedirs(path, exist_ok=True)
        except Exception as e:
            return {"error": f"Cannot create directory: {str(e)}"}
    if not os.path.isdir(path):
        return {"error": "Path is not a directory"}
    if not os.access(path, os.W_OK):
        return {"error": "Directory is not writable"}
    save_setting("reports_dir", path)
    logger.info(f"Reports directory set to: {path}")
    return {"success": True, "path": path}


def list_uploaded_files(limit=50):
    """List uploaded files with metadata.
    Returns list of dicts with: name, size, uploaded_at, type"""
    uploads_dir = get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)
    files = []
    try:
        for f in os.listdir(uploads_dir)[:limit]:
            fpath = os.path.join(uploads_dir, f)
            if os.path.isfile(fpath):
                files.append({
                    "name": f,
                    "size": os.path.getsize(fpath),
                    "uploaded_at": os.path.getmtime(fpath),
                    "type": f.split(".")[-1] if "." in f else "unknown"
                })
    except Exception as e:
        logger.error(f"Error listing uploaded files: {str(e)}")
    return sorted(files, key=lambda x: x["uploaded_at"], reverse=True)
