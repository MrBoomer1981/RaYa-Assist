"""
webdav_server.py — WebDAV сервер смонтированный в основной FastAPI.

Remotely Save в Obsidian подключается через:
  https://твой-домен.up.railway.app/webdav

Railway Variables:
  WEBDAV_USER     — логин  (default: raya)
  WEBDAV_PASSWORD — пароль (обязательно!)
  OBSIDIAN_VAULT_PATH — путь к vault (default: /data/obsidian_vault)
"""
import base64
import hashlib
import logging
import mimetypes
import os
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlparse, unquote

from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

def VAULT_PATH() -> Path:
    base = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/data/obsidian_vault"))
    subdir = os.getenv("OBSIDIAN_VAULT_SUBDIR", "RaYa-Vault")
    return base / subdir if subdir else base
WEBDAV_USER = os.getenv("WEBDAV_USER", "raya")
WEBDAV_PASS = os.getenv("WEBDAV_PASSWORD", "")
PREFIX      = "/webdav"


# ══════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════

def _check_auth(request: Request) -> bool:
    if not WEBDAV_PASS:
        return True
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, pwd = decoded.split(":", 1)
        return user == WEBDAV_USER and pwd == WEBDAV_PASS
    except Exception:
        return False


# ══════════════════════════════════════════════════════════
# PATH UTILS
# ══════════════════════════════════════════════════════════

def _req_to_path(request: Request) -> Path:
    """URL path → абсолютный путь на диске."""
    url_path = unquote(request.url.path)
    rel = url_path
    if rel.startswith(PREFIX):
        rel = rel[len(PREFIX):]
    rel = rel.lstrip("/")
    target = (VAULT_PATH() / rel).resolve()
    vault_resolved = VAULT_PATH().resolve()
    if not str(target).startswith(str(vault_resolved)):
        raise ValueError("Path traversal")
    return target


def _path_to_href(path: Path, request: Request) -> str:
    rel = path.relative_to(VAULT_PATH())
    href = PREFIX + "/" + str(rel).replace("\\", "/")
    if path.is_dir() and not href.endswith("/"):
        href += "/"
    return href


def _etag(path: Path) -> str:
    st = path.stat()
    return hashlib.md5(f"{path}{st.st_mtime}{st.st_size}".encode()).hexdigest()[:16]


def _http_date(dt: datetime) -> str:
    return formatdate(dt.timestamp(), usegmt=True)


# ══════════════════════════════════════════════════════════
# PROPFIND XML
# ══════════════════════════════════════════════════════════

def _propfind_xml(path: Path, href: str, depth: int) -> str:
    NS = "DAV:"
    root = ET.Element(f"{{{NS}}}multistatus")

    def _add(p: Path, h: str) -> None:
        resp     = ET.SubElement(root, f"{{{NS}}}response")
        ET.SubElement(resp, f"{{{NS}}}href").text = h
        propstat = ET.SubElement(resp, f"{{{NS}}}propstat")
        prop     = ET.SubElement(propstat, f"{{{NS}}}prop")
        try:
            st     = p.stat()
            is_dir = p.is_dir()
            rt     = ET.SubElement(prop, f"{{{NS}}}resourcetype")
            if is_dir:
                ET.SubElement(rt, f"{{{NS}}}collection")
            if not is_dir:
                ET.SubElement(prop, f"{{{NS}}}getcontentlength").text = str(st.st_size)
                ctype, _ = mimetypes.guess_type(str(p))
                ET.SubElement(prop, f"{{{NS}}}getcontenttype").text = ctype or "application/octet-stream"
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            ET.SubElement(prop, f"{{{NS}}}getlastmodified").text = _http_date(mtime)
            ET.SubElement(prop, f"{{{NS}}}getetag").text = f'"{_etag(p)}"'
            ET.SubElement(propstat, f"{{{NS}}}status").text = "HTTP/1.1 200 OK"
        except Exception:
            ET.SubElement(propstat, f"{{{NS}}}status").text = "HTTP/1.1 404 Not Found"

    _add(path, href)
    if depth > 0 and path.is_dir():
        for child in sorted(path.iterdir()):
            child_href = href.rstrip("/") + "/" + child.name
            if child.is_dir():
                child_href += "/"
            _add(child, child_href)

    return '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode")


# ══════════════════════════════════════════════════════════
# MAIN DISPATCHER
# ══════════════════════════════════════════════════════════

