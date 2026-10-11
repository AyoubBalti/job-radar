"""job-radar: finds visa-sponsoring software jobs in Europe and ranks them with a local LLM.

Runs entirely on your own machine: JobSpy for scraping, Ollama for scoring, SQLite for history.
Usage:
    python radar.py              # normal run
    python radar.py --no-llm     # keyword scoring only
    python radar.py --demo       # run the pipeline on built-in sample jobs (no scraping)
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).parent
DB_PATH = ROOT / "seen_jobs.db"
REPORT_DIR = ROOT / "reports"


# ---------- storage ----------

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
               url TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,
               score INTEGER, sponsorship TEXT, first_seen TEXT)"""
    )
    return conn


def is_seen(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone() is not None


def mark_seen(conn: sqlite3.Connection, job: dict) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (job["url"], job["title"], job["company"], job["location"],
         job.get("score"), job.get("sponsorship"), dt.date.today().isoformat()),
    )


# ---------- scraping ----------

def scrape(cfg: dict) -> list[dict]:
    from jobspy import scrape_jobs  # imported here so --demo works without network

    jobs: list[dict] = []
    for s in cfg["searches"]:
        print(f"Searching: {s['term']} in {s['location']}")
        # one site per call: if a site doesn't support a country (Glassdoor covers fewer
        # countries than Indeed) or rate-limits you, the other sites still run
        for site in cfg["sites"]:
            try:
                df = scrape_jobs(
                    site_name=site,
                    search_term=s["term"],
                    location=s["location"],
                    country_indeed=s["country"],
                    hours_old=cfg["hours_old"],
                    results_wanted=cfg["results_per_search"],
                    fetch_description=True,
                    verbose=0,
                )
            except Exception as exc:
                print(f"  {site}: skipped ({str(exc)[:120]})")
                continue
            for row in df.to_dict("records"):
                url = row.get("job_url_direct") or row.get("job_url")
                if not url:
                    continue
                jobs.append({
                    "url": str(url),
                    "title": str(row.get("title") or ""),
                    "company": str(row.get("company") or ""),
                    "location": str(row.get("location") or ""),
                    "site": str(row.get("site") or site),
                    "description": str(row.get("description") or ""),
                })
            print(f"  {site}: {len(df)} results")
    return jobs


# ---------- filtering & scoring ----------

def contains_any(text: str, phrases: list[str]) -> list[str]:
    low = text.lower()
    return [p for p in phrases if p.lower() in low]


def title_ok(title: str, cfg: dict) -> bool:
    t = title.lower()
    include = any(re.search(rf"\b{re.escape(w.lower())}\b", t) for w in cfg["title_include"])
    exclude = any(w.lower() in t for w in cfg["title_exclude"])
    return include and not exclude


def keyword_score(job: dict, cfg: dict) -> dict:
    text = f"{job['title']}\n{job['description']}"
    sponsor = contains_any(text, cfg["sponsorship_keywords"])
    blockers = contains_any(text, cfg["blocker_keywords"])
    skill_hits = contains_any(text, ["aws", "lambda", "python", "typescript", "llm", "langchain",
                                     "terraform", "serverless", "node", "openai", "bedrock", "cdk"])
    score = 40 + 8 * len(skill_hits) + (25 if sponsor else 0) - (40 if blockers else 0)
    return {
        "score": max(0, min(100, score)),
        "sponsorship": "yes" if sponsor and not blockers else ("no" if blockers else "unclear"),
        "german_required": any("deutsch" in b or "german" in b for b in blockers),
        "reason": f"Keywords: skills {skill_hits or 'none'}; sponsorship {sponsor or 'none'}; "
                  f"blockers {blockers or 'none'}",
    }


LLM_PROMPT = """You screen job postings for one candidate. Reply with JSON only, no prose.

CANDIDATE:
{profile}

JOB: {title} at {company} ({location})
DESCRIPTION:
{description}

Return exactly:
{{"score": <0-100 fit for this candidate, considering skills, seniority and language requirements>,
  "sponsorship": "<yes if the posting offers visa sponsorship or relocation, no if it rules it out, unclear otherwise>",
  "german_required": <true if fluent German is required, else false>,
  "reason": "<one sentence>"}}"""


def llm_score(job: dict, cfg: dict) -> dict | None:
    llm = cfg["llm"]
    prompt = LLM_PROMPT.format(
        profile=cfg["profile"].strip(),
        title=job["title"], company=job["company"], location=job["location"],
        description=job["description"][: llm["max_description_chars"]],
    )
    try:
        r = requests.post(
            f"{llm['base_url']}/api/chat",
            json={"model": llm["model"], "stream": False, "format": "json",
                  "messages": [{"role": "user", "content": prompt}],
                  "options": {"temperature": 0}},
            timeout=180,
        )
        r.raise_for_status()
        data = json.loads(r.json()["message"]["content"])
        return {
            "score": int(data.get("score", 0)),
            "sponsorship": str(data.get("sponsorship", "unclear")).lower(),
            "german_required": bool(data.get("german_required", False)),
            "reason": str(data.get("reason", "")),
        }
    except Exception as exc:
        print(f"  LLM failed for '{job['title']}' ({exc}); using keywords")
        return None


