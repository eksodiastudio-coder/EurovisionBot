#!/usr/bin/env bash
# Exit immediately if a command exits with a non-zero status
set -e

echo "=== Installing Python dependencies ==="
pip install --upgrade pip
pip install -r requirements.txt

echo "=== Installing FFmpeg ==="
# Download a static build of ffmpeg for 64-bit Linux
FFMPEG_DIR="$HOME/ffmpeg_bin"
mkdir -p "$FFMPEG_DIR"

if [ ! -f "$FFMPEG_DIR/ffmpeg" ]; then
    echo "Downloading static FFmpeg..."
    curl -sL https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz | tar -xJ --strip-components=1 -C "$FFMPEG_DIR"
    chmod +x "$FFMPEG_DIR/ffmpeg"
    chmod +x "$FFMPEG_DIR/ffprobe"
fi

echo "=== Build Complete ==="
