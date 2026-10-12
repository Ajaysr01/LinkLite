from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.session import get_db
from app.main import app


@pytest.fixture
def mock_db_session():
    mock = AsyncMock()
    mock.execute = AsyncMock(return_value=None)
    return mock


@pytest.mark.asyncio
async def test_health_healthy(mock_db_session):
    async def override_get_db():
        yield mock_db_session

    app.dependency_overrides[get_db] = override_get_db

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    with patch("app.api.health.get_redis_client", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert data["postgres"] == "ok"
            assert data["redis"] == "ok"
            assert "x-request-id" in response.headers

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_postgres_down(mock_db_session):
    mock_db_session.execute.side_effect = Exception("DB Connection Refused")

    async def override_get_db():
        yield mock_db_session

    app.dependency_overrides[get_db] = override_get_db

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    with patch("app.api.health.get_redis_client", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "degraded"
            assert data["postgres"] == "error"
            assert data["redis"] == "ok"

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_redis_down(mock_db_session):
    async def override_get_db():
        yield mock_db_session

    app.dependency_overrides[get_db] = override_get_db

    mock_redis = AsyncMock()
    mock_redis.ping.side_effect = Exception("Redis Connection Refused")

    with patch("app.api.health.get_redis_client", return_value=mock_redis):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/health")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "degraded"
            assert data["postgres"] == "ok"
            assert data["redis"] == "error"

    app.dependency_overrides.clear()
