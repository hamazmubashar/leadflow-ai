# Extra sites behind LeadFlow's Caddy

Caddy owns ports 80 and 443 on the server, so any other app on the same server is published
through it instead of running its own proxy. Caddy loads every `*.caddy` file in this folder;
with none, it serves LeadFlow only.

To add an app:

1. Point a domain at the server (for example a second DuckDNS subdomain).
2. Start the app with its public container attached to the external Docker network
   `shared-edge` (LeadFlow's compose file creates it), under a unique alias.
3. Copy the matching `*.caddy.example` to `*.caddy`, set its domain variable in
   `deploy/production.env`, and run `docker compose up -d caddy`.

Each site gets its own automatic HTTPS certificate and the same security headers as LeadFlow.
The `shared-edge` network is internal: apps on it have no internet egress (Caddy keeps its
own). Plain Caddy has no per-visitor rate limiting; apps that need it must enforce it themselves.
