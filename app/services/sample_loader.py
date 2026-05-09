import asyncio
from pathlib import Path
import yaml

from app.schemas.attack_sample_schema import AttackSample

# 负责从样本文件中加载并校验攻击样本。
class AttackSampleLoader:
    # 异步从 YAML 文件读取攻击样本。
    async def load_from_yaml(self, path: Path | str):
        return await asyncio.to_thread(self._load_from_yaml_sync,Path(path))


    # 在线程中同步解析 YAML 文件并转换为攻击样本模型。
    def _load_from_yaml_sync(self,path:Path):
        with path.open("r",encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return AttackSample.model_validate(data)


attack_sample_loader = AttackSampleLoader()
