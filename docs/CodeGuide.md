# MOA AI 코드 읽기 가이드

## 1. 가장 먼저 볼 실행 흐름

`POST /internal/v1/pipeline-runs`는 결과를 즉시 생성해서 반환하는 API가 아닙니다.
요청을 접수하고 `run_id`를 반환한 뒤, 같은 서버의 백그라운드 작업이 AI 단계를 실행합니다.
진행 상황과 결과는 GET으로 조회하고, 완료/실패 결과는 백엔드에도 전달합니다.

| 순서 | 파일 | 읽을 함수 | 역할 |
|---|---|---|---|
| 1 | `app/api/internal/v1/pipeline_runs.py` | `create_run` | 인증/회사 ID 검사 후 실행 접수, 백그라운드 작업 등록 |
| 2 | `app/workers/pipeline.py` | `PipelineRunStore.submit` | 중복 요청 검사, 실행 ID 발급, 최신 실행 기록 |
| 3 | 같은 파일 | `execute_pipeline` | 단계 진행 상태 저장, 결과 저장, 콜백 실행 |
| 4 | `app/services/pipeline/orchestrator.py` | `PipelineOrchestrator.execute` | 아래 AI 7단계를 순서대로 호출 |
| 5 | `app/workers/pipeline.py` | `deliver_result` | 최신 실행인지 검사 후 백엔드로 결과 전달 |

API 파일은 접수만, worker는 실행 기록과 전달만, orchestrator는 AI 처리 순서만 담당합니다.
따라서 각 단계가 어떻게 동작하는지 보려면 API 파일보다 orchestrator부터 읽는 게 편합니다.

## 2. AI 7단계

| 단계 | 담당 코드 | 입력 → 출력 |
|---|---|---|
| 문의 분석 | `analyzer.py`의 `InquiryAnalyzer.analyze` | 고객 대화 → 분류·긴급도·증상·요약·오류 코드·정보 부족 여부 |
| 정보 부족 판단 | `orchestrator.py`의 `needs_more_info` 분기 | 부족하면 추가 질문을 반환하고 여기서 종료 |
| 유사 문의 탐색 | `similar.py`의 `SimilarInquiryFinder.find` | 검색어 → 과거 문의 후보·그룹 제안. 최종 그룹 확정은 백엔드 |
| RAG 검색 | `app/services/rag/service.py`의 `RagService.search` | 검색어 임베딩 → Chroma 관련 자료·출처 |
| 근거 검증 | `drafter.py`의 `EvidenceValidator.validate` | 검색 후보 → 관련성·충돌·충분성을 판단한 근거 |
| 초안 생성 | 같은 파일의 `DraftGenerator.generate` | 검증된 근거 → 고객 답변 초안·인용 |
| 별도 검수 | `reviewer.py`의 `DraftReviewer.review` | 초안·근거 → 검수 항목별 결과·전체 결과 |

LLM에게 무엇을 판단시키는지는 각 파일 상단의 `*_PROMPT` 또는 `analysis_prompt.py`를 보면 됩니다.
생성 LLM은 `app/services/llm/bedrock.py`의 `create_llm`에서 Bedrock으로 준비합니다.
자료를 나누는 방식은 `rag/chunking.py`, 검색용 Bedrock Titan 임베딩 호출은 `rag/embedding.py`, 실제 Chroma 접근은 `rag/chroma_store.py`에 있습니다.

## 3. 코드로 확정하는 검사와 LLM 판단의 차이

**코드로 검사하는 것**

- 고객 입력에 없는 오류 코드를 모델이 만들어 냈는지.
- 자료가 공개 가능한지, 최신 버전인지, 폐기/재검토 상태인지, 인덱싱이 완료됐는지.
- 초안이 인용한 문서 ID가 허용된 근거에 있는지, 사용 문서와 인용 목록이 일치하는지.
- 모든 필수 검수가 명시적으로 통과했는지.
- 문의의 최신 실행인지, 같은 멱등키를 다른 내용으로 다시 사용했는지.

**LLM이 판단하는 것**

- 문의 카테고리·긴급도·추가 질문 필요 여부.
- 문서가 질문과 관련 있는지, 문서 내용이 서로 충돌하는지.
- 답변 문장이 사실 근거로 뒷받침되는지, 유효한 문서를 잘못 인용하지 않았는지.
- 기존 답변의 사실이 바뀌어 수정해야 하는지.

LLM이 판단하는 항목은 정답률을 보장하지 않습니다. 자동 테스트는 주로 대체 LLM 응답으로 연결과 예외 처리를 확인합니다.

## 4. 이번 수정의 핵심

### 검수 판정: `reviewer.py`

`combine_checks`가 검수 결과를 합치는 공통 기준입니다.

- 하나라도 실패면 전체 실패.
- 실패는 없지만 확인 불가/미실행이 있으면 전체 확인 불가.
- 모든 필수 항목이 통과한 경우에만 전체 통과.
- 기능 오류·후속 확인·담당자 재검토 요청은 자동 응답 범위를 통과시키지 않습니다.

