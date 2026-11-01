"""Pre-flight check: is every key and model working?

    python -m app.check ItsShannu44/demo-app
"""
from __future__ import annotations

import asyncio
import sys
import time

from .config import settings
from .github_client import GitHub
from .llm import LLM, _err, pick_fallback_model, pick_gemini_model

OK, BAD, WARN = "\033[32m✔\033[0m", "\033[31m✘\033[0m", "\033[33m!\033[0m"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


async def main() -> None:
    repo = sys.argv[1] if len(sys.argv) > 1 else (settings.allowed_repos or [""])[0]
    llm = LLM(settings)

    # GitHub
    if not settings.github_token:
        print(f"{BAD} GITHUB_TOKEN missing")
    elif repo:
        gh = GitHub(settings.github_token)
        try:
            run = await gh.latest_failed_run(repo)
            print(f"{OK} GitHub: {repo} reachable; latest failed run: {run['id'] if run else 'none'}")
        except Exception as e:
            print(f"{BAD} GitHub: {_err(e)}")
        ok = await gh.can_write(repo)
        if ok:
            print(f"{OK} GitHub: token can push to {repo}")
        elif ok is False:
            print(f"{BAD} GitHub: token can READ {repo} but NOT push to it. Edit the token: Repository access -> "
                  f"add {repo}; Contents + Pull requests -> Read and write")
        else:
            print(f"{WARN} GitHub: could not tell whether the token can push to {repo}")
        await gh.close()

    # Gemini
    if settings.gemini_api_key:
        try:
            best = pick_gemini_model(await llm.list_gemini_models())
            print(f"  Gemini: configured {settings.gemini_model}, best available {best}")
            t = time.time()
            await llm._gemini("Reply with JSON.", 'Return {"ok": true}', SCHEMA)
            print(f"{OK} Gemini {llm.gemini_model} answered in {time.time() - t:.1f}s")
        except Exception as e:
            print(f"{BAD} Gemini {llm.gemini_model}: {_err(e)}")
    else:
        print(f"{WARN} GEMINI_API_KEY missing")

    # Fallback
    if settings.fallback_api_key:
        try:
            ids = await llm.list_fallback_models()
            best = pick_fallback_model(ids)
            print(f"  Fallback: configured {settings.fallback_model}, best available {best}")
            if settings.fallback_model not in ids and best:
                llm.fallback_model = best
                print(f"{WARN} set FALLBACK_MODEL={best} in .env")
            t = time.time()
            await llm._openai_compat("Reply with JSON.", 'Return {"ok": true}', SCHEMA)
            print(f"{OK} Fallback {llm.fallback_model} answered in {time.time() - t:.1f}s")
        except Exception as e:
            print(f"{BAD} Fallback {llm.fallback_model}: {_err(e)}")
    else:
        print(f"{WARN} FALLBACK_API_KEY missing (recommended for the demo)")
    await llm.close()


if __name__ == "__main__":
    asyncio.run(main())