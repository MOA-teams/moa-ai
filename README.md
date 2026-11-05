# MOA AI

코드 읽기 순서와 단계별 책임, 리뷰 수정 내용은 [코드 읽기 가이드](docs/CodeGuide.md)를 참고하세요.

고객 문의 분석·RAG·답변 초안·별도 검수를 담당하는 FastAPI 서비스의 초기 구조입니다.
백엔드 8000 / AI 8001로 분리하고 내부 계약은 `/internal/v1`을 사용합니다.

## 담당 범위

- **백엔드:** PostgreSQL 구축·스키마·마이그레이션·영속 데이터 관리, 채팅·권한·그룹 확정·발송·GitHub 연동.
- **AI:** 문의 분석·정보 부족 판단·유사 문의 탐색·RAG·근거 검증·초안 생성·별도 검수·파이프라인 실행 관리.
- **벡터 저장소:** Chroma 로컬 영속 저장소. PostgreSQL 구축/변경은 하지 않습니다.

AI 저장소는 PostgreSQL에 직접 접속하거나 테이블을 생성/변경하지 않습니다.
실행 상태·멱등키·자료 메타데이터의 영속 저장은 백엔드와 연결 계약을 맞춰 구현합니다.
아직 없는 백엔드 저장 API를 임의로 만들어 호출하지 않습니다.

## 현재 구현 범위

- FastAPI 서버와 `/health`.
- 내부 API 5개의 요청/응답 모델·라우트, 서비스 Bearer 인증과 테넌트 검사.
- 공식 `google-genai` SDK 기반 비동기 Gemini 구조화 JSON 어댑터.
- 고정된 백엔드 주소로 결과를 전달하는 콜백 클라이언트와 제한된 재시도.
- Chroma 문서 분할·Gemini 임베딩·검색·출처 반환·유효성 필터, 자료 등록/상태 API.
- 7단계 파이프라인 오케스트레이터(`ANALYZE` → `CHECK_INFORMATION` → `FIND_SIMILAR` → `RETRIEVE` → `VALIDATE_EVIDENCE` → `GENERATE_DRAFT` → `REVIEW`):
  - 근거 검증(`app/services/pipeline/drafter.py`): 공개 사실 근거 유무·오류 코드 일치·버전 충돌 검증.
  - 답변 초안 생성(`app/services/pipeline/drafter.py`): 현재 유효 문서(`facts`)만 사실 근거로 사용하고 우수 답변(`style_examples`)은 표현 참고로 분리.
  - 별도 검수(`app/services/pipeline/reviewer.py`): 초안 생성과 분리된 LLM 검수 및 내부 코드/함수명 유출 차단 정규식 가드레일.
  - 유사 문의 탐색(`app/services/pipeline/similar.py`): Chroma의 `"문의 요약"` 소스를 검색해 후보 및 그룹을 제안하며 서로 다른 오류 코드(E-502/E-413/E-401) 간 오병합을 차단.
- 정식 `POST /internal/v1/pipeline-runs` (202 접수·비동기 실행·콜백 전달) 및 `GET /internal/v1/pipeline-runs/{run_id}` (상태·결과 조회·복구), `Idempotency-Key` 멱등 처리 및 오래된 `inquiry_revision` 콜백 차단.
  - 주의: 백엔드 영속 저장소(PostgreSQL) 연동 전까지 `PipelineRunStore`는 프로세스 내 인메모리 저장소로 동작하므로 서버 재시작 시 실행 기록이 초기화됩니다.
- 문서/코드 변경에 따른 기존 답변 영향 판별·수정안 생성·별도 검수(`app/services/pipeline/revision.py`) 및 오탐 방지(문서 버전이 올라도 해당 답변의 사실이 동일하면 수정 대상에서 제외).
- 로컬 테스트용 preview API(`POST /internal/v1/pipeline-preview`, `POST /internal/v1/answer-revision-preview`), 테스트·CI.

## 실행

