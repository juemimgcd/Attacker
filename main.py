from fastapi import FastAPI
from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.lifespan import create_lifespan
from conf.settings import settings

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app.app_name,
        debug=settings.app.debug,
        lifespan=create_lifespan(),
        version="v0.1.0",
    )



@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.get("/hello/{name}")
async def say_hello(name: str):
    return {"message": f"Hello {name}"}
