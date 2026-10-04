from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from orchestrator.bootstrap import build_default_orchestrator
from orchestrator.errors import OrchestratorError

orchestrator = build_default_orchestrator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    orchestrator.start_all()
    yield
    orchestrator.stop_all()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(OrchestratorError)
def orchestrator_error_handler(request: Request, exc: OrchestratorError) -> JSONResponse:
    """框架可区分错误统一映射为 400，body 带稳定 reason 与结构化 context。"""
    return JSONResponse(
        status_code=400,
        content={
            "error": type(exc).__name__,
            "reason": exc.reason.value,
            "detail": str(exc),
            "context": exc.context,
        },
    )


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/modules")
def list_modules():
    """查询各模块最终状态、失败原因、生效值与配置来源。"""
    return {
        mid: {
            "state": status.state.value,
            "failure_reason": status.failure_reason.value
            if status.failure_reason
            else None,
            "failure_detail": status.failure_detail,
            "config_values": status.config_values,
            "config_sources": {
                key: source.value for key, source in status.config_sources.items()
            },
        }
        for mid, status in orchestrator.statuses().items()
    }


class ConfigUpdateRequest(BaseModel):
    """一次运行期配置调整：{模块id: {配置键: 新值}}。"""

    changes: dict[str, dict]
    source: str = "runtime"
    change_id: str | None = None


class EnvironmentSwitchRequest(BaseModel):
    environment: str = Field(min_length=1)


@app.post("/modules/reload")
def reload_configs(req: ConfigUpdateRequest):
    """不停机热更新配置（含依赖级联、整批回滚、change_id 幂等）。"""
    result = orchestrator.update_config(
        req.changes, source=req.source, change_id=req.change_id
    )
    return result.as_dict()


@app.post("/environment")
def switch_environment(req: EnvironmentSwitchRequest):
    """切换部署环境：不串值，运行中模块原子重载，失败整体回滚。"""
    result = orchestrator.switch_environment(req.environment)
    return result.as_dict()


@app.get("/environment")
def current_environment():
    return {"active": orchestrator.environment, "available": orchestrator.environments()}


@app.get("/modules/{module_id}/config")
def module_config(module_id: str):
    """逐键查询当前生效值及来源层（含回落解释）与最近一次变更结果。"""
    last = orchestrator.last_change_for(module_id)
    return {
        "module_id": module_id,
        "environment": orchestrator.environment,
        "effective": orchestrator.config_explanation(module_id),
        "last_change": last.as_dict() if last else None,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
