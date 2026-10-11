"""Every evaluation scenario, with a prepared model script.

This proves what the server guarantees whatever the model says: nothing is changed
without a yes, an emergency gets the fixed script, a stranger is registered before a
booking, and a failed turn ends with a person. How well the real model does on the same
scenarios is measured separately with `python -m tests.evals` (see docs/runbook.md).
"""

import pytest

from tests.evals.harness import run
from tests.evals.scenarios import SCENARIOS, Scenario
from tests.integration.test_tenancy import TenancyFixture
from tests.integration.test_tenancy import pytestmark as pytestmark
from tests.integration.test_tenancy import tenancy as tenancy


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.name)
async def test_scenario(scenario: Scenario, tenancy: TenancyFixture) -> None:
    result = await run(scenario, tenancy)
    assert result.passed, f"{result.problems} | replies: {result.replies}"
