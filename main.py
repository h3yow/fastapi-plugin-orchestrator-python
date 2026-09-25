from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.bootstrap import build_orchestrator

orchestrator = build_orchestrator()


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
def module_states():
    return orchestrator.snapshot()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
