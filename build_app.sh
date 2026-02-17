#!/bin/bash

echo "🚀 Building Holded Connector Desktop App..."

# 1. Clean previous builds
rm -rf build dist

# 2. Run PyInstaller
# --onefile: single executable
# --noconsole: hide terminal
# --add-data: include static files
# --hidden-import: ensure all submodules are included if needed
# --windowed: creates a .app bundle on macOS
# --icon: optional, add if we had one
pyinstaller --noconsole --windowed --clean \
    --add-data "static:static" \
    --name "HoldedInsights" \
    --hidden-import "uvicorn.logging" \
    --hidden-import "uvicorn.loops" \
    --hidden-import "uvicorn.loops.auto" \
    --hidden-import "uvicorn.protocols" \
    --hidden-import "uvicorn.protocols.http" \
    --hidden-import "uvicorn.protocols.http.auto" \
    --hidden-import "uvicorn.lifespan" \
    --hidden-import "uvicorn.lifespan.on" \
    main_desktop.py

echo "✅ Build complete! You can find the executable in the 'dist' folder."
echo "💡 Note: You will still need to provide a .env file with your API key in the same folder as the executable."
