#!/usr/bin/env python3
"""
Content Publishing Agent — Flask Backend
=========================================
Web dashboard + background agent for automated content publishing.
Integrates Instagram-to-WP and Article Rewriter pipelines.

Run:  python app.py
Visit: http://localhost:5000
"""

import json
import os
import smtplib
import sqlite3
import textwrap
import threading
import time
import traceback
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
from flask import Flask, jsonify, render_template, request

from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# App Setup
# ──────────────────────────────────────────────

app = Flask(__name__)
DB_PATH = Path("agent.db")
LOCK = threading.Lock()
STOP_PROCESSING = threading.Event()  # Set this to halt queue processing

# ──────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────

def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Initialize database tables."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'article',
            status TEXT NOT NULL DEFAULT 'pending',
            error TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            wp_url TEXT,
            wp_post_id INTEGER,
            source_url TEXT,
            mode TEXT,
            featured_image_url TEXT,
            youtube_count INTEGER DEFAULT 0,
            image_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            last_checked TEXT,
            articles_found INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)

    # Default settings
    defaults = {
        "agent_enabled": "false",
        "check_interval_minutes": "30",
        "auto_publish": "true",
        "screening_enabled": "true",
        "screening_criteria": "Ghana news, African entertainment, sports highlights, trending stories. Skip weather reports, stock market listings, and obituaries.",
        "wp_url": os.getenv("WP_URL", "https://afripulsetv.com").rstrip("/"),
        "wp_username": os.getenv("WP_USERNAME", ""),
        "wp_app_password": os.getenv("WP_APP_PASSWORD", ""),
        "wp_category_id": os.getenv("WP_CATEGORY_ID", "1"),
        "wp_post_status": os.getenv("WP_POST_STATUS", "publish"),
        "google_api_key": os.getenv("GOOGLE_API_KEY", ""),
        "ig_username": os.getenv("IG_USERNAME", ""),
        "ig_password": os.getenv("IG_PASSWORD", ""),
        "notify_email_enabled": "false",
        "notify_email_to": "",
        "notify_email_smtp_host": "smtp.gmail.com",
        "notify_email_smtp_port": "587",
        "notify_email_smtp_user": "",
        "notify_email_smtp_password": "",
        "notify_telegram_enabled": "false",
        "notify_telegram_bot_token": "",
        "notify_telegram_chat_id": "",
        "notify_on_publish": "true",
        "notify_on_fail": "true",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def get_all_settings() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────
# Pipeline Wrappers
# ──────────────────────────────────────────────

def _apply_settings_to_env():
    """Push dashboard settings into environment for pipelines."""
    settings = get_all_settings()
    os.environ["WP_URL"] = settings.get("wp_url", "")
    os.environ["WP_USERNAME"] = settings.get("wp_username", "")
    os.environ["WP_APP_PASSWORD"] = settings.get("wp_app_password", "")
    os.environ["WP_CATEGORY_ID"] = settings.get("wp_category_id", "1")
    os.environ["WP_POST_STATUS"] = settings.get("wp_post_status", "publish")
    os.environ["GOOGLE_API_KEY"] = settings.get("google_api_key", "")
    os.environ["IG_USERNAME"] = settings.get("ig_username", "")
    os.environ["IG_PASSWORD"] = settings.get("ig_password", "")


def run_article_pipeline(url: str) -> dict:
    """Run the article rewriter pipeline."""
    _apply_settings_to_env()
    # Reimport to pick up new env vars
    import importlib
    import article_rewriter as ar
    importlib.reload(ar)
    return ar.process_article(url, dry_run=False)


def run_social_video_pipeline(url: str) -> dict:
    """Run the Social Media Video-to-WP pipeline."""
    _apply_settings_to_env()
    import importlib
    import main as m
    importlib.reload(m)
    return m.process_video(url)


# ──────────────────────────────────────────────
# Job Processor (Background Thread)
# ──────────────────────────────────────────────

def process_pending_jobs():
    """Process one pending job at a time."""
    if STOP_PROCESSING.is_set():
        return False  # Stop flag is active

    conn = get_db()
    job = conn.execute(
        "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    conn.close()

    if not job:
        return False

    job_id = job["id"]
    url = job["url"]
    mode = job["mode"]

    # Mark as running
    conn = get_db()
    conn.execute(
        "UPDATE jobs SET status = 'running', started_at = datetime('now') WHERE id = ?",
        (job_id,)
    )
    conn.commit()
    conn.close()

    try:
        print(f"[AGENT] Processing job #{job_id}: {mode} - {url}")

        if job["mode"] == "article":
            from article_rewriter import process_article
            result = process_article(job["url"])
        elif job["mode"] in ["instagram", "social"]:
            result = run_social_video_pipeline(job["url"])
        else:
            raise ValueError(f"Unknown mode: {job['mode']}")

        # Mark as done
        conn = get_db()
        conn.execute(
            "UPDATE jobs SET status = 'done', finished_at = datetime('now'), "
            "result_json = ? WHERE id = ?",
            (json.dumps(result), job_id)
        )

        # Save to posts table
        conn.execute(
            "INSERT INTO posts (title, wp_url, wp_post_id, source_url, mode, "
            "featured_image_url, youtube_count, image_count) VALUES (?,?,?,?,?,?,?,?)",
            (
                result.get("title", "Untitled"),
                result.get("post_url", ""),
                result.get("post_id", 0),
                url,
                mode,
                result.get("flyer_url", result.get("featured_image_url", "")),
                result.get("youtube_videos", 0),
                result.get("embedded_images", result.get("image_count", 0)),
            )
        )
        conn.commit()
        conn.close()

        print(f"[AGENT] ✅ Job #{job_id} completed: {result.get('title', 'N/A')}")

        # Send publish notification
        send_notification(
            event="published",
            title=result.get("title", "Untitled"),
            post_url=result.get("post_url", ""),
            mode=mode
        )

        return True

    except Exception as e:
        error_msg = f"{e}\n{traceback.format_exc()}"
        conn = get_db()
        conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = datetime('now'), "
            "error = ? WHERE id = ?",
            (error_msg[:2000], job_id)
        )
        conn.commit()
        conn.close()
        print(f"[AGENT] ❌ Job #{job_id} failed: {e}")

        # Send failure notification
        send_notification(
            event="failed",
            title=url,
            post_url=str(e)[:200],
            mode=mode
        )

        return True


# ──────────────────────────────────────────────
# Notifications (Email + WhatsApp)
# ──────────────────────────────────────────────

def send_notification(event: str, title: str, post_url: str, mode: str):
    """Send notification via configured channels when a post is published or fails."""
    try:
        if event == "published" and get_setting("notify_on_publish", "true") != "true":
            return
        if event == "failed" and get_setting("notify_on_fail", "true") != "true":
            return

        if event == "published":
            emoji = "✅"
            subject = f"Article Published: {title}"
            body = f"📰 New {mode} post published!\n\nTitle: {title}\nURL: {post_url}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        else:
            emoji = "❌"
            subject = f"Job Failed: {title[:50]}"
            body = f"⚠️ A {mode} job failed.\n\nSource: {title}\nError: {post_url}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M')}"

        # Email notification
        if get_setting("notify_email_enabled", "false") == "true":
            send_email_alert(subject, body)

        # Telegram notification
        if get_setting("notify_telegram_enabled", "false") == "true":
            send_telegram_alert(f"{emoji} {subject}\n\n{body}")

    except Exception as e:
        print(f"[AGENT] [WARN] Notification error: {e}")


def send_email_alert(subject: str, body: str):
    """Send email via SMTP."""
    try:
        to_addr = get_setting("notify_email_to", "")
        smtp_host = get_setting("notify_email_smtp_host", "smtp.gmail.com")
        smtp_port = int(get_setting("notify_email_smtp_port", "587"))
        smtp_user = get_setting("notify_email_smtp_user", "")
        smtp_pass = get_setting("notify_email_smtp_password", "")

        if not all([to_addr, smtp_user, smtp_pass]):
            print("[AGENT] [WARN] Email not configured — skipping")
            return

        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = to_addr
        msg["Subject"] = f"🤖 Content Agent — {subject}"
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        print(f"[AGENT] 📧 Email sent to {to_addr}")

    except Exception as e:
        print(f"[AGENT] [WARN] Email failed: {e}")


def send_telegram_alert(message: str):
    """Send Telegram message via Bot API.

    Setup:
    1. Message @BotFather on Telegram, send /newbot
    2. Copy the bot token
    3. Message your bot, then visit:
       https://api.telegram.org/bot<TOKEN>/getUpdates
       to find your chat_id
    """
    try:
        import requests as req

        token = get_setting("notify_telegram_bot_token", "")
        chat_id = get_setting("notify_telegram_chat_id", "")

        if not token or not chat_id:
            print("[AGENT] [WARN] Telegram not configured — skipping")
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = req.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)

        if resp.status_code == 200:
            print(f"[AGENT] 📱 Telegram sent to chat {chat_id}")
        else:
            error = resp.json().get("description", resp.text[:200])
            print(f"[AGENT] [WARN] Telegram API error: {error}")

    except Exception as e:
        print(f"[AGENT] [WARN] Telegram failed: {e}")


