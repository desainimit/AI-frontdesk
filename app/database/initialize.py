"""One-shot deployment job: migrate as owner, provision and verify restricted runtime role."""

import subprocess
import sys

from app.database.provision import main as provision
from app.platform.bootstrap import main as bootstrap_super_admin


def main() -> None:
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
    provision()
    bootstrap_super_admin()


if __name__ == "__main__":
    main()
