"""Asks a local LLM (via Ollama) to judge swim suitability day-by-day and give
heater lead-time advice - richer than a fixed scoring formula because it can
weigh season, current water temp, and the fact that a heated pool can be
adjusted a few days ahead of a cool or warm stretch.

Falls back to the rule-based weather.py scores if Ollama isn't reachable or
the model doesn't return usable JSON, so the dashboard never just breaks.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import llm
from db import DB_PATH
from envfile import env_flag

VALID_VERDICTS = ["great", "good", "marginal", "poor"]


def has_heater() -> bool:
    return env_flag("WG_POOL_HAS_HEATER", default=True)


def pool_context() -> str:
    """Free-text owner context (usage pattern, priorities) folded into the
    advisor prompt verbatim. This is deliberately not a set of structured
    fields (usage days, frequency, comfort-vs-cost priority, ...) - a sentence
    the owner writes covers all of that without a taxonomy to maintain, and
    the model reads prose fine.
    """
    return os.environ.get("WG_POOL_CONTEXT", "").strip()


def advice_schema(dates: list[str]) -> dict:
    """Builds the JSON Schema Ollama constrains sampling to (Ollama >= 0.5), so
    the output contract is enforced by the decoder rather than by asking the
    model politely - that's what lets a mid-size local model do this job
    reliably, since the failure mode becomes a weak verdict, not unparseable
    output.

    `date` is constrained to an enum of *this run's* actual forecast dates
    rather than a bare string. A free-form date field looks constrained but
    isn't - it lets a model return well-formed JSON with valid verdicts on
    dates that don't match the forecast, which is exactly the failure mode a
    schema is supposed to rule out. Building the schema per-call is the fix:
    with the real dates as the enum, a mismatched date is something the
    decoder cannot produce, not just something _valid_llm_result rejects
    after the fact.
    """
    return {
        "type": "object",
        "properties": {
            "days": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "enum": dates},
                        "verdict": {"type": "string", "enum": VALID_VERDICTS},
                        "note": {"type": "string"},
                    },
                    "required": ["date", "verdict", "note"],
                },
            },
            "heater_advice": {"type": "string"},
        },
        "required": ["days", "heater_advice"],
    }


def _latest_water_temp(water_body_id: str):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        """SELECT water_temp FROM snapshots WHERE water_body_id = ?
           ORDER BY fetched_at DESC LIMIT 1""",
        (water_body_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _build_prompt(
    days: list[dict], water_temp, today: datetime, context: str = "", heater: bool = True
) -> str:
    day_lines = "\n".join(
        f"- {d['date']} ({d['name']}): high {d['temp_f']}°F, "
        f"{d['pop_pct']}% chance of rain, wind {d['wind_mph']} mph, {d['short_forecast']}"
        for d in days
    )
    water_temp_line = f"The pool water was last measured at {water_temp}°F." if water_temp else ""
    pool_desc = "a heated outdoor residential pool" if heater else "an outdoor residential pool (no heater)"
    context_line = f"\nWhat the owner told us about how they use this pool: {context}" if context else ""

    if heater:
        heater_setup = (
            "The pool has a heater, so the owner can raise or lower the setpoint a couple of days ahead of an\n"
            "upcoming cool or warm stretch to compensate - a big pool takes a couple of days to visibly move in\n"
            "temperature, so lead time matters."
        )
        heater_ask = (
            "\n\nThen write one short, concrete paragraph of heater advice: call out specific days where the "
            "forecast\nruns cooler or warmer than the rest of the stretch, and suggest adjusting the heater "
            "setpoint roughly\n2-3 days ahead of that day so the water has time to catch up. If the whole week "
            "looks consistent, say so\nplainly instead of inventing an adjustment. If the owner told you how "
            "they use the pool, weigh lead time\nagainst the days they'll actually be swimming - a cold snap "
            "on a day they never use matters less than one\non a day they do."
        )
        heater_schema_note = '\n"heater_advice": "<2-4 sentences>"'
    else:
        heater_setup = "This pool has no heater."
        heater_ask = "\n\nThis pool has no heater, so leave heater_advice as an empty string - do not invent heater advice."
        heater_schema_note = '\n"heater_advice": ""'

    return f"""You are a practical assistant for the owner of {pool_desc}.
