# Instagram Video → WordPress Auto-Publisher

Automatically download Instagram Reels, analyze them with Google Gemini AI, generate blog content + media, and publish to WordPress.

## Prerequisites

- **Python 3.9+**
- **OpenCV & Pillow** — Installed automatically via `pip install -r requirements.txt`
- **A Google Gemini API Key** — [Get one here](https://aistudio.google.com/app/apikey)
- **WordPress Application Password** — Go to `Users → Profile → Application Passwords` in your WP admin
- **cookies.txt** — Export your Instagram cookies from Chrome/Edge using a "Get cookies.txt" extension and save as `cookies.txt` in the project root.

## Quick Setup

```bash
# 1. Clone / navigate to the project folder
cd insta-to-wp

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Create your config
copy .env.template .env        # Windows
# cp .env.template .env        # macOS/Linux

# 4. Edit .env with your real values
notepad .env
```

### .env Configuration

| Variable | Description |
|---|---|
| `GOOGLE_API_KEY` | Your Gemini API key |
| `WP_URL` | Your WordPress site (e.g. `https://mysite.com`) |
| `WP_USERNAME` | WordPress username |
| `WP_APP_PASSWORD` | WordPress Application Password (not your login password) |
| `WP_CATEGORY_ID` | Post category ID (default: `1`) |
| `WP_POST_STATUS` | `publish` or `draft` |

## Usage

### Single URL
```bash
python main.py https://www.instagram.com/reel/ABC123/
```

### Multiple URLs
```bash
python main.py https://www.instagram.com/reel/ABC123/ https://www.instagram.com/reel/DEF456/
```

### From a file (one URL per line)
```bash
python main.py --file urls.txt
```

### Dry run (no WordPress publishing)
```bash
python main.py --dry-run https://www.instagram.com/reel/ABC123/
```

## Article Rewriter Mode

Scrape any article from the web, rewrite it with AI, add related YouTube videos & images, and publish.

### Single Article
```bash
python main.py --rewrite https://example.com/some-news-article
```

### Multiple Articles from File
```bash
python main.py --rewrite --file articles.txt
```

### Dry Run (no publishing)
```bash
python main.py --rewrite --dry-run https://example.com/article
```

### What Article Rewriter Does

1. **Scrapes** the article content (title, body, images) from the URL.
2. **Rewrites** the article using **Gemini AI** into unique, SEO-optimized content (5-8 paragraphs).
3. **Finds related YouTube videos** and embeds them in the post.
4. **Downloads & uploads** article images to WordPress.
5. **Creates a Featured Image** (16:9 collage) from the article's images.
6. **Publishes** with Gutenberg blocks: paragraphs, YouTube embeds (`<!-- wp:embed -->`), image blocks, source link button, and ad shortcodes.
7. **Tags** the post with AI-generated SEO tags.

---

## What Instagram Mode Does

1. **Downloads** the Instagram Reels/Photos using `yt-dlp` or `instaloader`.
2. **Extracts** key frames using **OpenCV** (no external ffmpeg required).
3. **Analyzes** the media with **Google Gemini AI**.
4. **Generates** a catchy title + 2-paragraph blog post.
5. **Creates a Featured Image** (16:9) — A modern **3:1 collage** of frames with NO text.
6. **Creates a Square Flyer** (1080x1080) — Two seamless portrait frames with a **"WATCH THE VIDEO BELOW"** overlay.
7. **Uploads** media to WordPress.
8. **Publishes** the post with Gutenberg blocks (including a centered **Button Block** for the link).

## Output Structure

```
insta-to-wp/
├── downloads/          # Downloaded videos
├── output/             # Generated images + results.json
├── main.py
├── requirements.txt
├── .env
└── .env.template
```

## Troubleshooting

| Issue | Solution |
|---|---|
| `yt-dlp` fails | Update: `pip install -U yt-dlp`. Some private reels require login. |
| `ffmpeg not found` | Install ffmpeg and add to PATH. Restart your terminal. |
| WordPress 401 error | Check your Application Password — NOT your login password. |
| Gemini quota exceeded | Wait or upgrade your API plan. The script falls back to frame analysis. |

## WordPress Application Password Setup

1. Log into your WordPress admin
2. Go to **Users → Profile**
3. Scroll to **Application Passwords**
4. Enter a name (e.g. "Instagram Bot") and click **Add New**
5. Copy the generated password into your `.env` file
