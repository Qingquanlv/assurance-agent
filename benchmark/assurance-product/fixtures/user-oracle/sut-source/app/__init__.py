from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI
from tortoise import Tortoise

from app.core.exceptions import SettingNotFound
from app.core.init_app import (
    init_data,
    make_middlewares,
    register_exceptions,
    register_routers,
)

try:
    from app.settings.config import settings
except ImportError:
    raise SettingNotFound("Can not import settings")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_data()
    marker = Path(os.environ["AA_SUT_LIVE_MARKER"])
    identity = {
        "instance_id": os.environ["AA_SUT_INSTANCE_ID"],
        "pid": os.getpid(),
        "sqlite_path": settings.SQLITE_PATH,
    }
    encoded = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    token = bytes.fromhex(os.environ["AA_SUT_OWNERSHIP_TOKEN"])
    marker.write_text(
        json.dumps(
            {
                **identity,
                "proof": f"hmac-sha256:{hmac.new(token, encoded, hashlib.sha256).hexdigest()}",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    yield
    await Tortoise.close_connections()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_TITLE,
        description=settings.APP_DESCRIPTION,
        version=settings.VERSION,
        openapi_url="/openapi.json",
        middleware=make_middlewares(),
        lifespan=lifespan,
    )
    register_exceptions(app)
    register_routers(app, prefix="/api")
    return app


app = create_app()
