"""One-shot deployment job: migrate as owner, provision and verify restricted runtime role."""

import subprocess
import sys

from app.billing.provision import main as provision_financial
from app.config.settings import Settings
from app.database.provision import main as provision
from app.platform.bootstrap import main as bootstrap_super_admin


def main() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    provision()
    settings = Settings.from_environment()
    if settings.financial_database_url:
        provision_financial()
        print("Restricted financial login provisioned and verified.")
    else:
        print("Financial writes disabled: FINANCIAL_DATABASE_URL is not configured.")
    bootstrap_super_admin()


if __name__ == "__main__":
    main()
