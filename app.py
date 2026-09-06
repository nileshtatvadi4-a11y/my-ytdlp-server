import os
import sys
import subprocess
from flask import Flask, request, jsonify, Response, stream_with_context
import requests
import yt_dlp

app = Flask(__name__)

UPDATE_SECRET_KEY = "mysecret123"

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "service": "Personal yt-dlp API",
        "engine_version": yt_dlp.version.__version__
    })

# 1. Media Info Endpoint (Using Native Android Client - No Cookies Needed)
@app.route('/info', methods=['GET'])
def get_info():
    video_url = request.args.get('url')
    req_format = request.args.get('format', 'mp4')

    if not video_url:
        return jsonify({"success": False, "error": "Missing URL parameter"}), 400

    # Spoof Android App - Bypasses Datacenter IP block without login/cookies
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android']
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            formats = info.get('formats', [])

            # Filter out storyboard, preview images, and mhtml
            media_formats = [
                f for f in formats 
                if f.get('url') 
                and not str(f.get('format_id', '')).startswith('sb')
                and f.get('ext') not in ['mhtml', 'jpg', 'webp', 'png']
            ]

            if not media_formats:
                return jsonify({"success": False, "error": "No media streams found"}), 404

            chosen = None
            ext = "mp4"

            if req_format == 'mp3':
                # Pick audio-only stream with highest bitrate
                audio_streams = [
                    f for f in media_formats 
                    if f.get('acodec') != 'none' and f.get('vcodec') == 'none'
                ]
                if not audio_streams:
                    audio_streams = [f for f in media_formats if f.get('acodec') != 'none']
                
                chosen = max(audio_streams, key=lambda f: f.get('abr') or f.get('tbr') or 0)
                ext = "m4a"
            else:
                # First priority: Progressive stream (Video + Audio combined in MP4)
                progressive = [
                    f for f in media_formats 
                    if f.get('vcodec') != 'none' and f.get('acodec') != 'none'
                ]
                if progressive:
                    chosen = max(progressive, key=lambda f: f.get('height') or 0)
                else:
                    # Fallback to any valid video stream
                    video_only = [f for f in media_formats if f.get('vcodec') != 'none']
                    chosen = max(video_only, key=lambda f: f.get('height') or f.get('tbr') or 0) if video_only else media_formats[-1]
                
                ext = chosen.get('ext', 'mp4')

            if not chosen or not chosen.get('url'):
                return jsonify({"success": False, "error": "Playable stream link unavailable"}), 404

            # Exact file size calculation
            filesize = chosen.get('filesize') or chosen.get('filesize_approx') or 0
            duration = info.get('duration', 0)
            
            if filesize == 0 and duration:
                tbr = chosen.get('tbr') or chosen.get('abr') or chosen.get('vbr') or 0
                if tbr:
                    filesize = int((tbr * 1024 * duration) / 8)

            return jsonify({
                "success": True,
                "title": info.get('title', 'YouTube Media'),
                "duration": duration,
                "filesize_bytes": filesize,
                "filesize_mb": round(filesize / (1024 * 1024), 2) if filesize > 0 else 0,
                "stream_url": chosen.get('url'),
                "resolution": f"{chosen.get('height')}p" if chosen.get('height') else "Audio",
                "ext": ext
            })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# 2. Proxy Streamer with Header Passthrough
@app.route('/stream', methods=['GET'])
def stream_media():
    target_stream_url = request.args.get('url')
    if not target_stream_url:
        return "Missing stream URL", 400

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36"
    }
    
    try:
        req = requests.get(target_stream_url, headers=headers, stream=True, timeout=25)
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

# 3. Engine Update
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
