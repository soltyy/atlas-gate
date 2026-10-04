"""Самостоятельный Gate: никаких локальных SDK/реестров Router."""
import logging
import uvicorn
from fastapi import FastAPI
from . import __version__
from .settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    from .gate import check_settings, router
    settings = settings or Settings()
    check_settings(settings)
    if not settings.ATLAS_TOKEN:
        raise ValueError("Gate требует непустой ATLAS_TOKEN для административного API")
    app = FastAPI(title="Atlas Gate", version=__version__)
    app.state.settings = settings
    app.include_router(router)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.ATLAS_HOST, port=settings.ATLAS_PORT)