Today is {today.strftime('%A, %B %d, %Y')}. {water_temp_line}{context_line}
{heater_setup}

Here is the {len(days)}-day air-temperature and rain forecast:
{day_lines}

For each date, decide a swim verdict of exactly one of: great, good, marginal, poor - weighing the
air temperature against the water temp and against what's normal for this time of year, plus rain,
wind, and storms (storms should always be at least "marginal", never "great").{heater_ask}

Respond with ONLY this JSON shape, no other text:
{{"days": [{{"date": "YYYY-MM-DD", "verdict": "great|good|marginal|poor", "note": "<one short phrase>"}}, ...],{heater_schema_note}}}"""


def _call_llm(prompt: str, dates: list[str]) -> tuple[dict, llm.LLMResult] | None:
    result = llm.generate(
        prompt,
        model=llm.advisor_model(),
        fmt=advice_schema(dates),
        timeout=llm.timeout_s(180),
    )
    if not result or not result.text:
        return None
    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or "days" not in parsed or "heater_advice" not in parsed:
        return None
    return parsed, result


def _valid_llm_result(result: dict, days: list[dict], require_heater_advice: bool = True) -> bool:
    known_dates = {d["date"] for d in days}
    entries = result.get("days")
    advice = result.get("heater_advice")
    if not isinstance(entries, list) or not entries or not isinstance(advice, str):
        return False
    if require_heater_advice and not advice.strip():
        return False
    seen_dates = set()
    for e in entries:
        if not isinstance(e, dict):
            return False
        if e.get("date") not in known_dates:
            return False
        if e.get("verdict") not in VALID_VERDICTS:
            return False
        seen_dates.add(e["date"])
    # The date enum keeps every entry's date *known*, but says nothing about
    # *coverage* - a model that drops a day still passes if this check stops at
    # "no unknown dates." A dropped day would otherwise reach the dashboard
    # silently as one rule-based verdict mixed into an array reported as
    # source: "llm", so require every forecast date to actually be answered.
    return seen_dates == known_dates


def build_advice(weather_path: Path, history_path: Path) -> dict:
    weather = json.loads(weather_path.read_text())
    days = weather.get("days", [])
    if not days:
        return {"days": {}, "heater_advice": None, "source": "none", "model": None, "elapsed_s": None}

    history = json.loads(history_path.read_text())
    wb_id = next(iter(history.get("waterbodies", {})), None)
    water_temp = _latest_water_temp(wb_id) if wb_id else None

    heater = has_heater()
    today = datetime.now(timezone.utc)
    prompt = _build_prompt(days, water_temp, today, context=pool_context(), heater=heater)
    called = _call_llm(prompt, [d["date"] for d in days])

    if called and _valid_llm_result(called[0], days, require_heater_advice=heater):
        parsed, meta = called
        by_date = {d["date"]: d for d in parsed["days"]}
        return {
            "days": by_date,
            # Force None when there's no heater regardless of what the model
            # produced - the prompt asks for an empty string, but nothing
            # stops a model from writing heater text for equipment that
            # doesn't exist, and the dashboard should never show that box.
            "heater_advice": parsed["heater_advice"] if heater else None,
            "source": "llm",
            "model": meta.model,
            "elapsed_s": round(meta.elapsed_s, 1),
        }

    # Fallback: reuse the rule-based scores already in weather.json
    by_date = {
        d["date"]: {
            "date": d["date"],
            "verdict": "good" if d["good_swim_day"] else "poor",
            "note": d["swim_reason"],
        }
        for d in days
    }
    return {"days": by_date, "heater_advice": None, "source": "rule_based", "model": None, "elapsed_s": None}


def export_advice(weather_path: Path, history_path: Path, out_path: Path):
    advice = build_advice(weather_path, history_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(advice, indent=2))
    return advice


if __name__ == "__main__":
    from envfile import load_dotenv

    load_dotenv()
    here = Path(__file__).resolve().parent
    print(
        export_advice(
            here / "site" / "data" / "weather.json",
            here / "site" / "data" / "history.json",
            here / "site" / "data" / "swim_advice.json",
        )
    )
