import asyncio
import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic_evals import Dataset

from app.schemas.attack_sample_schema import AttackSample, BlackBoxCase
from app.schemas.graybox_schema import GrayBoxCase, LoadedGrayBoxDataset
from app.schemas.run_schema import LoadedDataset
from app.schemas.stateful_schema import LoadedStatefulDataset, StatefulCase


# 负责从样本文件中加载并校验攻击样本。
class AttackSampleLoader:
    # 异步从 YAML 文件读取攻击样本。
    async def load_from_yaml(self, path: Path | str):
        return await asyncio.to_thread(self._load_from_yaml_sync, Path(path))

    # 在线程中同步解析 YAML 文件并转换为攻击样本模型。
    def _load_from_yaml_sync(self, path: Path):
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return AttackSample.model_validate(data)


attack_sample_loader = AttackSampleLoader()


class BlackBoxDatasetLoader:
    async def load(
        self,
        path: Path | str,
        case_ids: list[str] | None = None,
    ) -> LoadedDataset:
        return await asyncio.to_thread(self._load_sync, Path(path), case_ids)

    def _load_sync(self, path: Path, case_ids: list[str] | None) -> LoadedDataset:
        raw_bytes = path.read_bytes()
        raw_data = yaml.safe_load(raw_bytes)
        dataset = Dataset[dict[str, Any], str, dict[str, Any]].from_file(path)

        cases: list[BlackBoxCase] = []
        for eval_case in dataset.cases:
            case = BlackBoxCase.model_validate(eval_case.inputs)
            if eval_case.name and eval_case.name != case.id:
                raise ValueError(
                    f"dataset case name {eval_case.name!r} must match input id {case.id!r}"
                )
            cases.append(case)

        if case_ids is not None:
            requested = set(case_ids)
            known = {case.id for case in cases}
            unknown = requested - known
            if unknown:
                raise ValueError(f"unknown case ids: {', '.join(sorted(unknown))}")
            cases = [case for case in cases if case.id in requested]

        return LoadedDataset(
            name=dataset.name or path.stem,
            version=str(raw_data.get("metadata", {}).get("version", "1")),
            source_path=path,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            cases=cases,
            snapshot=raw_data,
        )


black_box_dataset_loader = BlackBoxDatasetLoader()


class GrayBoxDatasetLoader:
    async def load(
        self,
        path: Path | str,
        case_ids: list[str] | None = None,
    ) -> LoadedGrayBoxDataset:
        return await asyncio.to_thread(self._load_sync, Path(path), case_ids)

    def _load_sync(
        self,
        path: Path,
        case_ids: list[str] | None,
    ) -> LoadedGrayBoxDataset:
        raw_bytes = path.read_bytes()
        raw_data = yaml.safe_load(raw_bytes)
        dataset = Dataset[dict[str, Any], str, dict[str, Any]].from_file(path)
        cases: list[GrayBoxCase] = []
        for eval_case in dataset.cases:
            case = GrayBoxCase.model_validate(eval_case.inputs)
            if eval_case.name and eval_case.name != case.id:
                raise ValueError(
                    f"dataset case name {eval_case.name!r} must match input id {case.id!r}"
                )
            cases.append(case)

        if case_ids is not None:
            requested = set(case_ids)
            known = {case.id for case in cases}
            unknown = requested - known
            if unknown:
                raise ValueError(f"unknown case ids: {', '.join(sorted(unknown))}")
            cases = [case for case in cases if case.id in requested]

        return LoadedGrayBoxDataset(
            name=dataset.name or path.stem,
            version="2",
            source_path=path,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            cases=cases,
            snapshot=raw_data,
        )


gray_box_dataset_loader = GrayBoxDatasetLoader()


class StatefulDatasetLoader:
    async def load(
        self,
        path: Path | str,
        case_ids: list[str] | None = None,
    ) -> LoadedStatefulDataset:
        return await asyncio.to_thread(self._load_sync, Path(path), case_ids)

    def _load_sync(
        self,
        path: Path,
        case_ids: list[str] | None,
    ) -> LoadedStatefulDataset:
        raw_bytes = path.read_bytes()
        raw_data = yaml.safe_load(raw_bytes)
        dataset = Dataset[dict[str, Any], str, dict[str, Any]].from_file(path)
        cases: list[StatefulCase] = []
        for eval_case in dataset.cases:
            case = StatefulCase.model_validate(eval_case.inputs)
            if eval_case.name and eval_case.name != case.id:
                raise ValueError(
                    f"dataset case name {eval_case.name!r} must match input id {case.id!r}"
                )
            cases.append(case)
        if case_ids is not None:
            requested = set(case_ids)
            unknown = requested - {case.id for case in cases}
            if unknown:
                raise ValueError(f"unknown case ids: {', '.join(sorted(unknown))}")
            cases = [case for case in cases if case.id in requested]
        return LoadedStatefulDataset(
            name=dataset.name or path.stem,
            version="3",
            source_path=path,
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            cases=cases,
            snapshot=raw_data,
        )


stateful_dataset_loader = StatefulDatasetLoader()
