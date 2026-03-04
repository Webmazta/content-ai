#!/usr/bin/env python3
"""
Social Media Video → WordPress Auto-Publisher
==========================================
Downloads videos from any social media platform (Instagram, TikTok, YouTube, X, etc.),
analyzes them with Google Gemini AI, generates blog content and media, 
then publishes to WordPress.

Usage:
    python main.py <video_url_1> [<video_url_2> ...]
    python main.py --file urls.txt
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from io import BytesIO
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

import google.generativeai as genai

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_CATEGORY_ID = int(os.getenv("WP_CATEGORY_ID", "1"))
WP_POST_STATUS = os.getenv("WP_POST_STATUS", "publish")
IG_USERNAME = os.getenv("IG_USERNAME", "")
IG_PASSWORD = os.getenv("IG_PASSWORD", "")

DOWNLOAD_DIR = Path("downloads")
OUTPUT_DIR = Path("output")

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _check_config():
    """Validate that all required env vars are set."""
    missing = []
    if not GOOGLE_API_KEY:
        missing.append("GOOGLE_API_KEY")
    if not WP_URL:
        missing.append("WP_URL")
    if not WP_USERNAME:
        missing.append("WP_USERNAME")
    if not WP_APP_PASSWORD:
        missing.append("WP_APP_PASSWORD")
    if missing:
        print(f"[ERROR] Missing environment variables: {', '.join(missing)}")
        print("        Copy .env.template → .env and fill in your values.")
        sys.exit(1)


def _ensure_dirs():
    """Create working directories if they don't exist."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """Try to load a nice font, fall back to default."""
    font_candidates = [
        # Windows
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        # Linux
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        # macOS
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
    ]
    for fp in font_candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    # Ultimate fallback
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _sanitize_filename(name: str) -> str:
    """Create a safe filename from a string."""
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'[\s]+', '_', name)
    return name[:80].strip("_")


# ──────────────────────────────────────────────
# 1. Video Download
# ──────────────────────────────────────────────

def download_video(url: str) -> Path:
    """
    Download a video from a social media URL.
    Tries multiple methods and fallbacks:
    - yt-dlp (with/without cookies) for most platforms
    - Specialized Instagram APIs/libraries for IG reels
    Returns the path to the downloaded video file.
    """
    print(f"\n[DOWNLOAD] Downloading video from: {url}")
    
    is_instagram = "instagram.com" in url.lower()

    # Clean the URL — remove tracking params
    clean_url = re.sub(r'\?.*$', '', url)

    import yt_dlp
    output_template = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")
    
    headers = {
        'Accept-Language': 'en-US,en;q=0.9',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    if "facebook.com" in url.lower():
        headers['Referer'] = 'https://www.facebook.com/'
    elif "instagram.com" in url.lower():
        headers['Referer'] = 'https://www.instagram.com/'
    elif "tiktok.com" in url.lower():
        headers['Referer'] = 'https://www.tiktok.com/'

    # ── Method 0: yt-dlp with manual cookies.txt ──
    cookies_file = Path("cookies.txt")
    if cookies_file.exists():
        try:
            print("   Method 0: Trying yt-dlp with manual cookies.txt...")
            ydl_opts = {
                'format': 'best',
                'outtmpl': output_template,
                'noplaylist': True,
                'quiet': False,
                'cookiefile': str(cookies_file.absolute()),
                'geo_bypass': True,
                'http_headers': headers
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(clean_url, download=True)
                filename = ydl.prepare_filename(info)
                if filename and Path(filename).exists():
                    print(f"   [OK] Downloaded (yt-dlp cookies.txt): {filename}")
                    return Path(filename)
        except Exception as e:
            print(f"   [SKIP] Manual cookies.txt failed: {e}")
    else:
        print("   (No manual cookies.txt found in project directory)")

    # ── Method 1: yt-dlp with Chrome cookies ──
    try:
        print("   Method 1: Trying yt-dlp with Chrome cookies...")
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': output_template,
            'noplaylist': True,
            'quiet': False,
            'cookiesfrombrowser': ('chrome',),
            'geo_bypass': True,
            'http_headers': headers
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            filename = ydl.prepare_filename(info)
            if filename and Path(filename).exists():
                print(f"   [OK] Downloaded (yt-dlp Chrome): {filename}")
                return Path(filename)
    except Exception as e:
        print(f"   [SKIP] Chrome cookies failed: {e}")

    # ── Method 2: yt-dlp with Edge cookies (fallback) ──
    try:
        print("   Method 2: Trying yt-dlp with Edge cookies...")
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': output_template,
            'noplaylist': True,
            'quiet': False,
            'cookiesfrombrowser': ('edge',),
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            filename = ydl.prepare_filename(info)
            if filename and Path(filename).exists():
                print(f"   [OK] Downloaded (yt-dlp Edge): {filename}")
                return Path(filename)
    except Exception as e:
        print(f"   [SKIP] Edge cookies failed: {e}")

    # ── Method 3: Direct Instagram API (numeric ID method) ──
    if is_instagram and IG_USERNAME and IG_PASSWORD:
        try:
            print("   Method 3: Trying Direct API method (Instagram only)...")
            return _download_via_api(clean_url)
        except Exception as e:
            print(f"   [SKIP] Direct API failed: {e}")

    # ── Method 4: instaloader ──
    if is_instagram:
        try:
            print("   Method 4: Trying instaloader (Instagram only)...")
            return _download_with_instaloader(clean_url)
        except Exception as e:
            print(f"   [SKIP] instaloader failed: {e}")

    # ── Method 5: yt-dlp without cookies ──
    try:
        print("   Method 5: Trying yt-dlp without cookies...")
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': output_template,
            'noplaylist': True,
            'quiet': False,
            'geo_bypass': True,
            'http_headers': headers
        }
        if IG_USERNAME and IG_PASSWORD:
            ydl_opts['username'] = IG_USERNAME
            ydl_opts['password'] = IG_PASSWORD
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=True)
            filename = ydl.prepare_filename(info)
            if filename and Path(filename).exists():
                print(f"   [OK] Downloaded (yt-dlp basic): {filename}")
                return Path(filename)
    except Exception as e:
        print(f"   [SKIP] yt-dlp basic failed: {e}")

    raise RuntimeError(
        f"Could not download video from {url}. All methods failed. "
        "Make sure the URL is public and supported by yt-dlp."
    )


