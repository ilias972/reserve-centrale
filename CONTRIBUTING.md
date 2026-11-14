# Contributing

Thank you for taking an interest in Réserve centrale. Focused bug fixes, documentation improvements and translation updates are useful contributions. For large changes, open an issue first so the scope can be discussed.

## Report a bug or suggest a change

- Search existing issues before opening a new one.
- Describe the relevant page or workflow, steps to reproduce, expected behaviour and observed behaviour.
- Include the application version, operating system, Python and PostgreSQL versions when relevant.
- Remove personal, organisational and operational data from logs, screenshots and examples. Never attach a database, backup, certificate, secret or real inventory.
- For a security vulnerability, use the private reporting instructions in [SECURITY.md](SECURITY.md), not a public issue.

## Prepare a pull request

1. Fork the repository and create a focused branch from `main`.
2. Make the smallest change that addresses the issue.
3. Add or update tests for behaviour changes. Documentation-only changes do not need application tests.
4. Run the relevant tests and `make quality`; include the commands and results in the pull request.
5. Submit the pull request with a short description, linked issue if applicable, and notes about any migration or user-visible change.

There is no required commit-message format or contributor licence agreement at this time.

## Application invariants

Stock changes are transactional and audited. Keep the established balances and quantity equations intact: `0 ≤ R ≤ P`, `D = P − R`, `Q = L + C + U`, and `Qf = F + A + W`. Do not bypass permissions, idempotency, locking, or the append-only history to simplify a change. Read [Architecture](docs/ARCHITECTURE.md) before changing a stock, request, purchase or receiving workflow.

## Translations

The interface supports English, French and Modern Standard Arabic (RTL). See [Translation workflow](docs/TRANSLATIONS.md) for catalog generation and review. Keep terminology consistent and do not translate names that users enter into their own installation.

## Third-party material

Preserve existing copyright notices and licence obligations. Before adding a library, icon, font, illustration or other external material, document its source and licence in the appropriate notice. Do not add material whose redistribution terms are unclear.
