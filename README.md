# OneTake DB Proxy

Lightweight JSON API proxy for the OneTake analytics dashboard. Sits between the Vercel frontend and Azure PostgreSQL, eliminating the need for direct public database access.

## Architecture

```
Vercel (Next.js)  ──HTTPS──→  API Gateway / K8s  ──internal──→  Azure PG
                               db_proxy.py                       (private)
                               Bearer token auth
```

**No database is exposed to the public internet.** The proxy is the only public-facing surface. All database traffic stays internal to the Azure network.

## Security

| Layer | Control |
|---|---|
| Authentication | Bearer token required on every request (except `/health`). 401 if missing/invalid. |
| Transport | HTTPS (TLS terminated by API Gateway / ingress). |
| Database | Read-only connection pool (asyncpg, max 5 connections). No DDL, no arbitrary SQL. |
| Data scope | Project names, campaign metrics (spend/clicks/conversions), channel mappings. **No PII, no credentials, no contributor personal data.** |
| Queries | All parameterized — no string interpolation, no SQL injection surface. |

## Endpoints

| Method | Path | Description | Auth |
|---|---|---|---|
| `GET` | `/health` | Health check + DB connectivity | No |
| `GET` | `/projects` | List all active projects | Yes |
| `GET` | `/projects/:id` | Single project by UUID | Yes |
| `GET` | `/projects/:id/funnel` | Weekly funnel data + WoW deltas | Yes |
| `GET` | `/projects/:id/channels` | Channel links for a project | Yes |
| `GET` | `/projects/unclassified` | Unclassified UTM sources | Yes |
| `POST` | `/projects/sync` | Trigger intake linking + view refresh | Yes |
| `POST` | `/refresh` | Refresh materialized view | Yes |

## Dependencies

```
Python 3.12
aiohttp >= 3.9.0
asyncpg >= 0.31.0
```

That's it. No other packages. No frameworks. 230 lines of code.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string (Azure PG) |
| `PROXY_SECRET` | Yes | Bearer token for authentication |
| `PROXY_PORT` | No | Listen port (default: 8080) |

## Running Locally

```bash
DATABASE_URL="postgresql://user:pass@host:5432/db?sslmode=require" \
PROXY_SECRET="your-secret-here" \
python3.12 db_proxy.py
```

## Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY db_proxy.py .
EXPOSE 8080
CMD ["python", "db_proxy.py"]
```

## Request Example

```bash
# Health check (no auth)
curl http://localhost:8080/health

# List projects (auth required)
curl -H "Authorization: Bearer your-secret" http://localhost:8080/projects

# Get funnel data
curl -H "Authorization: Bearer your-secret" http://localhost:8080/projects/<uuid>/funnel?view=weekly
```

## Response Format

All endpoints return JSON. Example `/projects/:id/funnel?view=weekly`:

```json
{
  "weeks": [
    {
      "project_id": "uuid",
      "codename": "centaurus",
      "week_start": "2026-05-11",
      "total_spend": 633.72,
      "total_clicks": 2916,
      "total_conversions": 217,
      "blended_cpa": 2.92
    }
  ],
  "wow": {
    "conversions": -60.3,
    "spend": -74.9,
    "cpa_direction": "down"
  },
  "current": { ... },
  "previous": { ... }
}
```

## Code Review Notes

- **Line 63–74**: `row_to_dict()` — serializes asyncpg Records to JSON-safe dicts. Handles UUID, Decimal, datetime, and array types.
- **Line 51–58**: Auth middleware — checks Bearer token on every request except `/health`.
- **Line 91–165**: Route handlers — each is a simple query → JSON response. All SQL uses parameterized `$1, $2` placeholders.
- **No write operations** except `REFRESH MATERIALIZED VIEW` on the `/refresh` endpoint.

---

*OneTake Platform · OneForma / Centific · May 2026*
