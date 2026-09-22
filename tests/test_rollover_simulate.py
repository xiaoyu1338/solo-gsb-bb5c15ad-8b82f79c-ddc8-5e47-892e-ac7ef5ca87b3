"""周期结转额度试算（/rollover-simulate）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import RolloverSimulateRequest
from app.rollover import rollover_simulate

client = TestClient(app)


def _run(policies, requests):
    return rollover_simulate(
        RolloverSimulateRequest.model_validate({"policies": policies, "requests": requests})
    )


def _view(result, policy_id):
    return result.policies[policy_id]


# ---------- 基本放行 / 扣减 ----------

def test_first_request_consumes_base_only():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 100, "carry_cap": 30}],
        [{"id": "r1", "timestamp": 1, "cost": 40, "policy_refs": ["P"]}],
    )
    r = resp.results[0]
    assert r.allowed is True
    assert r.rejected_by is None and r.rejection is None
    v = _view(r, "P")
    assert v.period == 0
    assert (v.period_start, v.period_end) == (0, 10)
    assert (v.carry_before, v.carry_after) == (0, 0)
    assert (v.base_before, v.base_after) == (100, 60)
    p = resp.policies[0]
    assert (p.period, p.carry, p.base, p.total) == (0, 0, 60, 60)
    assert resp.allowed_count == 1 and resp.rejected_count == 0


def test_carry_consumed_before_base():
    # 跨期后先扣即将失效的结转额，再扣本期基础额度
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 100, "carry_cap": 30}],
        [
            {"id": "r1", "timestamp": 1, "cost": 40, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 11, "cost": 20, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert v.period == 1
    # 上期未用 60，结转封顶 30；cost 20 全部从结转扣
    assert (v.carry_before, v.carry_after) == (30, 10)
    assert (v.base_before, v.base_after) == (100, 100)


def test_carry_then_base_when_carry_insufficient():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 100, "carry_cap": 30}],
        [
            {"id": "r1", "timestamp": 1, "cost": 40, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 11, "cost": 50, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    # 结转 30 全部扣完，剩余 20 从本期基础额度扣
    assert (v.carry_before, v.carry_after) == (30, 0)
    assert (v.base_before, v.base_after) == (100, 80)


# ---------- 跨期结转 ----------

def test_unused_base_carries_over_capped():
    # 上期未用 60 > carry_cap 25：只结转 25
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 100, "carry_cap": 25}],
        [
            {"id": "r1", "timestamp": 1, "cost": 40, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert v.period == 1
    assert v.carry_before == 25
    assert v.base_before == 100


def test_old_carry_expires_on_rollover():
    # 跨期时旧结转额失效：只结转上一期的未用基础额度，不叠加
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 100, "carry_cap": 30}],
        [
            {"id": "r1", "timestamp": 1, "cost": 40, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 11, "cost": 10, "policy_refs": ["P"]},
            {"id": "r3", "timestamp": 21, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    # r2：carry 30 扣 10 余 20，base 100
    # r3 进入周期 2：旧结转 20 失效，结转 = min(30, 上期 base 100) = 30
    v = _view(resp.results[2], "P")
    assert v.period == 2
    assert (v.carry_before, v.base_before) == (30, 100)


def test_fully_used_period_carries_nothing():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 1, "cost": 10, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 11, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert (v.carry_before, v.base_before) == (0, 10)


def test_zero_carry_cap_means_no_rollover():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 100, "carry_cap": 0}],
        [
            {"id": "r1", "timestamp": 1, "cost": 10, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 11, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert (v.carry_before, v.base_before) == (0, 100)


def test_multi_period_gap_carries_full_allowance_capped():
    # 跨过 ≥1 个完整未触及周期：中间各期基础额度未被消耗，
    # 结转 = min(carry_cap, allowance)
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 100, "carry_cap": 30}],
        [
            {"id": "r1", "timestamp": 1, "cost": 95, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 45, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert v.period == 4
    # 上期（周期 0）只剩 5，但跨过了完整的周期 1/2/3 → 按满额 100 结转，封顶 30
    assert (v.carry_before, v.base_before) == (30, 100)


def test_request_exactly_on_period_boundary_belongs_to_new_period():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 8, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 10, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    v = _view(resp.results[1], "P")
    assert v.period == 1
    assert (v.period_start, v.period_end) == (10, 20)
    assert (v.carry_before, v.base_before) == (2, 10)


def test_anchor_offsets_period_numbering():
    resp = _run(
        [{"id": "P", "anchor": 100, "period_seconds": 7, "allowance": 10, "carry_cap": 5}],
        [{"id": "r1", "timestamp": 115, "cost": 1, "policy_refs": ["P"]}],
    )
    v = _view(resp.results[0], "P")
    assert v.period == 2
    assert (v.period_start, v.period_end) == (114, 121)


def test_per_policy_independent_periods():
    resp = _run(
        [
            {"id": "A", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5},
            {"id": "B", "anchor": 3, "period_seconds": 4, "allowance": 10, "carry_cap": 5},
        ],
        [{"id": "r1", "timestamp": 11, "cost": 1, "policy_refs": ["A", "B"]}],
    )
    assert _view(resp.results[0], "A").period == 1
    assert _view(resp.results[0], "B").period == 2
    assert (_view(resp.results[0], "B").period_start,
            _view(resp.results[0], "B").period_end) == (11, 15)


# ---------- 原子性 ----------

def test_atomic_rejection_changes_nothing():
    resp = _run(
        [
            {"id": "P1", "anchor": 0, "period_seconds": 10, "allowance": 100, "carry_cap": 30},
            {"id": "P2", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5},
        ],
        [
            {"id": "r1", "timestamp": 1, "cost": 9, "policy_refs": ["P1", "P2"]},
            {"id": "r2", "timestamp": 2, "cost": 2, "policy_refs": ["P1", "P2"]},
        ],
    )
    assert resp.results[0].allowed is True
    r2 = resp.results[1]
    assert r2.allowed is False
    assert r2.rejected_by == "P2"
    # 两个策略都保持 r1 之后的状态
    assert (_view(r2, "P1").base_after, _view(r2, "P1").carry_after) == (91, 0)
    assert (_view(r2, "P2").base_after, _view(r2, "P2").carry_after) == (1, 0)
    states = {p.id: p for p in resp.policies}
    assert states["P1"].base == 91
    assert states["P2"].base == 1


def test_first_insufficient_policy_follows_refs_order():
    # P2 先不足、P1 后不足：按 policy_refs 顺序应报 P2
    resp = _run(
        [
            {"id": "P1", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 0},
            {"id": "P2", "anchor": 0, "period_seconds": 10, "allowance": 3, "carry_cap": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 5, "policy_refs": ["P1", "P2"]}],
    )
    r = resp.results[0]
    assert r.allowed is False
    assert r.rejected_by == "P2"
    assert r.rejection.policy_id == "P2"
    assert r.rejection.reason == "insufficient_balance"
    assert (r.rejection.carry, r.rejection.base, r.rejection.available) == (0, 3, 3)
    assert r.rejection.cost == 5
    # 原子：P1 也未扣减
    assert _view(r, "P1").base_after == 10


def test_rejection_still_rolls_period_forward():
    # 拒绝不扣减，但时间推进（跨期结转）仍然生效，before 为推进后的余额
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 4}],
        [
            {"id": "r1", "timestamp": 1, "cost": 9, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 11, "cost": 20, "policy_refs": ["P"]},
            {"id": "r3", "timestamp": 12, "cost": 11, "policy_refs": ["P"]},
        ],
    )
    r2 = resp.results[1]
    assert r2.allowed is False
    v2 = _view(r2, "P")
    assert v2.period == 1
    # 推进后 carry = min(4, 1) = 1、base = 10，合计 11 < 20 → 拒绝且不扣
    assert (v2.carry_before, v2.carry_after) == (1, 1)
    assert (v2.base_before, v2.base_after) == (10, 10)
    # r3 在同一周期：余额合计 11，cost 11 恰好放行
    assert resp.results[2].allowed is True


def test_cost_equal_to_total_balance_is_allowed():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 1, "cost": 8, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 11, "cost": 12, "policy_refs": ["P"]},
        ],
    )
    # 周期 1：carry 2 + base 10 = 12，恰好够
    assert resp.results[1].allowed is True
    v = _view(resp.results[1], "P")
    assert (v.carry_after, v.base_after) == (0, 0)


def test_cost_exceeding_allowance_alone_is_rejected():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 4, "carry_cap": 0}],
        [{"id": "r1", "timestamp": 0, "cost": 5, "policy_refs": ["P"]}],
    )
    assert resp.results[0].allowed is False
    assert resp.results[0].rejected_by == "P"
    assert resp.policies[0].total == 4


# ---------- 排序与终态 ----------

def test_unsorted_input_processed_chronologically_results_keep_order():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 100, "allowance": 1, "carry_cap": 0}],
        [
            {"id": "late", "timestamp": 5, "cost": 1, "policy_refs": ["P"]},
            {"id": "early", "timestamp": 0, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.request_id for r in resp.results] == ["late", "early"]
    assert [r.index for r in resp.results] == [0, 1]
    assert resp.results[0].allowed is False  # 实际先扣的是 t=0 的 early
    assert resp.results[1].allowed is True


def test_same_timestamp_keeps_input_order():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 100, "allowance": 1, "carry_cap": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 0, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    assert [r.allowed for r in resp.results] == [True, False]


def test_final_state_observed_at_last_request_timestamp():
    resp = _run(
        [{"id": "P", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5}],
        [
            {"id": "r1", "timestamp": 0, "cost": 3, "policy_refs": ["P"]},
            {"id": "r2", "timestamp": 25, "cost": 1, "policy_refs": ["P"]},
        ],
    )
    p = resp.policies[0]
    # 终态 t=25：周期 2 [20,30)；从周期 0 跨一个完整周期推进，
    # 按满额 10 结转封顶 5，r2 的 cost 1 从结转扣 → carry 4、base 10
    assert (p.period, p.period_start, p.period_end) == (2, 20, 30)
    assert (p.carry, p.base, p.total) == (4, 10, 14)


def test_unreferenced_policy_advanced_in_final_state():
    resp = _run(
        [
            {"id": "used", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 5},
            {"id": "idle", "anchor": 0, "period_seconds": 10, "allowance": 7, "carry_cap": 3},
        ],
        [
            {"id": "r1", "timestamp": 5, "cost": 1, "policy_refs": ["used"]},
            {"id": "r2", "timestamp": 45, "cost": 1, "policy_refs": ["used"]},
        ],
    )
    states = {p.id: p for p in resp.policies}
    # 从未被引用的策略也推进到最后请求时刻所在周期：中间周期未消耗，满额 7 结转封顶 3
    assert states["idle"].period == 4
    assert (states["idle"].carry, states["idle"].base) == (3, 7)


def test_policy_view_only_includes_referenced_policies():
    resp = _run(
        [
            {"id": "P1", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 0},
            {"id": "P2", "anchor": 0, "period_seconds": 10, "allowance": 10, "carry_cap": 0},
        ],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "policy_refs": ["P1"]},
            {"id": "r2", "timestamp": 1, "cost": 1, "policy_refs": ["P1", "P2"]},
        ],
    )
    assert set(resp.results[0].policies) == {"P1"}
    assert list(resp.results[1].policies) == ["P1", "P2"]


# ---------- Pydantic / 跨字段校验 ----------

def _validate_error(payload):
    with pytest.raises(RequestValidationErrors) as exc:
        RolloverSimulateRequest.model_validate(payload)
    return exc.value.errors


def test_duplicate_policy_ids_rejected():
    errors = _validate_error(
        {
            "policies": [
                {"id": "P", "anchor": 0, "period_seconds": 1, "allowance": 1, "carry_cap": 0},
                {"id": "P", "anchor": 0, "period_seconds": 2, "allowance": 2, "carry_cap": 1},
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["P"]}
            ],
        }
    )
    assert all(e["type"] == "duplicate_policy" for e in errors)


def test_unknown_policy_reference_rejected():
    errors = _validate_error(
        {
            "policies": [
                {"id": "P", "anchor": 0, "period_seconds": 1, "allowance": 1, "carry_cap": 0}
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["P", "X"]}
            ],
        }
    )
    assert any(
        e["type"] == "unknown_policy_ref" and e["input"] == "X" for e in errors
    )


def test_duplicate_request_ids_rejected():
    errors = _validate_error(
        {
            "policies": [
                {"id": "P", "anchor": 0, "period_seconds": 1, "allowance": 1, "carry_cap": 0}
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["P"]},
                {"id": "r", "timestamp": 1, "cost": 1, "policy_refs": ["P"]},
            ],
        }
    )
    assert any(e["type"] == "duplicate_request" for e in errors)


def test_bad_policy_fields_rejected_by_pydantic():
    from pydantic import ValidationError

    def validate(policy):
        RolloverSimulateRequest.model_validate(
            {
                "policies": [policy],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["P"]}
                ],
            }
        )

    base = {"id": "P", "anchor": 0, "period_seconds": 1, "allowance": 1, "carry_cap": 0}
    for bad in (
        {**base, "period_seconds": 0},
        {**base, "period_seconds": -1},
        {**base, "period_seconds": "nan"},
        {**base, "allowance": 0},
        {**base, "allowance": -2},
        {**base, "carry_cap": -1},
        {**base, "anchor": "inf"},
        {**base, "extra_field": 1},
    ):
        with pytest.raises(ValidationError):
            validate(bad)


def test_bad_request_fields_rejected_by_pydantic():
    from pydantic import ValidationError

    base_policy = [
        {"id": "P", "anchor": 0, "period_seconds": 1, "allowance": 1, "carry_cap": 0}
    ]

    def validate(req):
        RolloverSimulateRequest.model_validate(
            {"policies": base_policy, "requests": [req]}
        )

    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": 0, "cost": 0, "policy_refs": ["P"]})
    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": 0, "cost": 1, "policy_refs": []})
    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["P", "P"]})
    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": "nan", "cost": 1, "policy_refs": ["P"]})
    with pytest.raises(ValidationError):
        validate({"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["P"],
                  "unexpected": 1})


def test_empty_policies_or_requests_rejected_by_pydantic():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RolloverSimulateRequest.model_validate({"policies": [], "requests": []})


# ---------- HTTP ----------

def test_http_rollover_simulate_ok():
    response = client.post(
        "/rollover-simulate",
        json={
            "policies": [
                {"id": "plan_month", "anchor": 0, "period_seconds": 30,
                 "allowance": 100, "carry_cap": 40},
                {"id": "plan_day", "anchor": 0, "period_seconds": 10,
                 "allowance": 15, "carry_cap": 5},
            ],
            "requests": [
                {"id": "r1", "timestamp": 2, "cost": 12,
                 "policy_refs": ["plan_month", "plan_day"]},
                {"id": "r2", "timestamp": 12, "cost": 10,
                 "policy_refs": ["plan_month", "plan_day"]},
                {"id": "r3", "timestamp": 25, "cost": 8, "policy_refs": ["plan_day"]},
                {"id": "r4", "timestamp": 31, "cost": 20,
                 "policy_refs": ["plan_month", "plan_day"]},
                {"id": "r5", "timestamp": 35, "cost": 25,
                 "policy_refs": ["plan_month", "plan_day"]},
                {"id": "r6", "timestamp": 95, "cost": 30, "policy_refs": ["plan_month"]},
            ],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert [r["allowed"] for r in data["results"]] == [True, True, True, True, False, True]
    assert (data["allowed_count"], data["rejected_count"]) == (5, 1)
    # r2：plan_day 跨期，上期未用 3 全部结转（封顶 5），先扣结转再扣本期
    day_r2 = data["results"][1]["policies"]["plan_day"]
    assert day_r2["period"] == 1
    assert (day_r2["carry_before"], day_r2["carry_after"]) == (3.0, 0.0)
    assert (day_r2["base_before"], day_r2["base_after"]) == (15.0, 8.0)
    # r4：plan_month 结转 40 封顶，cost 20 全部从结转扣，本期额度不动
    month_r4 = data["results"][3]["policies"]["plan_month"]
    assert (month_r4["carry_before"], month_r4["carry_after"]) == (40.0, 20.0)
    assert (month_r4["base_before"], month_r4["base_after"]) == (100.0, 100.0)
    # r5：plan_day 两类余额合计 0 不足 → 原子拒绝，plan_month 不扣
    r5 = data["results"][4]
    assert r5["rejected_by"] == "plan_day"
    assert r5["rejection"]["reason"] == "insufficient_balance"
    assert r5["rejection"]["available"] == 0.0
    assert r5["policies"]["plan_month"]["carry_after"] == 20.0
    # r6：跨过完整周期，plan_month 按满额 100 结转封顶 40
    month_r6 = data["results"][5]["policies"]["plan_month"]
    assert month_r6["period"] == 3
    assert (month_r6["carry_before"], month_r6["carry_after"]) == (40.0, 10.0)
    # 终态：t=95，plan_day 推进到周期 9，满额 15 结转封顶 5
    states = {p["id"]: p for p in data["policies"]}
    assert states["plan_month"]["total"] == 110.0
    assert (states["plan_day"]["period"], states["plan_day"]["carry"],
            states["plan_day"]["base"]) == (9, 5.0, 15.0)


def test_http_validation_error_is_422():
    response = client.post(
        "/rollover-simulate",
        json={
            "policies": [
                {"id": "P", "anchor": 0, "period_seconds": 1, "allowance": 1, "carry_cap": 0}
            ],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "policy_refs": ["GHOST"]}
            ],
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "unknown_policy_ref" for e in detail)


def test_http_root_lists_rollover_simulate():
    root = client.get("/").json()
    assert root["rollover_simulate"] == "/rollover-simulate"
