# MOA AI 1차 구현 인수인계 — 2026-11-05

> 아래는 1차 구현 당시의 인수인계 기록입니다. 후속 구현과 코드 리뷰 수정이 반영된 현재 상태는 README와 [CodeGuide.md](CodeGuide.md)를 확인하세요. 아래의 미구현/501 설명은 당시 기준입니다.

## 작업 요청

이 저장소의 기존 구현과 `docs/InterfaceRule.md`, `docs/APISpec.md`, `docs/Integration.md`를 읽고 남은 AI 기능을 단계별로 구현해 주세요. 기존 요청/응답 계약을 유지하고, 단계마다 의미 있는 테스트와 실행 방법을 제공해 주세요.

- AI 저장소: https://github.com/MOA-teams/moa-ai
- 백엔드 저장소: https://github.com/MOA-teams/moa-backend
- 백엔드의 인터페이스 규칙에 변경이 있으면 로컬 snapshot과 비교해 계약 차이부터 확인하세요.
- PostgreSQL 구축·테이블·마이그레이션은 다른 백엔드 담당자의 범위입니다. AI 저장소에 추가하지 마세요.
- AI 담당: 문의 분석, 정보 부족 판단, 유사 문의 탐색, RAG, 근거 검증, 초안 생성, 별도 검수, 실행 관리.
- 백엔드 담당: 인증/권한, 문의·채팅·첨부, 접수 안내, 분석 요청/반영, 그룹 확정/알림, 승인/발송, 답변 평가, GitHub 연동.
- GitHub 자료 수집은 백엔드가 담당하고 AI는 전달받은 자료를 인덱싱합니다. 로컬 LLM 전환은 향후 방향이며 현재는 Gemini API입니다.

## 현재 구현 및 확인한 내용

| 기능 | 상태/진입점 |
|---|---|
| 문의 분석 | `app/services/pipeline/analyzer.py`: Gemini 구조화 출력으로 분류·긴급도·요약·증상·오류 코드·정보 부족 여부 판단 |
| 추가 질문/검색어 | 정보 부족 시 질문, 충분할 때 검색어 반환. 실제 채팅 전송은 하지 않음 |
| RAG 자료 등록 | `app/services/rag/service.py`: 문서 분할 → Gemini 임베딩 → 로컬 Chroma 저장 |
| 검색/출처 | 문서 ID·버전·청크·점수 반환. 고객 공개 자료와 내부 자료 분리 |
| 자료 변경 | 폐기/review_required/최신 정수 문서 버전/근거 문서 의존성을 검색에 반영 |
| 우수 답변 | 표현 참고 자료로 분리. 사실 근거로 취급하지 않음. 새 우수 답변을 선정하는 기능은 없음 |
| 임시 HTTP 테스트 | `POST /internal/v1/pipeline-preview`: 분석 → 추가 질문 또는 RAG 검색 |
| 백엔드 콜백 | `app/services/backend_client.py`에 전달 클라이언트와 제한 재시도만 구현. 실행 흐름에 미연결 |
| 자동 테스트 | 73개 통과. 대체 LLM/임베딩과 실제 임시 Chroma로 계약·분기·저장/검색 동작 확인 |
| 실제 외부 호출 | 저장 오류 문의의 Gemini 분석 → Chroma 검색 200 확인. DOC-005가 검색됨 |

자동 테스트 통과는 LLM 분류 정확도·답변 품질 평가 통과를 뜻하지 않습니다. 실제 Gemini 추가 질문 호출은 한 번 502로 실패했고 안정성을 입증하지 못했습니다.

## 오류 및 토큰 관련 구분

- `401 UNAUTHORIZED`: `MOA_INTERNAL_TOKEN`과 Swagger 인증 값이 불일치하거나 누락됨.
- `403 Tenant does not match service context`: `X-Tenant-ID`와 JSON의 `tenant_id`가 다름. 테스트에서는 둘 다 `syncnote`.
- `501 FEATURE_NOT_IMPLEMENTED`: 정식 파이프라인이 의도적으로 미구현 상태임. API 키/크레딧 부족 때문에 발생하는 오류가 아님.
- 과거 키로 크레딧 부족을 나타내는 402 응답이 있었지만, 키 교체 후 현재 설정 모델의 성공 호출을 확인함. 현재 모든 오류를 크레딧 부족으로 단정하지 마세요.
- Gemini 호출 중 503/504 및 외부 호출 실패 이력이 있음. 마지막 추가 질문 HTTP 호출은 `502 GEMINI_UNAVAILABLE`. 이 응답만으로 계정 한도·혼잡 등 구체적 원인을 확정할 수 없음.
- 모델이 반환한 오류 코드는 표기를 정규화한 뒤 고객 입력에 있는지 검사함. 입력에 없는 코드는 502로 차단함.
- 개발 에이전트의 작업 토큰과 Gemini API의 토큰/무료 한도, 내부 서비스 인증 토큰은 별개임. 남은 기능은 구현 전이며 토큰 부족이 그 기능들의 실패 원인으로 확인된 것은 아님.
- 원인 확인: `.venv/bin/python -m app.cli.analyze examples/analysis/error.json --debug`. 공급자 상태·키를 가린 메시지·소요 시간을 확인하고, 키·인증 헤더·고객 원문을 로그에 노출하지 마세요.

## 남은 작업과 권장 순서

