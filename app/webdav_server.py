"""
webdav_server.py — лёгкий WebDAV-сервер поверх Railway Volume.

Remotely Save в Obsidian подключается сюда как к обычному WebDAV.
Файлы хранятся в /data/obsidian_vault/ — на Railway Volume.

Настройки в Railway Variables:
  WEBDAV_USER     — логин  (default: raya)
  WEBDAV_PASSWORD — пароль (обязательно задать!)
  WEBDAV_PORT     — порт   (default: 8001)
  OBSIDIAN_VAULT_PATH — путь к vault (default: /data/obsidian_vault)

В Remotely Save:
  Server: https://<railway-domain>:8001  (или отдельный домен)
  Username / Password — из переменных выше
"""
import asyncio
import base64
import hashlib
import logging
import mimetypes
import os
import stat
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)

# ── Конфиг ────────────────────────────────────────────────────────────────────

VAULT_PATH  = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/data/obsidian_vault"))
WEBDAV_USER = os.getenv("WEBDAV_USER", "raya")
WEBDAV_PASS = os.getenv("WEBDAV_PASSWORD", "")
WEBDAV_PORT = int(os.getenv("WEBDAV_PORT", "8001"))
PREFIX      = "/webdav"    # все WebDAV запросы идут через /webdav/...


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

def _check_auth(request: web.Request) -> bool:
    """Проверяет Basic Auth."""
    if not WEBDAV_PASS:
        logger.warning("⚠️ WEBDAV_PASSWORD не задан — доступ открыт (небезопасно!)")
        return True

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode()
        user, pwd = decoded.split(":", 1)
        return user == WEBDAV_USER and pwd == WEBDAV_PASS
    except Exception:
        return False


def _auth_required(request: web.Request) -> Optional[web.Response]:
    if not _check_auth(request):
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Obsidian Vault"'},
            text="Unauthorized",
        )
    return None


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _url_to_path(url_path: str) -> Path:
    """Конвертирует URL путь в абсолютный путь на диске."""
    # Убираем PREFIX
    rel = url_path
    if rel.startswith(PREFIX):
        rel = rel[len(PREFIX):]
    rel = rel.lstrip("/")
    # Защита от path traversal
    target = (VAULT_PATH / rel).resolve()
    if not str(target).startswith(str(VAULT_PATH.resolve())):
        raise ValueError("Path traversal detected")
    return target


def _http_date(dt: datetime) -> str:
    return formatdate(dt.timestamp(), usegmt=True)


def _etag(path: Path) -> str:
    st = path.stat()
    return hashlib.md5(f"{path}{st.st_mtime}{st.st_size}".encode()).hexdigest()[:16]


def _propfind_xml(path: Path, href: str, depth: int) -> str:
    """Генерирует PROPFIND XML ответ."""
    NS = "DAV:"
    root = ET.Element(f"{{{NS}}}multistatus")

    def _add_response(p: Path, h: str) -> None:
        resp = ET.SubElement(root, f"{{{NS}}}response")
        ET.SubElement(resp, f"{{{NS}}}href").text = h

        propstat = ET.SubElement(resp, f"{{{NS}}}propstat")
        prop = ET.SubElement(propstat, f"{{{NS}}}prop")

        try:
            st = p.stat()
            is_dir = p.is_dir()

            # resourcetype
            rt = ET.SubElement(prop, f"{{{NS}}}resourcetype")
            if is_dir:
                ET.SubElement(rt, f"{{{NS}}}collection")

            # getcontentlength
            if not is_dir:
                ET.SubElement(prop, f"{{{NS}}}getcontentlength").text = str(st.st_size)

            # getlastmodified
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            ET.SubElement(prop, f"{{{NS}}}getlastmodified").text = _http_date(mtime)

            # getetag
            ET.SubElement(prop, f"{{{NS}}}getetag").text = f'"{_etag(p)}"'

            # getcontenttype
            if not is_dir:
                ctype, _ = mimetypes.guess_type(str(p))
                ET.SubElement(prop, f"{{{NS}}}getcontenttype").text = (
                    ctype or "application/octet-stream"
                )

            ET.SubElement(propstat, f"{{{NS}}}status").text = "HTTP/1.1 200 OK"

        except Exception as e:
            ET.SubElement(propstat, f"{{{NS}}}status").text = "HTTP/1.1 404 Not Found"
            logger.debug("propfind error for %s: %s", p, e)

    _add_response(path, href)

    if depth > 0 and path.is_dir():
        for child in sorted(path.iterdir()):
            child_href = href.rstrip("/") + "/" + child.name
            if child.is_dir():
                child_href += "/"
            _add_response(child, child_href)

    return '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode")


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def handle_options(request: web.Request) -> web.Response:
    return web.Response(
        status=200,
        headers={
            "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, MKCOL, PROPFIND, PROPPATCH, COPY, MOVE",
            "DAV": "1, 2",
            "MS-Author-Via": "DAV",
        },
    )


