"""Run offline migrations before starting one application worker."""

from typing import cast

from app.health import database_paths
from app.storage.migrations import DatabaseRole, ensure_database
from vinted_deal_hunter.config import Settings


def upgrade(settings: Settings) -> None:
    for role, path in database_paths(settings).items():
        ensure_database(path, cast(DatabaseRole, role))


def main() -> None:
    upgrade(Settings.from_env())
    print("All four database roles migrated successfully.")


if __name__ == "__main__":
    main()
