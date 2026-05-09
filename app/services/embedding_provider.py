import asyncio
import hashlib
import math


class EmbeddingProvider:
    def __init__(self, dimension: int = 64) -> None:
        # 你要做的事：
        # 1. 保存向量维度
        # 2. Day 4 先用固定维度，方便 fallback 检索
        # 3. 后续接真实 embedding 模型时，维度要和模型输出一致
        self.dimension = dimension

    async def embed(self, text: str) -> list[float]:
        # 你要做的事：
        # 1. 接收一段 embedding_text 或 query
        # 2. 使用 asyncio.to_thread 调用同步 embedding 实现
        # 3. 返回固定维度的 list[float]
        # 4. 不要在 async 函数里直接做重 CPU 计算
        return await asyncio.to_thread(self._embed_sync, text)

    def _embed_sync(self, text: str) -> list[float]:
        # 你要做的事：
        # 1. 把文本转成小写 token
        # 2. 用 hash 把 token 映射到固定维度下标
        # 3. 累加每个 token 对应的维度值
        # 4. 对最终向量做归一化，方便 cosine similarity
        # 5. 空文本返回全 0 向量
        vector = [0.0] * self.dimension
        tokens = text.lower().split()
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -0.1
            vector[index] += sign

        norm = math.sqrt(sum(value**2 for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]
