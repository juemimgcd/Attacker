from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from app.observability import configure_observability, shutdown_observability
from app.runtime import create_runtime
from conf.logging import setup_logger
from conf.settings import settings


def create_lifespan():
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logger()
        configure_observability(settings.observability)
        try:
            async with create_runtime(
                settings,
                recover_cleanups=settings.app.app_env.lower() not in {"production", "prod"},
            ) as runtime:
                runtime.install_on(app)
                logger.info(
                    "attacker service start",
                    service=settings.app.app_name,
                    environment=settings.app.app_env,
                )
                try:
                    yield
                finally:
                    app.state.catalog_ready = False
                    logger.info(
                        "attacker service stop",
                        service=settings.app.app_name,
                    )
        finally:
            shutdown_observability()

    return lifespan
