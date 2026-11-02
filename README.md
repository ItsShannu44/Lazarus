# Lazarus

**Brings failed CI builds back to life.**

Lazarus watches GitHub Actions. When a build fails, a team of AI agents reads the logs, finds the broken code, writes a fix, tests it in your own CI and opens a pull request. Nothing merges until a human approves.

*Built in 24 hours at **Agentathon** (31 Oct – 1 Nov 2026), organised by **Encode.ai**, by team **FluxWave**.*

## How it works

1. **Log Analyst** reads the failed CI logs and classifies the error.
2. **Code Locator** opens the files that caused it.
3. **Fixer** writes a minimal patch.
4. **Verifier** pushes it to a `lazarus/*` branch and waits for GitHub Actions. If CI is still red, the Fixer tries again, up to 2 more times.
5. **PR Agent** opens a pull request for you to approve or reject.

A live dashboard shows each step as it happens.

## Features

- Watches the repositories you choose and wakes up on every new CI failure.
- Opens a PR only after the fix passes the repository's own CI.
- Rejects fixes that cheat, such as skipping tests, removing assertions or silencing linters.
- Re-runs flaky or infrastructure failures instead of changing code.
- Retries by itself after rate limits and outages, and opens a GitHub issue when a human is really needed.
- Adds a CI workflow to repositories that don't have one.

## Tech stack

FastAPI · Next.js · Tailwind CSS · Gemini / Groq · GitHub REST API · GitHub Actions · MongoDB (optional)

## Getting started

**Requirements:** Python 3.11+, Node.js 20+, a Gemini or Groq API key, and a fine-grained GitHub token. Create the token from the account that owns the repositories, select those repositories, and set **Contents**, **Pull requests**, **Actions** and **Issues** to *Read and write*.

```bash
# Backend
cd backend
pip install -r requirements.txt
cp .env.example .env              # add GITHUB_TOKEN and an LLM key
python -m app.check owner/repo    # verify token and LLM access
uvicorn app.main:app --port 8000

# Dashboard
cd dashboard
npm install
cp .env.local.example .env.local
npm run dev
```

Open http://localhost:3000, click **Repositories**, and turn on **Watch** for the repositories Lazarus should fix.

## Tests

```bash
cd backend && pytest -q
```

The tests run the whole pipeline offline against a fake GitHub and a fake LLM, so they need no API keys.

## Origin

Lazarus was designed and built during **Agentathon**, a 24-hour hackathon organised by **Encode.ai**, by team **FluxWave** ([ItsShannu44](https://github.com/ItsShannu44)).