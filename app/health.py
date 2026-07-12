"""
health.py — health-check HTTP-сервер для Railway.

Бот работает через long polling и по умолчанию не слушает ни один порт —
Railway не может понять, жив ли процесс, кроме как по exit-коду (крашнулся
или нет), но зависший-но-не-упавший процесс так не поймать. Этот лёгкий
aiohttp-сервер даёт Railway (healthcheckPath в railway.toml) GET /health
для проверки, что процесс жив и event loop не завис.
"""
import logging
import time

from aiohttp import web

logger = logging.getLogger(__name__)

_start_time = time.monotonic()


async def _health(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "uptime_seconds": round(time.monotonic() - _start_time, 1),
    })


async def start_health_server(port: int) -> web.AppRunner:
    """
    Запускает health-check сервер в фоне текущего event loop.
    Возвращает AppRunner — вызывающий код должен вызвать await .cleanup() при остановке.
    """
    app = web.Application()
    app.router.add_get("/health", _health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()

    logger.info("🩺 Health-check слушает на 0.0.0.0:%d/health", port)
    return runner