def screen_article(title: str, summary: str, criteria: str) -> bool:
    """
    Use Gemini AI to decide if an article is worth publishing.
    Returns True if the article passes screening, False otherwise.
    """
    try:
        import google.generativeai as genai
        api_key = get_setting("google_api_key", "")
        if not api_key:
            return True  # No API key = skip screening

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")

        prompt = textwrap.dedent(f"""
            You are a content editor deciding which articles to publish on a news/blog website.

            ARTICLE TITLE: {title}
            ARTICLE SUMMARY: {summary[:500]}

            EDITORIAL CRITERIA:
            {criteria}

            Based on the criteria above, should this article be published?
            Consider: topic relevance, reader interest, quality, and uniqueness.

            Respond with ONLY a JSON object (no markdown, no code blocks):
            {{"publish": true/false, "reason": "Brief one-line reason"}}
        """).strip()

        response = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                max_output_tokens=100,
                temperature=0.2
            )
        )

        raw = response.text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()

        import re
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            should_publish = result.get("publish", True)
            reason = result.get("reason", "")
            status = "✅ APPROVED" if should_publish else "❌ REJECTED"
            print(f"[AGENT] {status}: {title[:50]}... — {reason}")
            return should_publish

        return True  # Default to publishing if parsing fails

    except Exception as e:
        print(f"[AGENT] [WARN] Screening failed: {e} — defaulting to publish")
        return True  # Fail-open: if screening errors, queue the article anyway


