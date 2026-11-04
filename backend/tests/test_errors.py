from httpx import ASGITransport, AsyncClient

from app.main import create_app


async def test_unexpected_error_returns_500_with_reason_code() -> None:
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database fell over")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/boom")

    assert resp.status_code == 500
    assert resp.json() == {"reason_code": "internal_error", "message": "Unexpected server error"}