async def _dispatch(request: Request) -> Response:
    """Единая точка входа для всех WebDAV запросов."""

    # Убеждаемся что vault существует
    VAULT_PATH().mkdir(parents=True, exist_ok=True)

    # Auth
    if not _check_auth(request):
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Obsidian Vault"'},
            content="Unauthorized",
        )

    method = request.method.upper()

    # OPTIONS — отвечаем без проверки пути
    if method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Allow": "OPTIONS,GET,HEAD,PUT,DELETE,MKCOL,PROPFIND,PROPPATCH,COPY,MOVE",
                "DAV": "1, 2",
                "MS-Author-Via": "DAV",
            },
        )

    try:
        path = _req_to_path(request)
    except ValueError:
        return Response(status_code=403, content="Forbidden")

    href = _path_to_href(path, request) if path.exists() else request.url.path

    # ── PROPFIND ──────────────────────────────────────────
    if method == "PROPFIND":
        if not path.exists():
            return Response(status_code=404)
        depth = 0 if request.headers.get("depth", "1") == "0" else 1
        xml   = _propfind_xml(path, href, depth)
        return Response(status_code=207, media_type="application/xml; charset=utf-8", content=xml)

    # ── GET / HEAD ────────────────────────────────────────
    if method in ("GET", "HEAD"):
        if not path.exists():
            return Response(status_code=404)
        if path.is_dir():
            items = sorted(path.iterdir())
            lines = [f"<li><a href='{i.name}{'/' if i.is_dir() else ''}'>{i.name}</a></li>" for i in items]
            return Response(content=f"<html><body><ul>{''.join(lines)}</ul></body></html>", media_type="text/html")
        data     = path.read_bytes() if method == "GET" else b""
        ctype, _ = mimetypes.guess_type(str(path))
        return Response(
            content=data,
            media_type=ctype or "application/octet-stream",
            headers={"ETag": f'"{_etag(path)}"'},
        )

    # ── PUT ───────────────────────────────────────────────
    if method == "PUT":
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = await request.body()
        path.write_bytes(data)
        logger.debug("📥 WebDAV PUT: %s (%d b)", path.relative_to(VAULT_PATH()), len(data))
        return Response(status_code=201 if created else 204)

    # ── DELETE ────────────────────────────────────────────
    if method == "DELETE":
        if not path.exists():
            return Response(status_code=404)
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        logger.debug("🗑️ WebDAV DELETE: %s", path.relative_to(VAULT_PATH()))
        return Response(status_code=204)

    # ── MKCOL ─────────────────────────────────────────────
    if method == "MKCOL":
        if path.exists():
            return Response(status_code=405)
        path.mkdir(parents=True, exist_ok=True)
        return Response(status_code=201)

    # ── MOVE ──────────────────────────────────────────────
    if method == "MOVE":
        dest_header = request.headers.get("destination", "")
        dest_rel    = unquote(urlparse(dest_header).path)
        if dest_rel.startswith(PREFIX):
            dest_rel = dest_rel[len(PREFIX):]
        dest = (VAULT_PATH() / dest_rel.lstrip("/")).resolve()
        if not str(dest).startswith(str(VAULT_PATH().resolve())):
            return Response(status_code=403)
        if not path.exists():
            return Response(status_code=404)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        return Response(status_code=201)

    # ── COPY ──────────────────────────────────────────────
    if method == "COPY":
        dest_header = request.headers.get("destination", "")
        dest_rel    = unquote(urlparse(dest_header).path)
        if dest_rel.startswith(PREFIX):
            dest_rel = dest_rel[len(PREFIX):]
        dest = (VAULT_PATH() / dest_rel.lstrip("/")).resolve()
        if not str(dest).startswith(str(VAULT_PATH().resolve())):
            return Response(status_code=403)
        if not path.exists():
            return Response(status_code=404)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(path), str(dest)) if path.is_dir() else shutil.copy2(str(path), str(dest))
        return Response(status_code=201)

    # ── PROPPATCH ─────────────────────────────────────────
    if method == "PROPPATCH":
        NS   = "DAV:"
        root = ET.Element(f"{{{NS}}}multistatus")
        resp = ET.SubElement(root, f"{{{NS}}}response")
        ET.SubElement(resp, f"{{{NS}}}href").text = request.url.path
        ps   = ET.SubElement(resp, f"{{{NS}}}propstat")
        ET.SubElement(ps, f"{{{NS}}}prop")
        ET.SubElement(ps, f"{{{NS}}}status").text = "HTTP/1.1 200 OK"
        xml  = '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode")
        return Response(status_code=207, media_type="application/xml", content=xml)

    return Response(status_code=405)


async def start_webdav_server():
    """Заглушка — WebDAV теперь встроен в FastAPI, отдельный сервер не нужен."""
    logger.info("📁 WebDAV встроен в основной веб-сервер на /webdav")
    return None
