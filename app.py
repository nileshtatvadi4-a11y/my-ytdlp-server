import os
import sys
import subprocess
from flask import Flask, request, jsonify, Response, stream_with_context
import requests
import yt_dlp

app = Flask(__name__)

UPDATE_SECRET_KEY = "mysecret123"
COOKIE_PATH = "/tmp/yt_cookies.txt"

# Smart Cookie Loader: Detects cookies from Environment Variables or Secret Files
def ensure_cookie_file():
    cookie_content = os.environ.get('YOUTUBE_COOKIES')
    if not cookie_content:
        # Fallback in case variable name was slightly different
        for k, v in os.environ.items():
            if 'cookie' in k.lower() or '# Netscape' in v or '__Secure' in v:
                cookie_content = v
                break
    
    if cookie_content and len(cookie_content.strip()) > 30:
        with open(COOKIE_PATH, "w", encoding="utf-8") as f:
            f.write(cookie_content.strip() + "\n")
        return COOKIE_PATH
    
    if os.path.exists(COOKIE_PATH):
        return COOKIE_PATH
    return None

@app.route('/')
def home():
    cookie_file = ensure_cookie_file()
    return jsonify({
        "status": "online",
        "service": "Personal yt-dlp API",
        "engine_version": yt_dlp.version.__version__,
        "cookies_loaded": bool(cookie_file)
    })

# 1. Media Info Endpoint with Cookies & Accurate File Size
@app.route('/info', methods=['GET'])
def get_info():
    video_url = request.args.get('url')
    req_format = request.args.get('format', 'mp4')

    if not video_url:
        return jsonify({"success": False, "error": "Missing URL parameter"}), 400

    cookie_file = ensure_cookie_file()

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
    }

    if cookie_file:
        ydl_opts['cookiefile'] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            formats = info.get('formats', [])

            # Filter out preview/storyboard images and invalid links
            media_formats = [
                f for f in formats 
                if f.get('url') 
                and not str(f.get('format_id', '')).startswith('sb')
                and f.get('ext') not in ['mhtml', 'jpg', 'webp', 'png']
            ]

            if not media_formats:
                return jsonify({"success": False, "error": "No playable media streams found."}), 404

            chosen = None
            ext = "mp4"

            if req_format == 'mp3':
                # Select best audio stream
                audio_streams = [
                    f for f in media_formats 
                    if f.get('acodec') != 'none' and f.get('vcodec') == 'none'
                ]
                if not audio_streams:
                    audio_streams = [f for f in media_formats if f.get('acodec') != 'none']
                
                chosen = max(audio_streams, key=lambda f: f.get('abr') or f.get('tbr') or 0)
                ext = "m4a"
            else:
                # Prioritize progressive video (video + audio combined)
                progressive = [
                    f for f in media_formats 
                    if f.get('vcodec') != 'none' and f.get('acodec') != 'none'
                ]
                if progressive:
                    chosen = max(progressive, key=lambda f: f.get('height') or 0)
                else:
                    video_streams = [f for f in media_formats if f.get('vcodec') != 'none']
                    chosen = max(video_streams, key=lambda f: f.get('height') or f.get('tbr') or 0) if video_streams else media_formats[-1]
                
                ext = chosen.get('ext', 'mp4')

            if not chosen or not chosen.get('url'):
                return jsonify({"success": False, "error": "Playable stream link unavailable"}), 404

            # Accurate filesize calculation
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

# 2. Proxy Streamer with Range Support (For Android DownloadManager)
@app.route('/stream', methods=['GET'])
def stream_media():
    target_stream_url = request.args.get('url')
    if not target_stream_url:
        return "Missing stream URL", 400

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # Forward Range header from Android DownloadManager
    range_header = request.headers.get('Range')
    if range_header:
        headers['Range'] = range_header

    try:
        req = requests.get(target_stream_url, headers=headers, stream=True, timeout=30)
        response_headers = {
            'Content-Type': req.headers.get('content-type', 'application/octet-stream'),
            'Accept-Ranges': 'bytes'
        }
        if req.headers.get('content-length'):
            response_headers['Content-Length'] = req.headers.get('content-length')
        if req.headers.get('content-range'):
            response_headers['Content-Range'] = req.headers.get('content-range')

        def generate():
            for chunk in req.iter_content(chunk_size=1024 * 64):
                if chunk:
                    yield chunk

        return Response(stream_with_context(generate()), status=req.status_code, headers=response_headers)
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
