"""Command line entry point:  python -m jobfinder [--config config] [--out docs]"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .extract import enrich
from .linkedin import build_links
from .render import write_outputs
from .score import load_profile, score_job, title_filter
from .sources import fetch_all

log = logging.getLogger("jobfinder")
ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def update_state(jobs, state_path: Path, new_days: int, today: date) -> None:
    """Remember when each job was first seen; mark recent ones as new."""
    state = {}
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    first_run = not state
    cutoff = (today - timedelta(days=new_days)).isoformat()
    current = {}
    for j in jobs:
        seen = state.get(j.key) or today.isoformat()
        current[j.key] = seen
        j.first_seen = seen
        j.is_new = (not first_run) and seen >= cutoff
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(current, indent=1, sort_keys=True), encoding="utf-8")


def run(config_dir: Path, out_dir: Path, state_path: Path, session=None) -> dict:
    companies = _load_yaml(config_dir / "companies.yaml").get("companies", [])
    profile = load_profile(_load_yaml(config_dir / "profile.yaml"), ROOT)

    raw, status = fetch_all(companies, title_filter(profile), session=session)
    log.info("Fetched %d title-matching postings from %d companies", len(raw), len(status))

    kept = []
    for job in raw:
        enrich(job)
        if score_job(job, profile):
            kept.append(job)

    cap = int(profile.get("max_jobs_per_company") or 0)
    if cap:
        by_co = defaultdict(list)
        for j in sorted(kept, key=lambda j: -j.score):
            by_co[j.company].append(j)
        kept = [j for lst in by_co.values() for j in lst[:cap]]

    kept.sort(key=lambda j: (-j.score, j.company, j.title))
    today = datetime.now(timezone.utc).date()
    update_state(kept, state_path, int(profile.get("new_days", 3)), today)

    meta = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "total_matches": len(kept),
        "new_matches": sum(j.is_new for j in kept),
        "companies": status,
        "linkedin": build_links(companies, profile),
        "profile": {k: profile[k] for k in ("titles", "seniority", "locations", "min_score")},
    }
    write_outputs(kept, meta, out_dir)
    log.info("Wrote %d matching jobs (%d new) to %s", len(kept), meta["new_matches"], out_dir)
    return meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find relevant open positions at selected companies.")
    ap.add_argument("--config", default=str(ROOT / "config"), help="folder with companies.yaml and profile.yaml")
    ap.add_argument("--out", default=str(ROOT / "docs"), help="output folder for the website")
    ap.add_argument("--state", default=str(ROOT / "data" / "seen.json"), help="file that tracks first-seen dates")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    meta = run(Path(args.config), Path(args.out), Path(args.state))
    failed = [c["company"] for c in meta["companies"] if not c["ok"]]
    if failed:
        log.warning("Failed companies: %s", ", ".join(failed))
    return 0
