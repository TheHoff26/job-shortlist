"""Writes the website (docs/index.html) plus jobs.json and jobs.csv."""

from __future__ import annotations

import csv
import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("template.html")


def write_outputs(jobs, meta: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "jobs": [j.to_public_dict() for j in jobs]}
    data = json.dumps(payload, ensure_ascii=False)

    (out_dir / "jobs.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    page = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__JOB_DATA__*/null", data.replace("</", "<\\/"))
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")

    cols = ["score", "is_new", "company", "title", "seniority", "location", "workplace",
            "years_experience", "languages", "education", "skills_matched", "summary",
            "criteria", "url", "posted", "first_seen"]
    with (out_dir / "jobs.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for j in payload["jobs"]:
            w.writerow([" | ".join(j[c]) if isinstance(j[c], list) else j[c] for c in cols])
