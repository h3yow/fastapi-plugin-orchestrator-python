from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from orchestrator.bootstrap import build_default_orchestrator

orchestrator = build_default_orchestrator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    orchestrator.start_all()
    yield
    orchestrator.stop_all()


app = FastAPI(lifespan=lifespan)


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


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
