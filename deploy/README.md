# Deploying the collision predictor

Manifests for a plain Kubernetes cluster, plus the environment contract shared with
`docker-compose.yml` and the `Dockerfile`. Nothing here is cluster specific beyond the image
reference, the ingress host and the TLS issuer.

## Files

| File | Purpose |
| --- | --- |
| `configmap.yaml` | Non-secret configuration (bind address, CORS allowlist, rate limit) |
| `secret.example.yaml` | Shape of the Secret. Never commit real values, create it out of band |
| `deployment.yaml` | One container with resource requests and limits, probes, non-root |
| `service.yaml` | ClusterIP service on port 80 to container port 8000 |
| `ingress.yaml` | Example TLS ingress for `sda.example.com` |

## Apply

```bash
kubectl create secret generic sda-secrets \
  --from-literal=SDA_SPACETRACK_USER='...' \
  --from-literal=SDA_SPACETRACK_PASS='...' \
  --from-literal=SDA_WEBHOOK_URLS='https://hooks.slack.com/services/...'

kubectl apply -f deploy/configmap.yaml
kubectl apply -f deploy/deployment.yaml
kubectl apply -f deploy/service.yaml
kubectl apply -f deploy/ingress.yaml     # optional
```

Check the install from inside the cluster with `GET /doctor`, or from a shell in the pod with
`python -m sda.doctor` (add `--network` for reachability probes of CelesTrak, OpenSky, USGS and
Space-Track).

## Environment contract

| Variable | Default | Meaning |
| --- | --- | --- |
| `SDA_HOST` | `127.0.0.1` | Bind address. Containers set `0.0.0.0`; the bare process stays on loopback |
| `SDA_PORT` | `8000` | Listen port |
| `SDA_CORS_ORIGINS` | localhost and 127.0.0.1 on `SDA_PORT` | Comma separated browser origins. No wildcard, credentials are off |
| `SDA_RATE_LIMIT` | `600/minute` | Per-IP token bucket. `off` disables it |
| `SDA_RATE_LIMIT_PATHS` | `/api/,/agent/` | Path prefixes the limiter guards |
| `SDA_RATE_LIMIT_TRUST_FORWARDED` | unset (off) | Only enable behind a proxy you control |
| `SDA_SPACETRACK_USER` / `SDA_SPACETRACK_PASS` | unset | Space-Track account. Without them `/tle/refresh/spacetrack` and `/reentries` answer 503 |
| `SDA_WEBHOOK_URLS` | unset | Comma separated alert webhooks, optionally prefixed `slack=`, `discord=` or `generic=` |

Credentials are read from the environment only. They are never written to disk and never
returned by an endpoint; `/alerts/config` and `/doctor` mask them.

## Scaling notes

The TLE catalog, the metrics counters, the alert log and the rate-limit buckets all live in
process memory. Two consequences for replicas above one:

- Each replica keeps its own rate-limit buckets, so the effective limit is
  `SDA_RATE_LIMIT` multiplied by the replica count.
- Alert deduplication and the Pc trend memory are per replica, so the same conjunction can
  alert once per replica. Run a single replica for alerting, or move the dedupe state to a
  shared store first.

A full-catalog screen is CPU bound. Give the pod at least one core, and prefer a vertical bump
over more replicas.

## Response headers

Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy:
strict-origin-when-cross-origin` and `X-Frame-Options: SAMEORIGIN`. There is deliberately no
Content-Security-Policy: the worldview globe loads CesiumJS and the visualization page loads
plotly.js from public CDNs with inline scripts, so a policy strict enough to matter would break
the UI. If those assets are ever vendored into `src/sda/templates/static/`, add a CSP in
`src/sda/ratelimit.py`.