async def handle_propfind(request: web.Request) -> web.Response:
    err = _auth_required(request)
    if err: return err

    try:
        path = _url_to_path(request.path)
    except ValueError:
        return web.Response(status=403)

    if not path.exists():
        return web.Response(status=404)

    depth_str = request.headers.get("Depth", "1")
    depth = 0 if depth_str == "0" else 1

    href = request.path
    if path.is_dir() and not href.endswith("/"):
        href += "/"

    xml = _propfind_xml(path, href, depth)
    return web.Response(
        status=207,
        content_type="application/xml",
        charset="utf-8",
        text=xml,
    )


async def handle_get(request: web.Request) -> web.Response:
    err = _auth_required(request)
    if err: return err

    try:
        path = _url_to_path(request.path)
    except ValueError:
        return web.Response(status=403)

    if not path.exists():
        return web.Response(status=404)

    if path.is_dir():
        # Простой листинг директории
        items = sorted(path.iterdir())
        lines = [f"<li><a href='{i.name}{'/' if i.is_dir() else ''}'>{i.name}</a></li>"
                 for i in items]
        html = f"<html><body><ul>{''.join(lines)}</ul></body></html>"
        return web.Response(content_type="text/html", text=html)

    content_type, _ = mimetypes.guess_type(str(path))
    data = path.read_bytes()
    return web.Response(
        body=data,
        content_type=content_type or "application/octet-stream",
        headers={"ETag": f'"{_etag(path)}"'},
    )


async def handle_put(request: web.Request) -> web.Response:
    err = _auth_required(request)
    if err: return err

    try:
        path = _url_to_path(request.path)
    except ValueError:
        return web.Response(status=403)

    created = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = await request.read()
    path.write_bytes(data)

    logger.debug("📥 WebDAV PUT: %s (%d bytes)", path.relative_to(VAULT_PATH), len(data))
    return web.Response(status=201 if created else 204)


async def handle_delete(request: web.Request) -> web.Response:
    err = _auth_required(request)
    if err: return err

    try:
        path = _url_to_path(request.path)
    except ValueError:
        return web.Response(status=403)

    if not path.exists():
        return web.Response(status=404)

    if path.is_dir():
        import shutil
        shutil.rmtree(path)
    else:
        path.unlink()

    logger.debug("🗑️ WebDAV DELETE: %s", path.relative_to(VAULT_PATH))
    return web.Response(status=204)


async def handle_mkcol(request: web.Request) -> web.Response:
    err = _auth_required(request)
    if err: return err

    try:
        path = _url_to_path(request.path)
    except ValueError:
        return web.Response(status=403)

    if path.exists():
        return web.Response(status=405)

    path.mkdir(parents=True, exist_ok=True)
    return web.Response(status=201)


