"""实测温度复核。

基于生成方案时的输入快照(plan_input,与 /api/plan 请求体相同)重新规划并
定位指定箱子,以上传的实测温度 CSV(含 time_seconds、temperature 两列)
按该箱装载批次逐批核算:

- CSV 校验:时间从 0 严格递增至全程秒数(运输时长 × 3600),至少两条记录,
  时间与温度均须为有限数值;任何非法记录整单拒绝(由入口统一转为 400)。
- 测温仪器绝对误差 instrument_error(℃,非负,默认 0):每时刻的真实温度
  视为实测值 ± 误差构成的闭区间(误差带)。相邻采样点间实测温度线性变化,
  误差带随时间整体平移(上下沿均为线性)。
- 超界区间(按各批允许温区 [lo, hi] 分别核算,边界相等不算超界):
  * 确定超界:整个误差带严格位于界外(下沿 < lo 或上沿 > hi);
  * 可能超界:误差带接触或跨越界外、但并非整体越界(下沿 ≤ lo ≤ 上沿
    且实测点未严格超下限,或下沿 ≤ hi ≤ 上沿且实测点未严格超上限);
  各自在穿越误差带相关边界值处切段,合并仅经边界点的连续段后输出。
- 度时下界/上界(对已设首选温度区间的批次):每时刻取误差带到首选区间
  的最近距离(度时下界)与最远距离(度时上界),在距离公式改变处切段后
  梯形精确积分并除以 3600;据此将度时预算判为确定超限(下界严格大于
  预算)、可能超限(上界严格大于预算而下界不超)或确定未超限。
- 误差为 0 时确定超界区间即原实测超界区间,度时下界=上界=原实测度时,
  与未引入误差时的复核结果一致。

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


def _parse_csv(csv_text: str, expected_seconds: float) -> list[tuple[float, float]]:
    """解析并校验实测温度 CSV,返回 [(秒, 温度), ...];非法记录抛 ValueError。"""
    if not csv_text or not csv_text.strip():
        raise ValueError(
            "实测温度 CSV 为空,请上传含 time_seconds,temperature 两列的文件"
        )
    reader = csv.reader(io.StringIO(csv_text.lstrip("﻿")))
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
    在区间内都是线性的,可精确切段与积分。重合的边界值(如误差为 0 时)
    只插一次,避免产生零长度区间。
    """
    unique_bounds = sorted(set(float(b) for b in bounds))
    pts = [samples[0]]
    for (t0, T0), (t1, T1) in zip(samples, samples[1:]):
        mids: list[tuple[float, float]] = []
        if T0 != T1:
            for b in unique_bounds:
                u = (b - T0) / (T1 - T0)
                if EPS < u < 1.0 - EPS:
                    mids.append((t0 + u * (t1 - t0), b))
        mids.sort()
        pts.extend(mids)
        pts.append((t1, T1))
    return pts


def _side_of(
    low_flag: bool, high_flag: bool
) -> str:
    """由是否触及下侧/上侧界外合成 side 标记。"""
    if low_flag and high_flag:
        return "both"
    return "low" if low_flag else "high"


