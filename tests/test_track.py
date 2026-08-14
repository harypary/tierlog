"""履歴・差分検出・公開文面の回帰テスト。

「サイトが読者に何を主張するか」を固定するためのテスト。
価格そのものより、断定してよいこと/いけないことの境界を守るのが目的。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.catalog import Affiliate, Tool
from src.collect import compare_checks
from src.pages import headline_for
from src.track import (
    KIND_ADDED,
    KIND_DECREASE,
    KIND_INCREASE,
    KIND_PAGE,
    Change,
    Snapshot,
    build_state,
    diff,
    should_record,
)

NOW = datetime(2026, 8, 7, tzinfo=UTC)


def snap(ts: str, signature: str, plans: dict, ok: bool = True) -> Snapshot:
    return Snapshot(ts=ts, slug="x", ok=ok, signature=signature, plans=plans)


def plan(amount: float, period: str = "month") -> dict:
    return {"amount": amount, "period": period, "confidence": "high"}


TOOL = Tool(
    slug="x", name="Acme", vendor="Acme", category="c",
    homepage="https://a.example", pricing_url="https://a.example/p",
    plans=("Pro",), currency="USD",
    affiliate=Affiliate("none", "", ""), patterns={},
)


# ---------------------------------------------------------------
# 差分検出
# ---------------------------------------------------------------
def test_price_increase_is_reported():
    a = snap("2026-01-01T00:00:00+00:00", "s1", {"Pro": plan(20)})
    b = snap("2026-02-01T00:00:00+00:00", "s2", {"Pro": plan(25)})
    (change,) = diff(a, b)
    assert change.kind == KIND_INCREASE
    assert (change.before, change.after) == (20, 25)


def test_price_decrease_is_reported():
    a = snap("2026-01-01T00:00:00+00:00", "s1", {"Pro": plan(25)})
    b = snap("2026-02-01T00:00:00+00:00", "s2", {"Pro": plan(20)})
    assert diff(a, b)[0].kind == KIND_DECREASE


def test_period_only_change_reports_nothing():
    """抽出を直すと周期の表記が変わる。ベンダーは何もしていないので黙る。"""
    a = snap("2026-01-01T00:00:00+00:00", "same", {"Pro": plan(20, "year")})
    b = snap("2026-02-01T00:00:00+00:00", "same", {"Pro": plan(20, "month")})
    assert diff(a, b) == []


def test_unattributable_change_is_reported_without_claiming_what():
    """価格集合は動いたがプランに紐付けられないとき。断定しない。"""
    a = snap("2026-01-01T00:00:00+00:00", "s1", {})
    b = snap("2026-02-01T00:00:00+00:00", "s2", {})
    assert diff(a, b)[0].kind == KIND_PAGE


# ---------------------------------------------------------------
# 記録の要否
# ---------------------------------------------------------------
def test_records_when_period_changes_even_if_amount_is_identical():
    """シグネチャは金額だけから作るので、これが無いと誤表記が永久に残る。"""
    a = snap("2026-01-01T00:00:00+00:00", "same", {"Pro": plan(20, "year")})
    b = snap("2026-02-01T00:00:00+00:00", "same", {"Pro": plan(20, "month")})
    assert should_record(a, b)


def test_does_not_record_when_nothing_moved():
    a = snap("2026-01-01T00:00:00+00:00", "same", {"Pro": plan(20)})
    b = snap("2026-02-01T00:00:00+00:00", "same", {"Pro": plan(20)})
    assert not should_record(a, b)


def test_never_records_a_failed_fetch():
    a = snap("2026-01-01T00:00:00+00:00", "s1", {"Pro": plan(20)})
    b = snap("2026-02-01T00:00:00+00:00", "", {}, ok=False)
    assert not should_record(a, b)


# ---------------------------------------------------------------
# 公開する文面 — 断定してよい境界
# ---------------------------------------------------------------
def test_new_plan_does_not_claim_the_vendor_added_it():
    """プランが現れる理由は「新設」と「抽出の改善」があり区別できない。"""
    change = Change(ts="2026-02-01T00:00:00+00:00", slug="x", kind=KIND_ADDED,
                    plan="Scale", after=43.0, period="month")
    text = headline_for(change, TOOL)
    assert "added" not in text.lower()
    assert "now tracking" in text.lower()
    assert "$43" in text


def test_price_movement_is_stated_plainly():
    """自分で記録した2つの数字の比較なので、こちらは断定してよい。"""
    change = Change(ts="2026-02-01T00:00:00+00:00", slug="x", kind=KIND_INCREASE,
                    plan="Pro", before=20.0, after=25.0, period="month")
    assert "raised" in headline_for(change, TOOL).lower()


def test_unattributable_change_wording_is_not_a_price_claim():
    change = Change(ts="2026-02-01T00:00:00+00:00", slug="x", kind=KIND_PAGE)
    text = headline_for(change, TOOL).lower()
    assert "raised" not in text and "cut" not in text


# ---------------------------------------------------------------
# 鮮度 — 古い価格を現在価格として出さない
# ---------------------------------------------------------------
def test_stale_prices_are_hidden():
    old = (NOW - timedelta(days=40)).isoformat()
    state = build_state("x", ("Pro",), [snap(old, "s1", {"Pro": plan(20)})],
                        {"checked_at": NOW.isoformat(), "ok": False}, NOW, stale_after_days=14)
    assert state.stale
    assert all(p.amount is None for p in state.plans)


def test_steady_price_is_not_marked_stale_just_because_it_never_changed():
    """履歴は変化時しか追記しない。確認日は latest 側から取る。"""
    old = (NOW - timedelta(days=200)).isoformat()
    state = build_state("x", ("Pro",), [snap(old, "s1", {"Pro": plan(20)})],
                        {"checked_at": NOW.isoformat(), "ok": True}, NOW, stale_after_days=14)
    assert not state.stale
    assert state.plans[0].amount == 20
    assert state.verified_at == NOW


# ---------------------------------------------------------------
# 月次点検の劣化判定
# ---------------------------------------------------------------
def test_degradation_is_reported():
    was = {"a": {"status": "OK", "resolved": 3, "expected": 3, "detail": ""}}
    now = {"a": {"status": "NG", "resolved": 0, "expected": 3, "detail": "x", "missing": []}}
    assert len(compare_checks(was, now)) == 1


def test_steady_partial_is_not_reported():
    """ずっと PARTIAL のものを毎月通知すると読まれなくなる。"""
    same = {"a": {"status": "PARTIAL", "resolved": 2, "expected": 3, "detail": "", "missing": ["c"]}}
    assert compare_checks(same, same) == []


def test_recovery_is_not_reported_as_degradation():
    was = {"a": {"status": "FETCH", "resolved": 0, "expected": 3, "detail": ""}}
    now = {"a": {"status": "OK", "resolved": 3, "expected": 3, "detail": "", "missing": []}}
    assert compare_checks(was, now) == []


def test_newly_added_tool_is_not_degradation():
    now = {"b": {"status": "PARTIAL", "resolved": 1, "expected": 3, "detail": "", "missing": []}}
    assert compare_checks({}, now) == []


def test_unknown_status_does_not_crash():
    """状態の種類は後から増える。古い check.json で落ちないこと。"""
    was = {"a": {"status": "WEIRD", "resolved": 3, "expected": 3, "detail": ""}}
    now = {"a": {"status": "OK", "resolved": 3, "expected": 3, "detail": "", "missing": []}}
    compare_checks(was, now)
