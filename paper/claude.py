"""
Optional Claude approval step for paper trades (strategy.use_claude = true).

Uses the ANTHROPIC_API_KEY secret. Every decision — buy or pass, with
Claude's reason — is recorded in signals.csv so it can be reviewed and so
verify.py can replay the exact same decisions deterministically.
"""

from __future__ import annotations

import json
import os


def make_decider():
    """Return a decider function, or None when no API key is available."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    client = anthropic.Anthropic(max_retries=2, timeout=60)

    def decide(ticker: str, quote: dict, cfg: dict) -> dict:
        s = cfg["strategy"]
        if s["sell_mode"] == "claude":
            target_note = ("Also recommend a realistic limit-sell price likely to fill before the close "
                           "as 'target_price'.")
        elif s["sell_mode"] == "percent":
            target_note = f"The sell target is fixed at +{s['sell_percent']}% from entry."
        else:
            target_note = f"The sell target is fixed at +${s['profit_target']:.2f}/share from entry."
        prompt = (
            "You are reviewing a paper (simulated) day trade for a momentum bot.\n\n"
            f"STOCK: {ticker}\nTime: {quote['timestamp']}\nPrice: ${quote['price']:.2f}\n"
            f"Change vs previous close: {quote['change_pct']:+.2f}%\n"
            f"Volume vs time-adjusted 30-day average: {quote['vol_ratio']:.1f}x\n\n"
            f"STRATEGY: {s['strategy_text']}\nPOSITION SIZE: ${s['position_size']:.0f}\n"
            f"{target_note}\n"
            + ("Positions are never sold at a loss automatically.\n" if not s.get("stop_loss_pct") else
               f"A stop-loss sits {s['stop_loss_pct']}% below entry.\n")
            + "\nIs this a high-probability same-day long entry? Respond ONLY with a JSON object:\n"
            '{"buy": true|false, "reason": "1-2 sentences", "confidence": "HIGH|MEDIUM|LOW", '
            '"target_price": number|null}'
        )
        try:
            resp = client.messages.create(
                model=s.get("claude_model") or "claude-opus-5-5",
                max_tokens=2000,
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": prompt}],
            )
            if resp.stop_reason == "refusal":
                return {"buy": False, "reason": "Claude declined to answer", "confidence": "N/A"}
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            text = text[text.find("{"): text.rfind("}") + 1]
            out = json.loads(text)
            out["buy"] = bool(out.get("buy"))
            return out
        except Exception as e:  # noqa: BLE001 — a Claude outage must not crash the run
            return {"buy": False, "reason": f"Claude error: {e}"[:200], "confidence": "N/A"}

    return decide


def replay_decider(signals: list[dict]):
    """Decider that returns the decisions Claude made originally (for deterministic replays)."""
    recorded = {(r["ticker"], r["timestamp"]): r for r in signals if r.get("decided_by") == "claude"}

    def decide(ticker: str, quote: dict, cfg: dict) -> dict:
        r = recorded.get((ticker, quote["timestamp"]))
        if not r:
            return {"buy": False, "reason": "no recorded decision", "decided_by": "replay"}
        return {"buy": r["decision"] == "BUY", "reason": r["reason"], "confidence": r.get("confidence", ""),
                "target_price": r.get("target_price") or None, "decided_by": "claude"}

    return decide
