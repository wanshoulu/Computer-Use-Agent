"""
OntoMind Agent 主应用入口
FastAPI 应用初始化、中间件配置和路由注册
"""
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.ontomind_agent.config import get_settings
from src.ontomind_agent.routes import agent_runtime, build_tasks, browser_task, hermes, inference, knowledge_runtime, pipeline, query, wiki_chat
from src.ontomind_agent.multimodal.routes import multimodal as multimodal_routes
from src.ontomind_agent.utils.logger import logger

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    """应用生命周期管理：启动和关闭时的初始化/清理"""
    logger.info("=" * 60)
    logger.info("OntoMind Agent Service 启动中...")
    logger.info(f"日志级别: {settings.log_level}")
    logger.info(f"后端服务: {settings.server_api_url}")
    # HugeGraph 暂未启用，使用 Jena TDB2 + Pellet 进行语义推理
    # logger.info(f"图数据库: {settings.hugegraph_url}")
    logger.info("=" * 60)

    # 启动时初始化：预热 Hermes CLI（模型配置 + MCP 注册 + 技能缓存）
    if settings.hermes_enabled:
        try:
            from src.ontomind_agent.routes.hermes import _init_hermes_default_model
            import time
            t0 = time.monotonic()
            await _init_hermes_default_model()
            t1 = time.monotonic()
            logger.info(f"[启动] Hermes 预热完成: {(t1 - t0)*1000:.0f}ms")
        except Exception as e:
            logger.warning(f"[启动] Hermes 预热跳过（非致命）: {e}")
    yield

    # 关闭时清理
    logger.info("OntoMind Agent Service 正在关闭...")
    # 取消运行中的构建任务
    try:
        from src.ontomind_agent.routes.build_tasks import get_orchestrator
        orchestrator = get_orchestrator()
        if orchestrator:
            await orchestrator.close()
            logger.info("BuildOrchestrator 已关闭")
    except Exception as e:
        logger.warning("关闭 BuildOrchestrator 异常: {}", e)
    # 关闭 Hermes Agent Pool
    try:
        from src.ontomind_agent.services.hermes_pool import shutdown_hermes_pool
        await shutdown_hermes_pool()
    except Exception as e:
        logger.warning("关闭 HermesAgentPool 异常: {}", e)
    await asyncio.sleep(0.5)
    logger.info("OntoMind Agent Service 已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="OntoMind Agent Service",
    description="智能本体构建 Agent 服务 - 基于大模型的自动本体建模平台",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS 中间件配置
# 从环境变量读取CORS配置
cors_origins = []
if settings.cors_allowed_origins:
    cors_origins = [origin.strip() for origin in settings.cors_allowed_origins.split(",")]
else:
    # 开发环境允许所有来源，生产环境应该配置具体域名
    environment = settings.environment.lower()
    if environment in ["prod", "production"]:
        raise ValueError("生产环境必须配置 CORS_ALLOWED_ORIGINS 环境变量")
    else:
        cors_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=[method.strip() for method in settings.cors_allowed_methods.split(",")],
    allow_headers=[header.strip() for header in settings.cors_allowed_headers.split(",")],
)


# 全局异常处理
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器，捕获未处理的异常并返回友好信息"""
    logger.error(f"未处理异常: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "内部服务器错误"},
    )


# 注册路由
app.include_router(build_tasks.router, prefix="/agent", tags=["构建任务"])
app.include_router(inference.router, prefix="/agent", tags=["智能推理"])
app.include_router(query.router, prefix="/agent", tags=["自然语言查询"])
app.include_router(wiki_chat.router, prefix="/agent", tags=["Wiki 对话构建"])

if settings.hermes_enabled:
    app.include_router(hermes.router, prefix="/agent/hermes", tags=["本体智能体"])
    app.include_router(browser_task.router, prefix="/agent/browser", tags=["Computer Use 任务"])

# The platform contract is stable even when its current provider (Hermes) is unavailable.
app.include_router(agent_runtime.router)
app.include_router(knowledge_runtime.router)

app.include_router(pipeline.router, prefix="/api/v1", tags=["实例化流水线"])
app.include_router(multimodal_routes.router, tags=["多模知识服务"])


# 健康检查
@app.get("/health", tags=["系统"])
async def health_check():
    """健康检查接口"""
    return {
        "status": "healthy",
        "service": "ontomind-agent",
        "version": "1.0.0",
    }


@app.get("/", tags=["系统"])
async def root():
    """根路径，返回服务信息"""
    return {
        "service": "OntoMind Agent Service",
        "version": "1.0.0",
        "docs": "/docs",
    }
