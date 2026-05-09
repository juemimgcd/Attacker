
from app.schemas.attack_sample_schema import AttackSample
from app.schemas.evidence_schema import EvidenceEvent
from app.schemas.index_schema import (
    IndexCollection,
    IndexDocument,
    IndexSourceType,
    IndexWriteResult,
    SearchSimilarRequest,
    SimilarCaseResult,
)
from app.services.embedding_provider import EmbeddingProvider
from app.storage.qdrant_store import QdrantStore




class SemanticIndexService:
    def __init__(self, embedding_provider=None, qdrant_store=None) -> None:
        # 你要做的事：
        # 1. 注入或创建 EmbeddingProvider
        # 2. 注入或创建 QdrantStore
        # 3. 不要在这里读取样本文件或执行攻击
        self.embedding_provider = embedding_provider
        self.qdrant_store = qdrant_store or QdrantStore()


    def build_sample_document(self, sample:AttackSample)->IndexDocument:
        # 你要做的事：
        # 1. 从 AttackSample 中提取 name、category、severity、prompt 等字段
        # 2. 拼出稳定的 embedding_text，字段顺序要固定
        # 3. 构造轻量 payload，至少包含 source_type、source_id、sample_id、title、summary、category、risk_level
        # 4. 返回 collection 为 attack_cases 的 IndexDocument
        embedding_text = "\n".join(
            [
                f"title:{sample.name}",
                f"category:{sample.category}",
                f"risk_level:{sample.severity.value}",
                f"expected:{sample.expected_violation}",
                f"judge_patterns:{','.join(sample.judge_patterns)}",


            ]
        )

        return IndexDocument(
            document_id=f"attack_sample:{sample.id}",
            source_type=IndexSourceType.attack_sample,
            source_id=sample.id,
            collection=IndexCollection.attack_cases,
            embedding_text=embedding_text,
            payload={
                "source_type": IndexSourceType.attack_sample.value,
                "source_id": sample.id,
                "sample_id": sample.id,
                "title": sample.name,
                "summary": f"{sample.category} sample: {sample.expected_violation}",
                "category": sample.category,
                "risk_level": sample.severity.value,
            },
        )




    def build_evidence_document(self, event):
        # 你要做的事：
        # 1. 从 EvidenceEvent 中提取 target_name、sample_id、request、response 摘要和 judge_result
        # 2. response_text 只截取摘要，不要把完整大文本塞进 embedding_text
        # 3. payload 至少包含 evidence_id、run_id、sample_id、target_name、risk_level
        # 4. 返回 collection 为 findings 的 IndexDocument

        judge = event.judge_result
        embedding_text = "\n".join(
            [
                f"target_name: {event.target_name}",
                f"sample_id: {event.sample_id}",
                f"request: {event.request_body}",
                f"response_summary: {event.response_text[:500]}",
                f"judge_reason: {judge.get('reason', '')}",
                f"risk_level: {judge.get('risk_level', 'low')}",
            ]
        )
        return IndexDocument(
            document_id=f"finding:{event.evidence_id}",
            source_type=IndexSourceType.finding,
            source_id=event.evidence_id,
            collection=IndexCollection.findings,
            embedding_text=embedding_text,
            payload={
                "source_type": IndexSourceType.finding.value,
                "source_id": event.evidence_id,
                "evidence_id": event.evidence_id,
                "run_id": event.run_id,
                "sample_id": event.sample_id,
                "target_name": event.target_name,
                "title": f"Finding for {event.sample_id}",
                "summary": str(judge.get("reason", "")),
                "risk_level": str(judge.get("risk_level", "low")),
            },
        )



    async def index_document(self, document):
        # 你要做的事：
        # 1. 调用 embedding_provider.embed(document.embedding_text) 生成向量
        # 2. 调用 qdrant_store.ensure_collection 确保 collection 存在
        # 3. 调用 qdrant_store.upsert_point 写入向量和 payload
        # 4. 返回 IndexWriteResult，标明 document_id、source_id、collection、backend
        vector = await self.embedding_provider.embed_document(document)
        await self.qdrant_store.ensure_collection(document.collection.value,len(vector))
        backend = await self.qdrant_store.upsert_point(
            collection=document.collection.value,
            point_id=document.document_id,
            vector=vector,
            payload=document.payload,
        )

        return IndexWriteResult(
            document_id=document.document_id,
            source_type=document.source_type,
            source_id=document.source_id,
            collection=document.collection,
            indexed=True,
            backend=backend
        )

    async def index_sample(self, sample:AttackSample)->IndexWriteResult:
        document = self.build_sample_document(sample)
        return await self.index_document(document)

    async def index_evidence(self,event:EvidenceEvent) -> IndexWriteResult:
        document = self.build_evidence_document(event)
        return await self.index_document(document)


    async def search_similar(self, request)->list[SimilarCaseResult]:
        # 你要做的事：
        # 1. 把 request.query 转成 query_vector
        # 2. 调用 qdrant_store.search
        # 3. 把底层返回的 id、score、payload 转成 SimilarCaseResult
        # 4. 搜索结果必须保留 source_type 和 source_id，方便追溯
        query_vector = await self.embedding_provider.embed(request.query)
        raw_results = await self.qdrant_store.search(
            collection=request.collection.value,
            query_vector=query_vector,
            limit=request.limit,
            filters=request.filters,
        )

        return [
            SimilarCaseResult(
                document_id=item["id"],
                source_type=IndexSourceType(item["payload"]["source_type"]),
                source_id=item["payload"]["source_id"],
                score=float(item["score"]),
                title=str(item["payload"].get("title", "")),
                summary=str(item["payload"].get("summary", "")),
                payload=item["payload"],
            )
            for item in raw_results
        ]

semantic_index_service = SemanticIndexService()