def ollama_available(cfg: dict) -> bool:
    try:
        requests.get(f"{cfg['llm']['base_url']}/api/tags", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


# ---------- report ----------

def write_report(jobs: list[dict], cfg: dict) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"{dt.date.today().isoformat()}.html"
    shown = sorted((j for j in jobs if j["score"] >= cfg["min_score_in_report"]),
                   key=lambda j: (j["sponsorship"] == "yes", j["score"]), reverse=True)
    badge = {"yes": "#1b7f3b", "unclear": "#8a6d00", "no": "#a12020"}
    rows = "\n".join(
        f"""<tr><td class="s">{j['score']}</td>
<td><a href="{html.escape(j['url'])}" target="_blank">{html.escape(j['title'])}</a><br>
<span class="m">{html.escape(j['company'])} &middot; {html.escape(j['location'])} &middot; {html.escape(j['site'])}</span></td>
<td><span class="b" style="background:{badge.get(j['sponsorship'], '#555')}">{html.escape(j['sponsorship'])}</span>
{'<br><span class="m">German required</span>' if j['german_required'] else ''}</td>
<td class="m">{html.escape(j['reason'])}</td></tr>"""
        for j in shown
    ) or '<tr><td colspan="4">No new matches today.</td></tr>'
    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<title>Job radar {dt.date.today()}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#222}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.6rem;vertical-align:top;text-align:left}}
.s{{font-weight:700;font-size:1.2rem}}.m{{color:#666;font-size:.9rem}}
.b{{color:#fff;padding:.15rem .5rem;border-radius:4px;font-size:.85rem}}a{{color:#0b57d0}}</style></head><body>
<h1>Job radar: {dt.date.today()}</h1>
<p class="m">{len(shown)} new matches shown (of {len(jobs)} new jobs scored). Sorted by sponsorship, then fit.</p>
<table><tr><th>Score</th><th>Job</th><th>Sponsorship</th><th>Why</th></tr>{rows}</table></body></html>""",
                    encoding="utf-8")
    return path


# ---------- demo data ----------

DEMO_JOBS = [
    {"url": "https://example.com/1", "title": "Backend Engineer (AWS, Python)", "company": "DemoTech",
     "location": "Berlin, DE", "site": "demo",
     "description": "Serverless AWS Lambda, Python, Terraform. We offer visa sponsorship and relocation support. English-speaking team."},
    {"url": "https://example.com/2", "title": "Software Engineer", "company": "LokalSoft",
     "location": "Munich, DE", "site": "demo",
     "description": "Java and Spring. Fließend Deutsch (C1) erforderlich. Must have the right to work in the EU."},
    {"url": "https://example.com/3", "title": "Senior Staff Engineer", "company": "BigCo",
     "location": "Amsterdam, NL", "site": "demo", "description": "Lead architecture across 20 teams."},
    {"url": "https://example.com/4", "title": "AI Engineer - LLM Platform", "company": "GenAI Labs",
     "location": "Amsterdam, NL", "site": "demo",
     "description": "LangChain, OpenAI, TypeScript, Node. Recognised sponsor for highly skilled migrant visas."},
]


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--demo", action="store_true", help="use built-in sample jobs, no scraping")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    conn = open_db() if not args.demo else sqlite3.connect(":memory:")
    if args.demo:
        conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,"
                     " score INTEGER, sponsorship TEXT, first_seen TEXT)")

    raw = DEMO_JOBS if args.demo else scrape(cfg)

    # dedupe within run and against history
    unique: dict[str, dict] = {}
    for j in raw:
        if j["url"] not in unique and not is_seen(conn, j["url"]):
            unique[j["url"]] = j
    candidates = [j for j in unique.values() if title_ok(j["title"], cfg)]
    print(f"{len(raw)} scraped, {len(unique)} new, {len(candidates)} pass title filter")

    use_llm = cfg["llm"]["enabled"] and not args.no_llm and ollama_available(cfg)
    if cfg["llm"]["enabled"] and not args.no_llm and not use_llm:
        print("Ollama not reachable; falling back to keyword scoring. Start it with: ollama serve")

    for i, job in enumerate(candidates, 1):
        print(f"[{i}/{len(candidates)}] scoring {job['title']} @ {job['company']}")
        result = (llm_score(job, cfg) if use_llm else None) or keyword_score(job, cfg)
        # hard rule: if the text contains a blocker phrase, never report a clean "yes"
        if contains_any(job["description"], cfg["blocker_keywords"]) and result["sponsorship"] == "yes":
            result["sponsorship"] = "unclear"
        job.update(result)
        mark_seen(conn, job)

    for j in unique.values():  # remember filtered-out jobs too, so they never reappear
        if "score" not in j:
            mark_seen(conn, {**j, "score": None, "sponsorship": None})
    conn.commit()

    report = write_report(candidates, cfg)
    print(f"Report: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
