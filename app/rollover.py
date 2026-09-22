"""周期结转额度试算：多策略的跨期结转与链上原子扣减。

每个策略把时间轴划分为固定长度的周期：周期 k 为半开区间
[anchor + k·period_seconds, anchor + (k+1)·period_seconds)。每个周期持有两类余额：

- 本期基础额度 base：周期开始时重置为 allowance；
- 结转余额 carry：上一周期未用完的基础额度结转到本周期，封顶 carry_cap，
  只在本周期内有效——跨期时旧结转额失效，由新的上期未用基础额度替代。

请求触及策略时先把策略惰性推进到请求所在周期（与 /simulate 的连续补充类似，
无论放行与否时间都会推进）：跨 1 个周期时 carry = min(carry_cap, 上期剩余 base)；
跨过 ≥1 个完整未触及周期时，中间各期基础额度未被消耗，
carry = min(carry_cap, allowance)。随后仅当请求引用的全部策略都满足
carry + base >= cost 时才原子扣减——先扣即将失效的结转额，再扣本期基础额度；
任一策略不足则全部策略不扣减，返回引用顺序上首个不足的策略。
"""
from __future__ import annotations

import math

from app.models import (
    EPS,
    RolloverPolicyFinalState,
    RolloverPolicyView,
    RolloverRejection,
    RolloverSimResult,
    RolloverSimulateRequest,
    RolloverSimulateResponse,
)
from app.simulation import _round


def rollover_simulate(payload: RolloverSimulateRequest) -> RolloverSimulateResponse:
    requests = payload.requests

    anchor = {p.id: float(p.anchor) for p in payload.policies}
    period_seconds = {p.id: float(p.period_seconds) for p in payload.policies}
    allowance = {p.id: float(p.allowance) for p in payload.policies}
    carry_cap = {p.id: float(p.carry_cap) for p in payload.policies}
    policy_ids = [p.id for p in payload.policies]

    def period_of(pid: str, now: float) -> int:
        """时刻 now 所在的周期编号；恰好在周期边界上的时刻属于右侧新周期。

        EPS 吸附浮点毛刺，保证 anchor + k·period_seconds 计算出的边界
        不会因二进制误差落回上一周期。
        """
        return math.floor((now - anchor[pid]) / period_seconds[pid] + EPS)

    # 模拟开始时刻 = 最早请求时间戳：各策略在该时刻所在周期持有满额基础额度、
    # 无结转（模拟视野内不存在更早的周期）。之后按请求触及惰性推进。
    t_start = min(request.timestamp for request in requests)
    t_end = max(request.timestamp for request in requests)

    # state[pid] = {"period": 当前周期编号, "carry": 结转余额, "base": 本期基础余额}
    state: dict[str, dict[str, float]] = {
        pid: {
            "period": period_of(pid, float(t_start)),
            "carry": 0.0,
            "base": allowance[pid],
        }
        for pid in policy_ids
    }

    def advance(pid: str, now: float) -> dict[str, float]:
        """把某个策略推进到时刻 now 所在的周期，返回推进后的状态。"""
        st = state[pid]
        period = period_of(pid, now)
        if period <= st["period"]:
            # 同一周期（时间非降，不会回退）：无需结转
            return st
        if period - st["period"] == 1:
            # 相邻周期：上期未用完的基础额度结转，封顶 carry_cap；旧结转失效
            st["carry"] = min(carry_cap[pid], st["base"])
        else:
            # 跨过 ≥1 个完整未触及周期：中间各期基础额度未被消耗，
            # 等价于最后一个完整周期满额结转
            st["carry"] = min(carry_cap[pid], allowance[pid])
        st["base"] = allowance[pid]
        st["period"] = period
        return st

    def period_bounds(pid: str, period: int) -> tuple[float, float]:
        start = anchor[pid] + period * period_seconds[pid]
        return _round(start), _round(start + period_seconds[pid])

    # 内部按时间顺序处理；同一时刻按原始输入顺序处理
    order = sorted(range(len(requests)), key=lambda i: (requests[i].timestamp, i))

    results_by_index: dict[int, RolloverSimResult] = {}

    for i in order:
        request = requests[i]
        now = float(request.timestamp)
        cost = float(request.cost)

        # 先把引用策略推进到请求所在周期（无论最终是否放行，时间都会推进），
        # 推进后的两类余额即判定基准
        for pid in request.policy_refs:
            advance(pid, now)

        # 引用顺序上首个两类余额合计不足的策略即拒绝策略
        rejected_pid: str | None = None
        for pid in request.policy_refs:
            st = state[pid]
            if st["carry"] + st["base"] + EPS < cost:
                rejected_pid = pid
                break

        policy_views: dict[str, RolloverPolicyView] = {}
        for pid in request.policy_refs:
            st = state[pid]
            carry_before = st["carry"]
            base_before = st["base"]
            if rejected_pid is None:
                # 先扣即将失效的结转额，再扣本期基础额度
                take_carry = min(carry_before, cost)
                st["carry"] = carry_before - take_carry
                st["base"] = base_before - (cost - take_carry)
            # 原子性：拒绝时所有策略保持不变（after == before）
            period_start, period_end = period_bounds(pid, int(st["period"]))
            policy_views[pid] = RolloverPolicyView(
                id=pid,
                allowance=_round(allowance[pid]),
                carry_cap=_round(carry_cap[pid]),
                period=int(st["period"]),
                period_start=period_start,
                period_end=period_end,
                carry_before=_round(carry_before),
                carry_after=_round(st["carry"]),
                base_before=_round(base_before),
                base_after=_round(st["base"]),
            )

        rejection: RolloverRejection | None = None
        if rejected_pid is not None:
            st = state[rejected_pid]
            rejection = RolloverRejection(
                policy_id=rejected_pid,
                reason="insufficient_balance",
                period=int(st["period"]),
                cost=_round(cost),
                carry=_round(st["carry"]),
                base=_round(st["base"]),
                available=_round(st["carry"] + st["base"]),
            )

        results_by_index[i] = RolloverSimResult(
            request_id=request.id,
            index=i,
            timestamp=_round(now),
            cost=_round(cost),
            allowed=rejected_pid is None,
            policies=policy_views,
            rejected_by=rejected_pid,
            rejection=rejection,
        )

    results = [results_by_index[i] for i in range(len(requests))]
    allowed_count = sum(1 for r in results if r.allowed)

    # 终态：把全部策略（含从未被引用的）推进到最后一个请求时刻
    final_states: list[RolloverPolicyFinalState] = []
    for pid in policy_ids:
        st = advance(pid, float(t_end))
        period_start, period_end = period_bounds(pid, int(st["period"]))
        final_states.append(
            RolloverPolicyFinalState(
                id=pid,
                allowance=_round(allowance[pid]),
                carry_cap=_round(carry_cap[pid]),
                period=int(st["period"]),
                period_start=period_start,
                period_end=period_end,
                carry=_round(st["carry"]),
                base=_round(st["base"]),
                total=_round(st["carry"] + st["base"]),
            )
        )

    return RolloverSimulateResponse(
        allowed_count=allowed_count,
        rejected_count=len(results) - allowed_count,
        results=results,
        policies=final_states,
    )
