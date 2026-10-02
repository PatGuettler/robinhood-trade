"""Unit tests for the paper-trading engine's fill rules, ledger and verification."""

from datetime import date, datetime, time as dtime, timedelta

import pytest

from paper import config as config_mod
from paper.engine import Engine, new_state
from paper.marketdata import ET, Bar, DailyBar, MarketData
from paper.report import roundtrips, summarize
from paper.storage import LocalStorage
from paper.verify import equity_check, ledger_checks, lookahead_check

DAY = date(2026, 9, 29)


def cfg(**strategy):
    base = {"watchlist": ["TEST"], "position_size": 1000, "min_change_pct": 1.5,
            "min_volume_multiplier": 1.0,
            # fill-rule tests use a small made-up stock; scanner filters get their own tests
            "min_avg_dollar_volume": 0, "max_change_pct": 0, "require_above_vwap": False}
    c = config_mod.normalize({"strategy": {**base, **strategy},
                              "execution": {"slippage_bps": 10}})
    return c


def t(hh, mm, day=DAY):
    return datetime.combine(day, dtime(hh, mm), ET)


def market(bars, prev_close=100.0, avg_volume=78_000):
    """Daily history: 30 flat days before DAY so prev_close/avg volume are known."""
    daily = [DailyBar(DAY - timedelta(days=i), prev_close, prev_close, prev_close, prev_close, avg_volume)
             for i in range(30, 0, -1)]
    return MarketData(intraday={"TEST": bars}, daily={"TEST": daily}, source="unit", interval_minutes=5)


def bar(hh, mm, o, h, l, c, v=2000, day=DAY):
    return Bar(t(hh, mm, day), o, h, l, c, v)


def run(c, bars, **kw):
    st = new_state(1, 10_000, start_ts=t(9, 30))
    eng = Engine(c, st, market(bars, **kw), mode="backtest", run_id="t")
    eng.run()
    return eng, st


def test_signal_fills_on_next_bar_open_with_slippage():
    bars = [bar(9, 30, 100, 100.5, 99.9, 100.2), bar(9, 35, 100.2, 102, 100.1, 101.8),
            bar(9, 40, 101.9, 102.1, 101.5, 102.0)]
    eng, st = run(cfg(), bars)
    buy = eng.fills[0]
    assert buy["side"] == "BUY"
    assert buy["bar_ts"] == t(9, 40).isoformat()            # bar AFTER the 9:35 signal
    assert buy["price"] == pytest.approx(101.9 * 1.001, abs=1e-4)
    assert buy["target"] == pytest.approx(101.9 * 1.001 + 2.0, abs=1e-4)
    assert st["cash"] == pytest.approx(9000, abs=0.01)


def test_target_sell_requires_high_to_reach_target():
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.5, 101.9, 102.4),
            bar(9, 40, 102.4, 104.0, 102.3, 103.9),          # high 104.0 < target 104.102
            bar(9, 45, 104.0, 104.2, 103.9, 104.1)]          # high 104.2 >= target
    eng, st = run(cfg(), bars)
    sells = [f for f in eng.fills if f["side"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["bar_ts"] == t(9, 45).isoformat()
    assert sells[0]["price"] == pytest.approx(102 * 1.001 + 2, abs=1e-4)
    assert sells[0]["reason"] == "target"


def test_gap_above_target_fills_at_open():
    d2 = DAY + timedelta(days=1)
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.5, 101.9, 102.4),
            bar(15, 55, 102.4, 102.5, 102.3, 102.4),
            bar(9, 30, 106, 107, 105.5, 106.5, day=d2)]
    eng, _ = run(cfg(), bars)
    sell = [f for f in eng.fills if f["side"] == "SELL"][0]
    assert sell["reason"] == "target (gap up)"
    assert sell["price"] == pytest.approx(106)


def test_no_stop_means_losing_position_is_held():
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.1, 95, 95.5),
            bar(15, 55, 95, 95.2, 90, 90)]
    eng, st = run(cfg(), bars)
    assert [f["side"] for f in eng.fills] == ["BUY"]
    assert "TEST" in st["positions"]
    assert eng.equity[-1]["unrealized_pnl"] < 0


def test_stop_beats_target_when_both_inside_one_bar():
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.1, 101.9, 102),
            bar(9, 40, 102, 110, 90, 100)]
    eng, _ = run(cfg(stop_loss_pct=2.0), bars)
    sell = [f for f in eng.fills if f["side"] == "SELL"][0]
    assert sell["reason"] == "stop"
    entry = 102 * 1.001
    assert sell["price"] == pytest.approx(entry * 0.98 * 0.999, abs=1e-4)
    assert sell["realized_pnl"] < 0