1. **근거 검증 → 답변 초안 → 별도 검수**
   - 분석/검색 결과를 재사용해 `app/services/pipeline/orchestrator.py`에 단계별 연결.
   - 검색 후보를 바로 검증된 사실로 간주하지 말고 관련성·현재 유효성·버전·근거 간 충돌을 확인.
   - 우수 답변은 표현 참고로만 사용하고 가격/정책 등 사실은 현재 유효한 문서에서 확인.
   - 근거가 부족하거나 충돌하면 임의 답변을 만들지 말고 계약에 맞는 보류/검토 결과 반환.
   - 고객 답변에 내부 코드/자료가 유출되지 않게 할 것. 개발팀 처리 필요 여부는 가상 업무 권한 기준과 근거로 판단하고 코드에 언급되었다는 이유만으로 개발팀으로 단정하지 말 것.
   - 생성과 별도 검수 단계를 분리하고, 검수 결과/지원되지 않는 문장을 `PipelineResult`에 반영.
2. **유사 문의 탐색**
   - 현재 Chroma 검색은 자료 검색이며 문의 이력 검색/그룹화는 미구현.
   - 문의 이력 제공 계약을 백엔드와 맞춘 뒤 유사 문의 후보와 그룹 제안 반환.
   - E-502/E-413/E-401 등 다른 오류가 단순 문장 유사성으로 같은 그룹이 되지 않는지 확인. 최종 그룹 확정은 백엔드 책임.
3. **정식 실행 API와 결과 조회**
   - `app/api/internal/v1/pipeline_runs.py`의 POST/GET은 현재 501.
   - `app/workers/pipeline.py`, orchestrator를 연결하고 `202` 접수·상태 전이·실패 결과·멱등 처리 구현.
   - 상태/멱등키 영속 저장은 백엔드 제공 계약을 확인해 연결. 임의 PostgreSQL 스키마나 존재하지 않는 API를 만들지 말 것.
   - 임시 메모리 구현을 쓰는 경우 재시작 시 사라진다는 한계를 명시하고 영속 워커 완료로 표현하지 말 것.
4. **백엔드 결과 전달과 자동화 연결**
   - `BackendClient.deliver()`를 완료/실패 결과에 연결하고 중복 전달·재시도·복구 처리.
   - 정보 부족 → 질문 결과 전달 → 백엔드 자동 전송 → 새 고객 메시지의 분석 요청으로 재개.
   - 근거/검수 결과에 따라 자동 답변 가능 여부 또는 담당자 검토 필요 결과를 백엔드에 전달. 실제 발송은 백엔드 담당.
   - 오래된 inquiry_revision/based_on_message_id 결과나 자료 변경 전 결과가 최신 대화에 반영되지 않도록 백엔드와 확인.
5. **자료 변경 후 답변 갱신**
   - 현재는 오래된 자료·근거가 무효화된 우수 답변을 검색에서 제외하는 단계까지만 구현.
   - 변경 영향을 받는 우수 답변/초안 식별 → 최신 문서 기반 수정안 생성 → 별도 검수까지 추가 필요.
   - 최종 답변/우수 답변의 저장·교체 및 선정 신호 수집은 백엔드 계약과 역할을 확인해서 연결.
6. **시연/실제 평가**
   - 라벨은 평가 전용으로 유지하고 서비스 입력/검색에 넣지 말 것. `seed/labels`, `seed/demo`를 일괄 인덱싱하지 말 것.
   - 현재 `examples/rag/sources.json`은 직접 만든 검색 테스트 문서 2개일 뿐 전체 목데이터 importer가 아님.
   - 분류·정보 부족·근거 없음·충돌·폐기·자료 갱신·외부 API 실패 등 시나리오 확인.
   - 기본 검색 임계값 0.5는 미보정 시작값이며 정답 확률이 아님.

## 로컬 실행과 테스트

Python 3.11 이상. 저장소 루트에서 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

기존 `.env`가 있으면 덮어쓰지 마세요. 개인 키는 GitHub에 없습니다. 새 환경에서는 `.env`에 다음을 설정합니다.

- `MOA_GEMINI_API_KEY`: 본인 API 키.
- `MOA_GEMINI_MODEL`: 현재 개발 기본값 `gemini-3.5-flash-lite`. 해당 계정에서 사용 가능한지 실제 확인.
- `MOA_GEMINI_EMBEDDING_MODEL`: `gemini-embedding-001`, 차원 768.
- `MOA_INTERNAL_TOKEN`: 임의의 충분히 긴 내부 서비스 토큰.
- `MOA_ALLOWED_TENANTS=syncnote`, `MOA_RAG_ENABLED=true`, `MOA_ENABLE_PREVIEW=true`.
- `.env.example`의 preview 기본값은 false이므로 로컬 테스트 때 true로 변경 필요.

```bash
# 서버 시작 전에 테스트 자료 등록
.venv/bin/python -m app.cli.retrieve "저장 시 E-502 오류" --sources examples/rag/sources.json
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001
```

- Swagger: http://127.0.0.1:8001/docs
- Authorize에는 `MOA_INTERNAL_TOKEN` 값만 입력. Gemini 키가 아님.
- `POST /internal/v1/pipeline-preview`에서 X-Tenant-ID와 본문 tenant_id 모두 `syncnote`.
- 예시 `저장 오류 → 문서 검색` 또는 `정보 부족 → 추가 질문` 선택.
- 정식 `/pipeline-runs`는 구현 전까지 정상 분석 테스트용으로 쓰지 말 것.

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

Chroma 데이터는 `.data/chroma`이며 Git 제외. 경로당 서버 프로세스/worker 1개만 사용하고 서버 실행 중 같은 경로에 CLI 쓰기를 동시에 하지 마세요. 현재 RAG 인덱싱은 프로세스 내 백그라운드 작업이므로 서버 종료 후 자동 복구되는 영속 큐가 아닙니다. 다중 worker 운영 및 내구성 큐는 후속 작업입니다.

`.env`, `.venv`, `.data`는 커밋하지 마세요. 기존 API/인터페이스 snapshot을 구현 완료 명세로 오해하지 말고 README의 구현 상태와 실제 코드를 함께 확인해 주세요.
