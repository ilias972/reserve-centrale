# Limitations

- One database and installation serve one organisation. Multi-tenant SaaS is not implemented.
- User profiles are fixed. Department assignments and limits need to be configured for each installing organisation.
- The workflows have not been validated for every organisation or sector.
- No regulatory certification for medicines, medical devices, lot or expiry tracking, recalls, or cold-chain workflows is claimed.
- A local installation is not a secured operational deployment. TLS, secrets, backups, restore testing, monitoring, support, and retention need environment-specific work.
- Web Push is disabled by default. Simulated tests do not prove physical delivery to a device.
- Export and load limits are candidate configuration values, not service commitments.
- The MIT notice applies only to project code for which the maintainer is entitled to grant those rights. Third-party code and assets remain under their own licences and notices; see [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
