"""FastAPI 入口:提供规划 API 与静态前端页面。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .planner import plan
from .resilience import assess_zone_failures
from .review import review
from .schemas import PlanRequest, ReviewRequest

app = FastAPI(
    title="冷链装箱温控规划器 API",
    description="按温区兼容与容量约束生成冷链分箱方案,支持分段运输逐段温区校验、"
    "累计热暴露(度时)预算与锁温区段(相邻同组段共用设定点);"
    "并提供单温区故障容灾评估:将每个运输段的每个可用温区分别模拟为不可用,"
    "复用完整规则重新规划并输出风险矩阵数据;"
    "以及实测温度复核:上传实测温度 CSV 并可选填写测温仪器绝对误差,"
    "按所选箱子的装载批次,将每时刻真实温度视为实测值±误差,"
    "核算误差带整体越界的确定超界区间、接触或跨越界外的可能超界区间,"
    "以及误差带到首选区间最近/最远距离积分得到的度时下界/上界与预算三态判定"
    "(温度线性插值、边界处切段、距离积分 ÷ 3600,误差为 0 时与原复核一致)",
    version="1.6.0",
)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """规划器抛出的业务校验错误统一返回 400,消息直接展示给用户。"""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/plan", tags=["plan"])
async def api_plan(req: PlanRequest) -> dict:
    return plan(req.model_dump())


@app.post("/api/zone-failure-assessment", tags=["assessment"])
async def api_zone_failure_assessment(req: PlanRequest) -> dict:
    """单温区故障容灾评估:请求体与 /api/plan 相同,基于同一输入快照,
    将每个运输段的每个可用温区分别模拟为不可用并重新规划。"""
    return assess_zone_failures(req.model_dump())


@app.post("/api/temperature-review", tags=["review"])
async def api_temperature_review(req: ReviewRequest) -> dict:
    """实测温度复核:请求体携带生成方案时的输入快照(plan_input)、箱号、
    实测温度 CSV(time_seconds,temperature 两列)与可选的测温仪器绝对误差
    (instrument_error,℃,非负默认 0);按快照重新规划定位箱子,将每时刻
    真实温度视为实测值±误差,逐批核算确定/可能超界区间与度时上下界。
    输入变化后旧复核结果过期,需重新生成方案。"""
    return review(req.model_dump())


# 前端静态资源(挂载在最后,避免覆盖 /api 与 /docs)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
