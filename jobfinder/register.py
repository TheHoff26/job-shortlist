"""Company list from the Company Scout register.

config/companies.yaml:

    register: https://thehoff26.github.io/company-scout/companies.json
    companies: []          # optional extra companies that are not in the register

The register is read at every run. If it can't be reached, the copy from the
last successful run (data/register-cache.json) is used, so the daily search
never stops because of a network hiccup.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("jobfinder")

LINK_REASONS = {
    "detect_failed": "no supported job system found",
    "pending": "job system not yet checked",
}


URL_BASED = {"workday", "successfactors", "teamtailor", "avature", "easycruit"}
ID_BASED = {"greenhouse", "lever", "ashby", "smartrecruiters", "personio", "recruitee"}


def to_company(c: dict) -> dict:
    """Map a register record to the job tool's company format."""
    jobs = c.get("jobs") or {}
    ats = jobs.get("ats") or "linkedin"
    out = {
        "name": c.get("name", "?"),
        "ats": ats,
        "enabled": c.get("status") != "paused",
        "rating": c.get("rating") or "",
        "segment": c.get("segment") or "",
        "segments_secondary": c.get("segments_secondary") or [],
        "linkedin_id": c.get("linkedin_id"),
        "register_id": c.get("id"),
        "careers_url": c.get("careers_url") or "",
    }
    if jobs.get("url"):
        out["url"] = jobs["url"]
    elif ats in URL_BASED and c.get("careers_url"):
        out["url"] = c["careers_url"]          # job board URL entered as careers page only
    if ats in URL_BASED and not out.get("url"):
        out["ats"], out["link_reason"] = "linkedin", "job system set, but no job board URL in the register"
        return out
    if ats in ID_BASED and not jobs.get("id"):
        out["ats"], out["link_reason"] = "linkedin", "job system set, but no board name (id) in the register"
        return out
    if jobs.get("id"):
        out["id"] = jobs["id"]
    if ats == "linkedin":
        if jobs.get("detect_failed"):
            out["link_reason"] = LINK_REASONS["detect_failed"]
        elif not jobs.get("ats"):
            out["link_reason"] = LINK_REASONS["pending"]
        else:
            out["link_reason"] = (jobs.get("detected") or "").split("– ", 1)[-1] or "set to LinkedIn only"
    return out


def load_register(url: str, session, cache: Path) -> dict | None:
    try:
        r = session.get(url, params={"t": "run"}, timeout=30, headers={"Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data.get("companies"), list):
            raise ValueError("register has no company list")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        log.info("Register: %d companies loaded", len(data["companies"]))
        return data
    except Exception as exc:
        if cache.is_file():
            log.warning("Register not reachable (%s) – using the copy from the last run", exc)
            return json.loads(cache.read_text(encoding="utf-8"))
        log.error("Register not reachable and no cached copy: %s", exc)
        return None


def companies_from(config: dict, session, cache: Path) -> tuple[list[dict], dict]:
    """Return (companies, info) where info has register url, segments and status."""
    info = {"register_url": "", "segments": [], "register_ok": False}
    companies: list[dict] = []
    url = (config or {}).get("register")
    if url:
        info["register_url"] = url
        data = load_register(url, session, cache)
        if data:
            info["register_ok"] = True
            info["segments"] = [{"id": s.get("id"), "name": s.get("name")} for s in data.get("segments", [])]
            companies = [to_company(c) for c in data["companies"]]
    names = {c["name"].lower() for c in companies}
    for extra in (config or {}).get("companies") or []:
        if extra.get("name", "").lower() not in names:
            companies.append(extra)
    return companies, info
