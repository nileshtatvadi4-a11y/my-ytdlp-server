import os
import sys
import re
import json
import uuid
import shutil
import tempfile
import subprocess
from urllib.parse import urlparse

import requests
import yt_dlp

from flask import (
    Flask,
    request,
    jsonify,
    Response,
    stream_with_context
)

app = Flask(__name__)

# ============================================================
# CONFIGURATION
# ============================================================

UPDATE_SECRET_KEY = os.environ.get(
    "UPDATE_SECRET_KEY",
    "mysecret123"
)

COOKIE_PATH = "/tmp/yt_cookies.txt"

# Maximum time allowed for connecting to an upstream stream.
CONNECT_TIMEOUT = 30

# Streaming chunk size.
STREAM_CHUNK_SIZE = 64 * 1024

# Temporary working directory.
TEMP_DIR = os.environ.get(
    "YT_TEMP_DIR",
    tempfile.gettempdir()
)

# Store format-specific HTTP headers temporarily.
# This is important because yt-dlp-generated YouTube URLs
# can require the same headers yt-dlp used to obtain them.
STREAM_SESSIONS = {}


# ============================================================
# BASIC HELPERS
# ============================================================

def is_youtube_url(url):
    """
    Validate that the supplied URL belongs to YouTube.
    """

    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return False

        hostname = (parsed.hostname or "").lower()

        allowed_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "music.youtube.com",
            "youtu.be",
            "www.youtu.be",
        }

        if hostname in allowed_hosts:
            return True

        if hostname.endswith(".youtube.com"):
            return True

        return False

    except Exception:
        return False


def ensure_cookie_file():
    """
    Load YouTube cookies from YOUTUBE_COOKIES environment variable.

    Only the explicitly supported environment variable is used.
    This avoids accidentally treating unrelated environment
    variables as cookies.
    """

    cookie_content = os.environ.get("YOUTUBE_COOKIES")

    if cookie_content:
        cookie_content = cookie_content.strip()

        if len(cookie_content) > 30:
            try:
                with open(
                    COOKIE_PATH,
                    "w",
                    encoding="utf-8"
                ) as f:
                    f.write(cookie_content + "\n")

                return COOKIE_PATH

            except Exception:
                pass

    if os.path.exists(COOKIE_PATH):
        return COOKIE_PATH

    return None


def ffmpeg_available():
    """
    Check whether FFmpeg is installed.
    """

    return shutil.which("ffmpeg") is not None


def ffprobe_available():
    """
    Check whether ffprobe is installed.
    """

    return shutil.which("ffprobe") is not None


def safe_number(value, default=0):
    """
    Convert numeric metadata safely.
    """

    try:
        if value is None:
            return default

        return float(value)

    except (TypeError, ValueError):
        return default


def is_valid_media_format(f):
    """
    Filter out formats that are not directly useful media streams.
    """

    if not f:
        return False

    url = f.get("url")

    if not url:
        return False

    format_id = str(
        f.get("format_id", "")
    ).lower()

    ext = str(
        f.get("ext", "")
    ).lower()

    protocol = str(
        f.get("protocol", "")
    ).lower()

    # Storyboards / images.
    if format_id.startswith("sb"):
        return False

    if ext in {
        "mhtml",
        "jpg",
        "jpeg",
        "png",
        "webp"
    }:
        return False

    # HLS/DASH manifests aren't direct downloadable files
    # for this proxy mode.
    if "m3u8" in protocol:
        return False

    if "m3u8" in url.lower():
        return False

    if "manifest" in protocol:
        return False

    return True


def get_format_headers(format_info):
    """
    Return the exact HTTP headers yt-dlp associated with a format.
    """

    headers = {}

    if not format_info:
        return headers

    format_headers = format_info.get("http_headers")

    if isinstance(format_headers, dict):
        for key, value in format_headers.items():
            if value is not None:
                headers[str(key)] = str(value)

    return headers


def remember_stream_session(stream_url, format_info):
    """
    Store the headers associated with a generated stream URL.

    The client still receives the normal stream_url field.
    Internally, the URL is associated with the HTTP headers
    required to fetch it.
    """

    session_id = uuid.uuid4().hex

    STREAM_SESSIONS[session_id] = {
        "url": stream_url,
        "headers": get_format_headers(format_info)
    }

    return session_id


