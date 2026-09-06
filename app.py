import os
import sys
import subprocess
from flask import Flask, request, jsonify, Response, stream_with_context
import requests
import yt_dlp

app = Flask(__name__)

# Secret key for updating engine
UPDATE_SECRET_KEY = "mysecret123"

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "service": "Personal yt-dlp API",
        "engine_version": yt_dlp.version.__version__
    })

# 1. Media Info & Exact Size Endpoint
@app.route('/info', methods=['GET'])
def get_info():
    video_url = request.args.get('url')
    req_format = request.args.get('format', 'mp4')

    if not video_url:
        return jsonify({"success": False, "error": "Missing URL parameter"}), 400

    # YouTube Bot-Block Bypass Configuration
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web']
            }
        }
    }

    if req_format == 'mp3':
        ydl_opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
    else:
        # Ensures video and audio are combined in a single stream without requiring local ffmpeg merge
        ydl_opts['format'] = 'best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            
            # File size calculation
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            
            # Fallback estimation if filesize is missing from headers
            if filesize == 0 and info.get('duration') and info.get('tbr'):
                # (bitrate in kbps * duration in seconds * 1024) / 8
                filesize = int((info['tbr'] * info['duration'] * 1024) / 8)

            stream_url = info.get('url')
            if not stream_url and 'formats' in info and len(info['formats']) > 0:
                stream_url = info['formats'][-1].get('url')

            return jsonify({
                "success": True,
                "title": info.get('title', 'YouTube Media'),
                "duration": info.get('duration', 0),
                "filesize_bytes": filesize,
                "filesize_mb": round(filesize / (1024 * 1024), 2) if filesize > 0 else 0,
                "stream_url": stream_url,
                "ext": "mp3" if req_format == 'mp3' else "mp4"
            })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# 2. Proxy Streamer (Forwards Content-Length to Android)
@app.route('/stream', methods=['GET'])
def stream_media():
    target_stream_url = request.args.get('url')
    if not target_stream_url:
        return "Missing stream URL", 400

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"
    }
    
    try:
        req = requests.get(target_stream_url, headers=headers, stream=True, timeout=20)
        total_length = req.headers.get('content-length')
        response_headers = {
            'Content-Type': req.headers.get('content-type', 'application/octet-stream')
        }
        if total_length:
            response_headers['Content-Length'] = total_length

        def generate():
            for chunk in req.iter_content(chunk_size=1024 * 64):
                if chunk:
                    yield chunk

        return Response(stream_with_context(generate()), headers=response_headers)
    except Exception as e:
        return f"Stream failed: {str(e)}", 500

# 3. In-App yt-dlp Update Endpoint
@app.route('/update', methods=['GET'])
def update_engine():
    key = request.args.get('key')
    if key != UPDATE_SECRET_KEY:
        return jsonify({"success": False, "message": "Unauthorized: Invalid Secret Key"}), 403

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"],
            capture_output=True,
            text=True
        )
        return jsonify({
            "success": True,
            "message": "Engine upgraded successfully!",
            "details": result.stdout.splitlines()[-1] if result.stdout else "Up to date"
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
