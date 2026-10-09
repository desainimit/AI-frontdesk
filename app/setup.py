"""One-shot setup job, safe to run again: apply migrations, provision the restricted
database logins and create the first super admin.

Run it before starting the API and the worker, and again after every upgrade.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

from app.database.provision import main as provision
from app.platform.bootstrap import main as bootstrap_super_admin

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def main() -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")
    provision()
    bootstrap_super_admin()


if __name__ == "__main__":
    main()
