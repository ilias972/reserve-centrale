# Architecture

## Application shape

Réserve centrale is a server-rendered Django monolith backed by PostgreSQL. JavaScript enhances selected forms, status updates, and navigation; business operations remain validated and committed by the server.

The interface uses Django’s native translation catalogs. English (`en`) is the default, French (`fr`) remains available, and Arabic (`ar`) uses right-to-left document direction and logical CSS properties. The selected language is stored in a same-site language cookie. Stored organisation data, such as item and supplier names, is not translated.

## Modules

- `accounts`: user profiles, sessions, and access checks.
- `catalog`: items, categories, departments, suppliers, photos, and department access rules.
- `inventory`: counts, append-only ledger, and stock balances.
- `requests`: internal requests, reservations, preparation, issues, and corrections.
- `purchasing`: recommendations, recorded purchases, and order remainders.
- `receiving`: arrivals, accepted receipts, isolation, and supplier exceptions.
- `cases`: reports and review cases.
- `core` and `audit`: operation intents, idempotency, versions, policies, auditing, and operations tools.
- `notifications`: in-app inbox, transactional outbox, and transport adapters.
- `reporting`: indicators, histories, and XLSX exports.

## Transactional guarantees

- `0 ≤ R ≤ P` and `D = P − R` for reserved, physical, and available stock.
- Request quantities remain separated into issued, cancelled, and open quantities.
- Supplier-order quantities remain separated into regularly received, cancelled, and expected quantities.
- Confirmed facts and audit events are preserved; corrections link to their originating facts.
- Idempotency, locking, permissions, and transactions remain in the server-side workflow.
- Reading a notification does not resolve its linked case.

## Fictitious data

`seed_demo` is an optional development command. It checks the target database name, local owner role, debug mode, explicit confirmation, and disabled external transports. It refuses non-empty databases and uses the existing business services to create traceable demo operations.

## Instance boundary

Each instance serves one organisation. Cross-organisation tenant isolation is not implemented or validated.
