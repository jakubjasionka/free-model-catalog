#!/usr/bin/env python3
"""Fetch the live model list from each configured provider and keep the free-tier subset.

Free-tier access is usually time-limited, so run this on a schedule (cron or a systemd timer)
and read the snapshot instead of trusting a hard-coded list.

Stdlib only. Keys are read from environment variables named in providers.json and are
never written to disk or printed.

    python scripts/fetch_free_models.py --out catalog/latest.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class FreeModel:
    provider: str
    model_id: str
    free_reason: str
    context_length: int | None


def _free_reason_from_id(model_id: str) -> str | None:
    lowered = model_id.lower()
    if lowered.endswith(":free"):
        return "id_suffix_:free"
    if "-free" in lowered:
        return "id_contains_-free"
    return None


def _is_zero_priced(item: dict[str, Any]) -> bool:
    pricing = item.get("pricing") or {}
    try:
        return float(pricing.get("prompt", "x")) == 0.0 and float(pricing.get("completion", "x")) == 0.0
    except (TypeError, ValueError):
        return False


def fetch_json(url: str, user_agent: str, api_key: str | None, timeout: float) -> dict[str, Any]:
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"unexpected payload from {url}")
    return data


def select_free(provider_id: str, items: list[dict[str, Any]]) -> list[FreeModel]:
    out: list[FreeModel] = []
    for item in items:
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            continue
        reason = _free_reason_from_id(model_id)
        if reason is None and _is_zero_priced(item):
            reason = "zero_price"
        if reason is None:
            continue
        ctx = item.get("context_length")
        if ctx is None:
            ctx = (item.get("limit") or {}).get("context")
        out.append(
            FreeModel(
                provider=provider_id,
                model_id=model_id,
                free_reason=reason,
                context_length=int(ctx) if isinstance(ctx, int) else None,
            )
        )
    return sorted(out, key=lambda m: m.model_id)


def run(config_path: Path, out_path: Path, timeout: float) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    report: dict[str, Any] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "providers": {},
        "free_models": [],
    }
    exit_code = 0
    for prov in config["providers"]:
        pid = prov["id"]
        key_env = prov.get("auth_env")
        api_key = os.environ.get(key_env) if key_env else None
        try:
            payload = fetch_json(prov["models_url"], prov.get("user_agent", "free-model-catalog"), api_key, timeout)
            items = payload.get("data", [])
            free = select_free(pid, items)
            status: dict[str, Any] = {"ok": True, "total_models": len(items), "free_models": len(free)}
            report["free_models"].extend(asdict(m) for m in free)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError) as exc:
            status = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:200]}
            exit_code = 1
        report["providers"][pid] = status
        print(f"[{pid}] {status}", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out_path} ({len(report['free_models'])} free models)", file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "providers.json")
    parser.add_argument("--out", type=Path, default=ROOT / "catalog" / "latest.json")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    return run(args.config, args.out, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