def check_rss_feeds():
    """Check all active RSS feeds for new articles. Uses AI screening if enabled."""
    conn = get_db()
    feeds = conn.execute("SELECT * FROM feeds WHERE active = 1").fetchall()
    conn.close()

    screening_enabled = get_setting("screening_enabled", "true") == "true"
    criteria = get_setting("screening_criteria", "")

    new_articles = 0
    screened_out = 0

    for feed in feeds:
        try:
            parsed = feedparser.parse(feed["url"])
            for entry in parsed.entries[:10]:  # Check last 10 entries
                article_url = entry.get("link", "")
                if not article_url:
                    continue

                # Check if we already have this URL in jobs or posts
                conn = get_db()
                existing = conn.execute(
                    "SELECT id FROM jobs WHERE url = ? UNION SELECT id FROM posts WHERE source_url = ?",
                    (article_url, article_url)
                ).fetchone()

                if not existing:
                    # AI Screening
                    if screening_enabled and criteria:
                        entry_title = entry.get("title", "")
                        entry_summary = entry.get("summary", entry.get("description", ""))
                        if not screen_article(entry_title, entry_summary, criteria):
                            screened_out += 1
                            conn.close()
                            continue

                    conn.execute(
                        "INSERT INTO jobs (url, mode) VALUES (?, 'article')",
                        (article_url,)
                    )
                    conn.commit()
                    new_articles += 1
                    print(f"[AGENT] 📰 Queued article: {article_url}")

                conn.close()

            # Update last_checked
            conn = get_db()
            conn.execute(
                "UPDATE feeds SET last_checked = datetime('now'), "
                "articles_found = articles_found + ? WHERE id = ?",
                (new_articles, feed["id"])
            )
            conn.commit()
            conn.close()

        except Exception as e:
            print(f"[AGENT] [WARN] RSS check failed for {feed['name']}: {e}")

    if screened_out:
        print(f"[AGENT] 🔍 Screened out {screened_out} article(s) that didn't match criteria")

    return new_articles


