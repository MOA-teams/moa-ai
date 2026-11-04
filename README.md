# MOA AI

고객 문의 분석·RAG·답변 초안·별도 검수를 담당하는 FastAPI 서비스의 초기 구조입니다.
백엔드 8000 / AI 8001로 분리하고 내부 계약은 `/internal/v1`을 사용합니다.

## 담당 범위

- **백엔드:** PostgreSQL 구축·스키마·마이그레이션·영속 데이터 관리, 채팅·권한·그룹 확정·발송·GitHub 연동.
- **AI:** 문의 분석·정보 부족 판단·유사 문의 탐색·RAG·근거 검증·초안 생성·별도 검수·파이프라인 실행 관리.
- **벡터 저장소:** 추후 AI 담당자가 선택할 수 있도록 인터페이스만 분리했습니다.

AI 저장소는 PostgreSQL에 직접 접속하거나 테이블을 생성/변경하지 않습니다.
실행 상태·멱등키·자료 메타데이터의 영속 저장은 백엔드와 연결 계약을 맞춰 구현합니다.
아직 없는 백엔드 저장 API를 임의로 만들어 호출하지 않습니다.

## 현재 구현 범위

- FastAPI 서버와 `/health`.
- 내부 API 5개의 요청/응답 모델·라우트, 서비스 Bearer 인증과 테넌트 검사.
- 공식 `google-genai` SDK 기반 비동기 Gemini 구조화 JSON 어댑터.
- 고정된 백엔드 주소로 결과를 전달하는 콜백 클라이언트와 제한된 재시도.
- 벡터 저장소 인터페이스, 파이프라인 단계/워커 진입점, 테스트·CI.

파이프라인 실행·상태 저장 연동·검색 인덱싱·소스 무효화는 아직 구현 전입니다.
관련 API는 인증·입력 검사를 통과한 후 **501 FEATURE_NOT_IMPLEMENTED**를 반환합니다.
Gemini 어댑터는 준비되어 있지만 라우트에 연결되지 않았습니다. 실제 LLM 호출과 전체 연동은 후속 구현 대상입니다.

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

## 환경 설정

| 변수 | 용도 |
|---|---|
| MOA_INTERNAL_TOKEN | 백엔드→AI 서비스 토큰; 비어 있으면 내부 요청 차단 |
| MOA_ALLOWED_TENANTS | 토큰이 접근할 수 있는 테넌트 목록; 기본 syncnote |
| MOA_GEMINI_API_KEY | Google AI Studio 키 |
| MOA_GEMINI_MODEL | 본인 계정에서 사용 가능한 모델 ID |
| MOA_GEMINI_TIMEOUT_MS | 모델 HTTP 제한 시간 |
| MOA_BACKEND_BASE_URL | 고정된 백엔드 주소 |
| MOA_BACKEND_SERVICE_TOKEN | AI→백엔드 콜백 토큰 |

Gemini 무료 사용 가능 여부·한도는 선택 모델/계정의 현재 조건을 확인합니다. 초기 프로젝트는 자동으로 API를 호출하지 않습니다.

## 백엔드 연결

| AI가 제공하는 API | 용도 |
|---|---|
| POST /internal/v1/pipeline-runs | 전체 파이프라인 실행 요청 |
| GET /internal/v1/pipeline-runs/{run_id} | 상태·결과 조회 |
| POST /internal/v1/rag/sources | 자료 인덱싱 요청 |
| POST /internal/v1/rag/source-statuses | 소스 유효성 반영 |
| GET /internal/v1/rag/sources/{source_id}/versions/{version} | 소스 상태 조회 |

공통 헤더: `Authorization: Bearer <service token>`, `X-Tenant-ID: syncnote`.
실행 요청은 `Idempotency-Key` 필수. 멱등 처리와 실행 상태 저장은 백엔드 연동을 포함한 후속 구현입니다.
백엔드 콜백 수신 경로: `POST /internal/v1/pipeline-results`.

## 검증

```bash
ruff check .
ruff format --check .
pytest
```

테스트·CI는 PostgreSQL이나 실제 Gemini API 없이 실행합니다.
계약 참고: [docs/APISpec.md](docs/APISpec.md), [docs/InterfaceRule.md](docs/InterfaceRule.md).
Gemini 어댑터 근거: [공식 Python SDK](https://googleapis.github.io/python-genai/).
