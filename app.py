import os
import sys
import subprocess
from flask import Flask, request, jsonify, Response, stream_with_context
import requests
import yt_dlp

app = Flask(__name__)

# अपनी पसंद का सीक्रेट पासवर्ड यहाँ सेट करें (CSR से अपडेट ट्रिगर करने के लिए)
UPDATE_SECRET_KEY = "mysecret123"

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "service": "Personal yt-dlp API",
        "engine_version": yt_dlp.version.__version__
    })

# 🌟 1. वीडियो/ऑडियो जानकारी और सटीक साइज़ निकालने का एंडपॉइंट
@app.route('/info', methods=['GET'])
def get_info():
    video_url = request.args.get('url')
    req_format = request.args.get('format', 'mp4') # 'mp4' या 'mp3'

    if not video_url:
        return jsonify({"error": "Missing URL parameter"}), 400

    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }

    if req_format == 'mp3':
        ydl_opts['format'] = 'bestaudio/best'
    else:
        # 720p प्रोग्रेसिव वीडियो जिसमें ऑडियो साथ हो
        ydl_opts['format'] = 'best[ext=mp4][height<=720]/best[height<=720]/best'

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            
            # सटीक फ़ाइल साइज़ निकालना
            filesize = info.get('filesize') or info.get('filesize_approx') or 0
            
            return jsonify({
                "success": True,
                "title": info.get('title', 'YouTube Media'),
                "duration": info.get('duration', 0),
                "filesize_bytes": filesize,
                "filesize_mb": round(filesize / (1024 * 1024), 2) if filesize > 0 else 0,
                "stream_url": info.get('url'),
                "ext": "mp3" if req_format == 'mp3' else "mp4"
            })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# 🌟 2. IP-Lock को बाईपास करने वाला स्मार्ट डाउनलोड प्रॉक्सी
# (यह सर्वर से वीडियो स्ट्रीम करके फ़ोन को भेजता है और Content-Length हेडर जोड़ता है)
@app.route('/stream', methods=['GET'])
def stream_media():
    target_stream_url = request.args.get('url')
    if not target_stream_url:
        return "Missing stream URL", 400

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
    
    # YouTube सर्वर से कनेक्शन
    req = requests.get(target_stream_url, headers=headers, stream=True)
    
    # फ़ोन के DownloadManager को असली साइज़ भेजना
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

# 🌟 3. वन-क्लिक इंजन अपडेट एंडपॉइंट (CSR से अपडेट करने के लिए)
@app.route('/update', methods=['GET'])
def update_engine():
    key = request.args.get('key')
    if key != UPDATE_SECRET_KEY:
        return jsonify({"success": False, "message": "Unauthorized: Invalid Secret Key"}), 403

    try:
        # बैकग्राउंड में सीधे yt-dlp का नया वर्शन इंस्टॉल करना
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
