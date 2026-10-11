#!/usr/bin/env python3
"""
job-radar 2.0

Scans job boards (via JobSpy) plus relocation / remote job sources, drops roles you
can't realistically get (wrong title, language or visa blockers), scores the rest with
keywords + a local Ollama model, and writes an HTML and CSV report.

    pip install -r requirements.txt
    python radar.py                    # full run
    python radar.py --quick            # fewer searches
    python radar.py --no-llm           # keyword scoring only
    python radar.py --only boards,arbeitnow,relocateme
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0"}
NOW = datetime.now(timezone.utc)

# Adzuna country codes (only countries Adzuna covers)
ADZUNA_CC = {"germany": "de", "netherlands": "nl", "france": "fr", "austria": "at", "belgium": "be",
             "italy": "it", "spain": "es", "poland": "pl", "switzerland": "ch", "uk": "gb"}

GERMAN_WORDS = {"und", "der", "die", "das", "mit", "für", "wir", "sie", "ein", "eine", "bei", "von",
                "zu", "ist", "sind", "deine", "ihre", "erfahrung", "kenntnisse", "aufgaben", "unser",
                "unsere", "dich", "uns", "oder", "auch", "als", "werden", "bieten", "du"}
GERMAN_WORDS.discard("du")  # also French
DUTCH_WORDS = {"het", "een", "wij", "voor", "met", "ervaring", "kennis", "bij", "ons", "jouw", "jij",
               "bieden", "werken", "functie", "ben", "zoek", "je"}


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Data model + small helpers
# ---------------------------------------------------------------------------
@dataclass
class Job:
    source: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    posted: datetime | None = None
    remote: bool = False
    country: str = ""
    relocation_board: bool = False   # the board only lists relocation-friendly jobs
    # analysis results
    kw_score: int = 0
    score: int = 0
    visa: str = "unknown"            # explicit | likely | unknown | unlikely | blocked | mixed
    language_ok: bool = True
    german_posting: bool = False
    reason: str = ""
    flags: list = field(default_factory=list)
    sponsor_hits: list = field(default_factory=list)
    tech_hits: list = field(default_factory=list)
    is_new: bool = True
    llm_used: bool = False

    @property
    def key(self) -> str:
        loc = norm(self.location.split(",")[0])
        raw = f"{norm(self.company)}|{norm(self.title)}|{loc}"
        return hashlib.md5(raw.encode()).hexdigest()


def s(v) -> str:
    """Safe string: None/NaN -> ''."""
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v).strip()


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s(text).lower()).strip()


def norm_text(text: str) -> str:
    t = s(text).lower().replace("ß", "ss").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", t)


def strip_html(text: str) -> str:
    return BeautifulSoup(s(text), "html.parser").get_text(" ", strip=True)


def word_rx(terms) -> re.Pattern:
    terms = [t.lower() for t in (terms or []) if t]
    if not terms:
        return re.compile(r"(?!x)x")
    body = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.I)


def parse_dt(v) -> datetime | None:
    if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
        return None
    try:
        if isinstance(v, (int, float)):
            if v > 1e12:
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        if isinstance(v, datetime):
            dt = v
        elif hasattr(v, "year") and not isinstance(v, str):  # datetime.date
            dt = datetime(v.year, v.month, v.day)
        else:
            txt = str(v).strip()
            if not txt:
                return None
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def http_json(url: str, **kw):
    r = requests.get(url, headers=UA, timeout=kw.pop("timeout", 30), **kw)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Source 1: JobSpy boards (LinkedIn, Indeed, Glassdoor, Google)
# ---------------------------------------------------------------------------
def build_searches(cfg: dict, quick: bool) -> list[tuple[str, str, str]]:
    terms = cfg["terms"][:2] if quick else cfg["terms"]
    out: list[tuple[str, str, str]] = []
    for c in cfg["countries"]:
        for t in terms:
            out.append((t, c["location"], c["country"]))
        if not quick:
            for tpl in cfg.get("sponsor_query_templates") or []:
                for t in terms[: cfg.get("sponsor_query_terms", 1)]:
                    out.append((tpl.format(term=t), c["location"], c["country"]))
    for x in cfg.get("extra_searches") or []:
        out.append((x["term"], x["location"], x["country"]))
    return out


def scrape_boards(cfg: dict, searches: list[tuple[str, str, str]]) -> list[Job]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log("python-jobspy is not installed; skipping job boards.")
        return []

    sites = cfg.get("sites", ["linkedin", "indeed", "glassdoor"])
    jobs: list[Job] = []
    for i, (term, location, country) in enumerate(searches, 1):
        log(f"[{i}/{len(searches)}] {term!r} in {location}")
        base = dict(
            search_term=term,
            google_search_term=f"{term} jobs in {location} since yesterday",
            location=location,
            results_wanted=cfg.get("results_per_search", 40),
            hours_old=cfg.get("hours_old", 72),
            country_indeed=country,
            linkedin_fetch_description=cfg.get("linkedin_fetch_description", True),
            description_format="markdown",
            verbose=0,
        )
        frames = []
        try:
            frames.append(scrape_jobs(site_name=sites, **base))
        except Exception as e:
            log(f"   combined run failed ({type(e).__name__}); retrying site by site")
            for site in sites:
                try:
                    frames.append(scrape_jobs(site_name=[site], **base))
                except Exception as e2:
                    log(f"   {site} skipped: {str(e2)[:90]}")
        for df in frames:
            if df is None or df.empty:
                continue
            for r in df.to_dict("records"):
                title, url = s(r.get("title")), s(r.get("job_url"))
                if not title or not url:
                    continue
                jobs.append(Job(
                    source=s(r.get("site")) or "board",
                    title=title,
                    company=s(r.get("company")),
                    location=s(r.get("location")),
                    url=url,
                    description=s(r.get("description")),
                    posted=parse_dt(r.get("date_posted")),
                    remote=bool(r.get("is_remote")) if not pd.isna(r.get("is_remote", None)) else False,
                    country=country,
                ))
        time.sleep(cfg.get("delay_between_searches", 4))
    return jobs


# ---------------------------------------------------------------------------
# Source 2+: relocation boards and free job APIs
# Each returns list[Job]; failures never stop the run.
# ---------------------------------------------------------------------------
def region_ok(location: str, cfg: dict) -> bool:
    """Remote jobs: keep only those open to Europe / worldwide / your target countries."""
    t = s(location).lower()
    if not t:
        return True
    allowed = [a.lower() for a in cfg.get("remote_allowed_regions", [])]
    allowed += [c["country"].lower() for c in cfg.get("countries", [])]
    return any(re.search(rf"(?<!\w){re.escape(a)}(?!\w)", t) for a in allowed)


def src_arbeitnow(cfg: dict, opts: dict) -> list[Job]:
    jobs = []
    for page in range(1, opts.get("pages", 3) + 1):
        data = http_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        for d in data.get("data", []):
            tags = ", ".join(d.get("tags") or []) + " " + ", ".join(d.get("job_types") or [])
            jobs.append(Job(
                source="arbeitnow", title=s(d.get("title")), company=s(d.get("company_name")),
                location=s(d.get("location")), url=s(d.get("url")),
                description=strip_html(d.get("description")) + f"\nTags: {tags}",
                posted=parse_dt(d.get("created_at")), remote=bool(d.get("remote")), country="germany"))
        time.sleep(1)
    return jobs


def src_remotive(cfg: dict, opts: dict) -> list[Job]:
    data = http_json("https://remotive.com/api/remote-jobs", params={"category": "software-dev", "limit": 200})
    jobs = []
    for d in data.get("jobs", []):
        loc = s(d.get("candidate_required_location"))
        if not region_ok(loc, cfg):
            continue
        jobs.append(Job(source="remotive", title=s(d.get("title")), company=s(d.get("company_name")),
                        location=loc or "Remote", url=s(d.get("url")),
                        description=strip_html(d.get("description")),
                        posted=parse_dt(d.get("publication_date")), remote=True))
    return jobs


def src_remoteok(cfg: dict, opts: dict) -> list[Job]:
    data = http_json("https://remoteok.com/api")
    jobs = []
    for d in data:
        if not isinstance(d, dict) or not d.get("position"):
            continue
        loc = s(d.get("location"))
        if not region_ok(loc, cfg):
            continue
        jobs.append(Job(source="remoteok", title=s(d.get("position")), company=s(d.get("company")),
                        location=loc or "Remote", url=s(d.get("url")) or s(d.get("apply_url")),
                        description=strip_html(d.get("description")) + "\nTags: " + ", ".join(d.get("tags") or []),
                        posted=parse_dt(d.get("epoch") or d.get("date")), remote=True))
    return jobs


def src_himalayas(cfg: dict, opts: dict) -> list[Job]:
    jobs = []
    for page in range(opts.get("pages", 5)):
        data = http_json("https://himalayas.app/jobs/api", params={"limit": 20, "offset": page * 20})
        items = data.get("jobs", [])
        if not items:
            break
        for d in items:
            restr = d.get("locationRestrictions") or []
            loc = ", ".join(restr) if restr else "Worldwide"
            if not region_ok(loc, cfg):
                continue
            jobs.append(Job(source="himalayas", title=s(d.get("title")), company=s(d.get("companyName")),
                            location=loc, url=s(d.get("applicationLink")) or s(d.get("guid")),
                            description=strip_html(d.get("description") or d.get("excerpt")),
                            posted=parse_dt(d.get("pubDate")), remote=True))
        time.sleep(1)
    return jobs


def src_adzuna(cfg: dict, opts: dict) -> list[Job]:
    app_id = opts.get("app_id") or os.environ.get("ADZUNA_APP_ID")
    app_key = opts.get("app_key") or os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log("   adzuna: no app_id/app_key, skipped")
        return []
    days = max(1, math.ceil(cfg.get("hours_old", 72) / 24))
    jobs = []
    for c in cfg["countries"]:
        cc = ADZUNA_CC.get(c["country"])
        if not cc:
            continue
        for term in cfg["terms"][: opts.get("terms", 2)]:
            data = http_json(f"https://api.adzuna.com/v1/api/jobs/{cc}/search/1", params={
                "app_id": app_id, "app_key": app_key, "results_per_page": 50, "what": term,
                "max_days_old": days, "content-type": "application/json"})
            for d in data.get("results", []):
                jobs.append(Job(source="adzuna", title=strip_html(d.get("title")),
                                company=s((d.get("company") or {}).get("display_name")),
                                location=s((d.get("location") or {}).get("display_name")),
                                url=s(d.get("redirect_url")), description=strip_html(d.get("description")),
                                posted=parse_dt(d.get("created")), country=c["country"]))
            time.sleep(1)
    return jobs


def src_relocateme(cfg: dict, opts: dict) -> list[Job]:
    """relocate.me has no public API. Best-effort: JSON-LD first, link scan as fallback.
    If it prints 0 results, the site layout changed and this function needs adjusting."""
    jobs: list[Job] = []
    for q in opts.get("queries", ["software engineer"]):
        try:
            r = requests.get("https://relocate.me/search", params={"query": q}, headers=UA, timeout=30)
            r.raise_for_status()
        except Exception as e:
            log(f"   relocate.me query {q!r} failed: {str(e)[:80]}")
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        found = 0
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except Exception:
                continue
            items = data if isinstance(data, list) else (data.get("@graph") or [data]) if isinstance(data, dict) else []
            for it in items:
                if not isinstance(it, dict) or it.get("@type") != "JobPosting":
                    continue
                addr = ((it.get("jobLocation") or {}) if isinstance(it.get("jobLocation"), dict) else {}).get("address", {}) or {}
                loc = ", ".join(x for x in [s(addr.get("addressLocality")), s(addr.get("addressCountry"))] if x)
                jobs.append(Job(source="relocate.me", title=s(it.get("title")),
                                company=s((it.get("hiringOrganization") or {}).get("name")),
                                location=loc, url=s(it.get("url")), description=strip_html(it.get("description")),
                                posted=parse_dt(it.get("datePosted")), relocation_board=True))
                found += 1
        if not found:  # fallback: any link that looks like a job page
            seen = set()
            for a in soup.select("a[href*='/job']"):
                href = a.get("href", "")
                url = href if href.startswith("http") else "https://relocate.me" + href
                title = a.get_text(" ", strip=True)
                if len(title) < 8 or url in seen:
                    continue
                seen.add(url)
                parent = a.find_parent(["article", "li", "div"])
                jobs.append(Job(source="relocate.me", title=title, company="", location="", url=url,
                                description=parent.get_text(" ", strip=True) if parent else title,
                                relocation_board=True))
                found += 1
        time.sleep(2)
    if not jobs:
        log("   relocate.me returned 0 jobs (layout may have changed)")
    return jobs


SOURCES = {
    "arbeitnow": src_arbeitnow,
    "relocateme": src_relocateme,
    "remotive": src_remotive,
    "remoteok": src_remoteok,
    "himalayas": src_himalayas,
    "adzuna": src_adzuna,
}


# ---------------------------------------------------------------------------
# Filtering + keyword analysis
# ---------------------------------------------------------------------------
class Matcher:
    def __init__(self, cfg: dict):
        self.inc = word_rx(cfg.get("title_include"))
        self.exc = word_rx(cfg.get("title_exclude"))
        self.spons = [norm_text(x) for x in cfg.get("sponsorship_keywords", [])]
        self.block = [norm_text(x) for x in cfg.get("blocker_keywords", [])]
        self.lang = [norm_text(x) for x in cfg.get("language_blockers", [])]
        self.skills = {k: word_rx([k]) for k in cfg.get("skills", [])}
        self.sponsor_companies: set[str] = set()
        path = cfg.get("sponsor_companies_file")
        if path and Path(path).exists():
            self.sponsor_companies = {norm(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()}

    def title_ok(self, title: str) -> bool:
        return bool(self.inc.search(title)) and not self.exc.search(title)


def required_years(text: str) -> int:
    nums = [int(m) for m in re.findall(r"(\d{1,2})\s*(?:\+|plus)?(?:\s*[-–]\s*\d{1,2})?\s*(?:years|yrs|jahre|ans)\b", text)]
    nums = [n for n in nums if 1 <= n <= 15]
    return max(nums) if nums else 0


def analyze(job: Job, m: Matcher) -> None:
    text = norm_text(f"{job.title}\n{job.description}")
    title_l = job.title.lower()

    blockers = [b for b in m.block if b in text]
    masked = text
    for b in blockers:
        masked = masked.replace(b, " ")
    spons = [k for k in m.spons if k in masked]
    lang_hits = [b for b in m.lang if b in text]
    tech = [k for k, rx in m.skills.items() if rx.search(text)]
    known = norm(job.company) in m.sponsor_companies if job.company else False

    tokens = re.findall(r"[a-zäöü]+", text)
    if len(tokens) >= 40:
        de = sum(t in GERMAN_WORDS for t in tokens) / len(tokens)
        nl = sum(t in DUTCH_WORDS for t in tokens) / len(tokens)
        job.german_posting = de > 0.07 or nl > 0.07

    years = required_years(text)

    if blockers and spons:
        job.visa = "mixed"
    elif blockers:
        job.visa = "blocked"
    elif spons or job.relocation_board:
        job.visa = "explicit"
    elif known:
        job.visa = "likely"
    else:
        job.visa = "unknown"

    job.language_ok = not lang_hits
    job.sponsor_hits = spons[:5]
    job.tech_hits = tech

    flags = []
    if blockers:
        flags.append("blocker: " + blockers[0])
    if lang_hits:
        flags.append("language: " + lang_hits[0])
    if job.german_posting:
        flags.append("posting not in English")
    if years >= 5:
        flags.append(f"asks {years}+ years")
    if "senior" in title_l:
        flags.append("senior title")
    if known:
        flags.append("known sponsor")
    job.flags = flags

    score = 40 + min(len(tech) * 3, 24)
    score += {"explicit": 20, "likely": 8, "mixed": -15, "blocked": -40, "unknown": 0}[job.visa]
    if len(spons) > 1:
        score += min(5 * (len(spons) - 1), 10)
    if not job.language_ok:
        score -= 25
    elif job.german_posting:
        score -= 10
    if years >= 6:
        score -= 20
    elif years == 5:
        score -= 8
    if "senior" in title_l:
        score -= 5
    if re.search(r"\b(junior|graduate|entry)\b", title_l):
        score += 3
    job.kw_score = max(0, min(100, score))


# ---------------------------------------------------------------------------
# Local LLM scoring (Ollama)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You screen job postings for ONE specific candidate who needs an employer willing to hire "
    "and relocate a non-EU engineer. Judge only from the posting text; do not invent facts. "
    "Reply with a single JSON object and nothing else."
)

JSON_SKELETON = """{
  "role_match": 0,
  "stack_match": 0,
  "seniority_fit": 0,
  "visa_signal": "unknown",
  "language_ok": true,
  "reason": "",
  "red_flags": []
}"""

RUBRIC = """Fill the JSON with these rules:
- role_match (0-10): how well the daily work matches backend engineering, AI/LLM integration, or cloud automation. 10 = near-identical, 5 = adjacent, 0 = unrelated.
- stack_match (0-10): overlap between the posting's required tools and the candidate's tools (Python, TypeScript, AWS serverless, IaC, LLM frameworks, Snowflake).
- seniority_fit (0-10): 10 = suits about 3 years of experience; 6 = a stretch (4-5 years asked); 2 = clearly senior, lead or manager; 2 = pure internship.
- visa_signal: "explicit" if the posting offers visa sponsorship or relocation; "likely" if it is an international company or English-speaking team with no residency restriction; "unknown" if nothing is said; "unlikely" if it needs existing local/EU work rights, citizenship, or clearance.
- language_ok: false only if fluent German, Dutch, Italian, Spanish or another language other than English/French is REQUIRED. English or French as working language is fine. Basic German is fine.
- reason: at most 25 words on why this is or is not a good target.
- red_flags: up to 3 short strings."""


def build_prompt(job: Job, cfg: dict) -> str:
    desc = job.description[: cfg["llm"].get("max_description_chars", 6000)]
    return (
        f"CANDIDATE PROFILE\n{cfg['profile'].strip()}\n\n"
        f"JOB POSTING\nTitle: {job.title}\nCompany: {job.company}\nLocation: {job.location}\n"
        f"Source: {job.source}\nDescription:\n{desc}\n\n"
        f"{RUBRIC}\n\nReturn exactly this JSON shape:\n{JSON_SKELETON}"
    )


def llm_available(llm: dict) -> bool:
    try:
        r = requests.get(llm["base_url"].rstrip("/") + "/api/tags", timeout=5)
        r.raise_for_status()
        names = [m.get("name", "") for m in r.json().get("models", [])]
    except Exception as e:
        log(f"Ollama not reachable ({type(e).__name__}); using keyword scoring only.")
        return False
    want = llm["model"]
    if not any(n == want or n == f"{want}:latest" for n in names):
        log(f"Model {want} not found in Ollama. Run: ollama pull {want}  (keyword scoring only for now)")
        return False
    return True


def llm_review(job: Job, cfg: dict) -> dict | None:
    llm = cfg["llm"]
    payload = {
        "model": llm["model"], "stream": False, "format": "json",
        "options": {"temperature": 0, "num_ctx": llm.get("num_ctx", 8192)},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": build_prompt(job, cfg)}],
    }
    for attempt in range(2):
        try:
            r = requests.post(llm["base_url"].rstrip("/") + "/api/chat", json=payload,
                              timeout=llm.get("timeout", 240))
            r.raise_for_status()
            data = json.loads(r.json()["message"]["content"])
            if isinstance(data, dict):
                return data
        except Exception:
            time.sleep(1)
    return None


def clamp10(v) -> int:
    try:
        return max(0, min(10, int(round(float(v)))))
    except (TypeError, ValueError):
        return 5


VISA_ADJ = {"explicit": 10, "likely": 3, "unknown": 0, "unlikely": -20, "blocked": -30, "mixed": 0}


def finalize(job: Job, review: dict | None) -> None:
    if review:
        job.llm_used = True
        base = clamp10(review.get("role_match")) * 4 + clamp10(review.get("stack_match")) * 3 \
            + clamp10(review.get("seniority_fit")) * 3
        sig = str(review.get("visa_signal", "unknown")).lower()
        if sig not in VISA_ADJ:
            sig = "unknown"
        if job.visa == "unknown":           # keywords were silent: trust the model's read
            job.visa = sig
        lang_ok = job.language_ok and bool(review.get("language_ok", True))
        job.language_ok = lang_ok
        score = base + VISA_ADJ[job.visa]
        if not lang_ok:
            score -= 25
        elif job.german_posting:
            score -= 10
        job.reason = s(review.get("reason"))[:240]
        for f in (review.get("red_flags") or [])[:3]:
            if s(f) and s(f) not in job.flags:
                job.flags.append(s(f))
    else:
        score = job.kw_score
        job.reason = job.reason or ("Matches: " + ", ".join(job.tech_hits[:6]) if job.tech_hits else "keyword score only")
    if job.visa == "blocked":
        score = min(score, 25)
    elif job.visa == "mixed":
        score = min(score, 45)
    job.score = max(0, min(100, int(score)))


# ---------------------------------------------------------------------------
# Dedupe, state, reports
# ---------------------------------------------------------------------------
def dedupe(jobs: list[Job]) -> list[Job]:
    best: dict[str, Job] = {}
    for j in jobs:
        k = j.key
        if k not in best:
            best[k] = j
            continue
        cur = best[k]
        if j.source not in cur.source.split("+"):
            cur.source += "+" + j.source
        if len(j.description) > len(cur.description):
            cur.description = j.description
        cur.relocation_board = cur.relocation_board or j.relocation_board
        cur.remote = cur.remote or j.remote
    return list(best.values())


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=0), encoding="utf-8")


def esc(x) -> str:
    return html.escape(s(x), quote=True)


def write_reports(jobs: list[Job], shown: list[Job], cfg: dict, stats: dict) -> tuple[Path, Path]:
    out = Path(cfg.get("output_dir", "reports"))
    out.mkdir(parents=True, exist_ok=True)
    stamp = NOW.strftime("%Y-%m-%d_%H%M")

    csv_path = out / f"all-jobs-{stamp}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["score", "new", "visa", "language_ok", "title", "company", "location", "source",
                    "posted", "url", "reason", "flags", "tech_hits"])
        for j in sorted(jobs, key=lambda x: -x.score):
            w.writerow([j.score, j.is_new, j.visa, j.language_ok, j.title, j.company, j.location, j.source,
                        j.posted.date().isoformat() if j.posted else "", j.url, j.reason,
                        "; ".join(j.flags), ", ".join(j.tech_hits)])

    rows = []
    for j in shown:
        cls = "hi" if j.score >= 75 else "mid" if j.score >= 60 else "lo"
        rows.append(
            f"<tr><td><span class='b {cls}'>{j.score}</span></td>"
            f"<td>{'<span class=new>NEW</span>' if j.is_new else ''}</td>"
            f"<td><a href='{esc(j.url)}' target='_blank' rel='noopener'>{esc(j.title)}</a>"
            f"<div class=s>{esc(j.company)} &middot; {esc(j.location)}{' &middot; remote' if j.remote else ''}</div></td>"
            f"<td class='v {esc(j.visa)}'>{esc(j.visa)}</td><td>{esc(j.source)}</td>"
            f"<td>{esc(j.reason)}<div class=f>{esc(' | '.join(j.flags[:4]))}</div></td></tr>")
    page = f"""<!doctype html><html lang=en><meta charset=utf-8><title>job-radar {stamp}</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#1b1f24;background:#fafafa}}
