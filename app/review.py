"""实测温度复核。

基于生成方案时的输入快照(plan_input,与 /api/plan 请求体相同)重新规划并
定位指定箱子,以上传的实测温度 CSV(含 time_seconds、temperature 两列)
按该箱装载批次逐批核算:

- CSV 校验:时间从 0 严格递增至全程秒数(运输时长 × 3600),至少两条记录,
  时间与温度均须为有限数值;任何非法记录整单拒绝(由入口统一转为 400)。
- 测温仪器绝对误差(可选,非负 ℃,默认 0):每时刻真实温度视为实测值 ±误差,
  即误差带 [实测-误差, 实测+误差];误差为 0 时退化为仅按实测值核算,
  结果与原复核完全一致。
- 超界区间:相邻采样点之间温度按线性变化,在误差带边界(实测 ±误差)穿越
  批次允许温区边界处切段,分别列出"确定超界"(误差带整体越界)与"可能超界"
  (误差带接触或跨越界外,但未整体越界)的连续起止秒数;恰在边界(带端点
  等于上下限)不算超界。
- 实测度时:对已设首选温度区间的批次,每时刻取误差带到首选区间的最近、
  最远距离,对时间积分(在距离公式改变处切段后梯形精确积分)并除以 3600,
  得到度时下界与上界(℃·h);据此将已设度时预算的批次判为确定超限
  (下界严格大于预算)、可能超限(上界严格大于预算但下界未超)或
  确定未超限(上界未超预算);未设预算的批次不判预算超限。

响应携带输入快照指纹(SHA-256),输入变化后前端据此将旧复核结果标记为已过期。
"""
from __future__ import annotations

import csv
import io
import math
from typing import Any

from .planner import EPS, _r, input_fingerprint, plan

# 末条记录时间与全程秒数的容许相对误差(吸收运输时长 4 位小数规整的换算误差)
_TIME_TOL = 1e-6


def _dist(t: float, lo: float, hi: float) -> float:
    """温度 t 到闭区间 [lo, hi] 的距离(区间内为 0)。"""
    if t < lo:
        return lo - t
    if t > hi:
        return t - hi
    return 0.0


def _band_dist_min(t: float, err: float, lo: float, hi: float) -> float:
    """误差带 [t-err, t+err] 到区间 [lo, hi] 的最近距离(相交为 0)。"""
    return max(0.0, lo - (t + err), (t - err) - hi)


def _band_dist_max(t: float, err: float, lo: float, hi: float) -> float:
    """误差带 [t-err, t+err] 到区间 [lo, hi] 的最远距离(在带端点处取得)。"""
    return max(_dist(t - err, lo, hi), _dist(t + err, lo, hi))


def _parse_csv(csv_text: str, expected_seconds: float) -> list[tuple[float, float]]:
    """解析并校验实测温度 CSV,返回 [(秒, 温度), ...];非法记录抛 ValueError。"""
    if not csv_text or not csv_text.strip():
        raise ValueError(
            "实测温度 CSV 为空,请上传含 time_seconds,temperature 两列的文件"
        )
    reader = csv.reader(io.StringIO(csv_text.lstrip("\ufeff")))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError(
            "实测温度 CSV 为空,请上传含 time_seconds,temperature 两列的文件"
        )
    header = [h.strip().lower() for h in rows[0]]
    if "time_seconds" not in header or "temperature" not in header:
        raise ValueError("CSV 首行须为表头,且包含 time_seconds,temperature 两列")
    ti, vi = header.index("time_seconds"), header.index("temperature")

    samples: list[tuple[float, float]] = []
    for line_no, row in enumerate(rows[1:], start=2):
        if max(ti, vi) >= len(row):
            raise ValueError(
                f"第 {line_no} 行缺少 time_seconds 或 temperature 列,已拒绝"
            )
        raw_t, raw_v = row[ti].strip(), row[vi].strip()
        try:
            t, temp = float(raw_t), float(raw_v)
        except ValueError:
            raise ValueError(
                f"第 {line_no} 行存在非法数值(time_seconds={raw_t!r}, "
                f"temperature={raw_v!r}),已拒绝"
            ) from None
        if not (math.isfinite(t) and math.isfinite(temp)):
            raise ValueError(
                f"第 {line_no} 行的数值须为有限值(time_seconds={raw_t!r}, "
                f"temperature={raw_v!r}),已拒绝"
            )
        samples.append((t, temp))

    if len(samples) < 2:
        raise ValueError("实测温度记录至少需要两条(覆盖全程起点与终点)")
    if abs(samples[0][0]) > 1e-9:
        raise ValueError(
            f"时间必须从 0 秒开始,首条记录为 {_r(samples[0][0])} 秒,已拒绝"
        )
    for i, ((ta, _), (tb, _)) in enumerate(zip(samples, samples[1:])):
        if tb <= ta:
            raise ValueError(
                f"时间必须严格递增:第 {i + 2} 条记录 {_r(tb)} 秒未大于"
                f"前一条 {_r(ta)} 秒,已拒绝"
            )
    last = samples[-1][0]
    if abs(last - expected_seconds) > _TIME_TOL * max(1.0, expected_seconds):
        raise ValueError(
            f"末条记录时间须等于全程秒数 {_r(expected_seconds)} 秒"
            f"(运输时长 × 3600),实际为 {_r(last)} 秒,已拒绝"
        )
    return samples