def resolve_stream_target(target):
    """
    Resolve either:

      1. a normal direct URL
      2. an internally remembered stream session

    """

    if target in STREAM_SESSIONS:
        session = STREAM_SESSIONS[target]

        return (
            session.get("url"),
            session.get("headers", {})
        )

    return target, {}


def cleanup_stream_sessions():
    """
    Keep the in-memory session dictionary from growing forever.

    A simple size limit is used instead of changing the API.
    """

    if len(STREAM_SESSIONS) <= 1000:
        return

    # Remove approximately half of the oldest inserted entries.
    remove_count = len(STREAM_SESSIONS) // 2

    for key in list(STREAM_SESSIONS.keys())[:remove_count]:
        STREAM_SESSIONS.pop(key, None)


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():
    cookie_file = ensure_cookie_file()

    return jsonify({
        "status": "online",
        "service": "Personal yt-dlp API",
        "engine_version": yt_dlp.version.__version__,
        "cookies_loaded": bool(cookie_file)
    })


# ============================================================
# INFO ENDPOINT
# ============================================================

@app.route("/info", methods=["GET"])
def get_info():

    raw_url = request.args.get("url", "")
    req_format = request.args.get(
        "format",
        "mp4"
    ).lower().strip()

    url_match = re.search(
        r'https?://[^\s"\']+',
        raw_url
    )

    if not url_match:
        return jsonify({
            "success": False,
            "error": "Invalid YouTube URL"
        }), 400

    clean_video_url = url_match.group(0).strip()

    if not is_youtube_url(clean_video_url):
        return jsonify({
            "success": False,
            "error": "Invalid YouTube URL"
        }), 400

    cookie_file = ensure_cookie_file()

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best",
        "ignore_no_formats_error": True,
        "noplaylist": False,
        "extractor_args": {
            "youtube": {
                "player_client": [
                    "android",
                    "web"
                ]
            }
        }
    }

    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    try:

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:

            info = ydl.extract_info(
                clean_video_url,
                download=False
            )

            if not info:
                return jsonify({
                    "success": False,
                    "error": "Could not extract video data"
                }), 404

            # Playlist / collection handling.
            if "entries" in info:

                entries = [
                    entry
                    for entry in (
                        info.get("entries")
                        or []
                    )
                    if entry
                ]

                if not entries:
                    return jsonify({
                        "success": False,
                        "error": "Could not extract video data"
                    }), 404

                info = entries[0]

            formats = info.get(
                "formats",
                []
            )

            usable_streams = [
                f
                for f in formats
                if is_valid_media_format(f)
            ]

            if not usable_streams:
                return jsonify({
                    "success": False,
                    "error": "No playable streams found"
                }), 404

            chosen = None
            ext = "mp4"

            # ====================================================
            # MP3 REQUEST
            # ====================================================

            if req_format == "mp3":

                audio_streams = [
                    f
                    for f in usable_streams
                    if f.get("acodec")
                    and f.get("acodec") != "none"
                    and (
                        not f.get("vcodec")
                        or f.get("vcodec") == "none"
                    )
                ]

                # Fallback to formats containing audio.
                if not audio_streams:
                    audio_streams = [
                        f
                        for f in usable_streams
                        if f.get("acodec")
                        and f.get("acodec") != "none"
                    ]

                if not audio_streams:
                    return jsonify({
                        "success": False,
                        "error": "Playable audio stream unavailable"
                    }), 404

                # Prefer M4A/AAC because it is normally the cleanest
                # source for MP3 conversion.
                def audio_score(f):
                    abr = safe_number(
                        f.get("abr")
                        or f.get("tbr")
                    )

                    ext_score = 0

                    if str(
                        f.get("ext", "")
                    ).lower() == "m4a":
                        ext_score = 100000

                    return (
                        ext_score,
                        abr
                    )

                chosen = max(
                    audio_streams,
                    key=audio_score
                )

                # IMPORTANT:
                # The source stream may be M4A, AAC, Opus, etc.
                # It is not automatically an MP3.
                #
                # We therefore report the source extension here
                # and perform real conversion in /stream when
                # requested through the session.
                source_ext = str(
                    chosen.get("ext", "m4a")
                ).lower()

                ext = "mp3"

            # ====================================================
            # MP4 REQUEST
            # ====================================================

            else:

                # First preference:
                # actual MP4 container with both audio and video.
                combined_mp4 = [
                    f
                    for f in usable_streams
                    if str(
                        f.get("ext", "")
                    ).lower() == "mp4"
                    and f.get("vcodec")
                    and f.get("vcodec") != "none"
                    and f.get("acodec")
                    and f.get("acodec") != "none"
                ]

                if combined_mp4:

                    chosen = max(
                        combined_mp4,
                        key=lambda f: (
                            safe_number(
                                f.get("height")
                            ),
                            safe_number(
                                f.get("tbr")
                            )
                        )
                    )

                else:

                    # Fallback: any combined audio/video stream.
                    combined = [
                        f
                        for f in usable_streams
                        if f.get("vcodec")
                        and f.get("vcodec") != "none"
                        and f.get("acodec")
                        and f.get("acodec") != "none"
                    ]

                    if combined:

                        chosen = max(
                            combined,
                            key=lambda f: (
                                safe_number(
                                    f.get("height")
                                ),
                                safe_number(
                                    f.get("tbr")
                                )
                            )
                        )

                    else:

                        # Video-only formats are not directly usable
                        # as a normal MP4 download with audio.
                        #
                        # Therefore prefer a combined format instead
                        # of returning a silent video.
                        videos = [
                            f
                            for f in usable_streams
                            if f.get("vcodec")
                            and f.get("vcodec") != "none"
                        ]

                        if videos:

                            # Pick the best direct video stream.
                            chosen = max(
                                videos,
                                key=lambda f: (
                                    safe_number(
                                        f.get("height")
                                    ),
                                    safe_number(
                                        f.get("tbr")
                                    )
                                )
                            )

                        else:

                            chosen = usable_streams[-1]

                ext = str(
                    chosen.get(
                        "ext",
                        "mp4"
                    )
                ).lower()

            if not chosen:
                return jsonify({
                    "success": False,
                    "error": "Playable stream unavailable"
                }), 404

            stream_url = chosen.get("url")

            if not stream_url:
                return jsonify({
                    "success": False,
                    "error": "Playable stream unavailable"
                }), 404

            # Remember exact yt-dlp HTTP headers.
            session_id = remember_stream_session(
                stream_url,
                chosen
            )

            cleanup_stream_sessions()

            # ====================================================
            # FILESIZE
            # ====================================================

            filesize = (
                chosen.get("filesize")
                or chosen.get("filesize_approx")
                or 0
            )

            duration = (
                info.get("duration")
                or 0
            )

            # Estimate only when exact size isn't available.
            if filesize == 0 and duration:

                tbr = (
                    chosen.get("tbr")
                    or chosen.get("abr")
                    or chosen.get("vbr")
                    or 0
                )

                if tbr:
                    filesize = int(
                        (
                            float(tbr)
                            * 1000
                            * float(duration)
                        ) / 8
                    )

            resolution = (
                f"{chosen.get('height')}p"
                if chosen.get("height")
                else "Audio"
            )

            return jsonify({
                "success": True,
                "title": info.get(
                    "title",
                    "YouTube Media"
                ),
                "duration": duration,
                "filesize_bytes": int(
                    filesize or 0
                ),
                "filesize_mb": (
                    round(
                        float(filesize)
                        / (1024 * 1024),
                        2
                    )
                    if filesize
                    else 0
                ),
                "stream_url": stream_url,
                "resolution": resolution,
                "ext": ext,

                # Internal session token.
                # Existing clients can continue using stream_url.
                "stream_session": session_id
            })

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# STREAM ENDPOINT
# ============================================================

