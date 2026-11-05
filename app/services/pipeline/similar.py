"""과거 문의를 검색하고 다른 오류 코드가 같은 그룹으로 묶이지 않도록 거른다."""

import re

from app.schemas.analysis import InquiryAnalysis
from app.schemas.pipeline import PipelineRequest, SimilarInquiries, SimilarInquiry
from app.schemas.rag import SearchHit, SearchRequest
from app.services.pipeline.analyzer import CODE_PATTERN
from app.services.rag.service import RagService

INQUIRY_ID_PATTERN = re.compile(r"^Q-\d{4}$")


def _extract_codes(text: str, metadata: dict) -> set[str]:
    codes: set[str] = set()
    meta_code = metadata.get("normalized_error_code") or metadata.get("error_code")
    if isinstance(meta_code, str):
        for match in CODE_PATTERN.finditer(meta_code):
            area, digits = match.groups()
            codes.add(f"E-{area.upper()}-{digits}" if area else f"E-{digits}")
    for match in CODE_PATTERN.finditer(text):
        area, digits = match.groups()
        codes.add(f"E-{area.upper()}-{digits}" if area else f"E-{digits}")
    return codes


class SimilarInquiryFinder:
    def __init__(self, rag: RagService | None, *, min_score: float = 0.5, top_k: int = 10):
        self.rag = rag
        self.min_score = min_score
        self.top_k = top_k

    async def find(
        self,
        request: PipelineRequest,
        analysis: InquiryAnalysis,
    ) -> SimilarInquiries:
        if self.rag is None:
            return SimilarInquiries(items=[], suggested_group_id=None, reason=None)

        query_text = analysis.search_query or analysis.summary
        search_req = SearchRequest(
            tenant_id=request.tenant_id,
            query=query_text,
            purpose="내부 조사",
            top_k=self.top_k,
            min_score=self.min_score,
        )
        try:
            search_result = await self.rag.search(search_req, source_types={"문의 요약"})
        except TypeError:
            search_result = await self.rag.search(search_req)

        candidates = [
            hit
            for hit in [*search_result.internal_findings, *search_result.facts]
            if hit.source_type == "문의 요약"
        ]

        target_code = analysis.normalized_error_code
        best_by_inquiry: dict[str, tuple[SimilarInquiry, set[str], str | None]] = {}

        for hit in candidates:
            inquiry_id = hit.metadata.get("inquiry_id") or hit.source_id
            if not isinstance(inquiry_id, str) or not INQUIRY_ID_PATTERN.fullmatch(inquiry_id):
                continue
            if inquiry_id == request.inquiry_id:
                continue

            candidate_codes = _extract_codes(f"{hit.title} {hit.text}", hit.metadata)
            # Prevent merging inquiries with different error codes (e.g., E-502 vs E-413 vs E-401)
            if target_code is not None:
                if candidate_codes and target_code not in candidate_codes:
                    continue
                if not candidate_codes and hit.score < max(0.85, self.min_score):
                    continue
            elif candidate_codes:
                # Current inquiry reported no error code; do not merge into error-code group
                continue

            candidate_cat = hit.metadata.get("category")
            if (
                isinstance(candidate_cat, str)
                and candidate_cat
                and candidate_cat != analysis.category.value
                and target_code is None
            ):
                continue

            group_id = hit.metadata.get("group_id")
            if not isinstance(group_id, str) or not group_id.strip():
                group_id = None

            clamped_score = round(max(0.0, min(1.0, hit.score)), 4)
            item = SimilarInquiry(
                inquiry_id=inquiry_id,
                group_id=group_id,
                score=clamped_score,
            )
            prev = best_by_inquiry.get(inquiry_id)
            if prev is None or item.score > prev[0].score:
                best_by_inquiry[inquiry_id] = (item, candidate_codes, candidate_cat)

        sorted_entries = sorted(
            best_by_inquiry.values(), key=lambda entry: entry[0].score, reverse=True
        )[:5]
        items = [entry[0] for entry in sorted_entries]

        suggested_group_id: str | None = None
        reason: str | None = None
        for item, codes, _ in sorted_entries:
            if item.group_id is not None:
                suggested_group_id = item.group_id
                if target_code and target_code in codes:
                    reason = f"오류 코드({target_code})와 증상이 일치"
                else:
                    reason = "유사 문의 증상과 카테고리가 일치"
                break

        if not items:
            return SimilarInquiries(items=[], suggested_group_id=None, reason=None)

        if reason is None:
            if target_code:
                reason = f"동일 오류 코드({target_code}) 관련 유사 문의"
            else:
                reason = "유사 증상 문의 후보"

        return SimilarInquiries(
            items=items,
            suggested_group_id=suggested_group_id,
            reason=reason,
        )


__all__ = ["SearchHit", "SimilarInquiryFinder"]
