# Local installation

This guide creates a development instance for one organisation. It uses a local PostgreSQL database and Django’s development server. Do not expose that server to a network or use it for production.

## Prerequisites

- Python 3.13.
- PostgreSQL compatible with the project migrations.
- GNU gettext tools, including `msgfmt`. On macOS, install them with `brew install gettext` and add Homebrew’s gettext `bin` directory to `PATH` if needed.
- A local PostgreSQL administrator connection for creating roles and the database.

## Get the source and install dependencies

```sh
git clone https://github.com/ilias972/reserve-centrale.git
cd reserve-centrale
python3.13 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python manage.py compilemessages
cp .env.example .env
```

Edit `.env` before continuing. Set a newly generated `DJANGO_SECRET_KEY`, a unique `OPERATION_GENERATION`, and the local database credentials described below. Do not reuse credentials from another installation and never commit `.env`. For example, generate values locally with `python3.13 -c 'import secrets; print(secrets.token_urlsafe(64))'` and `python3.13 -c 'import uuid; print(uuid.uuid4())'`. Keep the generated secret out of terminal logs and reports.

Keep these defaults for a local demo unless you have a specific reason to change them:

```dotenv
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
NOTIFICATION_EXTERNAL_ALLOWED=0
NOTIFICATION_PUSH_ENABLED=0
NOTIFICATION_EMAIL_MODE=CAPTURE
DATABASE_HOST=127.0.0.1
```

## Create local PostgreSQL roles and database

Use a local PostgreSQL administration connection. Replace each password placeholder with a different, locally generated secret. These SQL statements create roles for this local installation only.

```sql
CREATE ROLE stock_app_owner LOGIN PASSWORD 'replace-with-a-local-owner-secret';
CREATE ROLE stock_app_runtime LOGIN PASSWORD 'replace-with-a-local-runtime-secret';
CREATE ROLE stock_app_test LOGIN CREATEDB PASSWORD 'replace-with-a-local-test-secret';
GRANT CONNECT ON DATABASE postgres TO stock_app_test;
CREATE DATABASE demo_stock_app_local OWNER stock_app_owner;
GRANT CONNECT ON DATABASE demo_stock_app_local TO stock_app_runtime;
```

In `.env`, set `DATABASE_NAME=demo_stock_app_local`, `DATABASE_USER=stock_app_owner`, `DATABASE_PASSWORD` to the owner password, `RUNTIME_DATABASE_USER=stock_app_runtime`, `RUNTIME_DATABASE_PASSWORD` to its password, and the matching `TEST_DATABASE_USER`, `TEST_DATABASE_PASSWORD` and `TEST_DATABASE_NAME=test_stock_app_local`. Keep `DATABASE_HOST=127.0.0.1` and set the PostgreSQL port used by your local server.

### Secure the application schema before migrations

Connect as a PostgreSQL administrator to the new `demo_stock_app_local` database and run the following statements **before** `make migrate`. This removes schema-creation rights granted to `PUBLIC` by some PostgreSQL defaults and gives the application roles only the schema permissions they need.

```sql
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO stock_app_owner;
GRANT USAGE ON SCHEMA public TO stock_app_runtime;
```

The `grant_runtime` command checks that the runtime role cannot create objects. Do not skip that check.

## Apply migrations and create the first manager

Confirm that `.env` points to the dedicated local database, then run:

```sh
make migrate
.venv/bin/python manage.py grant_runtime --settings=config.settings.migration
.venv/bin/python manage.py bootstrap_admin --username manager --display-name "Local manager" --confirm-local-test --settings=config.settings.migration
```

`bootstrap_admin` is restricted to the local host and database owner. It creates one manager account and displays a temporary password once. Sign in at `/connexion`, replace that password immediately, and do not copy the command output into a log or evidence file. There is no default manager password.

Check the local configuration and migration state:

```sh
make check
.venv/bin/python manage.py showmigrations --settings=config.settings.migration
```

## Start locally

```sh
DJANGO_DEBUG=1 make run
```

Open `http://127.0.0.1:8000/connexion` on the same computer. `DJANGO_DEBUG=1` serves the static resources needed for this local development server; it does not enable a safe production configuration. This local HTTP setup does not provide HTTPS or validate access from another device.

## Optional fictitious demo

After changing the manager’s temporary password, follow [Fictitious demo data](DEMO.md). The seed command requires a dedicated database whose name starts with `demo_stock_app_`, checks its exact name and that it is empty, and refuses if external transports are enabled. It never resets or deletes data.

## Tests and quality checks

Tests use a separate PostgreSQL test role with `CREATEDB`, and create temporary databases whose names begin with `test_stock_app_`. They do not use the demo database. Keep the test role separate from the owner and runtime roles.

```sh
DATABASE_NAME=postgres TEST_DATABASE_NAME=test_stock_app_local \
  .venv/bin/python manage.py test tests --settings=config.settings.test --noinput --verbosity=2
make quality
```

Tests disable external transports, capture email locally and simulate push delivery.

## Troubleshooting

- **`msgfmt: command not found`:** install GNU gettext and ensure its `bin` directory is on `PATH`, then rerun `compilemessages`.
- **Database connection or password error:** check the `DATABASE_*`, `RUNTIME_DATABASE_*` and `TEST_DATABASE_*` values in the local `.env`, and verify the roles can connect to the named local database.
- **`The runtime role can still create in public`:** connect as the PostgreSQL administrator to the application database and run the three schema statements above; then rerun `grant_runtime`.
- **The demo command refuses its target:** confirm that the selected database is a dedicated local database named with the `demo_stock_app_` prefix, contains no business data, and has external transports disabled. Use a new empty demo database rather than resetting a shared database.
- **The demo command reports an incomplete or non-empty seed:** it makes no destructive changes. Use a newly created, dedicated demo database and follow [Fictitious demo data](DEMO.md).

## Operations

- Check configuration and migrations with `make check` and `showmigrations`.
- Apply migrations with `make migrate`.
- Check invariants with `.venv/bin/python manage.py check_consistency --settings=config.settings.migration`.

A real deployment requires separate decisions and work for TLS, secrets, off-host backups, monitoring, upload limits, retention, and network access. This document is a local development guide, not a production deployment procedure.
