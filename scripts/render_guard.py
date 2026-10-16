"""Render budget guard: keeps the public demo inside Arun's Render credit, so the card on file is never charged.

Render has no spend cap, so a scheduled GitHub workflow runs this every few hours. It suspends the demo's web
service once judging is over, once this month's bandwidth reaches a cap, or if the service is moved to a pricier
plan or scaled out. A suspended service costs nothing and can be resumed in the Render dashboard.

Usage: RENDER_API_KEY=... python scripts/render_guard.py
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

GB_PER = {"b": 1e-9, "kb": 1e-6, "mb": 1e-3, "gb": 1.0, "tb": 1e3}  # decimal, as Render bills


@dataclass
class Config:
    service: str = "fix-my-tutorial"
    plan: str = "starter"
    end_date: str = "2026-12-16"  # judging ends 15 Dec; nothing needs to run after this day (UTC)
    bandwidth_cap_gb: float = 30.0


def decide(now: datetime, cfg: Config, *, bandwidth_gb: float, plan: str, instances: int) -> list[str]:
    """Reasons to suspend, in plain words; empty means keep running."""
    reasons = []
    if now >= datetime.fromisoformat(cfg.end_date).replace(tzinfo=timezone.utc):
        reasons.append(f"It is {cfg.end_date} or later: judging is over.")
    if bandwidth_gb >= cfg.bandwidth_cap_gb:
        reasons.append(f"This month's bandwidth is {bandwidth_gb:.1f} GB, at or over the {cfg.bandwidth_cap_gb:.1f} GB cap.")
    if plan != cfg.plan:
        reasons.append(f'The service is on the "{plan}" plan; only "{cfg.plan}" is allowed.')
    if instances > 1:
        reasons.append(f"The service runs {instances} instances; only 1 is allowed.")
    return reasons


def _gb(series: list[dict]) -> float:
    total = 0.0
    for s in series:
        per = GB_PER.get(str(s.get("unit", "")).lower())
        if per is None:  # an answer the guard can't read must stop the run (and email), never count as zero
            raise ValueError(f"unknown bandwidth unit {s.get('unit')!r}")
        total += sum(float(v["value"]) for v in s.get("values", [])) * per
    return total


def run_guard(api, cfg: Config, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    listed = api.get("/services?" + urllib.parse.urlencode({"name": cfg.service, "limit": 100}))
    svc = next((x["service"] for x in listed if x["service"]["name"] == cfg.service), None)
    if svc is None:
        raise RuntimeError(f"Render service {cfg.service!r} not found")
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    q = urllib.parse.urlencode({"resource": svc["id"], "startTime": month_start,
                                "endTime": now.isoformat().replace("+00:00", "Z")})
    bandwidth = _gb(api.get(f"/metrics/bandwidth?{q}"))
    details = svc.get("serviceDetails") or {}
    reasons = decide(now, cfg, bandwidth_gb=bandwidth, plan=details.get("plan", "unknown"),
                     instances=int(details.get("numInstances") or 1))
    suspended = False
    if reasons and svc.get("suspended") != "suspended":
        api.post(f"/services/{svc['id']}/suspend")
        suspended = True
    return {"reasons": reasons, "bandwidth_gb": bandwidth, "suspended": suspended}


class RenderApi:
    """The Render REST API with a bearer key; anything other than 2xx raises, so a failed check never passes."""

    def __init__(self, key: str):
        self.key = key

    def _call(self, method: str, path: str):
        req = urllib.request.Request(f"https://api.render.com/v1{path}", method=method,
                                     headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
        return json.loads(body) if body else None

    def get(self, path: str):
        return self._call("GET", path)

    def post(self, path: str) -> None:
        self._call("POST", path)


if __name__ == "__main__":
    result = run_guard(RenderApi(os.environ["RENDER_API_KEY"]), Config())
    print(json.dumps(result, indent=1))
