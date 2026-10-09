"""Local carrier implementation with its own database and idempotent waybills."""

import os
from contextlib import asynccontextmanager
from secrets import compare_digest
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from tortoise import Tortoise, fields, models


class Shipment(models.Model):
    id = fields.IntField(pk=True)
    reference = fields.CharField(max_length=64, unique=True)
    waybill_no = fields.CharField(max_length=64, unique=True)
    payload = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)


class Parcel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reference: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")
    recipient: str = Field(min_length=1, max_length=80)
    phone: str = Field(pattern=r"^\+?[0-9 -]{7,24}$")
    address: str = Field(min_length=5, max_length=300)
    item_name: str = Field(min_length=1, max_length=100)
    quantity: StrictInt = Field(ge=1, le=1000)


@asynccontextmanager
async def lifespan(app):
    if not os.environ.get("CARRIER_TOKEN"):
        raise RuntimeError("CARRIER_TOKEN is required")
    await Tortoise.init(db_url=f"sqlite://{os.environ['CARRIER_DB']}", modules={"models": [__name__]})
    await Tortoise.generate_schemas(safe=True)
    try:
        yield
    finally:
        await Tortoise.close_connections()


app = FastAPI(title="Pilot Carrier", lifespan=lifespan)


def authorize(authorization: str = Header(default="")):
    if not compare_digest(authorization.encode(), f"Bearer {os.environ['CARRIER_TOKEN']}".encode()):
        raise HTTPException(401, "Invalid service credential")


def receipt(shipment):
    return {"reference": shipment.reference, "waybill_no": shipment.waybill_no, "status": "ACCEPTED"}


@app.get("/health")
async def health():
    await Tortoise.get_connection("default").execute_query("SELECT 1")
    return {"status": "ok"}


@app.post("/shipments", dependencies=[Depends(authorize)])
async def create_shipment(parcel: Parcel, idempotency_key: str = Header(default="")):
    if idempotency_key != parcel.reference:
        raise HTTPException(400, "Idempotency-Key must equal reference")
    existing = await Shipment.get_or_none(reference=parcel.reference)
    if existing:
        if existing.payload != parcel.model_dump():
            raise HTTPException(409, "Reference already used for another parcel")
        return receipt(existing)
    if parcel.quantity > 100:
        return JSONResponse(
            status_code=422, content={"code": "QUANTITY_LIMIT", "message": "单次寄件最多 100 件"}
        )
    shipment, _ = await Shipment.get_or_create(
        reference=parcel.reference,
        defaults={"waybill_no": f"WB-{uuid4().hex}", "payload": parcel.model_dump()},
    )
    if shipment.payload != parcel.model_dump():
        raise HTTPException(409, "Reference already used for another parcel")
    return receipt(shipment)


@app.get("/shipments/{reference}", dependencies=[Depends(authorize)])
async def get_shipment(reference: str):
    shipment = await Shipment.get_or_none(reference=reference)
    if shipment is None:
        raise HTTPException(404, "Shipment not found")
    return receipt(shipment)
