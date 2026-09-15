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
from src.collect import compare_checks, lost_plans
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
    page_changed,
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


def test_plan_unreadable_with_same_prices_is_not_published_as_delisted():
    """価格集合が同じなのにプランが消えた = こちらが読めなくなっただけ。"""
    a = snap("2026-01-01T00:00:00+00:00", "same", {"Pro": plan(17), "Team": plan(20)})
    b = snap("2026-02-01T00:00:00+00:00", "same", {"Pro": plan(17)})
    assert diff(a, b) == []


def test_reattribution_within_same_prices_is_recorded_but_not_published():
    """同じ金額の集合の中で別の金額に紐付いただけ。ベンダーは値段を動かしていない。"""
    a = snap("2026-01-01T00:00:00+00:00", "same", {"Team": plan(20)})
    b = snap("2026-02-01T00:00:00+00:00", "same", {"Team": plan(25)})
    assert should_record(a, b)
    assert diff(a, b) == []


def test_page_change_names_the_amounts_that_moved():
    """Claude(9/2)と Surfer SEO(9/9)は、中身の無い "page edited" としか書けなかった。"""
    a = Snapshot(ts="2026-01-01T00:00:00+00:00", slug="x", ok=True, signature="s1",
                 plans={}, tokens=("$17", "$20", "$29"))
    b = Snapshot(ts="2026-02-01T00:00:00+00:00", slug="x", ok=True, signature="s2",
                 plans={}, tokens=("$17", "$20", "$39"))
    (change,) = diff(a, b)
    assert change.kind == KIND_PAGE
    assert (change.appeared, change.disappeared) == (("$39",), ("$29",))
    text = headline_for(change, TOOL)
    assert "$39" in text and "$29" in text
    assert "raised" not in text.lower() and "cut" not in text.lower()


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


def test_first_token_list_is_a_baseline_not_a_change():
    """価格表記の一覧を持たない古い記録には基準を1回足す。変更としては公開しない。"""
    a = snap("2026-01-01T00:00:00+00:00", "same", {"Pro": plan(20)})
    b = Snapshot(ts="2026-02-01T00:00:00+00:00", slug="x", ok=True, signature="same",
                 plans={"Pro": plan(20)}, tokens=("$20",))
    assert should_record(a, b)
    assert not page_changed(a, b)
    assert diff(a, b) == []


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
# プラン単位の確認日 — 読めなかったプランを「確認済み」と出さない
# ---------------------------------------------------------------
def test_plan_not_read_on_latest_check_is_not_shown_as_verified():
    """Claude の Team が 2026-09-04 から11日間、読めていないのに Verified と出ていた。"""
    recorded = (NOW - timedelta(days=5)).isoformat()
    last_read = (NOW - timedelta(days=3)).isoformat()
    snaps = [snap(recorded, "s1", {"Pro": plan(17), "Team": plan(20)})]
    latest = {"checked_at": NOW.isoformat(), "ok": True,
              "plans_seen": {"Pro": NOW.isoformat(), "Team": last_read}}
    state = build_state("x", ("Pro", "Team"), snaps, latest, NOW, stale_after_days=14)
    pro, team = state.plans
    assert state.is_current(pro)
    assert not state.is_current(team)
    assert team.amount == 20
    assert team.verified_at == datetime.fromisoformat(last_read)
    assert [p.plan for p in state.unconfirmed_plans] == ["Team"]
    assert [p.plan for p in state.current_plans] == ["Pro"]


def test_plan_unread_past_the_stale_period_is_hidden():
    """読めない日が続いたプランの古い価格を、現在価格として出し続けない。"""
    old = (NOW - timedelta(days=20)).isoformat()
    snaps = [snap(old, "s1", {"Pro": plan(17), "Team": plan(20)})]
    latest = {"checked_at": NOW.isoformat(), "ok": True,
              "plans_seen": {"Pro": NOW.isoformat(), "Team": old}}
    state = build_state("x", ("Pro", "Team"), snaps, latest, NOW, stale_after_days=14)
    assert not state.stale
    assert state.plans[0].amount == 17
    assert state.plans[1].amount is None


def test_plan_that_stopped_being_readable_is_reported_once():
    """読めなくなった日に1回だけ知らせる。直すまで毎日 Issue に追記しない。"""
    day1, day2 = "2026-09-03T12:00:00+00:00", "2026-09-04T12:00:00+00:00"
    before = {"checked_at": day1, "ok": True, "plans_seen": {"Pro": day1, "Team": day1}}
    assert lost_plans(before, {"Pro"}) == ["Team"]
    next_day = {"checked_at": day2, "ok": True, "plans_seen": {"Pro": day2, "Team": day1}}
    assert lost_plans(next_day, {"Pro"}) == []


def test_no_baseline_means_no_lost_report():
    """plans_seen を導入する前の latest.json では何も言わない。"""
    assert lost_plans({"checked_at": "2026-09-03T12:00:00+00:00", "ok": True}, set()) == []


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
