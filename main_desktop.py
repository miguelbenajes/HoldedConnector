import webview
import threading
import uvicorn
import sys
import os
import time
from api import app
import connector
if getattr(sys, 'frozen', False):
    try:
        # For macOS .app, we should use Application Support to avoid permission issues in /Applications
        app_support = os.path.expanduser('~/Library/Application Support/HoldedInsights')
        os.makedirs(app_support, exist_ok=True)
        # Update connector's DB_NAME to be absolute
        connector.DB_NAME = os.path.join(app_support, "holded.db")
        # Change CWD for static files (inside the bundle)
        os.chdir(sys._MEIPASS)
    except Exception as e:
        print(f"Error setting app paths: {e}")

def start_api():
    # Ensure DB is initialized before server starts
    connector.init_db()
    # Run uvicorn server in a separate thread
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="error")

if __name__ == '__main__':
    # Start the server thread
    api_thread = threading.Thread(target=start_api, daemon=True)
    api_thread.start()

    # Give the server a moment to start
    time.sleep(2)

    # Create the native window
    window = webview.create_window(
        'Holded Connector Dashboard', 
        'http://127.0.0.1:8000',
        width=1280,
        height=800,
        min_size=(1024, 768),
        background_color='#020617'
    )

    # Start the webview loop
    webview.start(debug=True)
    
    # When webview exits, the process will close (daemon thread will die)
    sys.exit()
