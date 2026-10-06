"""Самостоятельный Gate: никаких локальных SDK/реестров Router."""
import logging
import asyncio
import contextlib
import ssl
import json
import os
from pathlib import Path
import uvicorn
from fastapi import FastAPI
from . import __version__
from .settings import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    from .gate import check_settings, router
    settings = settings or Settings()
    from .gate.state import read_keys_file
    secret_file = read_keys_file(settings.ATLAS_GATE_NODE_ENV_FILE)
    configured = Path(settings.ATLAS_GATE_NODES_FILE)
    if configured.is_file():
        for node in json.loads(configured.read_text(encoding="utf-8")):
            name = node.get("token_env")
            if name and name in secret_file:
                os.environ[name] = secret_file[name]
    check_settings(settings)
    if not settings.ATLAS_TOKEN:
        raise ValueError("Gate требует непустой ATLAS_TOKEN для административного API")
    if settings.ATLAS_TOKEN.startswith("REPLACE_") or settings.ATLAS_GATE_KEYRING_SECRET.startswith("REPLACE_"):
        raise ValueError("замените примерные секреты .env перед запуском")
    app = FastAPI(title="Atlas Gate", version=__version__)
    app.state.settings = settings
    app.state.node_mtls = False
    app.include_router(router)
    from .gate.node_channel import router as node_router, NodeBodyLimit
    app.include_router(node_router)
    app.add_middleware(NodeBodyLimit)
    return app


def create_node_app(owner: FastAPI) -> FastAPI:
    from .gate.node_channel import router, NodeBodyLimit
    @contextlib.asynccontextmanager
    async def lifespan(app):
        for _ in range(1000):
            if hasattr(owner.state, "gate"):
                app.state.gate = owner.state.gate
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("основной Gate не запущен")
        yield
    app = FastAPI(title="Atlas Gate node ingress", lifespan=lifespan)
    app.state.settings = owner.state.settings
    app.state.node_mtls = True
    app.include_router(router)
    app.add_middleware(NodeBodyLimit)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    app = create_app(settings)
    if not settings.ATLAS_GATE_NODE_TLS_PORT:
        uvicorn.run(app, host=settings.ATLAS_HOST, port=settings.ATLAS_PORT)
        return
    if not all((settings.ATLAS_GATE_NODE_TLS_CERT, settings.ATLAS_GATE_NODE_TLS_KEY, settings.ATLAS_GATE_NODE_TLS_CA)):
        raise ValueError("node mTLS listener требует CERT, KEY и CA")
    if settings.ATLAS_PORT == settings.ATLAS_GATE_NODE_TLS_PORT:
        raise ValueError("порты клиентов и узлов должны различаться")
    async def serve():
        public = uvicorn.Server(uvicorn.Config(app, host=settings.ATLAS_HOST, port=settings.ATLAS_PORT))
        nodes = uvicorn.Server(uvicorn.Config(create_node_app(app), host=settings.ATLAS_HOST, port=settings.ATLAS_GATE_NODE_TLS_PORT,
            ssl_certfile=settings.ATLAS_GATE_NODE_TLS_CERT, ssl_keyfile=settings.ATLAS_GATE_NODE_TLS_KEY,
            ssl_ca_certs=settings.ATLAS_GATE_NODE_TLS_CA, ssl_cert_reqs=ssl.CERT_REQUIRED,
            limit_concurrency=256, timeout_graceful_shutdown=35))
        tasks = [asyncio.create_task(public.serve()), asyncio.create_task(nodes.serve())]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            public.should_exit = nodes.should_exit = True
            await asyncio.gather(*tasks)
        finally:
            public.should_exit = nodes.should_exit = True
    asyncio.run(serve())
