"""
test_health.py — health-check сервер для Railway.

Не мокаем aiohttp — реально поднимаем сервер на эфемерном порту и делаем
настоящий HTTP-запрос, чтобы убедиться что Railway действительно получит 200.
"""
import aiohttp
import pytest

from app.health import start_health_server


_TEST_PORT = 18080  # маловероятный конфликт в CI/локально


@pytest.fixture
async def running_server():
    runner = await start_health_server(port=_TEST_PORT)
    yield _TEST_PORT
    await runner.cleanup()


async def test_health_endpoint_returns_200(running_server):
    port = running_server
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/health") as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"


async def test_health_endpoint_reports_uptime(running_server):
    port = running_server
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/health") as resp:
            data = await resp.json()
            assert "uptime_seconds" in data
            assert data["uptime_seconds"] >= 0


async def test_unknown_path_returns_404(running_server):
    port = running_server
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/does-not-exist") as resp:
            assert resp.status == 404
