#!/usr/bin/env python3
"""
Article Rewriter Module
=======================
Scrapes articles from URLs, rewrites them with Gemini AI,
finds related YouTube videos & images for embedding,
generates featured images, and publishes to WordPress.

Usage (standalone):
    python article_rewriter.py <article_url>
    python article_rewriter.py --file articles.txt
"""

import json
import os
import re
import textwrap
import time
import hashlib
from pathlib import Path
from io import BytesIO
from urllib.parse import urlparse, urljoin, quote_plus

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

import google.generativeai as genai

# ──────────────────────────────────────────────
# Configuration (reuses .env from main project)
# ──────────────────────────────────────────────
load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USERNAME = os.getenv("WP_USERNAME", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_CATEGORY_ID = int(os.getenv("WP_REWRITE_CATEGORY_ID", os.getenv("WP_CATEGORY_ID", "1")))
WP_POST_STATUS = os.getenv("WP_POST_STATUS", "publish")

DOWNLOAD_DIR = Path("downloads")
OUTPUT_DIR = Path("output")

# Common user-agent for scraping
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _ensure_dirs():
    """Create working directories if they don't exist."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


def _wp_auth() -> tuple[str, str]:
    """Return WordPress auth tuple."""
    return (WP_USERNAME, WP_APP_PASSWORD)


def _sanitize_filename(name: str) -> str:
    """Create a safe filename from a string."""
    safe = re.sub(r'[^\w\-]', '_', name)
    return safe[:80]


def _get_font(size: int):
    """Try to load a nice font, fall back to default."""
    font_paths = [
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for fp in font_paths:
        try:
            return ImageFont.truetype(fp, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.truetype("arial.ttf", size)
    except (IOError, OSError):
        return ImageFont.load_default()


def _extract_embed_info(url: str) -> dict | None:
    """Extract media embed info from a URL if it's a supported platform."""
    if not url:
        return None

    # YouTube
    yt_match = re.search(
        r'(?:youtube\.com/(?:watch\?v=|embed/|v/)|youtu\.be/)([\w-]{11})', url
    )
    if yt_match:
        vid_id = yt_match.group(1)
        return {
            "url": f"https://www.youtube.com/watch?v={vid_id}",
            "title": "YouTube Video",
            "embed_url": f"https://www.youtube.com/embed/{vid_id}",
            "provider": "youtube"
        }

    # Streamable
    st_match = re.search(r'streamable\.com/(?:e/|s/)?(\w+)', url)
    if st_match:
        vid_id = st_match.group(1)
        return {
            "url": f"https://streamable.com/{vid_id}",
            "title": "Streamable Video",
            "embed_url": f"https://streamable.com/e/{vid_id}",
            "provider": "streamable"
        }

    # Vimeo
    vim_match = re.search(r'(?:vimeo\.com|player\.vimeo\.com/video)/(\d+)', url)
    if vim_match:
        vid_id = vim_match.group(1)
        return {
            "url": f"https://vimeo.com/{vid_id}",
            "title": "Vimeo Video",
            "embed_url": f"https://player.vimeo.com/video/{vid_id}",
            "provider": "vimeo"
        }

    # Dailymotion
    dm_match = re.search(r'dailymotion\.com/(?:video|embed/video)/(\w+)', url)
    if dm_match:
        vid_id = dm_match.group(1)
        return {
            "url": f"https://www.dailymotion.com/video/{vid_id}",
            "title": "Dailymotion Video",
            "embed_url": f"https://www.dailymotion.com/embed/video/{vid_id}",
            "provider": "dailymotion"
        }

    # TikTok
    tt_match = re.search(r'tiktok\.com/(@[\w.-]+)/video/(\d+)', url)
    if tt_match:
        # Clean URL
        clean_url = f"https://www.tiktok.com/{tt_match.group(1)}/video/{tt_match.group(2)}"
        return {
            "url": clean_url,
            "title": "TikTok Video",
            "embed_url": clean_url,
            "provider": "tiktok"
        }

    # Instagram
    ig_match = re.search(r'instagram\.com/(?:p|reel|reels)/([\w-]+)', url)
    if ig_match:
        # Clean URL
        clean_url = f"https://www.instagram.com/reel/{ig_match.group(1)}/"
        return {
            "url": clean_url,
            "title": "Instagram Post",
            "embed_url": clean_url,
            "provider": "instagram"
        }

    # Facebook
    fb_match = re.search(r'facebook\.com/(?:watch/?\?v=|[\w.-]+/videos/|video\.php\?v=)(\d+)', url)
    if fb_match:
        # Clean URL
        clean_url = f"https://www.facebook.com/watch/?v={fb_match.group(1)}"
        return {
            "url": clean_url,
            "title": "Facebook Video",
            "embed_url": clean_url,
            "provider": "facebook"
        }

    # X (Twitter)
    x_match = re.search(r'(?:twitter\.com|x\.com)/[\w-]+/status/(\d+)', url)
    if x_match:
        # Clean URL
        clean_url = f"https://x.com/x/status/{x_match.group(1)}"
        return {
            "url": clean_url,
            "title": "X Post",
            "embed_url": clean_url,
            "provider": "x"
        }

    # Threads
    threads_match = re.search(r'threads\.net/(@[\w.-]+)/post/([\w-]+)', url)
    if threads_match:
        clean_url = f"https://www.threads.net/{threads_match.group(1)}/post/{threads_match.group(2)}"
        return {
            "url": clean_url,
            "title": "Threads Post",
            "embed_url": clean_url,
            "provider": "threads"
        }

    # Reddit
    reddit_match = re.search(r'reddit\.com/r/[\w-]+/comments/(\w+)', url)
    if reddit_match:
        # Strip query params
        clean_url = url.split("?")[0]
        return {
            "url": clean_url,
            "title": "Reddit Post",
            "embed_url": clean_url,
            "provider": "reddit"
        }

    # Spotify
    spotify_match = re.search(r'open\.spotify\.com/(track|album|playlist|episode|show)/([\w-]+)', url)
    if spotify_match:
        clean_url = f"https://open.spotify.com/{spotify_match.group(1)}/{spotify_match.group(2)}"
        return {
            "url": clean_url,
            "title": "Spotify Media",
            "embed_url": clean_url,
            "provider": "spotify"
        }

    # SoundCloud
    soundcloud_match = re.search(r'soundcloud\.com/[\w-]+/[\w-]+', url)
    if soundcloud_match:
        clean_url = url.split("?")[0]
        return {
            "url": clean_url,
            "title": "SoundCloud Audio",
            "embed_url": clean_url,
            "provider": "soundcloud"
        }

    # Pinterest
    pinterest_match = re.search(r'pinterest\.com/pin/(\d+)', url)
    if pinterest_match:
        clean_url = f"https://www.pinterest.com/pin/{pinterest_match.group(1)}/"
        return {
            "url": clean_url,
            "title": "Pinterest Pin",
            "embed_url": clean_url,
            "provider": "pinterest"
        }

    # Bluesky
    bsky_match = re.search(r'bsky\.app/profile/[\w.-]+/post/([\w-]+)', url)
    if bsky_match:
        return {
            "url": url,
            "title": "Bluesky Post",
            "embed_url": url,
            "provider": "bluesky"
        }

    return None


# ──────────────────────────────────────────────
# 1. Article Scraping
# ──────────────────────────────────────────────

def scrape_article(url: str) -> dict:
    """
    Scrape an article from a URL.
    Returns: {
        "title": str,
        "body": str,           # Full article text
        "images": [str],       # List of image URLs found in the article
        "source_url": str,
        "source_domain": str
    }
    """
    print(f"🌐 Scraping article: {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise Exception(f"Failed to fetch article: {e}")

    soup = BeautifulSoup(resp.text, "html.parser")
    domain = urlparse(url).netloc

    # --- Extract title ---
    title = ""
    # Try og:title first
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()
    # Fallback to <title> tag
    if not title and soup.title:
        title = soup.title.string.strip() if soup.title.string else ""
    # Fallback to first <h1>
    if not title:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    # --- Extract body text ---
    body_parts = []

    # Try common article containers (ordered by specificity)
    article_selectors = [
        "article",
        '[class*="article-body"]',
        '[class*="article-content"]',
        '[class*="post-content"]',
        '[class*="entry-content"]',
        '[class*="story-body"]',
        '[class*="content-body"]',
        '[role="main"]',
        "main",
        '[class*="content"]',
    ]

    content_elem = None
    for selector in article_selectors:
        found = soup.select_one(selector)
        if found:
            # Check it has substantial text
            text = found.get_text(strip=True)
            if len(text) > 200:
                content_elem = found
                break

    if content_elem is None:
        # Last resort: grab all paragraphs from body
        content_elem = soup.body if soup.body else soup

    # --- Noise Removal within content element ---
    # Remove common junk widgets that often get caught in article containers
    noise_selectors = [
        '[class*="related-post"]', '[class*="related-article"]', 
        '[class*="popular-post"]', '[class*="recommended"]',
        '[class*="suggested"]', '[class*="must-read"]',
        '[class*="read-more"]', '[class*="trending"]',
        '[class*="sidebar"]', '[class*="newsletter"]',
        '[class*="social-share"]', '[class*="author-box"]',
        '[class*="comment"]', '[class*="ad-"]', '[id*="ad-"]',
        'script', 'style', 'noscript', 'iframe'
    ]
    for noise_sel in noise_selectors:
        for noise_node in content_elem.select(noise_sel):
            noise_node.decompose()

    # Get paragraphs from the content element
    paragraphs = content_elem.find_all("p")
    for p in paragraphs:
        # Skip if paragraph is inside a blockquote (likely an embed metadata)
        if p.find_parents("blockquote"):
            continue
            
        text = p.get_text(strip=True)
        if len(text) > 30:  # Skip tiny fragments
            body_parts.append(text)

    # If we got very little, also grab headers + list items
    if len(body_parts) < 3:
        for tag in content_elem.find_all(["h2", "h3", "h4", "li"]):
            text = tag.get_text(strip=True)
            if len(text) > 20:
                body_parts.append(text)

    body = "\n\n".join(body_parts)

    # --- Extract images ---
    images = []
    # Try og:image first
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        img_url = og_image["content"]
        if not img_url.startswith("http"):
            img_url = urljoin(url, img_url)
        images.append(img_url)

    # Get images from article body
    if content_elem:
        for img in content_elem.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                if not src.startswith("http"):
                    src = urljoin(url, src)
                # Skip tiny icons, avatars, tracking pixels
                width = img.get("width", "")
                height = img.get("height", "")
                try:
                    if width and int(width) < 100:
                        continue
                    if height and int(height) < 100:
                        continue
                except ValueError:
                    pass
                if src not in images:
                    images.append(src)

    # --- Extract embedded media (videos, social posts, audio) ---
    embeds = []
    if content_elem:
        # 1. Find standard iframes (YouTube, Spotify, etc.)
        for iframe in content_elem.find_all("iframe"):
            src = iframe.get("src") or iframe.get("data-src") or ""
            embed_info = _extract_embed_info(src)
            if embed_info and embed_info["url"] not in [e["url"] for e in embeds]:
                embeds.append(embed_info)
                print(f"   🎬 Found embedded {embed_info['provider']}: {embed_info['url']}")

        # 2. Find TikTok blockquotes
        for blockquote in content_elem.find_all("blockquote", class_="tiktok-embed"):
            cite = blockquote.get("cite")
            if cite:
                embed_info = _extract_embed_info(cite)
                if embed_info and embed_info["url"] not in [e["url"] for e in embeds]:
                    embeds.append(embed_info)
                    print(f"   🎬 Found TikTok cite: {embed_info['url']}")

        # 3. Find Instagram blockquotes
        for blockquote in content_elem.find_all("blockquote", class_="instagram-media"):
            link = blockquote.find("a", href=True)
            if link:
                embed_info = _extract_embed_info(link["href"])
                if embed_info and embed_info["url"] not in [e["url"] for e in embeds]:
                    embeds.append(embed_info)
                    print(f"   🎬 Found Instagram embed: {embed_info['url']}")

        # 4. Find X (Twitter) blockquotes
        for blockquote in content_elem.find_all("blockquote", class_="twitter-tweet"):
            link = blockquote.find("a", href=True)
            if link:
                embed_info = _extract_embed_info(link["href"])
                if embed_info and embed_info["url"] not in [e["url"] for e in embeds]:
                    embeds.append(embed_info)
                    print(f"   🎬 Found X embed: {embed_info['url']}")

        # 5. Find Facebook video divs
        for fb_div in content_elem.find_all(["div", "span"], class_=["fb-video", "fb-post"]):
            href = fb_div.get("data-href")
            if href:
                embed_info = _extract_embed_info(href)
                if embed_info and embed_info["url"] not in [e["url"] for e in embeds]:
                    embeds.append(embed_info)
                    print(f"   🎬 Found Facebook embed: {embed_info['url']}")

        # 6. Also check for general media links in <a> tags
        for a_tag in content_elem.find_all("a", href=True):
            href = a_tag["href"]
            embed_info = _extract_embed_info(href)
            if embed_info and embed_info["url"] not in [e["url"] for e in embeds]:
                embeds.append(embed_info)
                print(f"   🎬 Found linked {embed_info['provider']}: {embed_info['url']}")

        # Check for <video> tags with source
        for video_tag in content_elem.find_all("video"):
            src = video_tag.get("src") or ""
            if not src:
                source_tag = video_tag.find("source")
                if source_tag:
                    src = source_tag.get("src", "")
            if src and src.startswith("http"):
                embeds.append({
                    "url": src,
                    "title": "Embedded Video",
                    "embed_url": src,
                    "provider": "video"
                })

    # Limit embeds
    embeds = embeds[:5]

    # Limit images
    images = images[:10]

    if not body:
        raise Exception(f"Could not extract article content from {url}")

    print(f"   ✅ Scraped: \"{title[:60]}...\"")
    print(f"   📝 {len(body_parts)} paragraphs, {len(images)} images, {len(embeds)} embeds found")

    return {
        "title": title,
        "body": body,
        "images": images,
        "videos": embeds,  # Keep key 'videos' for backward compatibility or rename? 
                           # Renaming in Step 3 and beyond.
        "source_url": url,
        "source_domain": domain,
    }


# ──────────────────────────────────────────────
# 2. Article Rewriting with Gemini
# ──────────────────────────────────────────────

def rewrite_article(original_title: str, original_body: str, source_url: str) -> dict:
    """
    Rewrite an article using Gemini AI.
    Returns: {
        "title": str,
        "paragraphs": [str],
        "seo_description": str,
        "image_alt_texts": [str],
        "tags": [str]
    }
    """
    print("✍️  Rewriting article with Gemini AI...")

    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    # Truncate very long articles to stay within token limits
    body_text = original_body[:8000] if len(original_body) > 8000 else original_body

    prompt = textwrap.dedent(f"""
        You are an expert content writer and SEO specialist. Rewrite the following article
        into a completely unique, engaging, SEO-optimized blog post. The rewritten article
        must be original — not a copy — and should feel like fresh journalism.

        ORIGINAL TITLE: {original_title}

        ORIGINAL ARTICLE:
        {body_text}

        SOURCE: {source_url}

        Please respond ONLY with valid JSON in this exact format (no markdown, no code blocks):
        {{
            "title": "A catchy, SEO-friendly blog post title (max 80 chars)",
            "paragraphs": [
                "Paragraph 1 — Strong opening that hooks the reader. 3-5 sentences.",
                "Paragraph 2 — Key details and context. 3-5 sentences.",
                "Paragraph 3 — More depth, quotes or data if relevant. 3-5 sentences.",
                "Paragraph 4 — Additional insights or background. 3-5 sentences.",
                "Paragraph 5 — Conclusion with a forward-looking statement. 3-5 sentences."
            ],
            "seo_description": "A compelling 150-160 character meta description for SEO.",
            "image_alt_texts": [
                "Descriptive alt text for the featured image",
                "Descriptive alt text for a secondary image"
            ],
            "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
        }}

        RULES:
        - Write 5-8 paragraphs minimum with rich detail.
        - Each paragraph should be 3-5 sentences.
        - Write in an engaging, journalistic style.
        - The title MUST be catchy and attention-grabbing (max 80 chars).
        - Make the content COMPLETELY unique — not a paraphrase but a rewrite.
        - Provide 3-5 relevant tags for categorization.
        - Write in English.
        - Do NOT include links or URLs in paragraphs.
        - Do NOT reference the original source in the text.
    """).strip()

    response = model.generate_content(
        prompt,
        generation_config=genai.GenerationConfig(
            max_output_tokens=4096,
            temperature=0.8
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
        json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
        if json_match:
            try:
                content = json.loads(json_match.group())
            except json.JSONDecodeError:
                content = _fallback_content(raw_text, original_title)
        else:
            content = _fallback_content(raw_text, original_title)

    # Ensure required fields
    if "paragraphs" not in content or not content["paragraphs"]:
        content["paragraphs"] = [raw_text[:500], raw_text[500:1000] if len(raw_text) > 500 else ""]

    if "tags" not in content:
        content["tags"] = []

    if "seo_description" not in content:
        content["seo_description"] = content["paragraphs"][0][:160]

    if "image_alt_texts" not in content:
        content["image_alt_texts"] = [content.get("title", original_title)]

    print(f"   ✅ Rewritten: \"{content['title']}\"")
    print(f"   📝 {len(content['paragraphs'])} paragraphs generated")
    return content


def _fallback_content(raw_text: str, original_title: str) -> dict:
    """Create structured content from unstructured text."""
    print("[WARN] Could not parse JSON, creating fallback content")
    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
    paragraphs = []
    current = []
    for line in lines:
        if len(line) > 50:
            current.append(line)
            if len(current) >= 3:
                paragraphs.append(" ".join(current))
                current = []
    if current:
        paragraphs.append(" ".join(current))

    return {
        "title": lines[0][:80] if lines else original_title,
        "paragraphs": paragraphs if paragraphs else [raw_text[:500]],
        "seo_description": (paragraphs[0][:160] if paragraphs else raw_text[:160]),
        "image_alt_texts": [original_title],
        "tags": [],
    }


# ──────────────────────────────────────────────
# 3. YouTube Video Discovery
# ──────────────────────────────────────────────

def find_youtube_videos(queries: list[str], max_videos: int = 2) -> list[dict]:
    """
    Find related YouTube videos by scraping YouTube search results.
    Returns: [{"url": str, "title": str, "embed_url": str}]
    """
    print("🎬 Searching for related YouTube videos...")
    videos = []

    for query in queries[:3]:  # Limit queries
        try:
            search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
            resp = requests.get(search_url, headers={
                "User-Agent": HEADERS["User-Agent"],
                "Accept-Language": "en-US,en;q=0.9",
            }, timeout=15)

            if resp.status_code != 200:
                continue

            # Extract video IDs from the page
            video_ids = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            # Remove duplicates while preserving order
            seen = set()
            unique_ids = []
            for vid in video_ids:
                if vid not in seen:
                    seen.add(vid)
                    unique_ids.append(vid)

            # Extract titles (try to match with videoIds)
            for vid_id in unique_ids[:2]:
                # Try to find the title near this video ID
                title_match = re.search(
                    rf'"videoId":"{vid_id}".*?"title":\s*\{{"runs":\s*\[\{{"text":\s*"([^"]+)"',
                    resp.text
                )
                vid_title = title_match.group(1) if title_match else f"Related: {query}"

                video_entry = {
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "title": vid_title,
                    "embed_url": f"https://www.youtube.com/embed/{vid_id}",
                    "video_id": vid_id,
                }
                # Avoid duplicates
                if not any(v["video_id"] == vid_id for v in videos):
                    videos.append(video_entry)
                    print(f"   📹 Found: {vid_title[:60]}...")

                if len(videos) >= max_videos:
                    break

        except Exception as e:
            print(f"   [WARN] YouTube search failed for '{query}': {e}")
            continue

        if len(videos) >= max_videos:
            break

    if not videos:
        print("   ⚠️  No YouTube videos found")
    else:
        print(f"   ✅ Found {len(videos)} video(s)")

    return videos


# ──────────────────────────────────────────────
# 4. Image Handling
# ──────────────────────────────────────────────

def filter_irrelevant_images(image_urls: list[str], article_title: str, article_body: str) -> list[str]:
    """
    Use Gemini and heuristics to filter out irrelevant images (ads, logos, icons).
    Returns a list of relevant image URLs.
    """
    if not image_urls:
        return []

    print(f"🧠 Filtering {len(image_urls)} image(s) for relevance...")

    # --- Level 1: Heuristic Filtering (Fast) ---
    red_flag_patterns = [
        r'logo', r'icon', r'favicon', r'avatar', r'spacer', r'pixel', 
        r'ad-banner', r'sidebar', r'header-image', r'site-identity',
        r'social-share', r'facebook-icon', r'twitter-icon', r'instagram-icon',
        r'loading', r'placeholder', r'transparent', r'\.gif'
    ]
    
    pre_filtered = []
    for url in image_urls:
        url_lower = url.lower()
        if any(re.search(pattern, url_lower) for pattern in red_flag_patterns):
            print(f"   🚫 Heuristic Block: {url.split('/')[-1][:40]}...")
            continue
        pre_filtered.append(url)

    if not pre_filtered:
        return []

    if len(pre_filtered) <= 2:
        # If very few left, just keep them to avoid over-filtering
        return pre_filtered

    # AI Filtering (Intelligent)
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        model = genai.GenerativeModel("gemini-2.0-flash")

        prompt = textwrap.dedent(f"""
            You are a content editor filtering images for a blog post.
            Decide which of these image URLs are likely to be ACTUAL ARTICLE CONTENT (e.g., photos of people, events, scenes) 
            vs IRRELEVANT IMAGES.

            IRRELEVANT CATEGORIES:
            - Site logos, icons, avatars
            - Advertisements or banners
            - THUMBNAILS from 'Related Articles' or 'Read More' sections
            - Navigational icons (home, search, menu)
            - Social media sharing icons

            ARTICLE TITLE: {article_title}
            ARTICLE CONTEXT: {article_body[:1000]}...

            IMAGE URLS:
            {chr(10).join([f"{i+1}. {url}" for i, url in enumerate(pre_filtered)])}

            Respond with ONLY a JSON object (no markdown, no code blocks):
            {{
                "keep_indices": [1, 3, 5],
                "reasoning": "Short explanation for choices"
            }}
            
            RULES:
            - "keep_indices" is a list of 1-based numbers matching the list above.
            - Focus on identifying images that add value to the story.
            - If an image looks like a logo or an ad, exclude it.
        """).strip()

        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                max_output_tokens=300,
                temperature=0.1
            )
        )

        raw = response.text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        
        # Simple JSON extraction
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            indices = result.get("keep_indices", [])
            
            final_urls = []
            for idx in indices:
                try:
                    if 1 <= idx <= len(pre_filtered):
                        final_urls.append(pre_filtered[idx-1])
                except (ValueError, TypeError):
                    continue
            
            print(f"   ✅ AI Filtered: {len(image_urls)} → {len(final_urls)} images kept")
            return final_urls if final_urls else pre_filtered[:3]

        return pre_filtered[:5]

    except Exception as e:
        print(f"   [WARN] AI Filtering failed: {e} — falling back to heuristics")
        return pre_filtered[:5]


def download_article_images(image_urls: list[str], max_images: int = 5) -> list[Image.Image]:
    """
    Download images from URLs and return as PIL Image objects.
    Filters out broken images and tiny ones.
    """
    print(f"🖼  Downloading article images ({len(image_urls)} found)...")
    images = []

    for img_url in image_urls[:max_images + 3]:  # Try extra in case some fail
        try:
            resp = requests.get(img_url, headers=HEADERS, timeout=15, stream=True)
            resp.raise_for_status()

            # Check content type
            content_type = resp.headers.get("Content-Type", "")
            if not content_type.startswith("image/"):
                continue

            img_data = BytesIO(resp.content)
            img = Image.open(img_data).convert("RGB")

            # Skip tiny images (icons, spacers, etc.)
            w, h = img.size
            if w < 200 or h < 150:
                continue

            images.append(img)
            print(f"   ✅ Downloaded image: {w}x{h}")

            if len(images) >= max_images:
                break

        except Exception as e:
            print(f"   [WARN] Failed to download image: {e}")
            continue

    if not images:
        print("   ⚠️  No usable images downloaded, will use placeholders")
        # Create a simple placeholder
        placeholder = Image.new("RGB", (1280, 720), color=(30, 30, 60))
        draw = ImageDraw.Draw(placeholder)
        font = _get_font(48)
        draw.text(
            (640, 360), "Article Image",
            fill=(200, 200, 200), font=font, anchor="mm"
        )
        images.append(placeholder)

    print(f"   📸 {len(images)} usable image(s) ready")
    return images


def create_article_featured_image(
    title: str, images: list[Image.Image], output_name: str
) -> Path:
    """
    Create a 16:9 featured image from article images.
    Uses up to 3 images in a collage layout, or a single image with overlay.
    """
    print("🖼  Creating featured image for article...")
    _ensure_dirs()

    WIDTH, HEIGHT = 1280, 720

    if len(images) >= 3:
        # 3-panel collage (same as main.py)
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        segment_w = WIDTH // 3

        for i in range(3):
            frame = images[i].copy().convert("RGB")
            src_w, src_h = frame.size
            target_ratio = segment_w / HEIGHT
            src_ratio = src_w / src_h

            if src_ratio > target_ratio:
                new_w = int(src_h * target_ratio)
                left = (src_w - new_w) // 2
                frame = frame.crop((left, 0, left + new_w, src_h))
            else:
                new_h = int(src_w / target_ratio)
                top = (src_h - new_h) // 2
                frame = frame.crop((0, top, src_w, top + new_h))

            frame = frame.resize((segment_w, HEIGHT), Image.LANCZOS)
            canvas.paste(frame, (i * segment_w, 0))

            if i > 0:
                draw = ImageDraw.Draw(canvas)
                draw.line(
                    [(i * segment_w, 0), (i * segment_w, HEIGHT)],
                    fill=(255, 255, 255), width=2
                )

    elif len(images) >= 2:
        # 2-panel layout
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        segment_w = WIDTH // 2

        for i in range(2):
            frame = images[i].copy().convert("RGB")
            src_w, src_h = frame.size
            target_ratio = segment_w / HEIGHT
            src_ratio = src_w / src_h

            if src_ratio > target_ratio:
                new_w = int(src_h * target_ratio)
                left = (src_w - new_w) // 2
                frame = frame.crop((left, 0, left + new_w, src_h))
            else:
                new_h = int(src_w / target_ratio)
                top = (src_h - new_h) // 2
                frame = frame.crop((0, top, src_w, top + new_h))

            frame = frame.resize((segment_w, HEIGHT), Image.LANCZOS)
            canvas.paste(frame, (i * segment_w, 0))

        # Center divider
        draw = ImageDraw.Draw(canvas)
        draw.line(
            [(segment_w, 0), (segment_w, HEIGHT)],
            fill=(255, 255, 255), width=2
        )

    else:
        # Single image — crop/resize to 16:9
        frame = images[0].copy().convert("RGB")
        src_w, src_h = frame.size
        target_ratio = WIDTH / HEIGHT
        src_ratio = src_w / src_h

        if src_ratio > target_ratio:
            new_w = int(src_h * target_ratio)
            left = (src_w - new_w) // 2
            frame = frame.crop((left, 0, left + new_w, src_h))
        else:
            new_h = int(src_w / target_ratio)
            top = (src_h - new_h) // 2
            frame = frame.crop((0, top, src_w, top + new_h))

        canvas = frame.resize((WIDTH, HEIGHT), Image.LANCZOS)

    # Add a subtle accent bar at top
    draw_top = ImageDraw.Draw(canvas)
    draw_top.rectangle([(0, 0), (WIDTH, 5)], fill=(255, 69, 58))

    output_path = OUTPUT_DIR / f"featured_article_{output_name}.jpg"
    canvas.save(str(output_path), "JPEG", quality=92)
    print(f"   ✅ Featured image saved: {output_path}")
    return output_path


# ──────────────────────────────────────────────
# 5. Gutenberg Content Builder
# ──────────────────────────────────────────────

def build_article_gutenberg_content(
    paragraphs: list[str],
    article_embeds: list[dict],
    wp_image_ids: list[dict],
    source_url: str,
) -> str:
    """
    Build WordPress Gutenberg block content with:
    - Paragraphs
    - YouTube video embeds
    - Image blocks
    - Source link button
    - Ad shortcodes
    """
    blocks = []

    # Simple strategy: Insert images and embeds evenly
    image_count = len(wp_image_ids)
    video_count = len(article_embeds)
    para_count = len(paragraphs)

    # Calculate insertion intervals
    video_insert_points = []
    if video_count > 0:
        interval = max(1, para_count // (video_count + 1))
        video_insert_points = [interval * (i + 1) for i in range(video_count)]

    image_insert_points = []
    if image_count > 0:
        interval = max(1, para_count // (image_count + 1))
        image_insert_points = [interval * (i + 1) for i in range(image_count)]

    video_idx = 0
    image_idx = 0

    for i, para in enumerate(paragraphs):
        blocks.append(f'<!-- wp:paragraph -->\n<p>{para}</p>\n<!-- /wp:paragraph -->')

        # Insert video/media embed if this is the right spot
        if video_idx < len(article_embeds) and (i + 1) in video_insert_points:
            vid = article_embeds[video_idx]
            vid_url = vid["url"]
            provider = vid.get("provider", "youtube")

            # Choose type: Most social/media embeds use 'rich'
            v_type = "video" if provider in ["youtube", "vimeo", "dailymotion", "streamable", "video"] else "rich"
            
            # Choose aspect ratio
            aspect_class = "wp-embed-aspect-16-9"
            if provider in ["tiktok", "instagram"]:
                aspect_class = "wp-embed-aspect-9-16"
            elif provider in ["spotify", "soundcloud", "x", "threads", "reddit", "pinterest", "bluesky"]:
                aspect_class = ""
            
            # SPECIAL CASE: Instagram and Facebook are deprecated in wp:embed
            if provider in ["instagram", "facebook"]:
                # Use Custom HTML block for Meta embeds
                if provider == "instagram":
                    # Force clean URL if not already
                    clean_url = vid_url.split("?")[0].rstrip("/")
                    embed_html = (
                        f'<blockquote class="instagram-media" data-instgrm-captioned data-instgrm-permalink="{clean_url}/?utm_source=ig_embed&amp;utm_campaign=loading" data-instgrm-version="14" style=" background:#FFF; border:0; border-radius:3px; box-shadow:0 0 1px 0 rgba(0,0,0,0.5),0 1px 10px 0 rgba(0,0,0,0.15); margin: 1px; max-width:540px; min-width:326px; padding:0; width:99.375%; width:-webkit-calc(100% - 2px); width:calc(100% - 2px);">'
                        f'<div style="padding:16px;"> <a href="{clean_url}/?utm_source=ig_embed&amp;utm_campaign=loading" style=" background:#FFFFFF; line-height:0; padding:0 0; text-align:center; text-decoration:none; width:100%;" target="_blank">View this post on Instagram</a></div>'
                        f'</blockquote>\n<script async src="//www.instagram.com/embed.js"></script>'
                    )
                else:  # Facebook
                    clean_url = vid_url.split("?")[0]
                    embed_html = (
                        f'<div class="fb-post" data-href="{clean_url}" data-width="500" data-show-text="true">'
                        f'<blockquote cite="{clean_url}" class="fb-xfbml-parse-ignore">View post on Facebook</blockquote>'
                        f'</div>\n<script async defer src="https://connect.facebook.net/en_US/sdk.js#xfbml=1&version=v12.0"></script>'
                    )
                
                blocks.append(
                    f'<!-- wp:html -->\n'
                    f'<div class="wp-block-html-embed {provider}-embed">\n{embed_html}\n</div>\n'
                    f'<!-- /wp:html -->'
                )
            else:
                # Use standard Gutenberg embed block
                embed_json = {
                    "url": vid_url,
                    "type": v_type,
                    "providerNameSlug": provider,
                    "responsive": True
                }
                if aspect_class:
                    embed_json["className"] = f"{aspect_class} wp-has-aspect-ratio"
                    
                blocks.append(
                    f'<!-- wp:embed {json.dumps(embed_json)} -->\n'
                    f'<figure class="wp-block-embed is-type-{v_type} is-provider-{provider} wp-block-embed-{provider} {"" if not aspect_class else aspect_class + " wp-has-aspect-ratio"}">'
                    f'<div class="wp-block-embed__wrapper">\n{vid_url}\n</div></figure>\n'
                    f'<!-- /wp:embed -->'
                )
            video_idx += 1

        # Insert image block if this is the right spot
        if image_idx < len(wp_image_ids) and (i + 1) in image_insert_points:
            img = wp_image_ids[image_idx]
            img_id = img["id"]
            img_url = img["url"]
            alt_text = img.get("alt", "Related image")
            blocks.append(
                f'<!-- wp:image {{"id":{img_id},"sizeSlug":"large","linkDestination":"none"}} -->\n'
                f'<figure class="wp-block-image size-large">'
                f'<img src="{img_url}" alt="{alt_text}" class="wp-image-{img_id}"/>'
                f'</figure>\n'
                f'<!-- /wp:image -->'
            )
            image_idx += 1

    # Add any remaining embeds
    while video_idx < len(article_embeds):
        vid = article_embeds[video_idx]
        vid_url = vid["url"]
        provider = vid.get("provider", "youtube")

        v_type = "video" if provider in ["youtube", "vimeo", "dailymotion", "streamable", "video"] else "rich"

        aspect_class = "wp-embed-aspect-16-9"
        if provider in ["tiktok", "instagram"]:
            aspect_class = "wp-embed-aspect-9-16"
        elif provider in ["spotify", "soundcloud", "x", "threads", "reddit", "pinterest", "bluesky"]:
            aspect_class = ""

        # SPECIAL CASE: Instagram and Facebook
        if provider in ["instagram", "facebook"]:
            if provider == "instagram":
                clean_url = vid_url.split("?")[0].rstrip("/")
                embed_html = (
                    f'<blockquote class="instagram-media" data-instgrm-captioned data-instgrm-permalink="{clean_url}/?utm_source=ig_embed&amp;utm_campaign=loading" data-instgrm-version="14" style=" background:#FFF; border:0; border-radius:3px; box-shadow:0 0 1px 0 rgba(0,0,0,0.5),0 1px 10px 0 rgba(0,0,0,0.15); margin: 1px; max-width:540px; min-width:326px; padding:0; width:99.375%; width:-webkit-calc(100% - 2px); width:calc(100% - 2px);">'
                    f'<div style="padding:16px;"> <a href="{clean_url}/?utm_source=ig_embed&amp;utm_campaign=loading" style=" background:#FFFFFF; line-height:0; padding:0 0; text-align:center; text-decoration:none; width:100%;" target="_blank">View this post on Instagram</a></div>'
                    f'</blockquote>\n<script async src="//www.instagram.com/embed.js"></script>'
                )
            else:
                clean_url = vid_url.split("?")[0]
                embed_html = (
                    f'<div class="fb-post" data-href="{clean_url}" data-width="500" data-show-text="true">'
                    f'<blockquote cite="{clean_url}" class="fb-xfbml-parse-ignore">View post on Facebook</blockquote>'
                    f'</div>\n<script async defer src="https://connect.facebook.net/en_US/sdk.js#xfbml=1&version=v12.0"></script>'
                )
            
            blocks.append(
                f'<!-- wp:html -->\n'
                f'<div class="wp-block-html-embed {provider}-embed">\n{embed_html}\n</div>\n'
                f'<!-- /wp:html -->'
            )
        else:
            embed_json = {
                "url": vid_url,
                "type": v_type,
                "providerNameSlug": provider,
                "responsive": True
            }
            if aspect_class:
                embed_json["className"] = f"{aspect_class} wp-has-aspect-ratio"

            blocks.append(
                f'<!-- wp:embed {json.dumps(embed_json)} -->\n'
                f'<figure class="wp-block-embed is-type-{v_type} is-provider-{provider} wp-block-embed-{provider} {"" if not aspect_class else aspect_class + " wp-has-aspect-ratio"}">'
                f'<div class="wp-block-embed__wrapper">\n{vid_url}\n</div></figure>\n'
                f'<!-- /wp:embed -->'
            )
        video_idx += 1

    # Add any remaining images
    while image_idx < len(wp_image_ids):
        img = wp_image_ids[image_idx]
        img_id = img["id"]
        img_url = img["url"]
        alt_text = img.get("alt", "Related image")
        blocks.append(
            f'<!-- wp:image {{"id":{img_id},"sizeSlug":"large","linkDestination":"none"}} -->\n'
            f'<figure class="wp-block-image size-large">'
            f'<img src="{img_url}" alt="{alt_text}" class="wp-image-{img_id}"/>'
            f'</figure>\n'
            f'<!-- /wp:image -->'
        )
        image_idx += 1

    # Ad shortcodes
    blocks.append(
        '<!-- wp:shortcode -->\n[ads2] [ads1]\n<!-- /wp:shortcode -->'
    )

    return "\n\n".join(blocks)


# ──────────────────────────────────────────────
# 6. WordPress Publishing
# ──────────────────────────────────────────────

def upload_media_to_wp(file_path: Path, filename: str = None) -> dict:
    """
    Upload an image to WordPress via REST API.
    Returns: {"id": int, "url": str}
    """
    if filename is None:
        filename = file_path.name

    print(f"☁️  Uploading {filename} to WordPress...")

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
            url, headers=headers, data=f,
            auth=_wp_auth(), timeout=60
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
    print(f"   ✅ Uploaded: {result['url']} (ID: {result['id']})")
    return result


def upload_image_from_url_to_wp(image_url: str, filename: str, alt_text: str = "") -> dict:
    """
    Download an image from URL and upload it to WordPress.
    Returns: {"id": int, "url": str, "alt": str}
    """
    print(f"☁️  Uploading remote image to WordPress: {filename}")

    try:
        resp = requests.get(image_url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if "png" in content_type:
            ext = ".png"
        else:
            ext = ".jpg"

        if not filename.endswith(ext):
            filename = filename + ext

        url = f"{WP_URL}/wp-json/wp/v2/media"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": content_type if content_type.startswith("image/") else "image/jpeg",
        }

        response = requests.post(
            url, headers=headers, data=resp.content,
            auth=_wp_auth(), timeout=60
        )

        if response.status_code not in (200, 201):
            raise Exception(f"Upload failed ({response.status_code})")

        data = response.json()

        # Set alt text if provided
        if alt_text:
            try:
                requests.post(
                    f"{WP_URL}/wp-json/wp/v2/media/{data['id']}",
                    json={"alt_text": alt_text},
                    auth=_wp_auth(),
                    timeout=15
                )
            except Exception:
                pass

        result = {
            "id": data["id"],
            "url": data.get("source_url", data.get("guid", {}).get("rendered", "")),
            "alt": alt_text,
        }
        print(f"   ✅ Uploaded: {result['url']} (ID: {result['id']})")
        return result

    except Exception as e:
        print(f"   [WARN] Failed to upload image from URL: {e}")
        return None


def publish_article_post(
    title: str,
    content_html: str,
    featured_media_id: int,
    seo_description: str = "",
    tags: list[str] = None,
) -> dict:
    """
    Create and publish a WordPress post via REST API.
    Returns: {"id": int, "url": str}
    """
    print("📝 Publishing rewritten article to WordPress...")

    post_data = {
        "title": title,
        "content": content_html,
        "status": WP_POST_STATUS,
        "categories": [WP_CATEGORY_ID],
        "featured_media": featured_media_id,
    }

    # Add excerpt/SEO description
    if seo_description:
        post_data["excerpt"] = seo_description

    # Handle tags — create if they don't exist
    if tags:
        tag_ids = []
        for tag_name in tags[:10]:  # Max 10 tags
            try:
                # Check if tag exists
                resp = requests.get(
                    f"{WP_URL}/wp-json/wp/v2/tags",
                    params={"search": tag_name},
                    auth=_wp_auth(),
                    timeout=10
                )
                existing = resp.json()
                if existing:
                    tag_ids.append(existing[0]["id"])
                else:
                    # Create new tag
                    create_resp = requests.post(
                        f"{WP_URL}/wp-json/wp/v2/tags",
                        json={"name": tag_name},
                        auth=_wp_auth(),
                        timeout=10
                    )
                    if create_resp.status_code in (200, 201):
                        tag_ids.append(create_resp.json()["id"])
            except Exception as e:
                print(f"   [WARN] Failed to handle tag '{tag_name}': {e}")
        if tag_ids:
            post_data["tags"] = tag_ids

    url = f"{WP_URL}/wp-json/wp/v2/posts"
    response = requests.post(
        url, json=post_data,
        auth=_wp_auth(), timeout=30
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
    print(f"   ✅ Published: {result['url']}")
    return result


# ──────────────────────────────────────────────
# 7. Full Article Rewrite Pipeline
# ──────────────────────────────────────────────

def process_article(url: str, dry_run: bool = False) -> dict:
    """
    Full pipeline for rewriting an article:
    scrape → rewrite → find media → create featured image → upload → publish.
    """
    print(f"\n{'='*60}")
    print(f"📰 Rewriting article: {url}")
    print(f"{'='*60}")

    _ensure_dirs()

    # Generate a safe name from the URL
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    safe_name = _sanitize_filename(
        urlparse(url).path.strip("/").split("/")[-1] or url_hash
    )
    if not safe_name:
        safe_name = url_hash

    # Step 1: Scrape the article
    article = scrape_article(url)

    # Step 2: Rewrite with Gemini AI
    rewritten = rewrite_article(
        article["title"], article["body"], article["source_url"]
    )

    # Step 3: Use embeds found in the source article
    article_embeds = article.get("videos", [])  # Key returned by scrape_article
    if article_embeds:
        print(f"🎬 Found {len(article_embeds)} media embed(s) in source article")
    else:
        print("🎬 No media embeds found in source article — skipping")

    # Step 4: Intelligent Image Filtering
    filtered_image_urls = filter_irrelevant_images(
        article["images"], rewritten["title"], article["body"]
    )

    # Step 5: Download & process images from the article
    article_images = download_article_images(filtered_image_urls)

    # Step 5: Create featured image
    featured_path = create_article_featured_image(
        rewritten["title"], article_images, safe_name
    )

    if dry_run:
        print(f"\n✅ DRY RUN complete for: {url}")
        print(f"   New title: {rewritten['title']}")
        print(f"   Paragraphs: {len(rewritten['paragraphs'])}")
        print(f"   Media embeds: {len(article_embeds)}")
        print(f"   Images: {len(article_images)}")
        return {
            "source_url": url,
            "title": rewritten["title"],
            "paragraphs": rewritten["paragraphs"],
            "seo_description": rewritten.get("seo_description", ""),
            "youtube_videos": article_embeds, # Keep key for dashboard compatibility if needed
            "image_count": len(article_images),
            "featured_image": str(featured_path),
            "status": "dry_run_complete",
        }

    # Step 6: Upload featured image to WordPress
    featured_media = upload_media_to_wp(
        featured_path, f"featured-article-{safe_name}.jpg"
    )

    # Step 7: Upload article images to WordPress for embedding
    wp_images = []
    alt_texts = rewritten.get("image_alt_texts", [])
    # Upload up to 3 body images from the FILTERED list
    for i, img_url in enumerate(filtered_image_urls[:3]):  
        alt_text = alt_texts[i] if i < len(alt_texts) else f"Image for {rewritten['title']}"
        wp_img = upload_image_from_url_to_wp(
            img_url, f"article-{safe_name}-{i}.jpg", alt_text
        )
        if wp_img:
            wp_images.append(wp_img)

    # Step 8: Build Gutenberg content
    content_html = build_article_gutenberg_content(
        paragraphs=rewritten["paragraphs"],
        article_embeds=article_embeds,
        wp_image_ids=wp_images,
        source_url=url,
    )

    # Step 9: Publish to WordPress
    post = publish_article_post(
        title=rewritten["title"],
        content_html=content_html,
        featured_media_id=featured_media["id"],
        seo_description=rewritten.get("seo_description", ""),
        tags=rewritten.get("tags", []),
    )

    result = {
        "source_url": url,
        "title": rewritten["title"],
        "post_url": post["url"],
        "post_id": post["id"],
        "featured_image_id": featured_media["id"],
        "youtube_videos": len(article_embeds),
        "embedded_images": len(wp_images),
    }

    print(f"\n🎉 SUCCESS! Rewritten article published: {post['url']}")
    return result


# ──────────────────────────────────────────────
# 8. Standalone CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Article Rewriter — Scrape, rewrite, and publish articles",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              python article_rewriter.py https://example.com/some-article
              python article_rewriter.py --file articles.txt
              python article_rewriter.py --dry-run https://example.com/article
        """)
    )
    parser.add_argument(
        "urls", nargs="*",
        help="Article URLs to rewrite and publish"
    )
    parser.add_argument(
        "--file", "-f", type=str,
        help="Path to a text file containing article URLs (one per line)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Scrape and rewrite but don't publish to WordPress"
    )

    args = parser.parse_args()

    # Collect URLs
    urls = list(args.urls) if args.urls else []

    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"[ERROR] File not found: {args.file}")
            import sys
            sys.exit(1)
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)

    if not urls:
        parser.print_help()
        print("\n[ERROR] No URLs provided.")
        import sys
        sys.exit(1)

    # Validate config
    if not GOOGLE_API_KEY:
        print("[ERROR] GOOGLE_API_KEY not set in .env")
        import sys
        sys.exit(1)
    if not args.dry_run and not all([WP_URL, WP_USERNAME, WP_APP_PASSWORD]):
        print("[ERROR] WordPress credentials not set in .env")
        import sys
        sys.exit(1)

    print(f"\n📋 Found {len(urls)} article URL(s) to process")
    print(f"   WordPress: {WP_URL}")
    print(f"   Post status: {WP_POST_STATUS}")
    print(f"   Dry run: {'YES' if args.dry_run else 'NO'}")

    results = []
    errors = []

    for i, url in enumerate(urls, 1):
        print(f"\n[{i}/{len(urls)}] Processing...")
        try:
            result = process_article(url, dry_run=args.dry_run)
            results.append(result)
        except Exception as e:
            print(f"\n❌ ERROR processing {url}: {e}")
            import traceback
            traceback.print_exc()
            errors.append({"url": url, "error": str(e)})

    # Summary
    print(f"\n{'='*60}")
    print("📊 ARTICLE REWRITER SUMMARY")
    print(f"{'='*60}")
    print(f"   Total articles: {len(urls)}")
    print(f"   Successful:     {len(results)}")
    print(f"   Errors:         {len(errors)}")

    if results:
        print(f"\n✅ Successful articles:")
        for r in results:
            print(f"   - {r.get('title', 'N/A')}")
            if "post_url" in r:
                print(f"     → {r['post_url']}")

    if errors:
        print(f"\n❌ Failed articles:")
        for e in errors:
            print(f"   - {e['url']}: {e['error']}")

    # Save results
    _ensure_dirs()
    results_path = OUTPUT_DIR / "rewrite_results.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump({"results": results, "errors": errors}, f, indent=2, ensure_ascii=False)
    print(f"\n📁 Results saved to: {results_path}")
