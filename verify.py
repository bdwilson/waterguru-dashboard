"""Optional second opinion on the local model's swim advice, via the Claude API.

The local model is doing a judgment task with no ground truth, so the useful
question isn't "did it return valid JSON" (constrained decoding guarantees that)
but "are the verdicts actually sensible." This asks Claude the same question a
human would: given the forecast, the water temp, and the season, does each
verdict hold up?

Two modes, set with WG_VERIFY_MODE:
  audit   - record agreement/disagreement, change nothing (default)
  correct - apply Claude's corrections to the published advice

Every run records exactly what it cost, in tokens and dollars, so "is this worth
a few cents a day" is answered with numbers instead of estimates. Entirely
optional: with no ANTHROPIC_API_KEY the pipeline runs as before.
"""
import json
import os
from pathlib import Path

from swim_advisor import VALID_VERDICTS

MODEL_DEFAULT = "claude-opus-5"

# USD per million tokens, Anthropic first-party API rates. Used only to report
# what a run cost - update if list prices move.
PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "agrees": {"type": "boolean"},
                    "verdict": {"type": "string", "enum": VALID_VERDICTS},
                    "note": {"type": "string"},
                    "issue": {"type": "string"},
                },
                "required": ["date", "agrees", "verdict", "note", "issue"],
                "additionalProperties": False,
            },
        },
        "heater_advice_sound": {"type": "boolean"},
        "heater_advice_issue": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["days", "heater_advice_sound", "heater_advice_issue", "summary"],
    "additionalProperties": False,
}


def model() -> str:
    return os.environ.get("WG_VERIFY_MODEL", MODEL_DEFAULT)


def mode() -> str:
    return os.environ.get("WG_VERIFY_MODE", "audit").strip().lower()


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and mode() in {"audit", "correct"}


def estimate_cost(model_id: str, usage) -> float | None:
    """USD for one call. Cache reads bill at ~0.1x input, writes at ~1.25x."""
    prices = PRICING.get(model_id) or PRICING.get(model_id.split("@")[0])
    if not prices:
        return None
    in_price, out_price = prices
    cost = (getattr(usage, "input_tokens", 0) or 0) / 1e6 * in_price
    cost += (getattr(usage, "cache_read_input_tokens", 0) or 0) / 1e6 * in_price * 0.1
    cost += (getattr(usage, "cache_creation_input_tokens", 0) or 0) / 1e6 * in_price * 1.25
    cost += (getattr(usage, "output_tokens", 0) or 0) / 1e6 * out_price
    return round(cost, 6)


def _build_prompt(days: list[dict], advice: dict, water_temp) -> str:
    forecast = "\n".join(
        f"- {d['date']} ({d['name']}): high {d['temp_f']}F, {d['pop_pct']}% chance of rain, "
        f"wind {d['wind_mph']} mph, {d['short_forecast']}"
        for d in days
    )
    calls = "\n".join(
        f"- {date}: {entry.get('verdict')} - {entry.get('note', '')}"
        for date, entry in advice.get("days", {}).items()
    )
    water_line = f"The pool water was last measured at {water_temp}F. " if water_temp else ""

    return f"""A smaller local model rated swim conditions for a heated outdoor residential pool.
Check its work.

{water_line}Forecast it was given:
{forecast}

Its verdicts (one of great/good/marginal/poor):
{calls}

Its heater advice: {advice.get('heater_advice') or '(none given)'}

For each date, decide whether the verdict is defensible given the air temperature, the water
temperature, rain, wind, storms, and the time of year. You are checking for judgment errors, not
enforcing your own taste - a verdict one step off from what you'd pick is still defensible, so mark
it as agreeing. Mark it as disagreeing only when the call is clearly wrong or internally
inconsistent with the other days (for example, rating a warm calm day worse than a stormy one).
When you disagree, give the verdict you would assign and a short note in the same voice, and say
briefly what was wrong. When you agree, repeat its verdict and note unchanged and leave the issue
empty.

Then say whether the heater advice is sound - a heated pool takes a couple of days to move
noticeably, so advice should point at specific days and lead them by roughly 2-3 days. If the
advice is fine, say so and leave the issue empty."""