def _instagram_login_session() -> requests.Session:
    """
    Create an authenticated Instagram session using the web login flow.
    Returns a requests.Session with valid cookies.
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://www.instagram.com/',
        'Origin': 'https://www.instagram.com',
    })

    # Step 1: Get CSRF token
    print("   Fetching Instagram CSRF token...")
    resp = session.get('https://www.instagram.com/accounts/login/', timeout=15)
    csrf_token = session.cookies.get('csrftoken', '')

    if not csrf_token:
        match = re.search(r'"csrf_token":"([^"]+)"', resp.text)
        if match:
            csrf_token = match.group(1)

    if not csrf_token:
        try:
            resp2 = session.get('https://www.instagram.com/data/shared_data/', timeout=10)
            csrf_token = resp2.json().get('config', {}).get('csrf_token', '')
        except Exception:
            pass

    if not csrf_token:
        raise Exception("Could not obtain CSRF token from Instagram")

    # Step 2: Login
    print(f"   Logging into Instagram as {IG_USERNAME}...")
    session.headers['X-CSRFToken'] = csrf_token

    login_data = {
        'username': IG_USERNAME,
        'enc_password': f'#PWD_INSTAGRAM_BROWSER:0:{int(time.time())}:{IG_PASSWORD}',
        'queryParams': '{}',
        'optIntoOneTap': 'false',
    }

    login_resp = session.post(
        'https://www.instagram.com/accounts/login/ajax/',
        data=login_data,
        timeout=15
    )

    try:
        login_json = login_resp.json()
    except Exception:
        raise Exception(f"Login response was not JSON: {login_resp.status_code}")

    if not login_json.get('authenticated'):
        msg = login_json.get('message', 'Unknown error')
        if login_json.get('two_factor_required'):
            raise Exception("Two-factor auth required. Disable 2FA or use app password.")
        raise Exception(f"Instagram login failed: {msg}")

    new_csrf = session.cookies.get('csrftoken', csrf_token)
    session.headers['X-CSRFToken'] = new_csrf

    print("   [OK] Instagram login successful")
    return session


def _download_via_api(url: str) -> Path:
    """Download Instagram video using the i.instagram.com private API."""
    session = _instagram_login_session()

    match = re.search(r'/(?:reels?|p)/([A-Za-z0-9_-]+)', url)
    if not match:
        raise ValueError(f"Could not extract shortcode from URL: {url}")

    shortcode = match.group(1)

    # Convert shortcode to numeric media ID
    ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    media_id = 0
    for char in shortcode:
        media_id = media_id * 64 + ALPHABET.index(char)
    media_id = str(media_id)

    print(f"   Shortcode: {shortcode} → Media ID: {media_id}")

    # Use the i.instagram.com API with X-IG-App-ID
    session.headers["X-IG-App-ID"] = "936619743392459"
    api_url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
    print(f"   Fetching media info...")

    resp = session.get(api_url, timeout=30)
    if resp.status_code != 200:
        raise Exception(f"Media API returned {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    items = data.get("items", [])
    if not items:
        raise Exception("No items in media API response")

    item = items[0]
    video_versions = item.get("video_versions", [])
    if not video_versions:
        raise Exception(f"No video_versions found. Media type: {item.get('media_type')}")

    video_url = video_versions[0]["url"]
    print(f"   Video URL found, downloading...")

    video_resp = session.get(video_url, stream=True, timeout=60)
    video_resp.raise_for_status()

    output_path = DOWNLOAD_DIR / f"{shortcode}.mp4"
    with open(output_path, "wb") as f:
        for chunk in video_resp.iter_content(chunk_size=8192):
            f.write(chunk)

    file_size = output_path.stat().st_size / (1024 * 1024)
    print(f"   [OK] Downloaded: {output_path} ({file_size:.1f} MB)")
    return output_path


def _download_with_instaloader(url: str) -> Path:
    """Fallback: download using instaloader."""
    import instaloader

    L = instaloader.Instaloader(
        download_videos=True,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        dirname_pattern=str(DOWNLOAD_DIR),
        filename_pattern="{shortcode}"
    )

    # Login if credentials available
    if IG_USERNAME and IG_PASSWORD:
        try:
            L.login(IG_USERNAME, IG_PASSWORD)
            print(f"   Logged into Instagram as {IG_USERNAME}")
        except Exception as login_err:
            print(f"[WARN] Instagram login failed: {login_err}")

    # Extract shortcode from URL
    match = re.search(r'/(?:reel|p)/([A-Za-z0-9_-]+)', url)
    if not match:
        raise ValueError(f"Could not extract shortcode from URL: {url}")

    shortcode = match.group(1)
    print(f"   Using instaloader for shortcode: {shortcode}")
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.download_post(post, target="")

    # Find downloaded video
    video_files = list(DOWNLOAD_DIR.glob(f"{shortcode}*.mp4"))
    if not video_files:
        video_files = list(DOWNLOAD_DIR.glob(f"{shortcode}*"))
    if not video_files:
        raise FileNotFoundError(f"Instaloader did not produce a video file for {shortcode}")

    print(f"   [OK] Downloaded (instaloader): {video_files[0]}")
    return video_files[0]


# ──────────────────────────────────────────────
# 2. Frame Extraction
# ──────────────────────────────────────────────

def extract_frames(video_path: Path, count: int = 3) -> list[Image.Image]:
    """
    Extract evenly-spaced frames from a video or load an image.
    Returns a list of PIL Image objects.
    """
    print(f"[VIDEO] Processing {video_path.name}...")

    # Check if it's an image first
    try:
        with Image.open(video_path) as img:
            img.verify()
        # If it's an image, return it replicated
        print(f"   Detected as image. Loading...")
        img = Image.open(video_path).convert("RGB")
        return [img.copy() for _ in range(count)]
    except Exception:
        # Not an image, proceed as video
        pass

    import cv2
    frames = []

    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            print(f"   [WARN] Could not open video: {video_path}")
        else:
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            if total_frames > 0:
                for i in range(count):
                    # Calculate frame index for even spacing
                    frame_idx = int((total_frames / (count + 1)) * (i + 1))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                    ret, cv2_frame = cap.read()
                    if ret:
                        # Convert BGR to RGB
                        rgb_frame = cv2.cvtColor(cv2_frame, cv2.COLOR_BGR2RGB)
                        # Convert to PIL Image
                        pil_img = Image.fromarray(rgb_frame)
                        frames.append(pil_img)
            cap.release()
    except Exception as e:
        print(f"   [WARN] OpenCV extraction failed: {e}")

    # Fallback: if cv2 didn't work, try placeholder logic
    if not frames:
        print("[WARN] Could not extract frames via OpenCV. Using placeholder images.")
        for _ in range(count):
            placeholder = Image.new("RGB", (1280, 720), color=(30, 30, 30))
            draw = ImageDraw.Draw(placeholder)
            font = _get_font(40)
            draw.text(
                (640, 360), "Video Frame",
                fill=(200, 200, 200), font=font, anchor="mm"
            )
            frames.append(placeholder)

    print(f"   [OK] Processed {len(frames)} frames/images")
    return frames


# ──────────────────────────────────────────────
# 3. Video Analysis with Gemini
# ──────────────────────────────────────────────

def analyze_video(video_path: Path) -> str:
    """
    Upload video to Gemini API and get a description of what's happening.
    Falls back to frame-based analysis if video upload fails.
    """
    print("[AI] Analyzing video with Gemini AI...")

    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    analysis_prompt = textwrap.dedent("""
        You are an expert content analyst. Watch this video carefully and provide:

        1. A detailed description of what is happening in the video.
        2. The key subjects/people/objects visible.
        3. The mood, setting, and context of the video.
        4. Any notable actions, events, or highlights.
        5. Any text, captions, or audio content you can identify.

        Be specific and descriptive. This analysis will be used to write a blog post.
    """).strip()

    try:
        # Check if it's an image
        try:
            with Image.open(video_path) as img:
                img.verify()
            print(f"   Detected as image. Using frame analysis...")
            return _analyze_frames(video_path)
        except Exception:
            pass

        # Proceed with video upload if not an image
        file_size_mb = video_path.stat().st_size / (1024 * 1024)
        print(f"   Uploading video ({file_size_mb:.1f} MB)...")

        video_file = genai.upload_file(
            path=str(video_path),
            display_name=video_path.name
        )

        # Wait for processing
        print("   Waiting for Gemini to process...")
        while video_file.state.name == "PROCESSING":
            time.sleep(3)
            video_file = genai.get_file(video_file.name)

        if video_file.state.name == "FAILED":
            raise Exception("Video processing failed on Gemini's side")

        response = model.generate_content(
            [analysis_prompt, video_file],
            generation_config=genai.GenerationConfig(
                max_output_tokens=2048,
                temperature=0.4
            )
        )

        # Clean up the uploaded file
        try:
            genai.delete_file(video_file.name)
        except Exception:
            pass

        analysis = response.text
        print("   [OK] Video analysis complete")
        return analysis

    except Exception as e:
        print(f"[WARN] Full video upload failed ({e}), falling back to frame analysis...")
        return _analyze_frames(video_path)


def _analyze_frames(video_path: Path) -> str:
    """Fallback: analyze extracted frames instead of full video."""
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    frames = extract_frames(video_path, count=4)

    prompt = textwrap.dedent("""
        You are an expert content analyst. These are frames from a social media video.
        Analyze these frames and provide:

        1. A detailed description of what appears to be happening.
        2. The key subjects/people/objects visible.
        3. The mood, setting, and context.
        4. Any notable actions, events, or highlights.
        5. Any text or captions visible in the frames.

        Be specific and descriptive. This analysis will be used to write a blog post.
    """).strip()

    # Convert frames to content parts
    content_parts = [prompt]
    for frame in frames[:4]:
        buf = BytesIO()
        frame.save(buf, format="JPEG", quality=85)
        buf.seek(0)
        content_parts.append(
            {"mime_type": "image/jpeg", "data": buf.getvalue()}
        )

    response = model.generate_content(
        content_parts,
        generation_config=genai.GenerationConfig(
            max_output_tokens=2048,
            temperature=0.4
        )
    )
    return response.text


# ──────────────────────────────────────────────
# 4. Content Generation
# ──────────────────────────────────────────────

def generate_content(analysis: str, source_url: str) -> dict:
    """
    Generate a blog post title and two paragraphs from the video analysis.
    Returns: {"title": str, "paragraph1": str, "paragraph2": str}
    """
    print("[CONTENT] Generating blog post content...")

    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = textwrap.dedent(f"""
        Based on the following video analysis, write an engaging blog post for a website.

        VIDEO ANALYSIS:
        {analysis}

        SOURCE URL: {source_url}

        Please respond ONLY with valid JSON in this exact format (no markdown, no code blocks):
        {{
            "title": "A catchy, SEO-friendly blog post title (max 80 chars)",
            "paragraph1": "First paragraph — introduce the video content and its significance. Write 3-5 sentences. Be engaging.",
            "paragraph2": "Second paragraph — provide more details or context. Write 3-5 sentences. End with a call-to-action to watch the full video."
        }}

        RULES:
        - Write in an engaging, journalistic style.
        - The title MUST be catchy and attention-grabbing.
        - Do NOT include links in the paragraphs.
        - Do NOT use placeholder text.
        - Write in English.
    """).strip()

    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            max_output_tokens=1024,
            temperature=0.7
        )
    )

    raw_text = response.text.strip()

    # Clean potential markdown code fences
    raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
    raw_text = re.sub(r'\s*```$', '', raw_text)

    try:
        content = json.loads(raw_text)
    except json.JSONDecodeError:
        # Try to extract JSON from the response
        json_match = re.search(r'\{[^{}]*"title"[^{}]*\}', raw_text, re.DOTALL)
        if json_match:
            content = json.loads(json_match.group())
        else:
            # Fallback: generate structured content manually
            print("[WARN] Could not parse JSON, creating fallback content")
            lines = raw_text.split("\n")
            content = {
                "title": lines[0][:80] if lines else "Video Highlight",
                "paragraph1": lines[1] if len(lines) > 1 else "Check out this amazing video.",
                "paragraph2": lines[2] if len(lines) > 2 else "Watch the full video for more!"
            }

    print(f"   [OK] Title: {content['title']}")
    return content


# ──────────────────────────────────────────────
# 5. Media Creation
# ──────────────────────────────────────────────

def create_featured_image(title: str, frames: list[Image.Image], output_name: str) -> Path:
    """
    Create a 16:9 featured image as a 3:1 collage of frames.
    No text overlays.
    """
    print("[IMAGE] Creating 3-frame collage featured image (16:9)...")

    WIDTH, HEIGHT = 1280, 720
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))

    # We need 3 frames for the collage. Replicate if we have fewer.
    collage_frames = frames[:3]
    while len(collage_frames) < 3:
        collage_frames.append(collage_frames[-1] if collage_frames else Image.new("RGB", (WIDTH, HEIGHT), (20, 20, 20)))

    # Width for each portrait segment (1280 / 3 = ~426.6)
    segment_w = WIDTH // 3
    
    for i in range(3):
        frame = collage_frames[i].copy().convert("RGB")
        src_w, src_h = frame.size
        
        # We want to fill a segment (segment_w, HEIGHT) with a portrait crop
        target_ratio = segment_w / HEIGHT
        src_ratio = src_w / src_h
        
        if src_ratio > target_ratio:
            # Too wide - crop sides
            new_w = int(src_h * target_ratio)
            left = (src_w - new_w) // 2
            frame = frame.crop((left, 0, left + new_w, src_h))
        else:
            # Too tall - crop top/bottom
            new_h = int(src_w / target_ratio)
            top = (src_h - new_h) // 2
            frame = frame.crop((0, top, src_w, top + new_h))
            
        frame = frame.resize((segment_w, HEIGHT), Image.LANCZOS)
        canvas.paste(frame, (i * segment_w, 0))

        # Divider lines
        if i > 0:
            draw = ImageDraw.Draw(canvas)
            draw.line([(i * segment_w, 0), (i * segment_w, HEIGHT)], fill=(255, 255, 255), width=2)

    # Accent bar at top (user previously had red, keeping it subtle)
    draw_top = ImageDraw.Draw(canvas)
    draw_top.rectangle([(0, 0), (WIDTH, 5)], fill=(255, 69, 58))

    output_path = OUTPUT_DIR / f"featured_{output_name}.jpg"
    canvas.save(str(output_path), "JPEG", quality=92)
    print(f"   [OK] Featured image saved: {output_path}")
    return output_path


def create_flyer(title: str, frames: list[Image.Image], output_name: str) -> Path:
    """
    Create a 1080x1080 square flyer:
    - 2 portrait frames side-by-side (seamless)
    - "WATCH THE VIDEO BELOW" overlay at the bottom
    """
    print("[FLYER] Creating square flyer image (1080x1080 - Seamless Reference Style)...")

    SIZE = 1080
    WIDTH, HEIGHT = SIZE, SIZE
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    
    # Place two portrait frames side-by-side (no gap)
    frame_w = WIDTH // 2
    frame_h = HEIGHT
    
    for i in range(2):
        if i < len(frames):
            frame = frames[i].copy().convert("RGB")
            src_w, src_h = frame.size
            
            # Crop to fill the square-half slot
            target_ratio = frame_w / frame_h
            src_ratio = src_w / src_h
            
            if src_ratio > target_ratio:
                new_w = int(src_h * target_ratio)
                left = (src_w - new_w) // 2
                frame = frame.crop((left, 0, left + new_w, src_h))
            else:
                new_h = int(src_w / target_ratio)
                top = (src_h - new_h) // 2
                frame = frame.crop((0, top, src_w, top + new_h))
                
            frame = frame.resize((frame_w, frame_h), Image.LANCZOS)
            canvas.paste(frame, (i * frame_w, 0))

    # Bottom Overlay - "WATCH THE VIDEO BELOW"
    overlay_h = 160
    overlay_y = HEIGHT - overlay_h - 60
    overlay_margin = 60
    
    # Rounded overlay (semi-transparent white)
    overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw_ov = ImageDraw.Draw(overlay)
    
    # Draw rounded rectangle
    shape = [overlay_margin, overlay_y, WIDTH - overlay_margin, overlay_y + overlay_h]
    draw_ov.rounded_rectangle(shape, radius=60, fill=(255, 255, 255, 210))
    
    # Outline
    draw_ov.rounded_rectangle(shape, radius=60, outline=(200, 200, 200, 255), width=3)
    
    canvas = canvas.convert("RGBA")
    canvas = Image.alpha_composite(canvas, overlay)
    canvas = canvas.convert("RGB")
    
    # Text on overlay
    draw_text = ImageDraw.Draw(canvas)
    cta_text = "WATCH THE VIDEO BELOW"
    font_cta = _get_font(54)
    
    # Center text
    bbox = draw_text.textbbox((0, 0), cta_text, font=font_cta)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    
    tx = (WIDTH - tw) // 2
    ty = overlay_y + (overlay_h - th) // 2 - 5
    
    draw_text.text((tx, ty), cta_text, fill=(30, 30, 40), font=font_cta)

    output_path = OUTPUT_DIR / f"flyer_{output_name}.jpg"
    canvas.save(str(output_path), "JPEG", quality=92)
    print(f"   [OK] Square flyer saved: {output_path}")
    return output_path


# ──────────────────────────────────────────────
# 6. WordPress Publishing
# ──────────────────────────────────────────────

def _wp_auth() -> tuple[str, str]:
    """Return WordPress auth tuple."""
    return (WP_USERNAME, WP_APP_PASSWORD)


def upload_media_to_wp(file_path: Path, filename: str = None) -> dict:
    """
    Upload an image to WordPress via REST API.
    Returns: {"id": int, "url": str}
    """
    if filename is None:
        filename = file_path.name

    print(f"   [JOB] Uploading {filename} to WordPress...")

    # Determine MIME type
    ext = file_path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
    mime_type = mime_map.get(ext, "image/jpeg")

    url = f"{WP_URL}/wp-json/wp/v2/media"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": mime_type,
    }

    with open(file_path, "rb") as f:
        response = requests.post(
            url,
            headers=headers,
            data=f,
            auth=_wp_auth(),
            timeout=60
        )

    if response.status_code not in (200, 201):
        raise Exception(
            f"WordPress media upload failed ({response.status_code}): "
            f"{response.text[:300]}"
        )

    data = response.json()
    result = {
        "id": data["id"],
        "url": data.get("source_url", data.get("guid", {}).get("rendered", ""))
    }
    print(f"   [OK] Uploaded: {result['url']} (ID: {result['id']})")
    return result


def publish_post(
    title: str,
    paragraph1: str,
    paragraph2: str,
    featured_media_id: int,
    flyer_id: int,
    flyer_url: str,
    source_url: str
) -> dict:
    """
    Create and publish a WordPress post via REST API.
    Returns: {"id": int, "url": str}
    """
    print("[JOB] Publishing post to WordPress...")

    # Construct post body with Gutenberg blocks
    post_body = textwrap.dedent(f"""\
        <!-- wp:paragraph -->
        <p>{paragraph1}</p>
        <!-- /wp:paragraph -->

        <!-- wp:paragraph -->
        <p>{paragraph2}</p>
        <!-- /wp:paragraph -->

        <!-- wp:image {{"id":{flyer_id},"sizeSlug":"full","linkDestination":"none"}} -->
        <figure class="wp-block-image size-full"><img src="{flyer_url}" alt="{title}" class="wp-image-{flyer_id}"/></figure>
        <!-- /wp:image -->

        <!-- wp:buttons {{"layout":{{"type":"flex","justifyContent":"center"}}}} -->
        <div class="wp-block-buttons">
            <!-- wp:button -->
            <div class="wp-block-button"><a class="wp-block-button__link wp-element-button" href="{source_url}" target="_blank" rel="noreferrer noopener">Watch VIDEO</a></div>
            <!-- /wp:button -->
        </div>
        <!-- /wp:buttons -->

        <!-- wp:shortcode -->
        [ads2] [ads1]
        <!-- /wp:shortcode -->
    """).strip()

    post_data = {
        "title": title,
        "content": post_body,
        "status": WP_POST_STATUS,
        "categories": [WP_CATEGORY_ID],
        "featured_media": featured_media_id,
    }

    url = f"{WP_URL}/wp-json/wp/v2/posts"
    response = requests.post(
        url,
        json=post_data,
        auth=_wp_auth(),
        timeout=30
    )

    if response.status_code not in (200, 201):
        raise Exception(
            f"WordPress post creation failed ({response.status_code}): "
            f"{response.text[:300]}"
        )

    data = response.json()
    result = {
        "id": data["id"],
        "url": data.get("link", data.get("guid", {}).get("rendered", ""))
    }
    print(f"   [OK] Post published: {result['url']}")
    return result


# ──────────────────────────────────────────────
# 7. Full Pipeline
# ──────────────────────────────────────────────

def process_video(url: str) -> dict:
    """
    Full pipeline for a single video URL:
    download → analyze → generate content → create media → publish to WP.
    """
    print(f"\n{'='*60}")
    print(f"[JOB] Processing: {url}")
    print(f"{'='*60}")

    import hashlib
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    safe_name = _sanitize_filename(
        re.search(r'/(?:reel|p|video|shorts)/([A-Za-z0-9_-]+)', url).group(1)
        if re.search(r'/(?:reel|p|video|shorts)/([A-Za-z0-9_-]+)', url)
        else url_hash
    )

    # Step 1: Download
    video_path = download_video(url)

    # Step 2: Extract frames
    frames = extract_frames(video_path, count=3)

    # Step 3: Analyze video with AI
    analysis = analyze_video(video_path)

    # Step 4: Generate blog content
    content = generate_content(analysis, url)
    title = content["title"]
    p1 = content["paragraph1"]
    p2 = content["paragraph2"]

    # Step 5: Create featured image (16:9 - 3:1 Collage)
    featured_path = create_featured_image(title, frames, safe_name)

    # Step 6: Create flyer (9:16) with 2 frames
    flyer_frames = frames[:2] if len(frames) >= 2 else frames + frames[:1]
    flyer_path = create_flyer(title, flyer_frames, safe_name)

    # Step 7: Upload media to WordPress
    featured_media = upload_media_to_wp(
        featured_path, f"featured-{safe_name}.jpg"
    )
    flyer_media = upload_media_to_wp(
        flyer_path, f"flyer-{safe_name}.jpg"
    )

    # Step 8: Publish post
    post = publish_post(
        title=title,
        paragraph1=p1,
        paragraph2=p2,
        featured_media_id=featured_media["id"],
        flyer_id=flyer_media["id"],
        flyer_url=flyer_media["url"],
        source_url=url
    )

    result = {
        "source_url": url,
        "title": title,
        "post_url": post["url"],
        "post_id": post["id"],
        "featured_image_id": featured_media["id"],
        "flyer_url": flyer_media["url"],
    }

    print(f"\n[DONE] SUCCESS! Post published: {post['url']}")
    return result


# ──────────────────────────────────────────────
# 8. CLI Entry Point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Instagram Video -> WordPress Auto-Publisher + Article Rewriter",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python main.py https://www.instagram.com/reel/ABC123/
              python main.py --file urls.txt
              python main.py URL1 URL2 URL3

            Article Rewriter:
              python main.py --rewrite https://example.com/some-article
              python main.py --rewrite --file articles.txt
              python main.py --rewrite --dry-run https://example.com/article
        """)
    )
    parser.add_argument(
        "urls", nargs="*",
        help="Video URLs (Instagram, TikTok, YT, etc.) or Article URLs with --rewrite"
    )
    parser.add_argument(
        "--file", "-f", type=str,
        help="Path to a text file containing URLs (one per line)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Analyze and generate content but don't publish to WordPress"
    )
    parser.add_argument(
        "--rewrite", "-r", action="store_true",
        help="Article rewrite mode: scrape, rewrite, and publish articles from any URL"
    )

    args = parser.parse_args()

    # Collect URLs
    urls = list(args.urls) if args.urls else []

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"[ERROR] File not found: {args.file}")
            sys.exit(1)
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)

    if not urls:
        parser.print_help()
        print("\n[ERROR] No URLs provided. Pass URLs as arguments or use --file.")
        sys.exit(1)

    # Validate config
    _check_config()
    _ensure_dirs()

    mode_label = "Article Rewrite" if args.rewrite else "Instagram Video"
    print(f"\n[JOB] Found {len(urls)} URL(s) to process")
    print(f"   Mode: {mode_label}")
    print(f"   WordPress: {WP_URL}")
    print(f"   Post status: {WP_POST_STATUS}")
    print(f"   Dry run: {'YES' if args.dry_run else 'NO'}")

    results = []
    errors = []

    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] Processing...")
        try:
            if args.rewrite:
                # Article rewrite mode
                from article_rewriter import process_article
                result = process_article(url, dry_run=args.dry_run)
                results.append(result)
            elif args.dry_run:
                # Only download, analyze, and generate — no WP publishing
                import hashlib
                url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
                _ensure_dirs()
                safe_name = _sanitize_filename(
                    re.search(r'/(?:reel|p|video|shorts)/([A-Za-z0-9_-]+)', url).group(1)
                    if re.search(r'/(?:reel|p|video|shorts)/([A-Za-z0-9_-]+)', url)
                    else url_hash
                )
                video_path = download_video(url)
                frames = extract_frames(video_path, count=3)
                analysis = analyze_video(video_path)
                content = generate_content(analysis, url)

                featured_path = create_featured_image(content["title"], frames[0], safe_name)
                flyer_frames = frames[:2] if len(frames) >= 2 else frames + frames[:1]
                flyer_path = create_flyer(content["title"], flyer_frames, safe_name)

                results.append({
                    "source_url": url,
                    "title": content["title"],
                    "paragraph1": content["paragraph1"],
                    "paragraph2": content["paragraph2"],
                    "featured_image": str(featured_path),
                    "flyer_image": str(flyer_path),
                    "status": "dry_run_complete"
                })
                print(f"\n[OK] Dry run complete for: {url}")
                print(f"   Title: {content['title']}")
            else:
                result = process_video(url)
                results.append(result)

        except Exception as e:
            print(f"\n[ERROR] processing {url}: {e}")
            errors.append({"url": url, "error": str(e)})

    # Summary
    print(f"\n{'='*60}")
    print("[SUMMARY] PROCESSING SUMMARY")
    print(f"{'='*60}")
    print(f"   Total URLs: {len(urls)}")
    print(f"   Successful: {len(results)}")
    print(f"   Errors:     {len(errors)}")

    if results:
        print(f"\n✅ Successful posts:")
        for r in results:
            print(f"   - {r.get('title', 'N/A')}")
            if "post_url" in r:
                print(f"     → {r['post_url']}")

    if errors:
        print(f"\n❌ Failed URLs:")
        for e in errors:
            print(f"   - {e['url']}: {e['error']}")

    # Save results to JSON
    results_path = OUTPUT_DIR / "results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({"results": results, "errors": errors}, f, indent=2, ensure_ascii=False)
    print(f"\n📁 Results saved to: {results_path}")


if __name__ == "__main__":
    main()
