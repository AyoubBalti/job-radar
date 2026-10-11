# job-radar

Finds software engineering jobs in Europe that offer visa sponsorship, ranks them against your profile with a **local LLM**, and writes a daily HTML report. Everything runs on your own machine: no cloud accounts, no API keys, no paid services.

## How it works

1. **Scrape** LinkedIn, Indeed and Glassdoor with [JobSpy](https://github.com/speedyapply/JobSpy) (open source) for each search in `config.yaml`.
2. **Deduplicate** against a local SQLite history, so each job appears only once, ever.
3. **Filter** titles (keeps engineer/developer roles, drops staff/principal/intern/frontend etc.).
4. **Score** each job with a local model through [Ollama](https://ollama.com): fit score 0-100, whether sponsorship is offered, and whether fluent German is required. Falls back to keyword scoring if Ollama isn't running.
5. **Report** to `reports/YYYY-MM-DD.html`, sorted by sponsorship first, then fit.

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
python radar.py            # full run
python radar.py --no-llm   # keyword scoring only (fast)
python radar.py --demo     # test the pipeline on sample jobs, no scraping
```

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

Everything lives in `config.yaml`: searches (term, city, country), title filters, sponsorship and blocker phrases, the LLM model (`qwen2.5:14b` by default), the minimum score shown, and your profile text used for scoring.

## Notes

- Keep `results_per_search` modest. Job boards rate-limit heavy scraping; LinkedIn in particular may temporarily block your IP if you push it.
- Scraping is for personal job searching. Respect each site's terms and don't redistribute the data.
- The LLM can be wrong. Always read the posting before applying.
- Scoring with `qwen2.5:14b` is slower than with 7b. If a run takes too long, lower `llm.max_jobs` or switch to a smaller model.