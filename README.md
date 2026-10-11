# job-radar

Finds software engineering jobs in Europe that offer visa sponsorship, ranks them against your profile with a **local LLM**, and writes an HTML report. Everything runs on your own machine: no cloud accounts and no paid services (the only optional key is a free Adzuna one).

## How it works

1. **Collect** jobs, country by country:
   - LinkedIn, Indeed, Glassdoor and Google Jobs through [JobSpy](https://github.com/speedyapply/JobSpy) (open source), for every term in every country listed in `config.yaml`, plus extra queries that put "visa sponsorship" and "relocation" into the search.
   - Extra sources: Arbeitnow, relocate.me, Remotive, RemoteOK, Himalayas, and Adzuna (optional, needs a free key). Remote jobs are kept only if they are open to Europe, worldwide, or your target countries.
2. **Filter** titles with whole-word matching (keeps engineer/developer roles, drops staff/principal/lead/intern/frontend etc.).
3. **Deduplicate** across sources, and against `data/seen.json` so each job is marked NEW only once.
4. **Score** each job: keyword analysis first (sponsorship phrases, visa and language blockers, required years, your skills), then a local model through [Ollama](https://ollama.com) rates role match, stack match, seniority fit, visa signal and language requirements. Falls back to keyword scoring if Ollama isn't running or the model isn't installed.
5. **Report** to `reports/report-<date>_<time>.html` (jobs above `min_score_in_report`, highest score first) and `reports/all-jobs-<date>_<time>.csv` (every job, for auditing).

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/AyoubBalti/job-radar.git
cd job-radar
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Local LLM (optional but recommended):

```bash
# install Ollama from https://ollama.com, then:
ollama pull qwen2.5:14b            # ~9 GB; needs ~16 GB RAM
```

`qwen2.5:14b` is the default model and gives noticeably better scoring than smaller ones. On a weaker machine, use `qwen2.5:7b` (~4.7 GB, needs ~8 GB RAM) or `qwen2.5:3b`, and change `llm.model` in `config.yaml`.

## Usage

```bash
python radar.py                      # full run
python radar.py --quick              # first 2 terms per country, no sponsorship-phrased queries
python radar.py --no-llm             # keyword scoring only (fast)
python radar.py --demo               # test the pipeline on sample jobs, no scraping
python radar.py --only boards,arbeitnow,relocateme   # run only some sources
python radar.py -c other.yaml        # use a different config file
```

`--demo` uses 7 built-in sample postings, never touches the network (except Ollama, unless you add `--no-llm`), and leaves `data/seen.json` untouched. Sources for `--only`: `boards`, `arbeitnow`, `relocateme`, `remotive`, `remoteok`, `himalayas`, `adzuna`.

Open the newest file in `reports/` in your browser.

## Run it every morning

**Linux / macOS (cron):** run `crontab -e` and add:

```
0 8 * * * cd /path/to/job-radar && .venv/bin/python radar.py >> radar.log 2>&1
```

**Windows (Task Scheduler):** create a basic task, daily at 08:00, action "Start a program":
- Program: `C:\path\to\job-radar\.venv\Scripts\python.exe`
- Arguments: `radar.py`
- Start in: `C:\path\to\job-radar`

## Configuration

Everything lives in `config.yaml`: countries and search terms, sponsorship-phrased queries, extra sources, title filters, sponsorship / visa-blocker / language-blocker phrases, the LLM model (`qwen2.5:14b` by default), the minimum score shown, `only_new_in_report`, and your profile text and skills used for scoring.

## Notes

- Keep `results_per_search` modest. Job boards rate-limit heavy scraping; LinkedIn in particular may temporarily block your IP if you push it.
- relocate.me has no public API, so its scraper is best-effort. If it reports 0 jobs, the site layout changed and `src_relocateme` in `radar.py` needs adjusting.
- Remotive and RemoteOK ask for attribution and limited request rates, so don't run them many times a day.
- Scoring with `qwen2.5:14b` is slower than with 7b. If a run takes too long, lower `llm.max_jobs` or switch to a smaller model.
- Scraping is for personal job searching. Respect each site's terms and don't redistribute the data.
- The LLM can be wrong. Always read the posting before applying.