def agent_loop():
    """Main agent background loop."""
    print("[AGENT] 🤖 Agent thread started")
    last_rss_check = 0

    while True:
        try:
            agent_enabled = get_setting("agent_enabled", "false") == "true"
            interval = int(get_setting("check_interval_minutes", "30"))

            if agent_enabled:
                # Check RSS feeds periodically
                now = time.time()
                if now - last_rss_check > interval * 60:
                    print("[AGENT] 📡 Checking RSS feeds...")
                    new = check_rss_feeds()
                    if new:
                        print(f"[AGENT] Found {new} new article(s)")
                    last_rss_check = now

                # Process pending jobs
                with LOCK:
                    process_pending_jobs()

            time.sleep(10)  # Check every 10 seconds

        except Exception as e:
            print(f"[AGENT] Loop error: {e}")
            time.sleep(30)


# ──────────────────────────────────────────────
# API Routes
# ──────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/stats")
def api_stats():
    conn = get_db()
    total_posts = conn.execute("SELECT COUNT(*) as c FROM posts").fetchone()["c"]
    today_posts = conn.execute(
        "SELECT COUNT(*) as c FROM posts WHERE date(created_at) = date('now')"
    ).fetchone()["c"]
    queue_size = conn.execute(
        "SELECT COUNT(*) as c FROM jobs WHERE status IN ('pending', 'running')"
    ).fetchone()["c"]
    total_jobs = conn.execute("SELECT COUNT(*) as c FROM jobs").fetchone()["c"]
    failed_jobs = conn.execute(
        "SELECT COUNT(*) as c FROM jobs WHERE status = 'failed'"
    ).fetchone()["c"]
    success_rate = (
        round((total_jobs - failed_jobs) / total_jobs * 100) if total_jobs > 0 else 100
    )
    active_feeds = conn.execute(
        "SELECT COUNT(*) as c FROM feeds WHERE active = 1"
    ).fetchone()["c"]
    agent_enabled = get_setting("agent_enabled", "false") == "true"
    conn.close()

    return jsonify({
        "total_posts": total_posts,
        "today_posts": today_posts,
        "queue_size": queue_size,
        "total_jobs": total_jobs,
        "failed_jobs": failed_jobs,
        "success_rate": success_rate,
        "active_feeds": active_feeds,
        "agent_enabled": agent_enabled,
    })


@app.route("/api/jobs", methods=["GET"])
def api_jobs_list():
    status_filter = request.args.get("status", "")
    limit = int(request.args.get("limit", "50"))

    conn = get_db()
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status_filter, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows])


@app.route("/api/jobs", methods=["POST"])
def api_jobs_create():
    data = request.get_json()
    urls = data.get("urls", [])
    mode = data.get("mode", "article")

    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split("\n") if u.strip() and not u.strip().startswith("#")]

    if not urls:
        return jsonify({"error": "No URLs provided"}), 400

    conn = get_db()
    created = []
    for url in urls:
        conn.execute(
            "INSERT INTO jobs (url, mode) VALUES (?, ?)",
            (url.strip(), mode)
        )
        created.append(url.strip())
    conn.commit()
    conn.close()

    # Trigger processing in background
    threading.Thread(target=_process_all_pending, daemon=True).start()

    return jsonify({"created": len(created), "urls": created}), 201


