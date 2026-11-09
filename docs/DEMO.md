# Fictitious demo data

The optional `seed_demo` command creates a small, entirely fictitious scenario. It uses existing business services to create departments, items, inventory counts, a purchase, receipt, request, preparation, and issue. Images are generated locally, and supplier names use reserved `.invalid` domains.

## Requirements

1. Create a dedicated local PostgreSQL database whose name starts with `demo_stock_app_`.
2. Apply migrations and create the manager account by following [Local installation](INSTALLATION.md).
3. Sign in once and replace the manager’s temporary password.
4. Confirm external transports are disabled.

## Create the demo dataset

From the project root:

```sh
DJANGO_DEBUG=1 ALLOW_FICTITIOUS_DEMO_SEED=1 .venv/bin/python manage.py seed_demo --confirm-demo-database demo_stock_app_local --settings=config.settings.development
```

Replace the database name in the option with the exact name returned by PostgreSQL. The command prints randomly generated passwords once for three fictitious requester/preparer accounts. Keep them local; do not put them in reports, screenshots, or Git. The manager account is never replaced.

## Re-running and resetting

If the demo is complete, a second run checks its invariants and makes no changes. If the database already contains business data or an incomplete demo, the command refuses to write and deletes nothing.

To start over, stop the application and verify that the database contains only fictitious demo data. Then manually drop and recreate **that dedicated database** with a local owner role. Never run a generic deletion command or point it at a shared, historical, or production database.

The demo includes an inventory, a partially received order, and a partially issued request. Checks cover `0 ≤ R ≤ P`, `D = P − R`, request quantity balances, and supplier-order quantity balances. Email and push transports remain inactive.
