"""Dashboard assets live in static/ so markup, styles, and behavior are editable."""

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
BLOG_HTML = (STATIC_DIR / "blog.html").read_text(encoding="utf-8")

# Explicit paths keep the server from exposing other project files.
ASSETS = {
    "/static/app.css": (STATIC_DIR / "app.css", "text/css; charset=utf-8"),
    "/static/blog.css": (STATIC_DIR / "blog.css", "text/css; charset=utf-8"),
    "/static/app.js": (STATIC_DIR / "app.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": (STATIC_DIR / "favicon.svg", "image/svg+xml"),
}
