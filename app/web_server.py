"""
web_server.py — FastAPI веб-интерфейс RaYa.
Защита через токен в URL: /?token=YOUR_TOKEN
Все разделы: чат, память, дневник, напоминания.
"""
import base64
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.tts_service import TTSService
from app.voice_service import VoiceService
from app.database import (
    MEMORY_CATEGORIES,
    clear_history,
    clear_memory,
    clear_structured_memory,
    delete_memory_entry,
    delete_reminder,
    get_active_reminders,
    get_conversation_context,
    get_structured_memory,
    load_diary_entries,
    load_history,
    load_memory,
    save_conversation_context,
    save_reminder,
)

logger = logging.getLogger(__name__)

STATIC_DIR  = Path("static")
_MEDIA_DIR  = Path("static/media")
_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
_WEB_TOKEN: str = os.getenv("WEB_TOKEN", "")

# ── Pydantic схемы ────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str

class ReminderRequest(BaseModel):
    text: str
    remind_at: str   # "YYYY-MM-DD HH:MM:SS"

# ── Приложение ────────────────────────────────────────────────────────────────

def create_app(llm_service) -> FastAPI:
    """
    Фабрика FastAPI приложения.
    llm_service передаётся снаружи — тот же экземпляр что использует бот.
    """
    app = FastAPI(title="RaYa", docs_url=None, redoc_url=None)
    _tts   = TTSService()
    _voice = VoiceService()

    # Создаём директорию для изображений если нет
    (STATIC_DIR / "media").mkdir(parents=True, exist_ok=True)

    def _check_token(token: str = Query(default="")) -> None:
        """Проверяет токен. Если WEB_TOKEN не задан — пропускает всех."""
        if _WEB_TOKEN and token != _WEB_TOKEN:
            raise HTTPException(status_code=401, detail="Неверный токен")

    # ── Статика ───────────────────────────────────────────────────────────────

    @app.get("/")
    async def index(token: str = Query(default="")):
        _check_token(token)
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Чат ───────────────────────────────────────────────────────────────────

    @app.post("/api/chat")
    async def chat(req: ChatRequest, token: str = Query(default="")):
        _check_token(token)
        try:
            user_id = settings.telegram_user_id
            result = await llm_service.chat(user_id, req.message)

            # Изображение от ImageAgent — сохраняем и отдаём URL
            image_url: Optional[str] = None
            if "image" in result.agent_name:
                image_bytes = (result.metadata or {}).get("image_bytes")
                if image_bytes:
                    import uuid
                    fname = f"{uuid.uuid4().hex}.jpg"
                    fpath = _MEDIA_DIR / fname
                    fpath.write_bytes(image_bytes)
                    image_url = f"/static/media/{fname}"
                    logger.info("🎨 Изображение сохранено: %s", fname)

            return {
                "reply":      result.reply,
                "agent_name": result.agent_name,
                "reminder":   result.reminder,
                "image_url":  image_url,
                "emotion":    (result.metadata or {}).get("emotion", "calm"),
            }
        except Exception as e:
            logger.exception("Ошибка чата")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/history")
    async def history(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        messages = load_history(user_id, limit=50)
        return [
            {"role": "human" if m.__class__.__name__ == "HumanMessage" else "ai",
             "content": m.content}
            for m in messages
        ]

    @app.delete("/api/history")
    async def delete_history(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        clear_history(user_id)
        return {"ok": True}

    # ── Память ────────────────────────────────────────────────────────────────

    @app.get("/api/memory")
    async def memory(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        structured = get_structured_memory(user_id)
        # Также возвращаем старые факты для совместимости
        legacy = load_memory(user_id)
        return {
            "structured": structured,
            "categories":  MEMORY_CATEGORIES,
            "legacy_facts": legacy,
        }

    @app.delete("/api/memory")
    async def delete_memory(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        clear_memory(user_id)
        clear_structured_memory(user_id)
        return {"ok": True}

    @app.delete("/api/memory/{category}/{key}")
    async def delete_memory_entry(
        category: str, key: str,
        token: str = Query(default=""),
    ):
        """Удаляет конкретную запись из структурированной памяти."""
        _check_token(token)
        user_id = settings.telegram_user_id
        ok = delete_memory_entry(user_id, category, key)
        return {"ok": ok}

    # ── Напоминания ───────────────────────────────────────────────────────────

    @app.get("/api/reminders")
    async def reminders(token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        items = get_active_reminders(user_id)
        return {"reminders": [
            {"id": r[0], "text": r[1], "remind_at": r[2]}
            for r in items
        ]}

    @app.post("/api/reminders")
    async def add_reminder(req: ReminderRequest, token: str = Query(default="")):
        _check_token(token)
        try:
            user_id = settings.telegram_user_id
            remind_at = datetime.strptime(req.remind_at, "%Y-%m-%d %H:%M:%S")
            rid = save_reminder(user_id, req.text, remind_at)
            return {"id": rid, "ok": True}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/api/reminders/{reminder_id}")
    async def remove_reminder(
        reminder_id: int,
        token: str = Query(default=""),
    ):
        _check_token(token)
        user_id = settings.telegram_user_id
        ok = delete_reminder(reminder_id, user_id)
        return {"ok": ok}

    # ── Контекст разговора ───────────────────────────────────────────────────

    @app.get("/api/context")
    async def conversation_context(token: str = Query(default="")):
        """Текущий контекст разговора: тема, цель, незавершённые темы, резюме."""
        _check_token(token)
        user_id = settings.telegram_user_id
        return get_conversation_context(user_id)

    @app.delete("/api/context")
    async def clear_context(token: str = Query(default="")):
        """Сбрасывает контекст разговора."""
        _check_token(token)
        user_id = settings.telegram_user_id
        save_conversation_context(user_id)
        return {"ok": True}

    # ── Дневник ───────────────────────────────────────────────────────────────

    @app.get("/api/diary")
    async def diary(limit: int = 20, token: str = Query(default="")):
        _check_token(token)
        user_id = settings.telegram_user_id
        entries = load_diary_entries(user_id, limit=limit)
        return {"entries": [
            {"created_at": e[0], "entry": e[1]}
            for e in entries
        ]}

    # ── Голос ────────────────────────────────────────────────────────────────

    @app.post("/api/voice")
    async def voice_chat(request: Request, token: str = Query(default="")):
        """
        Принимает аудио (webm/ogg) → Whisper → LLM → TTS.
        Возвращает: {text, reply, audio_base64 | null, agent_name}
        """
        _check_token(token)
        try:
            body = await request.body()
            if not body:
                raise HTTPException(status_code=400, detail="Пустое аудио")

            # Whisper — передаём байты напрямую
            text = await _voice.transcribe(body)

            if not text:
                return {"text": "", "reply": "Не удалось распознать речь", "audio_base64": None, "agent_name": "raya"}

            # LLM — генерируем ответ
            user_id = settings.telegram_user_id
            result  = await llm_service.chat(user_id, text, is_voice=True)

            # TTS — озвучиваем ответ
            audio_bytes = await _tts.synthesize(result.reply, is_voice=True) if _tts.enabled else None
            audio_b64   = base64.b64encode(audio_bytes).decode() if audio_bytes else None

            emotion = (result.metadata or {}).get("emotion", "calm")
            return {
                "text":        text,
                "reply":       result.reply,
                "audio_base64": audio_b64,
                "agent_name":  result.agent_name,
                "emotion":     emotion,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Ошибка голосового чата")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/api/tts_enabled")
    async def tts_status(token: str = Query(default="")):
        _check_token(token)
        return {"enabled": _tts.enabled}

    # ── Статус системы ────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def status(token: str = Query(default="")):
        _check_token(token)
        from app.agents.registry import get_enabled_agents
        return {
            "model":        settings.model_name,
            "search":       settings.search_enabled,
            "agents":       [a.name for a in get_enabled_agents()],
            "utc_time":     datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        }


    # ── WebDAV (Obsidian Remotely Save) ───────────────────────────────────────
    # Монтируем на тот же порт что и веб-интерфейс — Railway даёт только 1 порт

    try:
        from app.webdav_server import _dispatch
        app.add_route("/webdav",          _dispatch, methods=["GET","PUT","DELETE","PROPFIND","MKCOL","MOVE","COPY","PROPPATCH","OPTIONS","HEAD"])
        app.add_route("/webdav/",         _dispatch, methods=["GET","PUT","DELETE","PROPFIND","MKCOL","MOVE","COPY","PROPPATCH","OPTIONS","HEAD"])
        app.add_route("/webdav/{path:path}", _dispatch, methods=["GET","PUT","DELETE","PROPFIND","MKCOL","MOVE","COPY","PROPPATCH","OPTIONS","HEAD"])
        logger.info("📁 WebDAV смонтирован на /webdav (основной порт)")
    except Exception:
        logger.warning("⚠️ WebDAV не смонтирован")


    # ── Очистка vault от лишних файлов ───────────────────────────────────────

    @app.delete("/api/vault/cleanup")
    async def vault_cleanup(token: str = Query(default="")):
        """Удаляет все файлы из vault кроме папок созданных RaYa."""
        _check_token(token)
        import os, shutil
        vault_base = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/data/obsidian_vault"))
        subdir     = os.getenv("OBSIDIAN_VAULT_SUBDIR", "RaYa-Vault")
        vault      = vault_base / subdir if subdir else vault_base

        if not vault.exists():
            return {"ok": False, "error": "vault не найден"}

        raya_folders = {"Дневник", "Заметки", "Задачи", "Zettelkasten", ".obsidian"}
        deleted = []

        for item in list(vault.iterdir()):
            if item.name not in raya_folders:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
                deleted.append(item.name)

        return {"ok": True, "deleted": deleted, "count": len(deleted)}


    @app.delete("/api/vault/file")
    async def vault_delete_file(path: str = Query(default=""), token: str = Query(default="")):
        """Удаляет конкретный файл из vault по relative path."""
        _check_token(token)
        import os, shutil
        vault_base = Path(os.getenv("OBSIDIAN_VAULT_PATH", "/data/obsidian_vault"))
        subdir     = os.getenv("OBSIDIAN_VAULT_SUBDIR", "RaYa-Vault")
        vault      = vault_base / subdir if subdir else vault_base

        if not path:
            raise HTTPException(status_code=400, detail="path обязателен")

        target = (vault / path).resolve()
        # Защита от path traversal
        if not str(target).startswith(str(vault.resolve())):
            raise HTTPException(status_code=403, detail="Forbidden")

        if not target.exists():
            raise HTTPException(status_code=404, detail="Файл не найден")

        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

        return {"ok": True, "deleted": path}

    @app.get("/api/features")
    async def features(token: str = Query(default="")):
        """Статус feature flags."""
        _check_token(token)
        from app.feature_flags import status as ff_status
        return ff_status()

    logger.info("🌐 Веб-сервер создан")
    return app
