#!/usr/bin/env python3
"""Benchmarks candidate Ollama models on this project's two real prompts.

The point is to answer "can my machine run this model for this job?" with
numbers from your machine instead of guesses: wall time, tokens/sec, whether
the swim advisor's JSON contract validated, and what the model actually said.

    ./venv/bin/python bench_models.py                       # every installed model
    ./venv/bin/python bench_models.py gemma3:12b qwen2.5:32b
    ./venv/bin/python bench_models.py --job advisor --runs 3

Between models it asks Ollama to unload the previous one, so each timing is a
cold-ish start and peak memory stays at one model's worth - which is what you
want when checking whether a 27b/32b model fits alongside everything else.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import llm
from envfile import load_dotenv

HERE = Path(__file__).resolve().parent
DATA = HERE / "site" / "data"

# Stand-ins used when the real generated JSON isn't on disk yet, so the
# benchmark is runnable on a fresh checkout.
SAMPLE_DAYS = [
    {"date": "2026-08-04", "name": "Today", "temp_f": 91, "pop_pct": 10, "wind_mph": 6, "short_forecast": "Sunny"},
    {"date": "2026-08-05", "name": "Wednesday", "temp_f": 88, "pop_pct": 40, "wind_mph": 9, "short_forecast": "Scattered Showers"},
    {"date": "2026-08-06", "name": "Thursday", "temp_f": 74, "pop_pct": 70, "wind_mph": 18, "short_forecast": "Thunderstorms Likely"},
    {"date": "2026-08-07", "name": "Friday", "temp_f": 69, "pop_pct": 20, "wind_mph": 14, "short_forecast": "Partly Cloudy"},
    {"date": "2026-08-08", "name": "Saturday", "temp_f": 84, "pop_pct": 5, "wind_mph": 7, "short_forecast": "Sunny"},
]

SAMPLE_ROWS = [
    {
        "fetched_at": f"2026-07-{21 + i // 2:02d}T{8 if i % 2 == 0 else 20:02d}:00",
        "status": "GREEN" if i != 6 else "RED",
        "free_cl": round(3.1 - i * 0.12, 2),
        "ph": round(7.4 + i * 0.02, 2),
        "water_temp": 84 + (i % 3),
        "skimmer_flow": 32,
    }
    for i in range(14)
]


def _advisor_prompt() -> tuple[str, list[dict]]:
    from swim_advisor import _build_prompt

    weather_file = DATA / "weather.json"
    days = SAMPLE_DAYS
    if weather_file.exists():
        days = json.loads(weather_file.read_text()).get("days") or SAMPLE_DAYS
    return _build_prompt(days, water_temp=86, today=datetime.now(timezone.utc)), days


def _trend_prompt() -> str:
    lines = [
        f"{r['fetched_at'][:16]}  status={r['status']}  free_cl={r['free_cl']}  ph={r['ph']}  "
        f"temp={r['water_temp']}F  flow={r['skimmer_flow']}"
        for r in SAMPLE_ROWS
    ]
    return (
        "You are summarizing recent water-quality readings for a residential pool named 'Pool' "
        "for its owner. Here is the reading history, oldest first:\n\n"
        + "\n".join(lines)
        + "\n\nIn 2-3 short sentences, say whether free chlorine, pH, and water temp are trending up, "
        "down, or holding steady, and whether things look solid or need attention. Be direct and "
        "concrete with numbers. Do not repeat the raw data as a list - write plain prose. No preamble."
    )


def _unload(model: str):
    """keep_alive=0 evicts the model so the next one starts from a clean slate."""
    try:
        requests.post(
            f"{llm.host()}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": 0},
            timeout=30,
        )
    except requests.RequestException:
        pass


def _run_advisor(model: str, timeout: int) -> dict:
    from swim_advisor import advice_schema, _valid_llm_result

    prompt, days = _advisor_prompt()
    schema = advice_schema([d["date"] for d in days])
    result = llm.generate(prompt, model=model, fmt=schema, timeout=timeout)
    if not result:
        return {"ok": False, "detail": "no response (timeout, OOM, or Ollama unreachable)"}

    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError:
        return {"ok": False, "detail": "output was not valid JSON", "result": result}

    valid = isinstance(parsed, dict) and _valid_llm_result(parsed, days)
    verdicts = " ".join(
        f"{d.get('date', '?')[5:]}={d.get('verdict', '?')}" for d in parsed.get("days", [])
    ) if isinstance(parsed, dict) else ""
    return {
        "ok": valid,
        "detail": verdicts if valid else "JSON parsed but failed the schema/date check",
        "result": result,
        "extra": (parsed.get("heater_advice") or "")[:200] if isinstance(parsed, dict) else "",
    }


def _run_trend(model: str, timeout: int) -> dict:
    result = llm.generate(_trend_prompt(), model=model, timeout=timeout)
    if not result:
        return {"ok": False, "detail": "no response (timeout, OOM, or Ollama unreachable)"}
    text = " ".join(result.text.split())
    return {"ok": bool(text), "detail": text[:200], "result": result}


JOBS = {"advisor": _run_advisor, "trend": _run_trend}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("models", nargs="*", help="model tags to test (default: everything installed)")
    parser.add_argument("--job", choices=[*JOBS, "both"], default="both")
    parser.add_argument("--runs", type=int, default=1, help="repetitions per model/job")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--keep-loaded", action="store_true", help="don't unload between models")
    args = parser.parse_args()

    load_dotenv()

    models = args.models or llm.installed_models()
    if not models:
        print(f"No models found at {llm.host()} - is Ollama running? (`ollama serve`)", file=sys.stderr)
        sys.exit(1)

    jobs = list(JOBS) if args.job == "both" else [args.job]
    print(f"Ollama: {llm.host()}   models: {len(models)}   jobs: {', '.join(jobs)}   runs: {args.runs}\n")

    rows = []
    for model in models:
        for job in jobs:
            times, toks, oks, last = [], [], 0, None
            for _ in range(args.runs):
                started = time.monotonic()
                out = JOBS[job](model, args.timeout)
                elapsed = time.monotonic() - started
                last = out
                oks += 1 if out["ok"] else 0
                times.append(elapsed)
                res = out.get("result")
                if res and res.tokens_per_s:
                    toks.append(res.tokens_per_s)

            avg = sum(times) / len(times)
            tps = sum(toks) / len(toks) if toks else None
            rows.append((model, job, avg, tps, oks, args.runs))
            status = "ok " if oks == args.runs else ("part" if oks else "FAIL")
            tps_text = f"{tps:5.1f} tok/s" if tps else "     -     "
            print(f"{model:<28} {job:<8} {avg:6.1f}s  {tps_text}  {status} {oks}/{args.runs}")
            print(f"{'':<28} └─ {last['detail']}")
            if last.get("extra"):
                print(f"{'':<28}    {last['extra']}")
            print()

        if not args.keep_loaded:
            _unload(model)

    print("\nSummary (lower time + ok on every run = usable for that job)")
    print(f"{'model':<28} {'job':<8} {'avg':>7}  {'tok/s':>7}  pass")
    for model, job, avg, tps, oks, runs in rows:
        print(f"{model:<28} {job:<8} {avg:6.1f}s  {(f'{tps:.1f}' if tps else '-'):>7}  {oks}/{runs}")


if __name__ == "__main__":
    main()
