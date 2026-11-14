"""Create disposable, least-privilege PostgreSQL roles for a GitHub Actions job."""

from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path

import psycopg
from psycopg import sql


APP_DATABASE = "ci_stock_app"
OWNER_ROLE = "stock_app_owner"
RUNTIME_ROLE = "stock_app_runtime"
TEST_ROLE = "stock_app_test"


def require_actions_runner() -> Path:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("This helper is restricted to a GitHub Actions runner.")
    env_file = os.environ.get("GITHUB_ENV")
    if not env_file:
        raise SystemExit("GITHUB_ENV is required.")
    if (
        os.environ.get("PGHOST") != "127.0.0.1"
        or os.environ.get("PGDATABASE") != "postgres"
        or os.environ.get("PGUSER") != "postgres"
    ):
        raise SystemExit("Refusing a database target other than the local CI PostgreSQL service.")
    if os.environ.get("NOTIFICATION_EXTERNAL_ALLOWED") != "0":
        raise SystemExit("External notification transports must remain disabled.")
    if os.environ.get("NOTIFICATION_PUSH_ENABLED") != "0":
        raise SystemExit("Web Push must remain disabled.")
    return Path(env_file)


def prepare() -> None:
    env_file = require_actions_runner()
    port = os.environ.get("PGPORT", "5432")
    passwords = {
        "DATABASE_PASSWORD": secrets.token_urlsafe(32),
        "RUNTIME_DATABASE_PASSWORD": secrets.token_urlsafe(32),
        "TEST_DATABASE_PASSWORD": secrets.token_urlsafe(32),
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
        "OPERATION_GENERATION": str(uuid.uuid4()),
    }

    with psycopg.connect(
        dbname="postgres",
        user="postgres",
        password=os.environ["PGPASSWORD"],
        host="127.0.0.1",
        port=port,
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user, pg_is_in_recovery()")
            database_name, current_user, in_recovery = cursor.fetchone()
            if database_name != "postgres" or current_user != "postgres" or in_recovery:
                raise SystemExit("Refusing an unexpected PostgreSQL service identity.")

            for role in (OWNER_ROLE, RUNTIME_ROLE, TEST_ROLE):
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [role])
                if cursor.fetchone():
                    raise SystemExit(f"Refusing to reuse an existing CI role: {role}.")

            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(OWNER_ROLE), sql.Literal(passwords["DATABASE_PASSWORD"])
                )
            )
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(RUNTIME_ROLE),
                    sql.Literal(passwords["RUNTIME_DATABASE_PASSWORD"]),
                )
            )
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN CREATEDB PASSWORD {}").format(
                    sql.Identifier(TEST_ROLE), sql.Literal(passwords["TEST_DATABASE_PASSWORD"])
                )
            )
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(APP_DATABASE), sql.Identifier(OWNER_ROLE)
                )
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(APP_DATABASE), sql.Identifier(RUNTIME_ROLE)
                )
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE postgres TO {}").format(
                    sql.Identifier(TEST_ROLE)
                )
            )

    with psycopg.connect(
        dbname=APP_DATABASE,
        user="postgres",
        password=os.environ["PGPASSWORD"],
        host="127.0.0.1",
        port=port,
        autocommit=True,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            cursor.execute(
                sql.SQL("GRANT USAGE, CREATE ON SCHEMA public TO {}").format(
                    sql.Identifier(OWNER_ROLE)
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(RUNTIME_ROLE)
                )
            )

    values = {
        "DATABASE_NAME": APP_DATABASE,
        "DATABASE_USER": OWNER_ROLE,
        "RUNTIME_DATABASE_USER": RUNTIME_ROLE,
        "TEST_DATABASE_NAME": "test_stock_app_ci",
        "TEST_DATABASE_USER": TEST_ROLE,
        "DATABASE_HOST": "127.0.0.1",
        "DATABASE_PORT": port,
        "DJANGO_DEBUG": "0",
        "NOTIFICATION_EXTERNAL_ALLOWED": "0",
        "NOTIFICATION_PUSH_ENABLED": "0",
        "NOTIFICATION_EMAIL_MODE": "CAPTURE",
        "LOCAL_RECOVERY_ENABLED": "0",
        "TEST_REPORT_PATH": str(Path(os.environ["RUNNER_TEMP"]) / "reserve-tests" / "tests.json"),
        **passwords,
    }
    with env_file.open("a", encoding="utf-8") as output:
        for name, value in values.items():
            if "\n" in value or "\r" in value:
                raise SystemExit("Refusing a multiline CI environment value.")
            print(f"::add-mask::{value}")
            output.write(f"{name}={value}\n")


if __name__ == "__main__":
    prepare()
