# Local installation

This application is installed as a dedicated instance for one organisation. Django’s built-in development server is for local development only. Do not expose it to a network or use it as a production server.

## Prerequisites

- Python 3.13.
- PostgreSQL compatible with the project migrations.
- GNU gettext tools (`msgfmt`); on macOS, install them with `brew install gettext` and ensure the Homebrew `gettext` `bin` directory is on `PATH`.
- A PostgreSQL owner role, a separate runtime role, and a separate test role allowed to create its own temporary test database.

Databases, secrets, certificates, and accounts from other installations are not included. Generate new local values; never reuse credentials from another instance.

## Python environment

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python manage.py compilemessages
cp .env.example .env
```

Edit `.env` with a newly generated Django secret, a unique operation-generation ID, a local database, and local role credentials. To use the optional demo generator, name the dedicated database `demo_stock_app_*`. Never commit `.env`.

## Local PostgreSQL

Create the roles and dedicated database through a local PostgreSQL administration connection. The following are placeholders; replace every password with a locally generated value:

```sql
CREATE ROLE stock_app_owner LOGIN PASSWORD 'replace-with-a-local-secret';
CREATE ROLE stock_app_runtime LOGIN PASSWORD 'replace-with-another-local-secret';
CREATE ROLE stock_app_test LOGIN CREATEDB PASSWORD 'replace-with-a-test-only-secret';
GRANT CONNECT ON DATABASE postgres TO stock_app_test;
CREATE DATABASE demo_stock_app_local OWNER stock_app_owner;
GRANT CONNECT ON DATABASE demo_stock_app_local TO stock_app_runtime;
```

Set `DATABASE_NAME=demo_stock_app_local`, owner settings (`DATABASE_USER`, `DATABASE_PASSWORD`), runtime settings (`RUNTIME_DATABASE_USER`, `RUNTIME_DATABASE_PASSWORD`), and test settings (`TEST_DATABASE_USER`, `TEST_DATABASE_PASSWORD`) in `.env`. The test role is only for tests and must be able to create and remove its own `test_stock_app_*` databases. Keep the host at `127.0.0.1` for local use.

## Migrations and first manager

```sh
make migrate
.venv/bin/python manage.py grant_runtime --settings=config.settings.migration
.venv/bin/python manage.py bootstrap_admin --username manager --display-name "Local manager" --confirm-local-test --settings=config.settings.migration
```

`grant_runtime` grants the runtime role the SQL permissions expected by the application. `bootstrap_admin` is restricted to the local host and owner role. It creates the single manager account and displays a temporary password once. Sign in and change it immediately. Do not copy the command output into a log or evidence file.

Check configuration and migrations:

```sh
make check
.venv/bin/python manage.py showmigrations --settings=config.settings.migration
```

## Start locally

```sh
DJANGO_DEBUG=1 make run
```

`DJANGO_DEBUG=1` enables the static resources needed by Django’s development server. Use it only for this local start. Never use `runserver` or `DEBUG=1` for operations. The command listens on `127.0.0.1:8000`; open `http://127.0.0.1:8000/connexion` on the same computer. This local HTTP setup does not validate HTTPS or remote-device access.

## Optional fictitious demo

After changing the manager’s temporary password, follow [Fictitious demo](DEMO.md). The command requires a dedicated demo database, confirms its exact name, requires external transports to be disabled, and refuses a non-empty database. It does not reset or delete data.

## Tests

Tests require local PostgreSQL, the separate `TEST_DATABASE_USER` role with `CREATEDB`, and a temporary database whose name begins with `test_stock_app_`. Under `config.settings.test`, Django and child processes use that role only for the isolated test environment. It remains distinct from the owner and runtime roles. Tests do not use the demo database.

On PostgreSQL 14 and older clusters where `public` grants `CREATE` to `PUBLIC`, a PostgreSQL administrator must secure the new application database before running migrations. Replace the role names below with the configured owner and runtime roles, then run this SQL while connected to the application database:

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO stock_app_owner;
GRANT USAGE ON SCHEMA public TO stock_app_runtime;
```

The `grant_runtime` command verifies that the runtime role cannot create objects. It fails with an actionable error if the connected database owner cannot enforce that restriction. PostgreSQL 15+ installations should still run this check rather than assume the schema defaults are safe.

```sh
DATABASE_NAME=postgres TEST_DATABASE_NAME=test_stock_app_local \
  .venv/bin/python manage.py test tests --settings=config.settings.test --noinput --verbosity=2
make quality
```

Tests disable external transports, capture email locally, and simulate push delivery.

## Operations

- Check configuration and migrations with `make check` and `showmigrations`.
- Apply migrations with `make migrate`.
- Check invariants with `.venv/bin/python manage.py check_consistency --settings=config.settings.migration`.

Before operational use, configure TLS, secrets, off-host backups, monitoring, upload limits, retention, and network access for the selected environment. This guide is not a production deployment procedure.
