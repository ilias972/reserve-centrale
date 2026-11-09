# Security policy

This candidate does not yet provide an operational private vulnerability-reporting channel or a published security contact. Do not post sensitive vulnerability details in a public issue.

Before opening the repository, the maintainer should enable GitHub’s private vulnerability reporting or publish a verified private contact. No response time is promised.

Real deployments require separate environment configuration, unique secrets, TLS, permission review, tested backups, and an update process.

## Photo uploads

Photo routes limit files to 5 MiB and multipart bodies to 5,505,024 bytes. Oversized bodies are rejected before CSRF processing, and files that exceed the limit during WSGI/ASGI processing are also stopped. A deployment must additionally limit request-body size and concurrent uploads at its proxy or ingress server; those limits are not configured in this candidate. Do not publish Django’s development server.
