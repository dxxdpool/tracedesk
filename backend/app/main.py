from fastapi import FastAPI

from app.errors import ApiError, api_error_handler, unexpected_error_handler
from app.routes import evidence, experiments, hypotheses, investigations


def create_app() -> FastAPI:
    app = FastAPI(title="TraceDesk Evidence Ledger")
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    for module in (investigations, hypotheses, evidence, experiments):
        app.include_router(module.router)

    return app


app = create_app()
