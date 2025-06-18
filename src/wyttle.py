import os
import json
import re
from pathlib import Path
import shutil
import argparse
import http.server
import socketserver
import threading
import webbrowser
from urllib.parse import urlparse
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from csscompressor import compress as css_compress
from rjsmin import jsmin
import socket
import uuid
import logging

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


def load_config(config_path):
    """Load the wyttle.config.json file."""
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def resolve_inline_path(file_ref, current_file):
    """Resolve the path of a local CSS or JS file."""
    current_dir = Path(current_file).parent
    try:
        resolved_path = (current_dir / file_ref).resolve()
        return resolved_path if resolved_path.exists() else None
    except (FileNotFoundError, OSError):
        return None


def load_file_content(file_path):
    """Read content of a file."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def inline_css(content, file_path):
    """Inline local CSS files and minify."""

    def css_repl(match):
        href = match.group(1).strip()
        if href.startswith(("http", "//")):
            return match.group(0)  # Keep external URLs
        css_path = resolve_inline_path(href, file_path)
        if css_path:
            css_content = load_file_content(css_path)
            if css_content:
                try:
                    minified_css = css_compress(css_content)
                    return f"<style>{minified_css}</style>"
                except Exception as e:
                    logging.error(f"CSS compression failed for {css_path}: {e}")
                    return f"<style>{css_content}</style>"
            logging.warning(f"CSS file empty: {href}")
        else:
            logging.warning(f"CSS file not found: {href}")
        return match.group(0)  # Preserve original tag if file not found

    # Robust regex to handle various link tag formats
    return re.sub(
        r'<link\s+(?=[^>]*href=["\'][^"\']*["\'])(?=[^>]*rel=["\']stylesheet["\'])[^>]*?href=["\']([^"\']+)["\'][^>]*?>',
        css_repl,
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )


def inline_js(content, file_path):
    """Inline local JS files and minify."""

    def js_repl(match):
        src = match.group(1).strip()
        if src.startswith(("http", "//")):
            return match.group(0)  # Keep external URLs
        js_path = resolve_inline_path(src, file_path)
        if js_path:
            js_content = load_file_content(js_path)
            if js_content:
                minified_js = jsmin(js_content)
                return f"<script>{minified_js}</script>"
            logging.warning(f"JS file empty: {src}")
        else:
            logging.warning(f"JS file not found: {src}")
        return match.group(0)  # Preserve original tag if file not found

    return re.sub(
        r'<script\s+[^>]*?src=["\'](.*?)["\'][^>]*?></script>',
        js_repl,
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )


def minify_html(content, remove_empty_space=True, remove_comments=True):
    if remove_comments:
        content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)

    if remove_empty_space:
        content = re.sub(r"\s+", " ", content)
        content = re.sub(r">\s+<", "><", content)
        content = content.strip()

    return content


def resolve_template_path(template_ref, current_file):
    """Resolve the relative path of a template include."""
    match = re.match(r"<%@\s*(.*?)\s*%>", template_ref)
    if not match:
        return None
    template_path = match.group(1).strip()
    current_dir = Path(current_file).parent
    try:
        resolved_path = (current_dir / template_path).resolve()
        return resolved_path if resolved_path.exists() else None
    except (FileNotFoundError, OSError):
        return None


def process_template(content, template_data):
    """Replace template placeholders with provided data."""
    for key, value in template_data.items():
        # Handle both <template:key>...</template:key> and <template:key />
        placeholder = f"<template:{key}(?:>| />)"
        content = re.sub(placeholder, value, content, flags=re.DOTALL)
        # Clean up any closing tags if they exist
        content = re.sub(f"</template:{key}>", "", content, flags=re.DOTALL)
    return content


def process_js_blocks(content):
    """Convert %%...%% JS blocks into script tags with data-wyttle-ref."""

    def replace_js_block(match):
        js_code = match.group(2).strip()
        element_tag = match.group(1)
        unique_id = str(uuid.uuid4())
        minified_js = jsmin(js_code)
        script = f'<script>document.querySelector("[data-wyttle-ref=\\"{unique_id}\\"]").textContent = {minified_js};</script>'
        return f'<{element_tag} data-wyttle-ref="{unique_id}">{script}</{element_tag}>'

    return re.sub(
        r"<([a-zA-Z]+)[^>]*>%%(.*?)%%</\1>", replace_js_block, content, flags=re.DOTALL
    )


def process_file(file_path, output_dir, minify=True):
    """Process a single HTML file, handling includes, templates, JS blocks, CSS, and JS."""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Find all template includes
    includes = re.findall(r"<%@\s*[^>]+%>", content, re.DOTALL)
    template_data = {}

    # Extract template data (e.g., <template:title>...</template:title>)
    template_matches = re.findall(
        r"<template:([^>]+)>(.*?)</template:\1>", content, re.DOTALL
    )

    # Store template data and remove template tags from content
    for key, value in template_matches:
        template_data[key] = value.strip()
        content = re.sub(
            f"<template:{key}>.*?</template:{key}>", "", content, flags=re.DOTALL
        )

    # Process includes
    for include in includes:
        template_path = resolve_template_path(include, file_path)
        if template_path and template_path.exists():
            template_content = load_file_content(template_path)
            if template_content:
                processed_template = process_template(template_content, template_data)
                content = content.replace(include, processed_template)
            else:
                logging.warning(f"Template content empty: {include}")
        else:
            logging.warning(f"Template not found: {include}")
            content = content.replace(include, "")  # Remove invalid include

    # Remove residual tags (e.g., <%% />, self-closing templates)
    content = re.sub(r"<template:[^>]+ />|<%%\s*/>", "", content, flags=re.DOTALL)

    # Inline and minify CSS
    content = inline_css(content, file_path)

    # Inline and minify JS
    content = inline_js(content, file_path)

    # Process %%...%% JavaScript blocks
    content = process_js_blocks(content)

    # Minify HTML if enabled
    if minify:
        content = minify_html(content, remove_empty_space=True, remove_comments=True)

    # Write to output directory
    relative_path = file_path.relative_to(file_path.parents[1])
    output_path = output_dir / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)


def build_project(src_dir, dist_dir, config, minify=True):
    """Build the project by processing all HTML files."""
    src_path = Path(src_dir)
    dist_path = Path(dist_dir)

    # Clean the dist directory
    if dist_path.exists():
        shutil.rmtree(dist_path)
    dist_path.mkdir(parents=True)

    # Walk through src directory
    for root, _, files in os.walk(src_path / "pages"):
        for file in files:
            if file.endswith(".html"):
                file_path = Path(root) / file
                process_file(file_path, dist_path, minify)

    # Move pages to root
    pages_path = dist_path / "pages"
    if pages_path.exists():
        for file in pages_path.iterdir():
            shutil.move(str(file), str(dist_path))
        shutil.rmtree(pages_path)


class DevServerHandler(http.server.SimpleHTTPRequestHandler):
    """Custom handler to serve files from dist directory."""

    def __init__(self, *args, dist_dir=None, **kwargs):
        self.dist_dir = dist_dir
        super().__init__(*args, **kwargs)

    def translate_path(self, path):
        path = urlparse(path).path
        path = Path(self.dist_dir) / path.lstrip("/")
        return str(path)


class FileWatcher(FileSystemEventHandler):
    """Watch for file changes and rebuild project."""

    def __init__(self, src_dir, dist_dir, config):
        self.src_dir = src_dir
        self.dist_dir = dist_dir
        self.config = config

    def on_any_event(self, event):
        if event.is_directory or event.src_path.endswith(".config.json"):
            return
        logging.info(f"Change detected: {event.src_path}")
        build_project(self.src_dir, self.dist_dir, self.config, minify=False)


def start_dev_server(src_dir, dist_dir, config, port=8000, no_reload=False):
    """Start a development server with optional live reloading."""
    # Initial build
    build_project(src_dir, dist_dir, config, minify=False)

    # Set up file watcher
    observer = Observer()
    watcher = FileWatcher(src_dir, dist_dir, config)
    observer.schedule(watcher, src_dir, recursive=True)
    observer.start()

    # Serve index.html with optional reload script
    reload_script = (
        ""
        if no_reload
        else """
    <script>
    let ws = new WebSocket(`ws://${location.host}`);
    ws.onmessage = () => location.reload();
    </script>
    """
    )
    index_path = Path(dist_dir) / "pages" / "index.html"
    if index_path.exists():
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()
        content = content.replace("</body>", f"{reload_script}</body>")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(content)

    # Start WebSocket server for live reload
    if not no_reload:

        def ws_server():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("localhost", port))
                s.listen()
                while True:
                    conn, _ = s.accept()
                    with conn:
                        while True:
                            data = conn.recv(1024)
                            if not data:
                                break
                            conn.sendall(b"reload")

        threading.Thread(target=ws_server, daemon=True).start()

    # Start HTTP server
    handler = lambda *args, **kwargs: DevServerHandler(
        *args, dist_dir=dist_dir, **kwargs
    )
    with socketserver.TCPServer(("", port), handler) as httpd:
        logging.info(f"Serving at http://localhost:{port}")
        webbrowser.open(f"http://localhost:{port}/pages/index.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            observer.stop()
            httpd.server_close()
    observer.join()


def main():
    """Main function to run the Wyttle CLI."""
    parser = argparse.ArgumentParser(
        description="Wyttle: A simple static site generator."
    )
    parser.add_argument("--src", default="src", help="Source directory (default: src)")
    parser.add_argument(
        "--dist", default="dist", help="Output directory (default: dist)"
    )
    parser.add_argument(
        "--config",
        default="wyttle.config.json",
        help="Config file (default: wyttle.config.json)",
    )
    parser.add_argument("--dev", action="store_true", help="Start development server")
    parser.add_argument(
        "--no-reload", action="store_true", help="Disable live reloading in dev server"
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port for dev server (default: 8000)"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if args.dev:
        start_dev_server(args.src, args.dist, config, args.port, args.no_reload)
    else:
        build_project(args.src, args.dist, config)
        print(f"Build completed. Output in {args.dist}")


if __name__ == "__main__":
    main()
