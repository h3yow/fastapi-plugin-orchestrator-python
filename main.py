import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from orchestrator.bootstrap import build_default_orchestrator
from orchestrator.errors import OrchestratorError

# 多环境示例：同一套模块在 dev/prod 下各有一份互不影响的生效配置
ENVIRONMENT_CONFIGS = {
    "dev": {"core": {"workers": 2}},
    "prod": {"core": {"workers": 8}},
}

orchestrator = build_default_orchestrator(
    environment=os.environ.get("ORCH_ENVIRONMENT", "dev"),
    environment_configs=ENVIRONMENT_CONFIGS,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    orchestrator.start_all()
    yield
    orchestrator.stop_all()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(OrchestratorError)
def orchestrator_error_handler(request, exc: OrchestratorError):
    # 框架错误统一映射为 4xx，reason 与上下文一并返回，便于定位失败步骤
    return JSONResponse(
        status_code=422,
        content={
            "reason": exc.reason.value,
            "message": str(exc),
            "context": exc.context,
        },
    )


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/modules")
def list_modules():
    """查询各模块最终状态、失败原因与配置来源。"""
    return {
        mid: {
            "state": status.state.value,
            "failure_reason": status.failure_reason.value
            if status.failure_reason
            else None,
            "failure_detail": status.failure_detail,
            "config_sources": {
                key: source.value for key, source in status.config_sources.items()
            },
        }
        for mid, status in orchestrator.statuses().items()
    }


@app.get("/modules/{module_id}/config")
def get_module_config(module_id: str):
    """查询模块当前生效配置（逐键来源层）与最近一次调整结果。"""
    effective = orchestrator.effective_config(module_id)
    last = orchestrator.last_update(module_id)
    return {
        "environment": orchestrator.environment,
        "effective": {
            key: {"value": rv.value, "source": rv.source.value}
            for key, rv in effective.items()
        },
        "last_update": None
        if last is None
        else {
            "request_id": last.request_id,
            "target": last.target,
            "success": last.success,
            "failed_step": last.failed_step,
            "detail": last.detail,
            "affected": list(last.affected),
        },
    }


class ConfigUpdateRequest(BaseModel):
    overrides: dict
    request_id: str | None = None


@app.post("/modules/{module_id}/config")
def update_module_config(module_id: str, body: ConfigUpdateRequest):
    """不停机调整模块配置：校验失败整批拒绝，成功则级联依赖方一起重载。"""
    record = orchestrator.update_config(
        module_id, body.overrides, request_id=body.request_id
    )
    return {
        "success": record.success,
        "affected": list(record.affected),
        "request_id": record.request_id,
    }


@app.post("/environment/{name}")
def switch_environment(name: str):
    """切换部署环境并重解析全部运行中模块的配置。"""
    orchestrator.switch_environment(name)
    return {"environment": orchestrator.environment}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