def _process_all_pending():
    """Process all pending jobs (triggered by manual submission)."""
    STOP_PROCESSING.clear()  # Reset stop flag when starting
    while not STOP_PROCESSING.is_set():
        with LOCK:
            had_work = process_pending_jobs()
        if not had_work:
            break
        time.sleep(1)
    if STOP_PROCESSING.is_set():
        print("[AGENT] ⛔ Queue processing stopped by user")


@app.route("/api/jobs/stop", methods=["POST"])
def api_jobs_stop():
    """Stop processing the queue. Running job will finish, but no new jobs start."""
    STOP_PROCESSING.set()

    # Also mark any running jobs as failed/cancelled
    conn = get_db()
    running = conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
    for job in running:
        conn.execute(
            "UPDATE jobs SET status = 'failed', error = 'Cancelled by user', "
            "finished_at = datetime('now') WHERE id = ?",
            (job["id"],)
        )
    conn.commit()
    conn.close()

    count = len(running)
    print(f"[AGENT] ⛔ Queue stopped. {count} running job(s) cancelled.")
    return jsonify({"stopped": True, "cancelled_running": count})


@app.route("/api/jobs/cancel-all", methods=["POST"])
def api_jobs_cancel_all():
    """Cancel all pending jobs."""
    STOP_PROCESSING.set()
    conn = get_db()
    result = conn.execute("DELETE FROM jobs WHERE status = 'pending'")
    cancelled = result.rowcount
    conn.commit()
    conn.close()
    print(f"[AGENT] 🗑️ Cancelled {cancelled} pending job(s)")
    return jsonify({"cancelled": cancelled})


