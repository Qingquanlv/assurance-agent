"""Committed benchmark startup overrides; fault is frozen by the parent harness."""

import json
import os
from pathlib import Path

from tortoise.transactions import in_transaction

from app.controllers.user import UserController
from app.models.admin import User


class BenchmarkRollback(RuntimeError):
    pass


def install():
    fault = os.environ.get("AA_SUT_FAULT", "none")
    original = UserController.create_user
    if fault not in {"wrong-value", "rollback", "rollback-success", "missing-write"}:
        return

    def record(event, **fields):
        path = Path(os.environ["AA_SUT_FAULT_FACTS"])
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def create(self, obj_in):
        if fault == "wrong-value":
            obj_in.is_active = False
            return await original(self, obj_in)
        if fault == "missing-write":
            return User(id=2147483647, username=obj_in.username)
        try:
            async with in_transaction("sqlite") as connection:
                record("transaction_entered", connection_id=id(connection))
                user = await original(self, obj_in)
                count = await User.filter(username=obj_in.username).using_db(connection).count()
                record("write_observed", connection_id=id(connection), row_count=count)
                raise BenchmarkRollback("user-oracle-forced-rollback")
        except BenchmarkRollback:
            count = await User.filter(username=obj_in.username).count()
            record("rollback_observed", row_count=count)
            if fault == "rollback":
                raise
        # The original route returns the success envelope only after transaction exit.
        return user

    UserController.create_user = create