def _excursions(
    samples: list[tuple[float, float]], lo: float, hi: float, err: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """误差带视角的确定超界与可能超界连续时间段。

    每时刻真实温度位于误差带 [T-err, T+err](T 为线性插值实测温度)。
    - 确定超界:整带严格越界(T-err < lo 或 T+err > hi);
    - 可能超界:误差带接触或跨越界外但并非整带越界
      (T-err ≤ lo ≤ T+err 而 T 未严格低于 lo,或
       T-err ≤ hi ≤ T+err 而 T 未严格高于 hi);
    恰在边界(相等)不算超界。在穿越 lo±err、hi±err 处切段后,
    同类相邻微段(中间仅经边界点)各自合并为一个连续区间。
    返回 (确定超界区间列表, 可能超界区间列表)。
    """
    pts = _densify(samples, (lo - err, lo + err, hi - err, hi + err))
    # 每个连续段:[start, end, 实测最高, 实测最低, 误差带上沿最高, 下沿最低,
    #            触下侧界外, 触上侧界外]
    runs: dict[str, list[list[Any]]] = {"certain": [], "possible": []}
    cur: dict[str, list[Any] | None] = {"certain": None, "possible": None}

    def classify(Tm: float) -> str:
        """微段中点分类:certain 确定超界 / possible 可能超界 / none 带完全在界内。

        误差带 [Tm-err, Tm+err]:整带严格位于界外(Tm+err<lo 或 Tm-err>hi)
        为确定超界;否则带内只要含严格界外温度(Tm-err<lo 或 Tm+err>hi)
        即为可能超界;恰在边界(相等)不算超界。
        """
        if Tm + err < lo - EPS or Tm - err > hi + EPS:
            return "certain"
        if Tm - err < lo - EPS or Tm + err > hi + EPS:
            return "possible"
        return "none"

    def feed(kind: str, t0: float, t1: float, T0: float, T1: float) -> None:
        seg_hi, seg_lo = max(T0, T1), min(T0, T1)
        side_lo = seg_lo - err < lo - EPS  # 该微段误差带含严格低于下限的温度
        side_hi = seg_hi + err > hi + EPS  # 该微段误差带含严格高于上限的温度
        c = cur[kind]
        if c is None:
            cur[kind] = [t0, t1, seg_hi, seg_lo,
                         seg_hi + err, seg_lo - err, side_lo, side_hi]
        else:
            c[1] = t1
            c[2] = max(c[2], seg_hi)
            c[3] = min(c[3], seg_lo)
            c[4] = max(c[4], seg_hi + err)
            c[5] = min(c[5], seg_lo - err)
            c[6] = c[6] or side_lo
            c[7] = c[7] or side_hi

    prev_kind = "none"
    for (t0, T0), (t1, T1) in zip(pts, pts[1:]):
        kind = classify((T0 + T1) / 2.0)
        # 相邻同类微段(中间仅经边界点)合并为一个连续区间;类别切换时收尾
        if kind != prev_kind and prev_kind != "none":
            if cur[prev_kind] is not None:
                runs[prev_kind].append(cur[prev_kind])
                cur[prev_kind] = None
        if kind != "none":
            feed(kind, t0, t1, T0, T1)
        prev_kind = kind
    for kind in ("certain", "possible"):
        if cur[kind] is not None:
            runs[kind].append(cur[kind])

    def to_dicts(kind: str) -> list[dict[str, Any]]:
        out = []
        for s, e, mx, mn, band_hi, band_lo, side_lo, side_hi in runs[kind]:
            out.append(
                {
                    "start_s": _r(s),
                    "end_s": _r(e),
                    "duration_s": _r(e - s),
                    "min_temperature": _r(mn),
                    "max_temperature": _r(mx),
                    "band_t_min": _r(band_lo),
                    "band_t_max": _r(band_hi),
                    "side": _side_of(side_lo, side_hi),
                }
            )
        return out

    return to_dicts("certain"), to_dicts("possible")


def _degree_hours(
    samples: list[tuple[float, float]], pref_lo: float, pref_hi: float
) -> float:
    """实测累计度时(℃·h):实测温度到首选区间距离对时间的积分 ÷ 3600。

    在穿越首选区间边界处切段后,距离函数在每小段内是线性的,
    梯形求和即为精确积分(不以采样点是否超界或平均温度代替)。
    """
    pts = _densify(samples, (pref_lo, pref_hi))
    deg_s = 0.0
    for (t0, T0), (t1, T1) in zip(pts, pts[1:]):
        deg_s += (
            (_dist(T0, pref_lo, pref_hi) + _dist(T1, pref_lo, pref_hi))
            / 2.0
            * (t1 - t0)
        )
    return deg_s / 3600.0


def _degree_hours_bounds(
    samples: list[tuple[float, float]],
    pref_lo: float,
    pref_hi: float,
    err: float,
) -> tuple[float, float]:
    """度时下界/上界(℃·h):误差带到首选区间的最近/最远距离积分 ÷ 3600。

    每时刻真实温度 ∈ [T-err, T+err]:
    - 最近距离 = 误差带到首选区间的距离(带与区间相交时为 0),在 T 越过
      pref_lo-err、pref_hi+err 处公式改变;
    - 最远距离 = max(|(T-err) 到区间|, |(T+err) 到区间|),在 T 越过
      pref_lo+err、pref_hi-err 处公式改变;误差带宽于首选区间时两支还在
      首选区间中点处交叉(额外折点)。合并全部折点(含中点)后每微段内
      两者均为 T 的线性函数,梯形求和精确。
    """
    pts = _densify(
        samples,
        (
            pref_lo - err,
            pref_lo + err,
            (pref_lo + pref_hi) / 2.0,
            pref_hi - err,
            pref_hi + err,
        ),
    )

    def band_dist(T: float) -> tuple[float, float]:
        """误差带 [T-err, T+err] 到首选区间的(最近距离, 最远距离)。"""
        b_lo, b_hi = T - err, T + err
        nearest = 0.0 if (b_lo <= pref_hi + EPS and b_hi >= pref_lo - EPS) else min(
            abs(pref_lo - b_hi), abs(pref_hi - b_lo)
        )
        farthest = max(_dist(b_lo, pref_lo, pref_hi), _dist(b_hi, pref_lo, pref_hi))
        return nearest, farthest

    lo_s = hi_s = 0.0
    for (t0, T0), (t1, T1) in zip(pts, pts[1:]):
        n0, f0 = band_dist(T0)
        n1, f1 = band_dist(T1)
        dt = t1 - t0
        lo_s += (n0 + n1) / 2.0 * dt
        hi_s += (f0 + f1) / 2.0 * dt
    return lo_s / 3600.0, hi_s / 3600.0


def review(payload: dict[str, Any]) -> dict[str, Any]:
    """实测温度复核入口;输入不合法时抛 ValueError(与 plan 一致)。"""
    plan_input = payload.get("plan_input") or {}
    box_no = payload.get("box_no")
    csv_text = payload.get("csv") or ""
    err_raw = payload.get("instrument_error", 0.0)
    err = 0.0 if err_raw in (None, "") else float(err_raw)
    if not math.isfinite(err) or err < 0:
        raise ValueError("测温仪器绝对误差须为非负数值(℃)")
    if box_no is None:
        raise ValueError("请指定要复核的箱号 box_no")
    box_no = int(box_no)

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

        certain, possible = _excursions(samples, lo, hi, err)
        # 名义实测度时(以实测值本身积分);误差为 0 时与度时下/上界相同
        measured = (
            _degree_hours(samples, float(p_lo), float(p_hi)) if has_pref else None
        )
        dh_lower = dh_upper = None
        if has_pref:
            dh_lower, dh_upper = _degree_hours_bounds(
                samples, float(p_lo), float(p_hi), err
            )

        # 度时预算判定:下界严格大于预算 -> 确定超限;上界严格大于预算
        # (而下界不超)-> 可能超限;否则确定未超限。未设预算不判。
        verdict: str | None = None
        remaining_lower: float | None = None
        remaining_upper: float | None = None
        exceeded: bool | None = None
        remaining: float | None = None
        if budget is not None and dh_lower is not None and dh_upper is not None:
            remaining_lower = budget - dh_lower
            remaining_upper = budget - dh_upper
            if dh_lower > budget + EPS:
                verdict = "certain_exceeded"
            elif dh_upper > budget + EPS:
                verdict = "possible_exceeded"
            else:
                verdict = "certain_within"
            # e=0 时与原字段保持一致(严格大于预算才判超限)
            if err <= EPS:
                exceeded = measured is not None and measured > budget + EPS
                remaining = budget - (measured or 0.0)

        if certain:
            limit_verdict = "certain_exceeded"
        elif possible:
            limit_verdict = "possible_exceeded"
        else:
            limit_verdict = "certain_within"

        batches_out.append(
            {
                "batch_no": no,
                "name": name,
                "units": units_by_no[no],
                "allowed_t_min": _r(lo),
                "allowed_t_max": _r(hi),
                # 误差为 0 时与原复核字段一致:确定超界即实测超界
                "within_limits": not certain,
                "excursion_count": len(certain),
                "excursions": certain,
                "limit_verdict": limit_verdict,
                "certain_excursion_count": len(certain),
                "certain_excursions": certain,
                "possible_excursion_count": len(possible),
                "possible_excursions": possible,
                "preferred_t_min": _r(float(p_lo)) if has_pref else None,
                "preferred_t_max": _r(float(p_hi)) if has_pref else None,
                "measured_degree_hours": (
                    _r(measured) if measured is not None else None
                ),
                "degree_hours_lower": _r(dh_lower) if dh_lower is not None else None,
                "degree_hours_upper": _r(dh_upper) if dh_upper is not None else None,
                "max_degree_hours": _r(budget) if budget is not None else None,
                "remaining_budget": (
                    _r(remaining) if remaining is not None else None
                ),
                "remaining_budget_lower": (
                    _r(remaining_lower) if remaining_lower is not None else None
                ),
                "remaining_budget_upper": (
                    _r(remaining_upper) if remaining_upper is not None else None
                ),
                "budget_exceeded": exceeded,
                "budget_verdict": verdict,
            }
        )

    return {
        "input_fingerprint": input_fingerprint(plan_input),
        "box_no": box_no,
        "transport_hours": _r(transport_hours),
        "duration_seconds": _r(expected_s),
        "sample_count": len(samples),
        "instrument_error": _r(err),
        "batches": batches_out,
        "summary": {
            "batch_count": len(batches_out),
            # 误差为 0 时 batches_with_excursions / total_excursions 即原字段
            "batches_with_excursions": sum(
                1 for b in batches_out if b["certain_excursion_count"]
            ),
            "total_excursions": sum(
                b["certain_excursion_count"] for b in batches_out
            ),
            "batches_certain_excursion": sum(
                1 for b in batches_out if b["certain_excursion_count"]
            ),
            "batches_possible_excursion": sum(
                1
                for b in batches_out
                if not b["certain_excursion_count"] and b["possible_excursion_count"]
            ),
            "total_certain_excursions": sum(
                b["certain_excursion_count"] for b in batches_out
            ),
            "total_possible_excursions": sum(
                b["possible_excursion_count"] for b in batches_out
            ),
            "budget_checked_count": sum(
                1 for b in batches_out if b["budget_verdict"] is not None
            ),
            "budget_exceeded_count": sum(
                1 for b in batches_out if b["budget_verdict"] == "certain_exceeded"
            ),
            "budget_possible_count": sum(
                1 for b in batches_out if b["budget_verdict"] == "possible_exceeded"
            ),
            "all_within_limits": all(
                b["limit_verdict"] == "certain_within" for b in batches_out
            ),
            "all_certainly_within_limits": all(
                b["limit_verdict"] == "certain_within" for b in batches_out
            ),
        },
    }