def _call_claude(prompt: str):
    """Returns (parsed_dict, usage, model_id) or None. Never raises."""
    try:
        import anthropic
    except ImportError:
        print("verify: anthropic SDK not installed (pip install anthropic), skipping")
        return None

    client = anthropic.Anthropic()
    model_id = model()
    output_config = {"format": {"type": "json_schema", "schema": VERIFY_SCHEMA}}
    effort = os.environ.get("WG_VERIFY_EFFORT", "low").strip()
    if effort:
        output_config["effort"] = effort

    kwargs = {
        "model": model_id,
        "max_tokens": 4096,
        "output_config": output_config,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        # Server-side fallback re-runs the request on another model if safety
        # classifiers decline it. Vanishingly unlikely for pool weather, but it
        # costs one parameter. Older SDKs don't know the argument - fall back to
        # the plain endpoint rather than failing the run.
        try:
            resp = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        except (TypeError, anthropic.BadRequestError):
            resp = client.messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        print(f"verify: API error {e.status_code}: {e.message}")
        return None
    except anthropic.APIConnectionError as e:
        print(f"verify: connection error: {e}")
        return None

    # A refusal returns HTTP 200 with empty or partial content - check before reading.
    if resp.stop_reason == "refusal":
        print("verify: request was declined by safety classifiers, skipping")
        return None

    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        print("verify: response was not valid JSON, skipping")
        return None

    return parsed, resp.usage, resp.model


def verify(advice: dict, weather_path: Path, water_temp=None) -> dict | None:
    """Returns a verification block to attach to swim_advice.json, or None."""
    if advice.get("source") != "llm" or not advice.get("days"):
        return None

    days = json.loads(weather_path.read_text()).get("days", [])
    if not days:
        return None

    called = _call_claude(_build_prompt(days, advice, water_temp))
    if not called:
        return None
    parsed, usage, model_id = called

    entries = parsed.get("days") or []
    disagreements = [e for e in entries if not e.get("agrees")]
    cost = estimate_cost(model_id, usage)

    return {
        "model": model_id,
        "mode": mode(),
        "checked": len(entries),
        "disagreements": [
            {"date": e["date"], "was": advice["days"].get(e["date"], {}).get("verdict"),
             "suggested": e["verdict"], "issue": e.get("issue", "")}
            for e in disagreements
        ],
        "heater_advice_sound": parsed.get("heater_advice_sound"),
        "heater_advice_issue": parsed.get("heater_advice_issue") or None,
        "summary": parsed.get("summary"),
        "corrections_applied": mode() == "correct" and bool(disagreements),
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
        },
        "cost_usd": cost,
        "_entries": entries,
    }


def apply(advice: dict, verification: dict) -> dict:
    """Merges the verification into the advice, correcting verdicts in correct mode."""
    entries = verification.pop("_entries", [])

    if verification.get("corrections_applied"):
        for e in entries:
            if e.get("agrees") or e["date"] not in advice["days"]:
                continue
            advice["days"][e["date"]].update(
                {"verdict": e["verdict"], "note": e.get("note") or advice["days"][e["date"]].get("note")}
            )

    advice["verification"] = verification
    return advice


def verify_and_apply(advice_path: Path, weather_path: Path, water_temp=None) -> dict | None:
    if not enabled():
        return None
    advice = json.loads(advice_path.read_text())
    verification = verify(advice, weather_path, water_temp)
    if not verification:
        return None
    advice_path.write_text(json.dumps(apply(advice, verification), indent=2))
    return verification


if __name__ == "__main__":
    from envfile import load_dotenv

    load_dotenv()
    here = Path(__file__).resolve().parent
    result = verify_and_apply(
        here / "site" / "data" / "swim_advice.json",
        here / "site" / "data" / "weather.json",
    )
    if result is None:
        print("verification skipped (set ANTHROPIC_API_KEY, and run fetch.py first)")
    else:
        cost = f"${result['cost_usd']:.4f}" if result["cost_usd"] is not None else "unknown"
        print(
            f"{result['model']}: checked {result['checked']} days, "
            f"{len(result['disagreements'])} disagreement(s), "
            f"{result['usage']['input_tokens']} in / {result['usage']['output_tokens']} out, {cost}"
        )
        for d in result["disagreements"]:
            print(f"  {d['date']}: {d['was']} -> {d['suggested']} ({d['issue']})")