def _densify(
    samples: list[tuple[float, float]], bounds: tuple[float, ...]
) -> list[tuple[float, float]]:
    """在温度线性穿越任一边界值处插入采样点,返回稠密点列。

    插入后每个小区间内温度线性且不再穿越任何边界,超界判定与距离函数
    在区间内都是线性的,可精确切段与积分。
    """
    pts = [samples[0]]
    for (t0, T0), (t1, T1) in zip(samples, samples[1:]):
        mids: list[tuple[float, float]] = []
        if T0 != T1:
            for b in bounds:
                u = (b - T0) / (T1 - T0)
                if EPS < u < 1.0 - EPS:
                    mids.append((t0 + u * (t1 - t0), b))
        mids.sort()
        pts.extend(mids)
        pts.append((t1, T1))
    return pts


def _band_excursions(
    samples: list[tuple[float, float]], lo: float, hi: float, err: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按误差带 [实测-err, 实测+err] 判定确定/可能超界的连续时间段。

    - 确定超界:误差带整体越界 ⟺ 实测温度 > hi+err 或 < lo-err;
    - 可能超界:误差带接触或跨越界外但未整体越界 ⟺ 实测温度 > hi-err 或
      < lo+err,且不满足确定超界。
    带端点恰等于上下限(边界相等)不算超界。温度在相邻采样点间线性变化,
    在 lo±err、hi±err 四条温度界处切段后,每个小区间只会整体属于某一类;
    连续同类段(中间仅经过恰在界上的点也视为同一段)合并为一个区间输出。
    err=0 时确定超界即实测温度超界,可能超界恒为空,与原复核结果一致。
    返回 (确定超界区间, 可能超界区间)。
    """
    bounds = tuple(sorted({lo - err, lo + err, hi - err, hi + err}))
    pts = _densify(samples, bounds)
    spans: list[tuple[str, list[float]]] = []  # (kind, [start, end, max_T, min_T])
    cur_kind: str | None = None
    cur: list[float] | None = None
    for (t0, T0), (t1, T1) in zip(pts, pts[1:]):
        mid = (T0 + T1) / 2.0
        if mid > hi + err or mid < lo - err:
            kind = "certain"  # 误差带整体越界(严格;恰等不算)
        elif mid > hi - err or mid < lo + err:
            kind = "possible"  # 误差带接触或跨越界外,但未整体越界
        else:
            kind = None
        if kind is None:
            if cur is not None:
                spans.append((cur_kind, cur))
                cur = None
            continue
        seg_hi, seg_lo = max(T0, T1), min(T0, T1)
        if cur is not None and cur_kind == kind:
            cur[1] = t1
            cur[2] = max(cur[2], seg_hi)
            cur[3] = min(cur[3], seg_lo)
        else:
            if cur is not None:
                spans.append((cur_kind, cur))
            cur_kind = kind
            cur = [t0, t1, seg_hi, seg_lo]
    if cur is not None:
        spans.append((cur_kind, cur))

    def fmt_span(s: float, e: float, mx: float, mn: float) -> dict[str, Any]:
        # side 按误差带越界方向判定;err=0 时退化为实测温度越界方向
        band_lo, band_hi = mn - err, mx + err
        return {
            "start_s": _r(s),
            "end_s": _r(e),
            "duration_s": _r(e - s),
            "min_temperature": _r(mn),
            "max_temperature": _r(mx),
            "side": (
                "both"
                if band_lo < lo and band_hi > hi
                else "low"
                if band_lo < lo
                else "high"
            ),
        }

    certain = [fmt_span(*span) for kind, span in spans if kind == "certain"]
    possible = [fmt_span(*span) for kind, span in spans if kind == "possible"]
    return certain, possible


def _degree_hours_band(
    samples: list[tuple[float, float]], pref_lo: float, pref_hi: float, err: float
) -> tuple[float, float, float]:
    """实测度时(名义值)与误差带度时下界、上界(℃·h),对时间积分 ÷ 3600。

    每时刻真实温度落在误差带 [T-err, T+err] 内:
    - 下界距离 = 误差带到首选区间的最近距离(带与区间相交时为 0);
    - 上界距离 = 误差带到首选区间的最远距离(在带端点处取得)。
    两者都是实测温度 T 的分段线性函数,距离公式改变处(转折点)只可能出现在
    pref_lo±err、pref_hi±err、pref_lo、pref_hi 及首选区间中点(误差带整体
    覆盖首选区间时,最远端点在带两端切换)这些温度界上;在这些界处切段后,
    每小段内三种距离关于时间都是线性的,梯形求和即为精确积分
    (不以采样点是否超界或平均温度代替)。err=0 时下界=上界=名义值,
    与原复核结果一致。返回 (名义值, 下界, 上界)。
    """
    bounds = tuple(
        sorted(
            {
                pref_lo - err,
                pref_lo,
                pref_lo + err,
                pref_hi - err,
                pref_hi,
                pref_hi + err,
                (pref_lo + pref_hi) / 2.0,
            }
        )
    )
    pts = _densify(samples, bounds)
    nom_s = lo_s = hi_s = 0.0
    for (t0, T0), (t1, T1) in zip(pts, pts[1:]):
        dt = t1 - t0
        nom_s += (_dist(T0, pref_lo, pref_hi) + _dist(T1, pref_lo, pref_hi)) / 2.0 * dt
        lo_s += (
            _band_dist_min(T0, err, pref_lo, pref_hi)
            + _band_dist_min(T1, err, pref_lo, pref_hi)
        ) / 2.0 * dt
        hi_s += (
            _band_dist_max(T0, err, pref_lo, pref_hi)
            + _band_dist_max(T1, err, pref_lo, pref_hi)
        ) / 2.0 * dt
    return nom_s / 3600.0, lo_s / 3600.0, hi_s / 3600.0


def review(payload: dict[str, Any]) -> dict[str, Any]:
    """实测温度复核入口;输入不合法时抛 ValueError(与 plan 一致)。"""
    plan_input = payload.get("plan_input") or {}
    box_no = payload.get("box_no")
    csv_text = payload.get("csv") or ""
    if box_no is None:
        raise ValueError("请指定要复核的箱号 box_no")
    box_no = int(box_no)

    # 测温仪器绝对误差(可选,非负 ℃,默认 0):真实温度视为实测值 ±误差
    err_raw = payload.get("instrument_error", 0)
    if err_raw in (None, ""):
        err_raw = 0
    try:
        err = float(err_raw)
    except (TypeError, ValueError):
        raise ValueError(
            "测温仪器绝对误差 instrument_error 须为非负数值(℃)"
        ) from None
    if not math.isfinite(err) or err < 0:
        raise ValueError("测温仪器绝对误差 instrument_error 须为非负有限数值(℃)")

    # 复核绑定生成方案时的输入快照:按快照重新规划并定位箱子
    result = plan(plan_input)
    transport_hours = float(result["summary"]["transport_hours"])
    expected_s = transport_hours * 3600.0
    if expected_s <= 0:
        raise ValueError("运输时长为 0,无法进行实测温度复核")

    boxes = result["boxes"]
    box = next((b for b in boxes if b["box_no"] == box_no), None)
    if box is None:
        raise ValueError(
            f"方案中不存在第 {box_no} 箱(本次方案共 {len(boxes)} 箱),"
            f"请重新生成方案后选择有效箱号"
        )

    samples = _parse_csv(csv_text, expected_s)

    cargoes = plan_input.get("cargoes") or []
    # 箱内装载批次(按批号去重并汇总件数)
    units_by_no: dict[int, int] = {}
    for it in box["items"]:
        units_by_no[it["batch_no"]] = units_by_no.get(it["batch_no"], 0) + it["units"]

    batches_out: list[dict[str, Any]] = []
    for no in sorted(units_by_no):
        c = cargoes[no - 1]
        name = str(c.get("name") or f"批次 {no}")
        lo, hi = float(c["t_min"]), float(c["t_max"])
        p_lo, p_hi = c.get("preferred_t_min"), c.get("preferred_t_max")
        has_pref = p_lo not in (None, "") and p_hi not in (None, "")
        mb = c.get("max_degree_hours")
        budget = None if mb in (None, "") else float(mb)

        # 确定超界(误差带整体越界)与可能超界(误差带接触或跨越界外)
        certain, possible = _band_excursions(samples, lo, hi, err)
        nominal = lower = upper = None
        if has_pref:
            nominal, lower, upper = _degree_hours_band(
                samples, float(p_lo), float(p_hi), err
            )
        exceeded: bool | None = None
        remaining: float | None = None
        budget_status: str | None = None
        if budget is not None and nominal is not None:
            # 名义判定(兼容字段):实测值度时严格大于预算才判超限
            exceeded = nominal > budget + EPS
            remaining = budget - nominal
            # 误差带判定:下界严格大于预算→确定超限;上界未超预算→确定未超限;
            # 其余(上界超、下界未超)→可能超限;未设预算的批次不判
            if lower > budget + EPS:
                budget_status = "exceeded"
            elif upper <= budget + EPS:
                budget_status = "within"
            else:
                budget_status = "possible"

        batches_out.append(
            {
                "batch_no": no,
                "name": name,
                "units": units_by_no[no],
                "allowed_t_min": _r(lo),
                "allowed_t_max": _r(hi),
                # 误差带全程未接触界外才算确定在界内;err=0 时与原判定一致
                "within_limits": not certain and not possible,
                "excursion_count": len(certain),
                "excursions": certain,
                "possible_excursion_count": len(possible),
                "possible_excursions": possible,
                "preferred_t_min": _r(float(p_lo)) if has_pref else None,
                "preferred_t_max": _r(float(p_hi)) if has_pref else None,
                "measured_degree_hours": (
                    _r(nominal) if nominal is not None else None
                ),
                "degree_hours_lower": _r(lower) if lower is not None else None,
                "degree_hours_upper": _r(upper) if upper is not None else None,
                "max_degree_hours": _r(budget) if budget is not None else None,
                "remaining_budget": (
                    _r(remaining) if remaining is not None else None
                ),
                "budget_exceeded": exceeded,
                "budget_status": budget_status,
            }
        )

    return {
        "input_fingerprint": input_fingerprint(plan_input),
        "box_no": box_no,
        "instrument_error": _r(err),
        "transport_hours": _r(transport_hours),
        "duration_seconds": _r(expected_s),
        "sample_count": len(samples),
        "batches": batches_out,
        "summary": {
            "batch_count": len(batches_out),
            "batches_with_excursions": sum(
                1 for b in batches_out if b["excursion_count"]
            ),
            "total_excursions": sum(b["excursion_count"] for b in batches_out),
            "batches_with_possible_excursions": sum(
                1 for b in batches_out if b["possible_excursion_count"]
            ),
            "total_possible_excursions": sum(
                b["possible_excursion_count"] for b in batches_out
            ),
            "budget_checked_count": sum(
                1 for b in batches_out if b["budget_status"] is not None
            ),
            "budget_exceeded_count": sum(
                1 for b in batches_out if b["budget_status"] == "exceeded"
            ),
            "budget_possible_count": sum(
                1 for b in batches_out if b["budget_status"] == "possible"
            ),
            "all_within_limits": all(b["within_limits"] for b in batches_out),
        },
    }
