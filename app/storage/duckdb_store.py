import asyncio
import json
from pathlib import Path
from typing import Any

import duckdb

from app.schemas.evidence_schema import EvidenceEvent, ArtifactRef
from conf.storage_conf import get_duckdb_path


# 封装 DuckDB 的异步调用边界，避免业务层直接操作同步连接。
class DuckDBStore:
    # 初始化 DuckDB 存储对象并确定数据库文件路径。
    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or get_duckdb_path()

    # 异步初始化 DuckDB 文件和连接可用性。
    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    # 在线程中同步创建 DuckDB 目录并测试连接。
    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.database_path)) as con:
            con.execute("select 1")



    # 异步初始化证据相关的 DuckDB 表结构。
    async def initialize_schema(self) -> None:
        await asyncio.to_thread(self._initialize_schema_sync)

    # 在线程中同步创建测试运行、发现和制品引用表。
    def _initialize_schema_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.database_path)) as con:
            con.execute(
                """
                create table if not exists test_runs (
                    evidence_id varchar primary key,
                    run_id varchar,
                    target_name varchar,
                    sample_id varchar,
                    violated boolean,
                    risk_level varchar,
                    latency_ms integer,
                    error varchar,
                    created_at timestamp
                )
                """
            )
            con.execute(
                """
                create table if not exists findings (
                    evidence_id varchar,
                    run_id varchar,
                    target_name varchar,
                    sample_id varchar,
                    risk_level varchar,
                    reason varchar,
                    matched_patterns_json varchar,
                    created_at timestamp
                )
                """
            )
            con.execute(
                """
                create table if not exists artifacts (
                    evidence_id varchar,
                    object_key varchar,
                    artifact_type varchar,
                    storage_backend varchar
                )
                """
            )


    # 异步执行不返回结果集的 SQL 语句。
    async def execute(self, sql: str, parameters: list[Any] | None = None) -> None:
        await asyncio.to_thread(self._execute_sync, sql, parameters or [])

    # 在线程中同步执行不返回结果集的 SQL 语句。
    def _execute_sync(self, sql: str, parameters: list[Any]) -> None:
        with duckdb.connect(str(self.database_path)) as con:
            con.execute(sql, parameters)

    # 异步执行查询 SQL 并返回全部结果。
    async def fetch_all(self, sql: str, parameters: list[Any] | None = None) -> list[tuple]:
        return await asyncio.to_thread(self._fetch_all_sync, sql, parameters or [])

    # 在线程中同步执行查询 SQL 并取回全部结果。
    def _fetch_all_sync(self, sql: str, parameters: list[Any]) -> list[tuple]:
        with duckdb.connect(str(self.database_path)) as con:
            return con.execute(sql, parameters).fetchall()



    # 异步保存一次测试运行的概要结果。
    async def save_test_run(self, event: EvidenceEvent) -> None:
        judge = event.judge_result
        await self.execute(
            """
            insert or replace into test_runs
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event.evidence_id,
                event.run_id,
                event.target_name,
                event.sample_id,
                bool(judge.get("violated", False)),
                str(judge.get("risk_level", "low")),
                event.latency_ms,
                event.error,
                event.created_at,
            ],
        )

    # 异步保存命中违规规则的安全发现记录。
    async def save_finding(self, event: EvidenceEvent) -> None:
        judge = event.judge_result
        if not judge.get("violated", False):
            return
        await self.execute(
            """
            insert into findings
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                event.evidence_id,
                event.run_id,
                event.target_name,
                event.sample_id,
                str(judge.get("risk_level", "low")),
                str(judge.get("reason", "")),
                json.dumps(judge.get("matched_patterns", []), ensure_ascii=False),
                event.created_at,
            ],
        )

    # 异步保存证据事件关联的对象存储制品引用。
    async def save_artifact_ref(self, evidence_id: str, artifact: ArtifactRef) -> None:
        await self.execute(
            """
            insert into artifacts
            values (?, ?, ?, ?)
            """,
            [
                evidence_id,
                artifact.object_key,
                artifact.artifact_type,
                artifact.storage_backend,
            ],
        )