@app.route("/api/jobs/<int:job_id>", methods=["DELETE"])
def api_jobs_delete(job_id):
    conn = get_db()
    conn.execute(
        "DELETE FROM jobs WHERE id = ? AND status = 'pending'", (job_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({"deleted": True})


@app.route("/api/jobs/<int:job_id>/retry", methods=["POST"])
def api_jobs_retry(job_id):
    conn = get_db()
    conn.execute(
        "UPDATE jobs SET status = 'pending', error = NULL, "
        "started_at = NULL, finished_at = NULL WHERE id = ?",
        (job_id,)
    )
    conn.commit()
    conn.close()

    threading.Thread(target=_process_all_pending, daemon=True).start()
    return jsonify({"retried": True})


@app.route("/api/posts")
def api_posts_list():
    limit = int(request.args.get("limit", "50"))
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM posts ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/feeds", methods=["GET"])
def api_feeds_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM feeds ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/feeds", methods=["POST"])
def api_feeds_create():
    data = request.get_json()
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()

    if not name or not url:
        return jsonify({"error": "Name and URL required"}), 400

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)", (name, url)
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Feed URL already exists"}), 409
    finally:
        conn.close()

    return jsonify({"created": True}), 201


@app.route("/api/feeds/<int:feed_id>", methods=["DELETE"])
def api_feeds_delete(feed_id):
    conn = get_db()
    conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": True})


@app.route("/api/feeds/<int:feed_id>/toggle", methods=["POST"])
def api_feeds_toggle(feed_id):
    conn = get_db()
    conn.execute(
        "UPDATE feeds SET active = CASE WHEN active = 1 THEN 0 ELSE 1 END WHERE id = ?",
        (feed_id,)
    )
    conn.commit()
    conn.close()
    return jsonify({"toggled": True})


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    settings = get_all_settings()
    # Mask password fields for security
    masked = dict(settings)
    if masked.get("wp_app_password"):
        masked["wp_app_password"] = "••••••" + masked["wp_app_password"][-4:]
    if masked.get("ig_password"):
        masked["ig_password"] = "••••••" + masked["ig_password"][-4:]
    if masked.get("google_api_key"):
        masked["google_api_key"] = masked["google_api_key"][:8] + "••••••"
    if masked.get("notify_email_smtp_password"):
        masked["notify_email_smtp_password"] = "••••••" + masked["notify_email_smtp_password"][-4:]
    return jsonify(masked)


@app.route("/api/settings", methods=["POST"])
def api_settings_update():
    data = request.get_json()
    for key, value in data.items():
        # Don't save masked values
        if "••••••" in str(value):
            continue
        set_setting(key, str(value))
    return jsonify({"updated": True})


@app.route("/api/agent/toggle", methods=["POST"])
def api_agent_toggle():
    current = get_setting("agent_enabled", "false")
    new_val = "false" if current == "true" else "true"
    set_setting("agent_enabled", new_val)
    return jsonify({"agent_enabled": new_val == "true"})


@app.route("/api/test-notification", methods=["POST"])
def api_test_notification():
    """Send a test notification to verify email/WhatsApp config."""
    results = {"email": None, "whatsapp": None}

    # Test email
    if get_setting("notify_email_enabled", "false") == "true":
        try:
            to_addr = get_setting("notify_email_to", "")
            smtp_host = get_setting("notify_email_smtp_host", "smtp.gmail.com")
            smtp_port = int(get_setting("notify_email_smtp_port", "587"))
            smtp_user = get_setting("notify_email_smtp_user", "")
            smtp_pass = get_setting("notify_email_smtp_password", "")

            if not all([to_addr, smtp_user, smtp_pass]):
                results["email"] = {"ok": False, "error": "Missing email config: fill in all SMTP fields"}
            else:
                msg = MIMEMultipart()
                msg["From"] = smtp_user
                msg["To"] = to_addr
                msg["Subject"] = "🤖 Content Agent — Test Notification"
                msg.attach(MIMEText(
                    "✅ Email notifications are working!\n\n"
                    "You will receive alerts when articles are published or jobs fail.\n\n"
                    f"— Content Agent @ {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                    "plain"
                ))

                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                    server.starttls()
                    server.login(smtp_user, smtp_pass)
                    server.send_message(msg)

                results["email"] = {"ok": True, "message": f"Test email sent to {to_addr}"}
                print(f"[AGENT] 📧 Test email sent to {to_addr}")

        except smtplib.SMTPAuthenticationError as e:
            error = f"Authentication failed. For Gmail, make sure you're using an App Password (not your regular password). Error: {e}"
            results["email"] = {"ok": False, "error": error}
            print(f"[AGENT] [WARN] Test email auth failed: {e}")
        except Exception as e:
            results["email"] = {"ok": False, "error": str(e)}
            print(f"[AGENT] [WARN] Test email failed: {e}")
    else:
        results["email"] = {"ok": False, "error": "Email notifications are disabled"}

    # Test Telegram
    if get_setting("notify_telegram_enabled", "false") == "true":
        try:
            import requests as req
            token = get_setting("notify_telegram_bot_token", "")
            chat_id = get_setting("notify_telegram_chat_id", "")

            if not token or not chat_id:
                results["telegram"] = {"ok": False, "error": "Missing bot token or chat ID"}
            else:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                resp = req.post(url, json={
                    "chat_id": chat_id,
                    "text": "✅ Content Agent test — Telegram notifications are working!",
                }, timeout=10)

                if resp.status_code == 200:
                    results["telegram"] = {"ok": True, "message": f"Test message sent to chat {chat_id}"}
                    print(f"[AGENT] 📱 Test Telegram sent to chat {chat_id}")
                else:
                    error = resp.json().get("description", resp.text[:200])
                    results["telegram"] = {"ok": False, "error": f"Telegram API: {error}"}
        except Exception as e:
            results["telegram"] = {"ok": False, "error": str(e)}
    else:
        results["telegram"] = {"ok": False, "error": "Telegram notifications are disabled"}

    return jsonify(results)


# ──────────────────────────────────────────────
# Startup
# ──────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("🤖 Content Publishing Agent")
    print(f"📊 Dashboard: http://localhost:5000")
    print(f"💾 Database: {DB_PATH.absolute()}")

    # Start agent background thread
    agent_thread = threading.Thread(target=agent_loop, daemon=True)
    agent_thread.start()

    app.run(host="0.0.0.0", port=5000, debug=False)