Python 3.11 이상. 프로젝트 폴더에서 실행합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
# .env에 내부 서비스 토큰·Gemini 키·모델명 입력
uvicorn app.main:app --reload --port 8001
```

Swagger: http://localhost:8001/docs. `uvicorn main:app`도 지원합니다.
DB 설치·Docker·마이그레이션 없이 서버를 시작할 수 있습니다.
`/health`는 서버 실행 상태만 나타내며 AI 기능 완성을 의미하지 않습니다.

## 첫 기능: 문의 분석·정보 부족 판단

프로젝트 폴더의 `.env`에 아래 값을 입력합니다. `.env`는 Git에서 제외됩니다.

```dotenv
MOA_GEMINI_API_KEY=본인의_API_키
MOA_GEMINI_MODEL=gemini-3.5-flash-lite
```

모델은 본인 계정에서 사용 가능한 모델로 변경할 수 있습니다.
[Google 공식 모델 목록](https://ai.google.dev/gemini-api/docs/models)과
[요금/무료 사용 조건](https://ai.google.dev/gemini-api/docs/pricing)을 기준으로 확인하세요.

서버·DB·내부 인증 토큰 없이 문의 파일 하나를 분석합니다.

```bash
.venv/bin/python -m app.cli.analyze examples/analysis/error.json
```

오류 원인을 직접 확인하려면 `--debug`를 붙입니다.

```bash
.venv/bin/python -m app.cli.analyze examples/analysis/error.json --debug
```

실패 시 `diagnostics`에 모델명·공급자 HTTP 상태·키를 제거한 오류 메시지·소요 시간·설정된 제한 시간이 표시됩니다.
`provider_http_status=503`은 공급자 서비스 이용 불가, `504`는 공급자 응답 제한 시간 초과입니다.
`provider_http_status=null`, `cause_type=ReadTimeout`이면 클라이언트에서 대기 시간이 초과된 경우입니다.
메시지 원문/요청 본문/전체 예외 traceback/HTTP 헤더는 로그에 출력하지 않습니다.
모델·계정 상태와 요청 조건에 따라 원인이 다를 수 있으므로 `provider_message`도 함께 확인합니다.

- `account.json`: 비밀번호 변경 방법 문의
- `insufficient.json`: “안 돼요”처럼 설명이 부족한 문의
- `error.json`: 저장 시 E502 오류 문의

반환: 카테고리·긴급도·증상·요약·정규화 오류 코드·정보 부족 여부·추가 질문·검색어·짧은 판정 근거.
입력이 충분하면 검색어를 만들고, 부족하면 검색어 대신 추가 질문을 반환합니다.
증상/검색의 충분성과 최종 해결·원인 판정은 구분합니다. 이미 대화에 있는 정보를 다시 묻지 않도록 프롬프트에 명시합니다.

기능은 `app/services/pipeline/analyzer.py`, 판정 기준은 `analysis_prompt.py`에 있습니다.
정보 부족 판정과 분류는 Gemini 출력이며 정확성을 보장하지 않습니다. 오류 코드의 입력 근거와 출력 구조는 코드로 추가 검사합니다.
이 실행은 문의 분석 1회만 호출하며 RAG 검색·답변 생성·고객 발송·콜백을 실행하지 않습니다.
첨부 이미지/PDF의 실제 내용 분석은 이번 기능에서 제외했습니다.

무료 한도 초과/인증·모델 설정 오류/타임아웃/잘못된 JSON 응답은 별도 error.code로 반환합니다.
입력이 과도하게 크면 원문을 조용히 잘라내지 않고 모델 호출 전 거절합니다.
자동 테스트는 제공자 응답을 대체해 연결과 분기를 검사하며, 실제 Gemini의 분류 정확도를 측정한 결과는 아닙니다.

## Chroma RAG 검색과 Swagger 임시 테스트

Chroma는 `.data/chroma`에 저장하고 Git에서 제외합니다. 별도 PostgreSQL/Docker 설정은 필요 없습니다.
대화·고객·최종 답변 테이블은 만들지 않으며 검색용 소스/청크만 저장합니다.

```bash
# 최초 1회: 테스트 문서 두 개를 임베딩하고 Chroma 검색 확인
.venv/bin/python -m app.cli.retrieve "저장 시 E-502 오류" --sources examples/rag/sources.json
# 서버 실행 (Chroma 경로당 프로세스/worker는 1개)
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001
```

서버 실행 중 같은 경로에 CLI를 동시에 연결해 쓰지 않습니다. 운영 다중 프로세스 연결은 Chroma 서버 구성으로 별도 전환합니다.
테스트 문서는 직접 만든 작은 목 문서이며 실제 고객사 문서/정답 labels가 아닙니다.

1. http://127.0.0.1:8001/docs 접속.
2. **Authorize**에 `.env`의 `MOA_INTERNAL_TOKEN` 값을 입력합니다. Gemini API 키가 아닙니다.
3. `POST /internal/v1/pipeline-preview` 선택 → **Try it out**.
4. `X-Tenant-ID`에 `syncnote` 입력, 예시에서 **저장 오류 → 문서 검색** 선택 → **Execute**.
5. `analysis`와 `retrieval.facts`의 문서 ID·버전·청크·검색 점수를 확인합니다.
6. **정보 부족 → 추가 질문** 예시를 선택하면 질문과 `retrieval: null`이 반환됩니다.

preview는 `MOA_ENABLE_PREVIEW=true`인 로컬 개발 환경에서만 노출합니다.
저장된 문서가 없거나 주제와 맞지 않으면 facts가 빈 배열일 수 있습니다. 답변을 지어내서 채우지 않습니다.
`POST /internal/v1/pipeline-preview`는 기본적으로 백엔드 콜백 없이 분석·검색까지만 실행하며, `?full_pipeline=true` 쿼리를 붙이면 근거 검증·초안 생성·별도 검수까지 로컬에서 미리 확인할 수 있습니다. 문서 개정에 따른 기존 답변 수정안 미리보기는 `POST /internal/v1/answer-revision-preview`로 테스트할 수 있습니다.

자료 변경 테스트는 `POST /internal/v1/rag/source-statuses`로 review_required=true를 설정하고 검색에서 빠지는지 확인합니다.
같은 소스의 상태 변경은 source_revision을 올리고 event_id를 새로 사용합니다. 중복 이벤트는 재적용하지 않습니다.
새 정수 문서 버전이 등록되면 이전 버전은 검색에서 제외합니다. 새 버전 인덱싱 실패 시 낡은 버전으로 되돌아가지 않습니다.
우수 답변은 형식 참고로 분리하며 폐기된 문서 근거가 연결되면 검색하지 않습니다.
내부 코드/자료는 고객 답변 검색에 포함하지 않습니다. 코드 커밋의 활성 상태는 백엔드가 source-statuses로 전달해야 합니다.

검색 점수는 정답 확률이 아닙니다. 기본 임계값 0.5는 시작값이며 실데이터로 조정해야 합니다.
로컬 Chroma의 상태 변경은 단일 프로세스 잠금과 일괄 쓰기를 사용합니다. 다중 worker에서의 원자성을 주장하지 않습니다.
서버 종료/재시작 중 대기했던 인덱싱은 같은 source 요청을 다시 보내 재실행합니다. 내구성 큐 복구는 아직 없습니다.

## 환경 설정

| 변수 | 용도 |
|---|---|
| MOA_INTERNAL_TOKEN | 백엔드→AI 서비스 토큰; 비어 있으면 내부 요청 차단 |
| MOA_ALLOWED_TENANTS | 토큰이 접근할 수 있는 테넌트 목록; 기본 syncnote |
| MOA_GEMINI_API_KEY | Google AI Studio 키 |
| MOA_GEMINI_MODEL | 본인 계정에서 사용 가능한 모델 ID |
| MOA_GEMINI_TIMEOUT_MS | 모델 HTTP 제한 시간 |
| MOA_ANALYSIS_MAX_INPUT_CHARS | 문의 분석 입력 문자 제한; 기본 30,000 |
| MOA_GEMINI_EMBEDDING_MODEL | 검색용 임베딩 모델; 기본 gemini-embedding-001 |
| MOA_EMBEDDING_DIMENSIONS | 임베딩 차원; 기본 768 |
| MOA_CHROMA_PATH | 로컬 Chroma 폴더; 기본 .data/chroma |
| MOA_RAG_ENABLED | Chroma RAG 활성화 |
| MOA_ENABLE_PREVIEW | 로컬 분석·검색 테스트 API 노출 |
| MOA_RAG_MIN_SCORE | 검색 후보 최소 코사인 유사도; 기본 0.5 |
| MOA_BACKEND_BASE_URL | 고정된 백엔드 주소 |
| MOA_BACKEND_SERVICE_TOKEN | AI→백엔드 콜백 토큰 |

Gemini 무료 사용 가능 여부·한도는 선택 모델/계정의 현재 조건을 확인합니다. 서버 시작·테스트는 API를 호출하지 않으며, 분석 실행 도구는 Gemini API를 호출합니다.

## 백엔드 연결

| AI가 제공하는 API | 용도 |
|---|---|
| POST /internal/v1/pipeline-runs | 전체 파이프라인 실행 요청 |
| GET /internal/v1/pipeline-runs/{run_id} | 상태·결과 조회 |
| POST /internal/v1/rag/sources | 자료 인덱싱 요청 |
| POST /internal/v1/rag/source-statuses | 소스 유효성 반영 |
| GET /internal/v1/rag/sources/{source_id}/versions/{version} | 소스 상태 조회 |

공통 헤더: `Authorization: Bearer <service token>`, `X-Tenant-ID: syncnote`.
실행 요청은 `Idempotency-Key` 필수(동일 키·동일 본문은 기존 `run_id` 반환, 동일 키·다른 본문은 `409 IDEMPOTENCY_CONFLICT`). 실행 상태와 멱등키 저장은 현재 프로세스 내 인메모리(`PipelineRunStore`)로 동작하며, 재시작 시 초기화됩니다(영속 워커/DB 저장은 백엔드 계약 연결 대상).
백엔드 콜백 수신 경로: `POST /internal/v1/pipeline-results`. 콜백 전달이 실패해도 AI 내부 실행 결과(`run_status=완료/실패`)는 유지되어 `GET /internal/v1/pipeline-runs/{run_id}`로 복구 조회할 수 있습니다.

## 검증

```bash
ruff check .
ruff format --check .
pytest
```

자동 테스트·CI는 임시 Chroma와 대체 임베딩/LLM 응답을 사용하며 PostgreSQL과 실제 Gemini API를 호출하지 않습니다.
계약 참고: [docs/APISpec.md](docs/APISpec.md), [docs/InterfaceRule.md](docs/InterfaceRule.md).
Gemini 어댑터 근거: [공식 Python SDK](https://googleapis.github.io/python-genai/).
