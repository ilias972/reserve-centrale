# Réserve centrale

A self-hosted Django and PostgreSQL application for managing one organisation’s internal catalogue, stock, reservations, requests, issues, supplier purchasing, receipts, case follow-up, notifications, reporting, and XLSX exports.

The project is designed for a dedicated installation per organisation. It is not a hosted service and does not provide multi-tenant isolation.

## Features

- Item catalogue, categories, counting units, suppliers, and department-level access rules.
- Physical stock, reservations, availability, and an append-only movement ledger.
- Internal requests, preparation, partial issues, and traceable corrections.
- Purchase recommendations and tracking of orders actually placed with suppliers.
- Receipts, quarantined goods, supplier exceptions, and review cases.
- In-app notifications, personal and administrative histories, reporting, and XLSX exports.
- Local consistency checks and backup/restore tools.

Core quantities are kept distinct:

- `0 ≤ reserved ≤ physical`, with `available = physical − reserved`.
- A request’s quantity is split into issued, cancelled, and still-open amounts.
- A supplier order’s quantity is split into regularly received, cancelled, and still-expected amounts.

A delivery to a department is not measured patient or service consumption.

## Languages

The interface supports English (default), French, and Modern Standard Arabic with right-to-left layout. Users choose a language in the interface. User-entered item, supplier, and department names remain as stored.

## Requirements

- Python 3.13.
- PostgreSQL supported by the project migrations.
- Separate PostgreSQL owner, runtime, and test roles.

See [Installation](docs/INSTALLATION.md) for a clean local setup. See [Fictitious demo](docs/DEMO.md) for an optional generated dataset that contains no organisation data.

## Run locally

After completing the installation guide and applying migrations:

```sh
DJANGO_DEBUG=1 make run
```

Open `http://127.0.0.1:8000/connexion` on the same computer. Django’s development server is for local development only; this command does not provide HTTPS or remote-device access.

## Tests and quality checks

Tests require a local PostgreSQL instance and a dedicated test role that can create its own temporary `test_stock_app_*` databases. They do not use a demo or production database.

```sh
DATABASE_NAME=postgres TEST_DATABASE_NAME=test_stock_app_local \
  .venv/bin/python manage.py test tests --settings=config.settings.test --noinput --verbosity=2
make quality
```

External email and Web Push transports are disabled in tests. Email is captured locally and push transport is simulated.

## Architecture

The application is a server-rendered Django monolith. PostgreSQL is the transactional source of truth; JavaScript enhances selected forms and navigation. See [Architecture](docs/ARCHITECTURE.md).

The supported-language catalogs and RTL review process are documented in [Translation workflow](docs/TRANSLATIONS.md).

## Scope and limitations

One database and installation serve one organisation. Roles are fixed in this version. The software has not been validated for every organisation or regulated workflow. It does not claim regulatory certification for medicines, medical devices, expiry tracking, recalls, or cold-chain management. Production security, TLS, backups, monitoring, retention, and support require environment-specific work. Web Push is disabled by default, and physical iPhone delivery is not generally validated.

See [Limitations](docs/LIMITATIONS.md) before evaluating the software for operational use.

## License

The project code identified as licensable by the named maintainer is offered under the MIT License; see [`LICENSE`](LICENSE). Third-party components keep their own licences and notices in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). The MIT notice does not replace those obligations or independently establish rights in third-party or externally sourced materials.

## Contributing and security

Contribution instructions are in [`CONTRIBUTING.md`](CONTRIBUTING.md). Issues and pull requests are enabled. Use GitHub private vulnerability reporting for sensitive security reports; see [`SECURITY.md`](SECURITY.md).
