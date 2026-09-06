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

# 1. Media Info & Dynamic Format Resolver
@app.route('/info', methods=['GET'])
def get_info():
    video_url = request.args.get('url')
    req_format = request.args.get('format', 'mp4')

    if not video_url:
        return jsonify({"success": False, "error": "Missing URL parameter"}), 400

    # No strict format lock here - allows yt-dlp to safely fetch all available streams
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['web', 'mweb', 'ios']
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            formats = info.get('formats', [])
            
            chosen_format = None
            ext = "mp4"

            if req_format == 'mp3':
                # Filter for audio-only streams
                audio_streams = [
                    f for f in formats 
                    if f.get('url') and f.get('acodec') != 'none' and f.get('vcodec') == 'none'
                ]
                if not audio_streams:
                    # Fallback to any stream with audio
                    audio_streams = [f for f in formats if f.get('url') and f.get('acodec') != 'none']
                
                if audio_streams:
                    # Pick highest bitrate audio
                    chosen_format = max(audio_streams, key=lambda f: f.get('abr') or f.get('tbr') or 0)
                ext = "m4a"
            else:
                # Filter for progressive streams (Both Video + Audio combined)
                progressive_streams = [
                    f for f in formats 
                    if f.get('url') and f.get('vcodec') != 'none' and f.get('acodec') != 'none'
                ]
                if progressive_streams:
                    # Pick highest resolution (720p if available, else 360p)
                    chosen_format = max(progressive_streams, key=lambda f: f.get('height') or 0)
                else:
                    # Fallback to best available single stream with direct URL
                    valid_streams = [f for f in formats if f.get('url')]
                    if valid_streams:
                        chosen_format = max(valid_streams, key=lambda f: f.get('height') or f.get('tbr') or 0)
                ext = chosen_format.get('ext', 'mp4') if chosen_format else "mp4"

            if not chosen_format or not chosen_format.get('url'):
                return jsonify({"success": False, "error": "No playable direct stream found for this video"}), 404

            # Calculate accurate file size
            filesize = chosen_format.get('filesize') or chosen_format.get('filesize_approx') or 0
            duration = info.get('duration', 0)
            
            # Smart bitrate fallback if filesize header is missing
            if filesize == 0 and duration:
                tbr = chosen_format.get('tbr') or chosen_format.get('abr') or chosen_format.get('vbr') or 0
                if tbr:
                    filesize = int((tbr * 1024 * duration) / 8)

            return jsonify({
                "success": True,
                "title": info.get('title', 'YouTube Media'),
                "duration": duration,
                "filesize_bytes": filesize,
                "filesize_mb": round(filesize / (1024 * 1024), 2) if filesize > 0 else 0,
                "stream_url": chosen_format.get('url'),
                "resolution": f"{chosen_format.get('height')}p" if chosen_format.get('height') else "Audio",
                "ext": ext
            })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# 2. Proxy Streamer with Direct Header Passthrough
@app.route('/stream', methods=['GET'])
def stream_media():
    target_stream_url = request.args.get('url')
    if not target_stream_url:
        return "Missing stream URL", 400

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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