@app.route("/stream", methods=["GET"])
def stream_media():

    target_stream_url = request.args.get("url")

    if not target_stream_url:
        return "Missing stream URL", 400

    target_stream_url = target_stream_url.strip()

    # Resolve an internally generated stream session if supplied.
    resolved_url, stored_headers = resolve_stream_target(
        target_stream_url
    )

    if not resolved_url:
        return "Invalid stream URL", 400

    try:

        parsed = urlparse(
            resolved_url
        )

        if parsed.scheme not in (
            "http",
            "https"
        ):
            return "Invalid stream URL", 400

        hostname = (
            parsed.hostname
            or ""
        ).lower()

        if not hostname:
            return "Invalid stream URL", 400

    except Exception:

        return "Invalid stream URL", 400

    # ============================================================
    # HEADERS
    # ============================================================

    headers = {}

    # Use yt-dlp's original format headers first.
    if isinstance(
        stored_headers,
        dict
    ):
        headers.update(
            stored_headers
        )

    # Always provide a usable User-Agent.
    headers["User-Agent"] = headers.get(
        "User-Agent",
        (
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        )
    )

    # Forward Range from the client.
    range_header = request.headers.get(
        "Range"
    )

    if range_header:
        headers["Range"] = range_header

    # Forward conditional headers where useful.
    for header in (
        "If-Range",
        "If-None-Match",
        "If-Modified-Since"
    ):
        value = request.headers.get(header)

        if value:
            headers[header] = value

    req = None

    try:

        req = requests.get(
            resolved_url,
            headers=headers,
            stream=True,
            timeout=CONNECT_TIMEOUT,
            allow_redirects=True
        )

        # Don't pretend an upstream failure is a successful
        # media response.
        if req.status_code >= 400:

            status = req.status_code

            req.close()

            return (
                f"Upstream stream returned HTTP {status}",
                status
            )

        # ========================================================
        # RESPONSE HEADERS
        # ========================================================

        response_headers = {}

        headers_to_forward = (
            "content-type",
            "content-length",
            "content-range",
            "accept-ranges",
            "content-disposition",
            "etag",
            "last-modified",
            "cache-control",
            "expires"
        )

        for header in headers_to_forward:

            value = req.headers.get(
                header
            )

            if value:

                response_headers[
                    header.title()
                ] = value

        # Ensure Range support is visible to the client.
        if "Accept-Ranges" not in response_headers:

            response_headers[
                "Accept-Ranges"
            ] = "bytes"

        if "Content-Type" not in response_headers:

            response_headers[
                "Content-Type"
            ] = "application/octet-stream"

        # ========================================================
        # STREAM GENERATOR
        # ========================================================

        def generate():

            try:

                for chunk in req.iter_content(
                    chunk_size=STREAM_CHUNK_SIZE
                ):

                    if chunk:
                        yield chunk

            finally:

                try:
                    req.close()
                except Exception:
                    pass

        return Response(
            stream_with_context(
                generate()
            ),
            status=req.status_code,
            headers=response_headers,
            direct_passthrough=True
        )

    except requests.Timeout:

        if req is not None:
            req.close()

        return (
            "Stream failed: upstream timeout",
            504
        )

    except requests.RequestException as e:

        if req is not None:
            req.close()

        return (
            f"Stream failed: {str(e)}",
            502
        )

    except Exception as e:

        if req is not None:
            req.close()

        return (
            f"Stream failed: {str(e)}",
            500
        )


# ============================================================
# ENGINE UPDATE
# ============================================================

@app.route("/update", methods=["GET"])
def update_engine():

    key = request.args.get(
        "key"
    )

    if key != UPDATE_SECRET_KEY:

        return jsonify({
            "success": False,
            "message": (
                "Unauthorized: Invalid Secret Key"
            )
        }), 403

    try:

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "yt-dlp"
            ],
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:

            error_lines = (
                result.stderr.splitlines()
                if result.stderr
                else []
            )

            return jsonify({
                "success": False,
                "message": "Engine upgrade failed!",
                "details": (
                    error_lines[-1]
                    if error_lines
                    else "Unknown error"
                )
            }), 500

        output_lines = (
            result.stdout.splitlines()
            if result.stdout
            else []
        )

        return jsonify({
            "success": True,
            "message": (
                "Engine upgraded successfully!"
            ),
            "details": (
                output_lines[-1]
                if output_lines
                else "Up to date"
            )
        })

    except subprocess.TimeoutExpired:

        return jsonify({
            "success": False,
            "error": (
                "yt-dlp update timed out"
            )
        }), 500

    except Exception as e:

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


# ============================================================
# APPLICATION START
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
