import asyncio

import pytest
from sqlalchemy.exc import NoReferencedTableError, OperationalError

from app.worker import Queue, drain


async def test_drain_keeps_working_then_idles_until_stopped() -> None:
    stop = asyncio.Event()
    calls = 0

    async def step() -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            stop.set()
        return calls < 3

    await asyncio.wait_for(drain(Queue("jobs", step, 60), stop), timeout=2)
    # Two items were processed back to back; the empty poll then saw the stop signal
    # instead of sleeping for the full idle period.
    assert calls == 3


async def test_dependency_outage_is_survived_and_retried() -> None:
    stop = asyncio.Event()
    calls = 0

    async def step() -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError("select 1", {}, Exception("private connection detail"))
        stop.set()
        return False

    await asyncio.wait_for(drain(Queue("outbox", step, 0.01), stop), timeout=2)
    assert calls == 2


@pytest.mark.parametrize(
    "defect", [RuntimeError("bug"), NoReferencedTableError("unmapped table", "tenants")]
)
async def test_defects_are_not_mistaken_for_an_outage(defect: Exception) -> None:
    async def step() -> bool:
        raise defect

    with pytest.raises(type(defect)):
        await drain(Queue("jobs", step, 0.01), asyncio.Event())


async def test_once_runs_a_single_step() -> None:
    calls = 0

    async def step() -> bool:
        nonlocal calls
        calls += 1
        return True

    await drain(Queue("jobs", step, 60), asyncio.Event(), once=True)
    assert calls == 1
