import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_live_health_endpoint():
    """Integration test connecting to live PostgreSQL and Redis running in Docker."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data == {
            "status": "healthy",
            "postgres": "ok",
            "redis": "ok",
        }
        assert "x-request-id" in response.headers