h1{{font-size:20px}} .meta{{color:#555;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{padding:8px 10px;border-bottom:1px solid #e5e7eb;vertical-align:top;text-align:left}}
th{{background:#f1f3f5;position:sticky;top:0}}
a{{color:#0b57d0;text-decoration:none;font-weight:600}} .s{{color:#666;font-size:12px}} .f{{color:#b45309;font-size:12px}}
.b{{display:inline-block;min-width:34px;text-align:center;padding:2px 6px;border-radius:10px;font-weight:700;color:#fff}}
.hi{{background:#15803d}} .mid{{background:#b45309}} .lo{{background:#6b7280}}
.new{{background:#dbeafe;color:#1d4ed8;padding:1px 6px;border-radius:6px;font-size:11px;font-weight:700}}
.v.explicit{{color:#15803d;font-weight:700}} .v.likely{{color:#4d7c0f}} .v.blocked,.v.unlikely{{color:#b91c1c}} .v.mixed{{color:#b45309}}
</style>
<h1>job-radar report</h1>
<div class=meta>{NOW.strftime('%Y-%m-%d %H:%M UTC')} &middot; collected {stats['collected']} &middot; after filters {stats['filtered']}
&middot; unique {stats['unique']} &middot; LLM-scored {stats['llm']} &middot; shown {len(shown)} (min score {cfg.get('min_score_in_report', 50)})</div>
<table><tr><th>Score</th><th></th><th>Job</th><th>Visa</th><th>Source</th><th>Why</th></tr>
{''.join(rows)}</table></html>"""
    html_path = out / f"report-{stamp}.html"
    html_path.write_text(page, encoding="utf-8")
    return html_path, csv_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def demo_jobs() -> list[Job]:
    """Sample postings to test filters, scoring and the report without scraping."""
    def mk(title, company, location, desc, remote=False, country=""):
        return Job(source="demo", title=title, company=company, location=location,
                   url="https://example.com/demo/" + norm(title).replace(" ", "-"),
                   description=desc, posted=NOW, remote=remote, country=country)
    return [
        mk("Backend Engineer (Python, AWS)", "Acme Cloud GmbH", "Berlin, Germany",
           "Build serverless AWS systems with Python, TypeScript, Lambda, SQS and Terraform. "
           "3+ years of experience. We offer visa sponsorship and a relocation package. "
           "Working language is English.", country="germany"),
        mk("AI Engineer (LLM)", "Shamrock AI", "Dublin, Ireland",
           "Ship LLM features with Azure OpenAI, LangChain and Python on AWS. 2-4 years of experience. "
           "We support Critical Skills permit applications and offer relocation assistance.", country="ireland"),
        mk("Software Engineer", "Tulip Software BV", "Amsterdam, Netherlands",
           "TypeScript and AWS microservices. You must have the right to work in the EU. "
           "No visa sponsorship is available.", country="netherlands"),
        mk("Software Engineer Backend", "Bavaria Systems", "Munich, Germany",
           "Python, AWS, Docker. Fluent German (C1) is required; our team language is German.",
           country="germany"),
        mk("Staff Software Engineer", "BigCorp", "Paris, France",
           "Lead architecture across teams. 10+ years of experience.", country="france"),
        mk("Cloud Engineer", "Globex Remote", "Worldwide",
           "Fully remote team. AWS, Terraform and Python. English is the working language.",
           remote=True),
        mk("Senior Backend Engineer", "Fintech SA", "Luxembourg",
           "8+ years of experience with Python and AWS. Visa sponsorship and relocation support "
           "available. English and French spoken.", country="luxembourg"),
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description="job-radar 2.0")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--no-llm", action="store_true", help="keyword scoring only")
    ap.add_argument("--quick", action="store_true", help="first 2 terms per country, no sponsorship-phrased queries")
    ap.add_argument("--demo", action="store_true", help="run on built-in sample jobs: no scraping, history untouched")
    ap.add_argument("--only", help="comma list: boards,arbeitnow,relocateme,remotive,remoteok,himalayas,adzuna")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    only = {x.strip() for x in args.only.split(",")} if args.only else None

    jobs: list[Job] = []
    if args.demo:
        log("Demo mode: using sample jobs, no scraping.")
        jobs = demo_jobs()
        only = set()  # nothing else runs
    if only is None or "boards" in only:
        searches = build_searches(cfg, args.quick)
        log(f"Boards: {len(searches)} searches on {', '.join(cfg.get('sites', []))}")
        jobs += scrape_boards(cfg, searches)
        log(f"Boards returned {len(jobs)} jobs")

    extra = cfg.get("extra_sources") or {}
    for name, fn in SOURCES.items():
        opts = extra.get(name) or {}
        if (only is not None and name in only) or (only is None and opts.get("enabled")):
            log(f"Source: {name}")
            try:
                got = fn(cfg, opts)
                log(f"   {len(got)} jobs")
                jobs += got
            except Exception as e:
                log(f"   {name} failed: {type(e).__name__}: {str(e)[:100]}")

    stats = {"collected": len(jobs)}

    cutoff = NOW - timedelta(hours=cfg.get("hours_old", 72) + 24)
    m = Matcher(cfg)
    jobs = [j for j in jobs if j.title and j.url and (j.posted is None or j.posted >= cutoff) and m.title_ok(j.title)]
    stats["filtered"] = len(jobs)
    jobs = dedupe(jobs)
    stats["unique"] = len(jobs)
    log(f"{stats['collected']} collected -> {stats['filtered']} after filters -> {stats['unique']} unique")

    for j in jobs:
        analyze(j, m)
    jobs.sort(key=lambda j: -j.kw_score)

    llm_cfg = cfg.get("llm", {})
    use_llm = llm_cfg.get("enabled", False) and not args.no_llm and llm_available(llm_cfg)
    to_review = [j for j in jobs if j.visa != "blocked"][: llm_cfg.get("max_jobs", 150)] if use_llm else []
    reviewed = {id(j) for j in to_review}
    for n, j in enumerate(to_review, 1):
        log(f"LLM {n}/{len(to_review)}: {j.title[:60]} @ {j.company[:30]}")
        finalize(j, llm_review(j, cfg))
    for j in jobs:
        if id(j) not in reviewed:
            finalize(j, None)
    stats["llm"] = sum(j.llm_used for j in jobs)

    state_path = cfg.get("state_file", "data/seen.json")
    state = {} if args.demo else load_state(state_path)
    for j in jobs:
        j.is_new = j.key not in state
        state.setdefault(j.key, NOW.date().isoformat())
    if not args.demo:
        save_state(state_path, state)

    min_score = cfg.get("min_score_in_report", 50)
    shown = [j for j in jobs if j.score >= min_score and (j.is_new or not cfg.get("only_new_in_report", False))]
    shown.sort(key=lambda j: (-j.score, not j.is_new))

    html_path, csv_path = write_reports(jobs, shown, cfg, stats)
    log(f"\n{len(shown)} jobs in report. Top picks:")
    for j in shown[:10]:
        log(f"  {j.score:3d} {'NEW ' if j.is_new else '    '}{j.title[:55]} @ {j.company[:25]} [{j.visa}] {j.source}")
    log(f"\nHTML: {html_path}\nCSV:  {csv_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)