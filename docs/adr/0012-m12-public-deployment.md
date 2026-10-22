# ADR 0012: M12 public deployment and live demo

- Status: Accepted
- Date: 2026-10-23

## Context

LeadFlow AI is a portfolio project shown to clients on Upwork, Fiverr and LinkedIn. A client should be able to try it in a browser in one click, without an account, and without seeing or changing anyone else's data. Hosting must cost nothing. Up to M11 the app ran only on `127.0.0.1`.

## Decisions

### 1. A private, disposable sandbox per demo visitor
- "Explore the live demo" copies a template database (migrated and filled with the fictional Harborline Home & Living story at startup) into a new SQLite file for that visitor only.
- The visitor acts as an admin **inside that copy only**, so they can import CSVs, change statuses and approve drafts. Demo tokens are never valid for the main database, and main tokens are never valid for a sandbox.
- A sandbox is deleted on sign-out, after 2 hours and on restart. Limits: 30 at once, 5 new per address per hour, 300 leads, 100 rows per import, and no AI drafts.
- The demo data has a realistic mix of tiers (8 hot, 11 warm, 5 cold), uses `example.com` addresses and Ofcom's fictional phone range only, and a test enforces both.
- **Why not one shared demo account:** visitors would see and break each other's changes, and a shared password published on a portfolio page invites abuse.

### 2. Docker Compose with Caddy
- Four services: `api`, `dashboard`, `backup` (nightly SQLite copies with the backup API, 7 kept) and `caddy`, the only one published (80/443), with automatic Let's Encrypt HTTPS and security headers.
- Containers run as a non-root user with a read-only filesystem, no Linux capabilities and `no-new-privileges`. Visitor addresses are forwarded through Caddy so per-address limits apply per person.
- **Why Caddy instead of nginx and certbot:** automatic certificates and renewal with a 20-line config.

### 3. Oracle Cloud Always Free, with a DuckDNS name
- One `VM.Standard.A1.Flex` (ARM) server with 1 OCPU and 2 GB of memory plus a 2 GB swap file, Ubuntu 24.04, and a free DuckDNS subdomain. Total cost: $0.
- `deploy/server-setup.sh` prepares a fresh server once: updates, Docker, opening ports 80/443 in Oracle's server-side firewall, automatic security updates, and SSH with keys only.
- **Sizing:** Oracle may reclaim Always Free servers whose CPU, network *and* memory use stay below 20% for 7 days, which matches a low-traffic demo. With 2 GB, the app's normal memory use (about 30%) keeps it above that line. This is a legitimate sizing choice, not artificial load.
- **Why not Render's free tier:** it sleeps after inactivity (a cold start of about a minute) and has no persistent disk, so SQLite data and demos would vanish.

### 4. Administrator-assisted password help
- "Forgot password?" opens instructions only (contact the administrator, who runs `manage_users set-password`). There is no form, no reset endpoint and no token.
- **Why:** self-service reset needs email delivery, which the project deliberately lacks, and Oracle restricts outbound email (SMTP) by default. A form that asked for a username would only add an enumeration risk.

### 5. A developer credit
- "Built by Hamaz Mubashar · AI Automation & LLM Engineer" at the bottom of the sign-in page, linked only when `LEADFLOW_DEVELOPER_URL` (https only) is set.

## Consequences

- One click gives a client a working, private copy of the product, over HTTPS, for free.
- In-memory state (open demos, lockouts) resets on an API restart; one API process only.
- Backups live on the same disk as the database; copying them off the server is manual for now.
- Next (M13): automated security checks in CI (OWASP ZAP, pip-audit, gitleaks), a demo video and a case study.
