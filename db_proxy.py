#!/usr/bin/env python3.12
"""Lightweight DB proxy — exposes Azure PG data as JSON endpoints.

Runs as a simple aiohttp server inside the Azure Container App.
Vercel (Next.js) calls these endpoints instead of querying Azure PG directly,
bypassing the IP whitelist issue.

Endpoints:
  GET /health                          → { "ok": true }
  GET /projects                        → list all projects
  GET /projects/:id/funnel?view=weekly → weekly funnel data
  GET /projects/:id/channels           → channel links
  GET /projects/unclassified           → unclassified UTMs
  POST /projects/sync                  → trigger WP sync + auto-link
  POST /refresh                        → refresh materialized view

Auth: Bearer token via PROXY_SECRET env var.

Usage:
  PROXY_SECRET=mysecret python3.12 worker/db_proxy.py
  # Listens on port 8080 by default (PROXY_PORT env var)
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import uuid as _uuid
from decimal import Decimal

import asyncpg
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("AZURE_DATABASE_URL", "")
PROXY_SECRET = os.environ.get("PROXY_SECRET", "")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))

pool: asyncpg.Pool | None = None


# ── Middleware: Auth ──────────────────────────────────────────────

@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path == "/health":
        return await handler(request)

    if PROXY_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {PROXY_SECRET}":
            return web.json_response({"error": "Unauthorized"}, status=401)

    return await handler(request)


# ── Helpers ──────────────────────────────────────────────────────

def row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, _uuid.UUID):
            d[k] = str(v)
        elif isinstance(v, Decimal):
            d[k] = float(v)
        elif hasattr(v, "isoformat"):
            d[k] = v.isoformat()
        elif isinstance(v, list):
            d[k] = [str(i) for i in v]
    return d


async def query(sql: str, *args) -> list[dict]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [row_to_dict(r) for r in rows]


async def execute(sql: str, *args) -> str:
    async with pool.acquire() as conn:
        return await conn.execute(sql, *args)


# ── Routes ───────────────────────────────────────────────────────

async def health(request: web.Request):
    return web.json_response({"ok": True, "db": pool is not None})


async def list_projects(request: web.Request):
    status = request.query.get("status", "active")
    rows = await query(
        "SELECT * FROM projects WHERE status = $1 ORDER BY codename", status
    )
    return web.json_response(rows)


async def get_project(request: web.Request):
    pid = request.match_info["id"]
    rows = await query("SELECT * FROM projects WHERE id = $1::UUID", pid)
    if not rows:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response(rows[0])


async def get_funnel(request: web.Request):
    pid = request.match_info["id"]
    view = request.query.get("view", "weekly")

    if view == "daily":
        start = request.query.get("start", "2026-01-01")
        end = request.query.get("end", "2099-12-31")
        rows = await query(
            "SELECT * FROM project_daily_funnel WHERE project_id = $1::UUID "
            "AND date >= $2::DATE AND date <= $3::DATE ORDER BY date DESC",
            pid, start, end,
        )
        return web.json_response(rows)

    # Weekly with WoW
    rows = await query(
        "SELECT * FROM project_weekly_summary WHERE project_id = $1::UUID "
        "ORDER BY week_start DESC LIMIT 12",
        pid,
    )

    current = rows[0] if rows else None
    previous = rows[1] if len(rows) > 1 else None

    wow = None
    if current and previous:
        def delta(key):
            c = current.get(key, 0) or 0
            p = previous.get(key, 0) or 0
            if p == 0:
                return None
            return round((c - p) / p * 100, 1)

        wow = {
            "impressions": delta("total_impressions"),
            "clicks": delta("total_clicks"),
            "spend": delta("total_spend"),
            "conversions": delta("total_conversions"),
            "cpa_direction": (
                "up" if (current.get("blended_cpa") or 0) > (previous.get("blended_cpa") or 0)
                else "down"
            ) if current.get("blended_cpa") and previous.get("blended_cpa") else None,
        }

    return web.json_response({
        "weeks": rows,
        "wow": wow,
        "current": current,
        "previous": previous,
    })


async def get_channels(request: web.Request):
    pid = request.match_info["id"]
    rows = await query(
        "SELECT pcl.*, cd.slug AS channel_slug, cd.display_name AS channel_name, "
        "cd.category AS channel_category "
        "FROM project_channel_links pcl "
        "JOIN channel_definitions cd ON cd.id = pcl.channel_id "
        "WHERE pcl.project_id = $1::UUID "
        "ORDER BY cd.category, pcl.confidence DESC",
        pid,
    )
    return web.json_response(rows)


async def get_unclassified(request: web.Request):
    items = await query("SELECT * FROM unclassified_channels_pending")
    channels = await query("SELECT * FROM channel_definitions ORDER BY category, slug")
    return web.json_response({"items": items, "channels": channels})


async def trigger_sync(request: web.Request):
    """Trigger project sync — links intakes, discovers aliases, links channels."""
    results = []

    # Link intake_requests
    r = await query("SELECT link_intake_to_projects() AS count")
    results.append({"step": "intake_link", "result": f"{r[0]['count']} linked"})

    # Refresh view
    try:
        await execute("REFRESH MATERIALIZED VIEW project_weekly_summary")
        results.append({"step": "refresh_view", "result": "done"})
    except Exception as e:
        results.append({"step": "refresh_view", "result": f"error: {str(e)[:100]}"})

    return web.json_response({"ok": True, "steps": results})


async def get_ga4_funnel(request: web.Request):
    """GA4 acquisition funnel: WP entry → profile → NDA per project."""
    pid = request.match_info["id"]
    rows = await query(
        "SELECT campaign_name, source, medium, wp_entry, apply_click, signup, mfa_setup, "
        "profile_created, nda_signed, certification, browsing_jobs, doing_tasks "
        "FROM ga4_project_funnel WHERE project_id = $1::UUID ORDER BY nda_signed DESC",
        pid,
    )

    def total(key):
        return sum(r.get(key, 0) or 0 for r in rows)

    tw = total("wp_entry")
    return web.json_response({
        "by_source": rows,
        "totals": {
            "wp_entry": tw,
            "apply_click": total("apply_click"),
            "signup": total("signup"),
            "mfa_setup": total("mfa_setup"),
            "profile_created": total("profile_created"),
            "nda_signed": total("nda_signed"),
            "certification": total("certification"),
            "browsing_jobs": total("browsing_jobs"),
            "doing_tasks": total("doing_tasks"),
        },
        "rates": {
            "wp_to_apply": round(total("apply_click") / tw * 100, 1) if tw > 0 else 0,
            "wp_to_signup": round(total("signup") / tw * 100, 1) if tw > 0 else 0,
            "wp_to_nda": round(total("nda_signed") / tw * 100, 1) if tw > 0 else 0,
            "wp_to_tasks": round(total("doing_tasks") / tw * 100, 1) if tw > 0 else 0,
            "nda_to_tasks": round(total("doing_tasks") / total("nda_signed") * 100, 1) if total("nda_signed") > 0 else 0,
            "apply_to_nda": round(total("nda_signed") / total("apply_click") * 100, 1) if total("apply_click") > 0 else 0,
        },
    })


async def get_locales(request: web.Request):
    """Per-language apply links + platform request IDs for a project."""
    pid = request.match_info["id"]
    rows = await query(
        "SELECT language, apply_url, platform_request_id, is_active, first_seen_at, last_seen_at "
        "FROM project_locale_links WHERE project_id = $1::UUID ORDER BY language",
        pid,
    )
    return web.json_response(rows)


async def refresh_view(request: web.Request):
    try:
        await execute("REFRESH MATERIALIZED VIEW project_weekly_summary")
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)[:200]}, status=500)


# ── App Setup ────────────────────────────────────────────────────

async def on_startup(app: web.Application):
    global pool
    if not DATABASE_URL:
        logger.error("DATABASE_URL or AZURE_DATABASE_URL not set")
        sys.exit(1)
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)
    logger.info("DB pool created → %s", DATABASE_URL.split("@")[1].split("/")[0] if "@" in DATABASE_URL else "unknown")


async def on_cleanup(app: web.Application):
    if pool:
        await pool.close()


def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/health", health)
    app.router.add_get("/projects", list_projects)
    app.router.add_get("/projects/unclassified", get_unclassified)
    app.router.add_get("/projects/{id}", get_project)
    app.router.add_get("/projects/{id}/funnel", get_funnel)
    app.router.add_get("/projects/{id}/ga4-funnel", get_ga4_funnel)
    app.router.add_get("/projects/{id}/locales", get_locales)
    app.router.add_get("/projects/{id}/channels", get_channels)
    app.router.add_post("/projects/sync", trigger_sync)
    app.router.add_post("/refresh", refresh_view)

    return app


if __name__ == "__main__":
    app = create_app()
    logger.info("Starting DB proxy on port %d", PROXY_PORT)
    web.run_app(app, host="0.0.0.0", port=PROXY_PORT)