def test_close_at_eod():
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.1, 101.9, 102),
            bar(15, 55, 102.5, 102.9, 102.4, 102.8)]
    eng, st = run(cfg(close_at_eod=True), bars)
    sell = [f for f in eng.fills if f["side"] == "SELL"][0]
    assert sell["reason"] == "end of day"
    assert sell["price"] == pytest.approx(102.8 * 0.999, abs=1e-4)
    assert not st["positions"]


def test_volume_filter_blocks_quiet_moves():
    bars = [bar(9, 30, 100, 102, 100, 101.8, v=10), bar(9, 35, 102, 102.1, 101.9, 102, v=10)]
    eng, _ = run(cfg(min_volume_multiplier=1.2), bars)
    assert not eng.fills and not eng.signals


def test_down_moves_pass_when_direction_up():
    bars = [bar(9, 30, 100, 100, 97, 97.5), bar(9, 35, 97.5, 97.6, 97, 97.2)]
    eng, _ = run(cfg(), bars)
    assert not eng.fills
    assert eng.signals[0]["decision"] == "PASS"


def test_last_entry_time_respected():
    bars = [bar(15, 45, 100, 102, 100, 101.8), bar(15, 50, 102, 102.1, 101.9, 102)]
    eng, _ = run(cfg(), bars)
    assert not eng.fills


def test_cash_limit_and_max_positions():
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.1, 101.9, 102)]
    eng, st = run(cfg(position_size=20_000), bars)
    assert not eng.fills
    assert eng.signals[0]["decision"] == "SKIP"


def test_claude_decider_is_used_and_recorded():
    seen = []

    def decider(sym, quote, c):
        seen.append(quote)
        return {"buy": False, "reason": "looks extended", "confidence": "LOW"}

    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.1, 101.9, 102)]
    st = new_state(1, 10_000, start_ts=t(9, 30))
    eng = Engine(cfg(), st, market(bars), mode="live", run_id="t", decider=decider)
    eng.run()
    assert len(seen) == 1 and not eng.fills
    assert eng.signals[0]["decided_by"] == "claude" and eng.signals[0]["reason"] == "looks extended"


def test_live_runs_in_chunks_match_one_backtest():
    """Processing bars across several live runs gives the same result as one replay."""
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.5, 101.9, 102.4),
            bar(9, 40, 102.4, 104.0, 102.3, 103.9), bar(9, 45, 104.0, 104.2, 103.9, 104.1),
            bar(9, 50, 104.1, 104.3, 103.0, 103.2)]
    one, _ = run(cfg(), bars)

    st = new_state(1, 10_000, start_ts=t(9, 30))
    fills = []
    for cutoff in (t(9, 37), t(9, 46), t(10, 0)):
        eng = Engine(cfg(), st, market(bars), mode="live", run_id="x")
        eng.run(until=cutoff)
        fills += eng.fills
    strip = lambda fs: [(f["side"], f["bar_ts"], f["price"]) for f in fs]
    assert strip(fills) == strip(one.fills)


def test_incomplete_bar_is_not_processed():
    bars = [bar(9, 30, 100, 102, 100, 101.8)]
    st = new_state(1, 10_000, start_ts=t(9, 30))
    eng = Engine(cfg(), st, market(bars), mode="live", run_id="x")
    assert eng.run(until=t(9, 33)) == 0
    assert eng.run(until=t(9, 35)) == 1


def test_verification_passes_and_catches_tampering():
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.5, 101.9, 102.4),
            bar(9, 45, 104.0, 104.2, 103.9, 104.1)]
    eng, _ = run(cfg(), bars)
    fills = [{k: str(v) for k, v in f.items()} for f in eng.fills]
    sigs = [{k: str(v) for k, v in s.items()} for s in eng.signals]
    assert all(c["status"] == "pass" for c in ledger_checks(fills, 10_000, 10, "x"))
    assert lookahead_check(fills, sigs, "x")["status"] == "pass"
    assert equity_check([{k: str(v) for k, v in e.items()} for e in eng.equity], "x")["status"] == "pass"

    tampered = [dict(f) for f in fills]
    tampered[1]["price"] = str(float(tampered[1]["price"]) + 5)   # sell above what the bar allowed
    results = {c["name"]: c["status"] for c in ledger_checks(tampered, 10_000, 10, "x")}
    assert results["fill_rules"] == "fail"
    assert results["cash_ledger"] == "fail"


def test_summary_and_roundtrips():
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.5, 101.9, 102.4),
            bar(9, 45, 104.0, 104.2, 103.9, 104.1)]
    eng, st = run(cfg(), bars)
    trips = roundtrips(eng.fills)
    assert len(trips) == 1 and trips[0]["pnl"] > 0
    sm = summarize(eng.fills, eng.equity, 10_000)
    assert sm["wins"] == 1 and sm["win_rate"] == 100.0
    assert sm["ending_equity"] == pytest.approx(st["cash"], abs=0.01)


