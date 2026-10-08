import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.billing import provision
from app.config.settings import Settings


def test_financial_operator_errors_do_not_expose_password_sql(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    private = "must-not-appear-in-operator-output"

    async def failed(_settings: Settings) -> None:
        raise SQLAlchemyError(f"CREATE ROLE financial PASSWORD '{private}'")

    monkeypatch.setattr(provision, "provision_financial_role", failed)
    monkeypatch.setattr(Settings, "from_environment", lambda: settings)
    with pytest.raises(SystemExit) as error:
        provision.main()
    assert "Financial login provisioning failed" in str(error.value)
    assert private not in str(error.value)
    assert error.value.__suppress_context__