근거 없는 문장을 검수가 적발하면 최대 한 번 수정·재검수를 수행합니다. 다시 통과하지 못한 답변을 자동 발송 가능으로 처리하지 않습니다.

### 출처/최신성: `grounding.py`

초안 생성과 기존 답변 수정에서 중복되던 검사를 모았습니다.

- `document_map`: 검증된 검색 결과에서 인용 가능한 문서 ID·버전 목록을 만듭니다.
- `verified_citations`: 존재하지 않는 문서, 누락된 인용, 사용하지 않은 문서의 인용을 거절합니다. 임의의 다른 문서로 교체하지 않습니다.
- `facts_are_current`: LLM 호출 전후 근거의 버전·source_revision·공개/유효/검색 가능 상태를 다시 확인합니다.
- `invalidate_verification`: 작성 중 근거가 바뀌면 검수를 실패로 변경하고 자동 발송을 금지합니다. 생성된 초안은 담당자 확인용으로 보존할 수 있습니다.

근거 검증에서 관련 문서 일부를 선택했는데 충돌도 보고되면, 선택한 문서 집합만 한 번 재검증합니다. 문서 하나를 선택했다는 이유로 충돌을 무시하지 않습니다.

최신 공개 자료 목록은 `ChromaVectorStore.list_current_public_sources`에서 한 번의 잠금 안에 가져옵니다.
검색과 답변 갱신이 동일한 최신 버전 선택·유효성 기준을 사용하므로, 최신 버전이 폐기되거나 인덱싱에 실패해도 이전 버전으로 돌아가지 않습니다.

### 기존 답변 수정: `revision.py`

`find_stale_best_answers`는 재검토할 답변 후보를 찾습니다.
`resolve_latest_facts`는 참조한 문서의 최신 공개 근거만 가져옵니다.
`revise_answer`는 기존 답변과 대조해 필요할 때 수정안을 만들고 별도 검수를 요청합니다.

- 참조 문서 일부라도 빠지면 담당자 확인으로 돌립니다.
- 내부 문서를 공개 문서로 바꾸지 않습니다.
- 영향 분석 중·별도 검수 중 문서가 바뀌면 정상 완료로 승인하지 않습니다.
- `revision_review_context`는 기존 답변도 같은 검수기를 사용하도록 검토 전용 입력을 만드는 함수입니다.
- `manual_revision`은 직접 확인이 필요한 결과를 만드는 함수입니다.

### 실행 기록: `workers/pipeline.py`

`PipelineRunStore`는 실행 기록을 메모리에 저장합니다.
`_by_idempotency`는 중복 요청, `_latest_revision`은 오래된 문의 버전, `_latest_run`은 같은 버전의 재분석까지 포함한 최신 실행을 관리합니다.
실행 ID는 `RUN-` 뒤에 UUID를 붙이므로 새 프로세스에서도 같은 카운터 값이 재사용되지 않습니다.

AI 실행 완료/실패와 백엔드 콜백 전달 완료/실패는 별개입니다.
콜백이 실패해도 AI 결과를 실패로 덮어쓰지 않고 조회할 수 있게 유지합니다.

## 5. 코드 리뷰 시 확인 순서

1. `orchestrator.py`: 추가 질문/직접 확인/초안 가능/실패의 네 가지 결과 분기를 확인합니다.
2. `reviewer.py`: `combine_checks`와 자동 응답 범위 제한을 확인합니다.
3. `grounding.py`: 인용을 거절하는 조건과 작성 중 자료 변경 검사를 확인합니다.
4. `revision.py`: 최신 근거가 없는 경우와 수정 불필요/필요 분기를 확인합니다.
5. `workers/pipeline.py`: 멱등 처리·최신 실행·콜백 실패 처리를 확인합니다.
6. `tests/test_pipeline_safety.py`: 이번 리뷰에서 발견한 문제들이 다시 발생하지 않는지 확인합니다.

기존 통합 시나리오는 `tests/test_pipeline.py`, 테스트용 LLM/임베딩·자료 생성 도구는 `tests/pipeline_support.py`에 있습니다.

```bash
.venv/bin/pytest tests/test_pipeline_safety.py -q
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

## 6. 유지되는 한계

- 실행 기록·멱등키는 메모리 저장이므로 AI 서버 재시작 시 사라집니다. 영속 실행 저장소는 백엔드 계약 연결 대상입니다.
- 같은 Chroma 경로에는 단일 서버 프로세스/worker만 사용합니다.
- 자료 최신성은 검사 시점을 기준으로 합니다. 이후 네트워크 전송 중 변경까지 원자적으로 묶을 수 없으므로, 백엔드는 실제 적용/발송 시 최신 문의·실행·근거를 다시 검사해야 합니다.
- 기존 답변 수정은 서비스/preview 제공 단계이며 정식 변경 이벤트·저장 API 연결은 별도 계약이 필요합니다.
- 자동 회귀 검사는 외부 LLM API의 가용성·실제 답변 품질을 보장하지 않습니다.