def test_local_storage_csv_roundtrip(tmp_path):
    s = LocalStorage(tmp_path)
    s.append_csv("a.csv", [{"x": 1, "y": 2}], ["x", "y"])
    s.append_csv("a.csv", [{"x": 3, "y": 4}], ["x", "y"])
    assert s.read_csv("a.csv") == [{"x": "1", "y": "2"}, {"x": "3", "y": "4"}]


# ── Scanner filters, ranking and daily picks ─────────────────────────────────

def test_liquidity_filter_skips_thin_stocks():
    bars = [bar(9, 30, 100, 102, 100, 101.8), bar(9, 35, 102, 102.1, 101.9, 102)]
    eng, _ = run(cfg(min_avg_dollar_volume=20e6), bars)      # 78k shares x $100 = $7.8M/day
    assert not eng.fills and not eng.signals


def test_max_change_cap_skips_overextended():
    bars = [bar(9, 30, 100, 116, 100, 115), bar(9, 35, 115, 116, 114, 115.5)]
    eng, _ = run(cfg(max_change_pct=12), bars)
    assert not eng.fills


def test_vwap_filter_skips_fading_stock():
    # spikes to 106 on heavy volume (out of the entry window), then fades to 102 (+2%) below VWAP
    bars = [bar(9, 30, 100, 106, 100, 105.5, v=20000), bar(9, 35, 105.5, 105.6, 101.9, 102.0, v=2000),
            bar(9, 40, 102, 102.2, 101.8, 102.1)]
    late = dict(entry_start="09:40")         # only the faded 9:35 bar can trigger
    off, _ = run(cfg(require_above_vwap=False, **late), bars)
    on, _ = run(cfg(require_above_vwap=True, **late), bars)
    assert [f["side"] for f in off.fills] == ["BUY"]
    assert not on.fills and not on.signals


def _two_stock_market(strong, weak):
    daily = {s: [DailyBar(DAY - timedelta(days=i), 100, 100, 100, 100, 78_000) for i in range(30, 0, -1)]
             for s in (strong, weak)}
    intraday = {
        strong: [bar(9, 30, 100, 104, 100, 103.5, v=8000), bar(9, 35, 103.5, 103.6, 103.4, 103.5)],
        weak: [bar(9, 30, 100, 102, 100, 101.6, v=3000), bar(9, 35, 101.6, 101.7, 101.5, 101.6)],
    }
    return MarketData(intraday=intraday, daily=daily, source="unit", interval_minutes=5)


def test_strongest_signal_is_bought_first_when_slots_are_limited():
    st = new_state(1, 10_000, start_ts=t(9, 30))
    eng = Engine(cfg(max_open_positions=1), st, _two_stock_market("ZZZ", "AAA"), mode="backtest", run_id="t")
    eng.run()
    buys = [f["ticker"] for f in eng.fills if f["side"] == "BUY"]
    assert buys == ["ZZZ"]                                    # not alphabetical "AAA"
    assert [s["decision"] for s in eng.signals if s["ticker"] == "AAA"] == ["SKIP"]


def test_allowed_restricts_entries_to_the_days_picks():
    st = new_state(1, 10_000, start_ts=t(9, 30))
    eng = Engine(cfg(), st, _two_stock_market("ZZZ", "AAA"), mode="backtest", run_id="t",
                 allowed={DAY.isoformat(): {"AAA"}})
    eng.run()
    assert {f["ticker"] for f in eng.fills if f["side"] == "BUY"} == {"AAA"}


def test_scanner_picks_use_only_prior_days():
    from paper.scanner import pick_for_day
    flat = lambda i: DailyBar(DAY - timedelta(days=i), 50, 50.5, 49.5, 50, 1_000_000)
    hot = [flat(i) for i in range(40, 1, -1)] + [DailyBar(DAY - timedelta(days=1), 50, 56, 50, 55, 5_000_000)]
    quiet = [flat(i) for i in range(40, 0, -1)]
    future_spike = quiet + [DailyBar(DAY, 50, 70, 50, 69, 9_000_000)]       # happens ON the day — must be ignored
    daily = {"HOT": hot, "QUIET": quiet, "FUTURE": future_spike, "PENNY": [
        DailyBar(DAY - timedelta(days=i), 2, 2.5, 1.5, 2, 50_000_000) for i in range(40, 0, -1)]}
    picks = pick_for_day(DAY, daily, {"scan_size": 2, "min_price": 5, "min_avg_dollar_volume": 20e6})
    names = [p["ticker"] for p in picks]
    assert names[0] == "HOT" and "PENNY" not in names and len(names) == 2
    scores = {p["ticker"]: p["score"] for p in picks}
    assert scores.get("FUTURE", scores.get("QUIET")) == pick_for_day(
        DAY, {"QUIET": quiet}, {"scan_size": 2, "min_price": 5, "min_avg_dollar_volume": 20e6})[0]["score"]
