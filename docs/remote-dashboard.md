# Optional remote dashboard access

The default dashboard is loopback-only at `127.0.0.1:8765`. First verify it works locally. You must configure your own network access and authentication; no deployed hostname, tailnet, SSH keys or account state is shipped.

## Private access

An SSH tunnel from a client with authorized access is a straightforward option:

```bash
ssh -L 8765:127.0.0.1:8765 USER@YOUR_SERVER
```

Open the local URL on that client. Your account, firewall and SSH policy belong to your host; this platform does not provision them.

## Optional Tailscale/Caddy route

`dashboard_access.py` supports user-owned Tailscale/Caddy binaries at:

```text
runtime/tools/tailscale/tailscale
runtime/tools/tailscale/tailscaled
runtime/tools/caddy/caddy
```

Install those programs yourself from their official distributions, authenticate Tailscale into a network you control, and check its current policy/feature availability. Do not copy another installation's Tailscale state. The helper uses userspace networking and a local socket under `runtime/tailscale/`.

```bash
runtime/tools/tailscale/tailscale --socket="$PWD/runtime/tailscale/tailscaled.sock" up \
  --accept-dns=false --accept-routes=false --hostname=research-observatory
python dashboard_access.py start
python dashboard_access.py credentials
```

If no Tailscale daemon is running, `start` first launches it and reports the authentication requirement; then authenticate with the displayed socket and rerun `start`. The helper creates a private dashboard password file, configures a loopback Caddy proxy on port 8877, and validates its configuration. It does not publish a public endpoint by itself.

Only after verifying the proxy requires authentication may you configure an allowed HTTPS publication route, for example Funnel to `http://127.0.0.1:8877`. Never route it directly to 8765/8766. Use the generated HTTPS hostname, not a copied hostname from someone else's environment. Availability and eligibility depend on your own network/provider policy; free public hosting is not guaranteed by this project.

The proxy provides owner-level access and host/origin filtering. Protect generated credentials and use HTTPS. `status` inspects access-service status; `restart-proxy` restarts the proxy. Resetting publication does not stop research jobs. Detached services currently have no bundled boot-time supervision; configure your own service manager or start them after a reboot.
