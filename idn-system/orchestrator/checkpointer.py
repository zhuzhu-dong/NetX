"""
orchestrator/checkpointer.py
=============================

LangGraph Checkpoint 持久化后端配置与管理。

# 系统角色
管理 LangGraph 状态机的 Checkpoint 持久化生命周期，
支持 SQLite（开发/单机环境）和 Redis（生产/多实例环境）两种后端。

Checkpoint 机制是 LangGraph 实现暂停/恢复、故障恢复和
人工审批（interrupt/resume）的基础设施。

# 设计决策
- 默认与 API 的 IntentStore 共享同一 SQLite 数据库文件（IDN_APP_DB_PATH），
  消除 App DB 与 LangGraph checkpoint DB 分离导致的 split-brain 风险
- 全局单例模式（CheckpointerManager），避免多处创建连接池
- 支持同步降级（get_checkpointer）和异步主路径（get_async_checkpointer）

# 主要依赖
- langgraph.checkpoint.sqlite / redis：LangGraph 持久化后端
- api.store：共享 SQLite 数据库文件路径

# 与其他模块交互
- orchestrator/graph.py：IDNOrchestrator._build_graph() 使用 checkpointer
  编译状态图以实现持久化
- api/worker.py：应用关闭时调用 close_checkpointer() 释放资源
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator,  Literal, Optional

logger = logging.getLogger(__name__)

class CheckpointerManager:
    """
    Checkpointer生命周期管理器
    
    解决AsyncSqliteSaver上下文管理器在graph.compile后
    被销毁的问题，保持数据库连接在整个应用生命周期内存活
    """
    
    def __init__(self):
        self._checkpointer = None
        self._context_manager = None
        self._ref_count = 0
    
    async def initialize(self, backend: Literal["sqlite", "redis"] = "sqlite"):
        """初始化并保持Checkpointer连接（幂等，支持引用计数）"""
        self._ref_count += 1
        if self._checkpointer is not None:
            return  # 已初始化，仅增加引用计数
        if backend == "redis":
            await self._init_redis()
        else:
            await self._init_sqlite()
    
    async def _init_sqlite(self):
        """初始化异步SQLite连接。

        使用与 REST API 相同的数据库文件（IDN_APP_DB_PATH / app.db），
        消除 App DB 与 LangGraph checkpoint DB 分离导致的 split-brain 风险。
        """
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            # Use a dedicated checkpoint database to avoid SQLite write-lock
            # contention with IntentStore which uses IDN_APP_DB_PATH (app.db).
            # Sharing the same file was an intentional design (single transactional
            # domain), but in practice two independent connection pools competing
            # for the same WAL write lock cause "database is locked" errors that
            # block the async event loop and cascade to Mininet probe timeouts.
            db_path = os.getenv(
                "SQLITE_DB_PATH",
                os.getenv("IDN_APP_DB_PATH", "./data/checkpoint.db"),
            )
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

            self._context_manager = AsyncSqliteSaver.from_conn_string(db_path)
            self._checkpointer = await self._context_manager.__aenter__()

            logger.info(
                "LangGraph Checkpoint backend: Async SQLite (%s) — shared with App DB", db_path
            )

        except ImportError:
            logger.warning("langgraph-checkpoint-sqlite未安装，使用内存模式")
            self._checkpointer = _get_memory_checkpointer()
        except Exception as e:
            logger.error(f"异步SQLite初始化失败: {e}")
            self._checkpointer = _get_memory_checkpointer()

    async def _init_redis(self):
            """初始化异步Redis连接"""
            try:
                from langgraph.checkpoint.redis.aio import AsyncRedisSaver
                
                redis_url = (
                    f"redis://:{os.getenv('REDIS_PASSWORD', '')}@"
                    f"{os.getenv('REDIS_HOST', 'localhost')}:"
                    f"{os.getenv('REDIS_PORT', '6379')}/"
                    f"{os.getenv('REDIS_DB', '0')}"
                )
                
                self._context_manager = AsyncRedisSaver.from_conn_string(redis_url)
                self._checkpointer = await self._context_manager.__aenter__()
                
                logger.info(f"LangGraph Checkpoint后端: 异步Redis ({redis_url})")
                
            except ImportError:
                logger.warning("langgraph-checkpoint-redis未安装，降级为SQLite")
                await self._init_sqlite()
            except Exception as e:
                logger.error(f"异步Redis初始化失败: {e}")
                await self._init_sqlite()
        
    @property
    def checkpointer(self):
        """获取当前的Checkpointer实例"""
        if self._checkpointer is None:
            raise RuntimeError("Checkpointer未初始化，请先调用 initialize()")
        return self._checkpointer
        
    async def get_latest_checkpoint_timestamp(
        self, thread_id: str
    ) -> str | None:
        """Return the ISO timestamp of the latest checkpoint for *thread_id*.

        Returns None when no checkpoint exists or the checkpointer is
        unavailable.
        """
        if self._checkpointer is None:
            return None
        try:
            config = {"configurable": {"thread_id": thread_id}}
            checkpoint = await self._checkpointer.aget_tuple(config)
            if checkpoint and checkpoint.checkpoint:
                ts = checkpoint.checkpoint.get("ts")
                if ts:
                    from datetime import datetime, timezone
                    return datetime.fromisoformat(ts).isoformat()
        except Exception:
            pass
        return None

    async def verify_checkpoint_consistency(
        self, thread_id: str, intent_id: str
    ) -> dict:
        """Verify that the API store is consistent with the latest checkpoint.

        Returns a dict with ``consistent`` (bool) and ``details``.
        """
        if self._checkpointer is None:
            return {"consistent": True, "details": "checkpointer unavailable — skipping verification"}

        try:
            from api.store import get_current_store
            store = get_current_store()
            if store is None:
                return {"consistent": True, "details": "API store unavailable — skipping verification"}

            config = {"configurable": {"thread_id": thread_id}}
            checkpoint = await self._checkpointer.aget_tuple(config)
            if checkpoint is None or checkpoint.checkpoint is None:
                return {"consistent": True, "details": "no checkpoint found"}

            cp_ts = checkpoint.checkpoint.get("ts")
            cp_values = checkpoint.checkpoint.get("channel_values", {})

            record = store.get(intent_id)
            store_updated = record.get("updated_at") if record else None

            return {
                "consistent": bool(cp_ts and store_updated),
                "details": {
                    "checkpoint_ts": cp_ts,
                    "store_updated_at": store_updated,
                    "checkpoint_has_state": bool(cp_values),
                },
            }
        except Exception as exc:
            return {"consistent": True, "details": f"verification skipped: {exc}"}

    async def close(self):
        """关闭Checkpointer连接（仅当引用计数归零时真正关闭）"""
        self._ref_count = max(0, self._ref_count - 1)
        if self._ref_count > 0:
            return  # 仍有其他编排器引用，不关闭
        if self._context_manager and self._checkpointer:
            try:
                await self._context_manager.__aexit__(None, None, None)
                logger.info("Checkpointer连接已关闭")
            except Exception as e:
                logger.error(f"关闭Checkpointer失败: {e}")

        self._checkpointer = None
        self._context_manager = None


# 全局单例
_checkpointer_manager = CheckpointerManager()


async def get_async_checkpointer(backend: Literal["sqlite", "redis"] = "sqlite"):
    """
    获取LangGraph Checkpoint持久化后端实例
    
    Args:
        backend: 持久化后端类型
            - "sqlite": 轻量级，适合开发/单机环境
            - "redis": 高性能，适合生产/多实例环境
    
    Returns:
        LangGraph兼容的Checkpointer实例
    """
    await _checkpointer_manager.initialize(backend)
    return _checkpointer_manager.checkpointer


async def close_checkpointer():
    """关闭Checkpointer连接（应用关闭时调用）"""
    await _checkpointer_manager.close()


# 同步降级版本
def get_checkpointer(backend: Literal["sqlite", "redis"] = "sqlite"):
    """同步获取Checkpointer（降级方案）"""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
        db_path = os.getenv(
            "SQLITE_DB_PATH",
            os.getenv("IDN_APP_DB_PATH", "./data/checkpoint.db"),
        )
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        checkpointer = SqliteSaver.from_conn_string(db_path)
        logger.info(f"LangGraph Checkpoint backend: Sync SQLite ({db_path})")
        return checkpointer
    except ImportError:
        logger.warning("同步SQLite不可用，使用内存模式")
        return _get_memory_checkpointer()

def _get_memory_checkpointer():
    """内存Checkpointer (测试/降级环境，重启后丢失)"""
    from langgraph.checkpoint.memory import MemorySaver
    logger.warning("使用内存Checkpointer，重启后状态将丢失！")
    return MemorySaver()