async def handle_move(request: web.Request) -> web.Response:
    err = _auth_required(request)
    if err: return err

    try:
        src = _url_to_path(request.path)
        dest_header = request.headers.get("Destination", "")
        # Dest может быть полным URL — берём только path
        from urllib.parse import urlparse
        dest_path_str = urlparse(dest_header).path
        dest = _url_to_path(dest_path_str)
    except ValueError:
        return web.Response(status=403)

    if not src.exists():
        return web.Response(status=404)

    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.move(str(src), str(dest))
    return web.Response(status=201)


async def handle_copy(request: web.Request) -> web.Response:
    err = _auth_required(request)
    if err: return err

    try:
        src = _url_to_path(request.path)
        dest_header = request.headers.get("Destination", "")
        from urllib.parse import urlparse
        dest_path_str = urlparse(dest_header).path
        dest = _url_to_path(dest_path_str)
    except ValueError:
        return web.Response(status=403)

    if not src.exists():
        return web.Response(status=404)

    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    if src.is_dir():
        shutil.copytree(str(src), str(dest))
    else:
        shutil.copy2(str(src), str(dest))
    return web.Response(status=201)


async def handle_proppatch(request: web.Request) -> web.Response:
    # Remotely Save иногда шлёт PROPPATCH — отвечаем 207 Multi-Status
    err = _auth_required(request)
    if err: return err

    href = request.path
    NS = "DAV:"
    root = ET.Element(f"{{{NS}}}multistatus")
    resp = ET.SubElement(root, f"{{{NS}}}response")
    ET.SubElement(resp, f"{{{NS}}}href").text = href
    propstat = ET.SubElement(resp, f"{{{NS}}}propstat")
    ET.SubElement(propstat, f"{{{NS}}}prop")
    ET.SubElement(propstat, f"{{{NS}}}status").text = "HTTP/1.1 200 OK"
    xml = '<?xml version="1.0" encoding="utf-8"?>' + ET.tostring(root, encoding="unicode")
    return web.Response(status=207, content_type="application/xml", text=xml)


# ══════════════════════════════════════════════════════════════════════════════
# РОУТЕР И ЗАПУСК
# ══════════════════════════════════════════════════════════════════════════════

async def _dispatch(request: web.Request) -> web.Response:
    """Диспетчер по методу HTTP."""
    method = request.method.upper()
    dispatch = {
        "OPTIONS":   handle_options,
        "PROPFIND":  handle_propfind,
        "GET":       handle_get,
        "HEAD":      handle_get,
        "PUT":       handle_put,
        "DELETE":    handle_delete,
        "MKCOL":     handle_mkcol,
        "MOVE":      handle_move,
        "COPY":      handle_copy,
        "PROPPATCH": handle_proppatch,
    }
    handler = dispatch.get(method)
    if handler:
        return await handler(request)
    return web.Response(status=405)


def create_webdav_app() -> web.Application:
    """Создаёт aiohttp приложение с WebDAV роутером."""
    VAULT_PATH.mkdir(parents=True, exist_ok=True)
    app = web.Application()
    app.router.add_route("*", PREFIX,        _dispatch)
    app.router.add_route("*", PREFIX + "/",  _dispatch)
    app.router.add_route("*", PREFIX + "/{tail:.*}", _dispatch)
    return app


async def start_webdav_server() -> asyncio.Task:
    """Запускает WebDAV сервер как фоновую задачу asyncio."""
    if not WEBDAV_PASS:
        logger.warning("⚠️ WEBDAV_PASSWORD не задан — WebDAV не запущен (небезопасно без пароля)")
        return None

    app    = create_webdav_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site   = web.TCPSite(runner, "0.0.0.0", WEBDAV_PORT)
    await site.start()

    logger.info(
        "📁 WebDAV запущен | порт: %d | vault: %s | user: %s",
        WEBDAV_PORT, VAULT_PATH, WEBDAV_USER,
    )
    return runner
