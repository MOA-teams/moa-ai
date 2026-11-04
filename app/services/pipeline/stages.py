from enum import StrEnum


class PipelineStage(StrEnum):
    ANALYZE = "문의 분석"
    CHECK_INFORMATION = "정보 부족 판단"
    FIND_SIMILAR = "유사 문의 탐색"
    RETRIEVE = "RAG 검색"
    VALIDATE_EVIDENCE = "근거 검증"
    GENERATE_DRAFT = "초안 생성"
    REVIEW = "별도 검수"


STAGE_ORDER = tuple(PipelineStage)
# Generation and REVIEW must remain separate executions.
