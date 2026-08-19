#!/usr/bin/env bash
# Exit immediately on error
set -e

echo "=== Upgrading pip and installing requirements ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Installing FFmpeg into Python environment ==="
# Download static ffmpeg and place binaries directly into the active Python bin folder
curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar -xJ --wildcards --strip-components=1 -C "$VIRTUAL_ENV/bin" "*/ffmpeg" "*/ffprobe"

# Make executable
chmod +x "$VIRTUAL_ENV/bin/ffmpeg"
chmod +x "$VIRTUAL_ENV/bin/ffprobe"

echo "=== FFmpeg installed successfully ==="
