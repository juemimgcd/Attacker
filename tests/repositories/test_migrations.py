import sqlite3

from alembic.config import Config

from alembic import command
from conf.settings import settings


def test_empty_sqlite_database_upgrades_to_v1_head(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "migration.sqlite3"
    monkeypatch.setattr(
        settings.database,
        "url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(Config("alembic.ini"), "head")

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {
        "runs",
        "events",
        "findings",
        "state_fixtures",
        "state_snapshots",
        "retrieval_events",
        "replays",
    } <= tables
