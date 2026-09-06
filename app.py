import os
import sys
import re
import subprocess
from flask import Flask, request, jsonify, Response, stream_with_context
import requests
import yt_dlp

app = Flask(__name__)

UPDATE_SECRET_KEY = "mysecret123"
COOKIE_PATH = "/tmp/yt_cookies.txt"

def ensure_cookie_file():
    cookie_content = os.environ.get('YOUTUBE_COOKIES')
    if not cookie_content:
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

# 1. Media Info Endpoint with Clean URLs & Direct MP4 Streams
@app.route('/info', methods=['GET'])
def get_info():
    raw_url = request.args.get('url', '')
    req_format = request.args.get('format', 'mp4')

    # Smart Regex: Extracts pure URL even if text or newlines surround it
    url_match = re.search(r'https?://[^\s"\']+', raw_url)
    if not url_match:
        return jsonify({"success": False, "error": "Invalid YouTube URL"}), 400

    clean_video_url = url_match.group(0).strip()
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
            info = ydl.extract_info(clean_video_url, download=False)
            formats = info.get('formats', [])

            # Strictly reject manifests, m3u8 playlists, and images
            direct_streams = [
                f for f in formats
                if f.get('url')
                and 'm3u8' not in str(f.get('protocol', '')).lower()
                and not str(f.get('url', '')).endswith('.m3u8')
                and 'manifest.googlevideo.com' not in str(f.get('url', ''))
                and str(f.get('ext', '')).lower() not in ['mhtml', 'jpg', 'webp', 'png']
            ]

            if not direct_streams:
                return jsonify({"success": False, "error": "No direct media streams found."}), 404

            chosen = None
            ext = "mp4"

            if req_format == 'mp3':
                # Pure audio streams
                audio_streams = [
                    f for f in direct_streams 
                    if f.get('acodec') and f.get('acodec') != 'none' 
                    and (not f.get('vcodec') or f.get('vcodec') == 'none')
                ]
                if not audio_streams:
                    audio_streams = [f for f in direct_streams if f.get('acodec') and f.get('acodec') != 'none']
                
                chosen = max(audio_streams, key=lambda f: f.get('abr') or f.get('tbr') or 0)
                ext = "m4a"
            else:
                # Progressive video streams (Video + Audio combined in MP4)
                progressive = [
                    f for f in direct_streams 
                    if f.get('vcodec') and f.get('vcodec') != 'none' 
                    and f.get('acodec') and f.get('acodec') != 'none'
                ]
                if progressive:
                    # Pick 720p if available, otherwise 360p
                    chosen = max(progressive, key=lambda f: f.get('height') or 0)
                else:
                    chosen = direct_streams[-1]
                ext = chosen.get('ext', 'mp4')

            if not chosen or not chosen.get('url'):
                return jsonify({"success": False, "error": "Playable direct link unavailable"}), 404

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

# 2. Proxy Streamer with Range Support
@app.route('/stream', methods=['GET'])
def stream_media():
    target_stream_url = request.args.get('url')
    if not target_stream_url:
        return "Missing stream URL", 400

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

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

# 3. Engine Update Endpoint
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
