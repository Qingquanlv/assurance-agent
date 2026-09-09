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


def _write_live_marker(path: Path, payload: dict[str, object]) -> None:
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(encoded)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_data()
    from app.benchmark_faults import install
    install()
    marker = Path(os.environ["AA_SUT_LIVE_MARKER"])
    identity = {
        "instance_id": os.environ["AA_SUT_INSTANCE_ID"],
        "pid": os.getpid(),
        "sqlite_path": settings.SQLITE_PATH,
    }
    encoded = json.dumps(identity, separators=(",", ":"), sort_keys=True).encode()
    token = bytes.fromhex(os.environ["AA_SUT_OWNERSHIP_TOKEN"])
    _write_live_marker(
        marker,
        {
            **identity,
            "proof": f"hmac-sha256:{hmac.new(token, encoded, hashlib.sha256).hexdigest()}",
        },
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
