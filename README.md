# komodo-homepage-sync

Auto-discover services for [Homepage](https://gethomepage.dev) from container labels across all servers managed by [Komodo](https://komo.do), without exposing any Docker socket.

Homepage can build its dashboard from `homepage.*` container labels, but only by talking to the Docker API of every host, usually through a socket proxy. Even a read-only proxy hands out `docker inspect`, which includes every container's environment variables and therefore its secrets.

Komodo already knows every container on every server. `komodo-homepage-sync` reads that state through Komodo's API and serves it to Homepage as a tiny, read-only, Docker-compatible API that contains only what Homepage needs:

| Endpoint | Returns |
|---|---|
| `GET /containers/json` | Containers that have `homepage.*` labels: id, name, image, state, status and **only** the `homepage.*` labels. Supports `all` and the `health` filter Homepage uses for status dots. |
| `GET /containers/{id}/stats` | CPU, memory and network numbers taken from Komodo, if it reports them. |
| `GET /containers/{id or name}/json` | A minimal inspect for Homepage v1 status dots: `Id`, `Name` and `State` (status, health). Nothing else. |
| `GET /_ping`, `GET /version` | Liveness. |
| anything else | `404` / `405`. No full inspect, logs, exec, env, mounts or commands. |

```
 home network                                              anywhere
┌────────────────────────────────────────────┐          ┌──────────────┐
│ Komodo Core ── periphery agents on servers │          │   Homepage   │
│      ▲                                     │          │ docker.yaml: │
│      │ read-only API key                   │  HTTPS   │  komodo:     │
│ komodo-homepage-sync  ◀────────────────────┼──────────┤   host: ...  │
│  (labels + state only)                     │  token   │              │
└────────────────────────────────────────────┘          └──────────────┘
```

The Komodo API key stays next to Komodo. A Homepage instance that gets compromised learns only what is on the dashboard anyway.

## Labels

Use the normal [Homepage labels](https://gethomepage.dev/configs/docker/#automatic-service-discovery) on any container Komodo manages, on any server:

```yaml
services:
  jellyfin:
    image: jellyfin/jellyfin
    labels:
      homepage.group: Media
      homepage.name: Jellyfin
      homepage.icon: jellyfin.png
      homepage.href: https://jellyfin.example.com
      homepage.siteMonitor: https://jellyfin.example.com
      homepage.widget.type: jellyfin
      homepage.widget.url: http://jellyfin:8096
      homepage.widget.key: "{{HOMEPAGE_VAR_JELLYFIN_KEY}}"
```

Keep secrets out of labels: reference them as `{{HOMEPAGE_VAR_*}}` or `{{HOMEPAGE_FILE_*}}` and define them on the Homepage side. Labels whose key ends in `key`, `password`, `secret`, `token` (and similar) with a literal value are dropped and logged.

Container names are exposed as `<server>.<container>` so names stay unique across servers (`NAME_FORMAT`).

## Setup

1. **Komodo:** create a service user, give it **Read** with the **Inspect** permission on the servers to include, and create an API key for it. Inspect is needed once per container to read its labels; the result is cached for the container's lifetime and only `homepage.*` labels are kept.

2. **Run komodo-homepage-sync** next to Komodo ([examples/compose.yaml](examples/compose.yaml)):

   ```yaml
   services:
     komodo-homepage-sync:
       image: ghcr.io/yesm1ke/komodo-homepage-sync:0.1
       restart: unless-stopped
       environment:
         KOMODO_URL: http://komodo-core:9120
         KOMODO_API_KEY_FILE: /run/secrets/komodo_api_key
         KOMODO_API_SECRET_FILE: /run/secrets/komodo_api_secret
         AUTH_TOKEN_FILE: /run/secrets/auth_token
   ```

   Put it behind a TLS reverse proxy if Homepage connects over an untrusted network.

3. **Homepage** `docker.yaml`:

   ```yaml
   komodo:
     host: komodo-homepage-sync.example.com
     port: 443
     protocol: https
     headers:
       Authorization: Bearer {{HOMEPAGE_VAR_KOMODO_SYNC_TOKEN}}
   ```

   Homepage also supports client certificates (`tls: {caFile, certFile, keyFile}`) if your proxy does mTLS.

## Configuration

| Variable | Default | |
|---|---|---|
| `KOMODO_URL` | — | Komodo Core URL, e.g. `http://komodo-core:9120` |
| `KOMODO_API_KEY`, `KOMODO_API_SECRET` | — | API key of the read-only service user |
| `KOMODO_SERVERS` | all | Comma-separated server names or ids to include |
| `AUTH_TOKEN` | none | If set, requests must send `Authorization: Bearer <token>` |
| `POLL_INTERVAL` | `30` | Seconds between refreshes from Komodo |
| `NAME_FORMAT` | `{server}.{name}` | Container name as seen by Homepage |
| `LISTEN_HOST`, `LISTEN_PORT` | `0.0.0.0`, `2375` | |
| `LOG_LEVEL` | `INFO` | |

Every variable can be read from a file by appending `_FILE`, e.g. `KOMODO_API_SECRET_FILE`.

## Compatibility

Built against Komodo v2.3 (`ListAllContainers`, `InspectContainer`). Works with Homepage v1.x and v2.x. The service has no dependencies beyond the Python standard library.

## License

MIT
