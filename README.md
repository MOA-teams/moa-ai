# MOA AI

고객 문의 분석·RAG·답변 초안·별도 검수를 담당하는 FastAPI 서비스의 초기 구조입니다.
`moa-backend`와 같은 `app/api`, `core`, `db`, `deps`, `schemas`, `services`, `workers` 구성을 사용합니다.
백엔드 8000 / AI 8001로 분리하고 내부 계약은 `/internal/v1`을 사용합니다.

## 현재 구현 범위

- FastAPI 서버, `/health`, PostgreSQL 연결 검사 `/health/ready`.
- 내부 API 5개의 요청/응답 모델·라우트, 서비스 Bearer 인증과 테넌트 검사.
- PostgreSQL(async SQLAlchemy/asyncpg), AI 전용 테이블·Alembic 초기 migration.
- 공식 `google-genai` SDK 기반 비동기 Gemini 구조화 JSON 어댑터.
- 고정된 백엔드 주소로 결과를 전달하는 콜백 클라이언트와 제한된 재시도.
- 벡터 DB 교체 인터페이스, 파이프라인 단계/워커 진입점, 테스트·CI.

**초기 커밋의 기능 경계:** 파이프라인 실행/저장, 검색 인덱싱, 소스 무효화는 아직 구현하지 않았습니다.
해당 내부 API는 인증·입력 검사를 통과한 후 **501 FEATURE_NOT_IMPLEMENTED**를 반환합니다.
가짜 분석/검색 결과나 202 성공 응답을 만들지 않습니다. Gemini 어댑터는 준비되어 있지만 API 라우트에 연결되지 않았습니다.
실제 LLM 호출과 전체 백엔드 연동은 후속 구현 대상입니다.

## 실행

Python 3.12 이상 권장. 아래 명령은 프로젝트 폴더에서 실행합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
# .env에서 내부 서비스 토큰·Gemini 키·사용 가능한 모델명을 입력
# PostgreSQL이 없다면 로컬 개발용 컨테이너 실행
docker compose up -d postgres
alembic upgrade head
uvicorn app.main:app --reload --port 8001
```

Swagger: http://localhost:8001/docs. 백엔드와 같은 `uvicorn main:app` 실행도 지원합니다.
`/health`는 외부 의존성 없이 응답하고, `/health/ready`는 PostgreSQL 연결을 확인합니다.
ready는 Gemini/벡터DB/파이프라인 기능 완성을 의미하지 않습니다.
동일 PostgreSQL 인스턴스를 써도 AI 테이블만 migration하며 백엔드 문의·채팅 테이블을 생성/변경하지 않습니다.
기존 DB에 적용 전 AI migration 이력과 테이블 소유권을 분리해 주세요(AI는 `ai_alembic_version` 사용).

## 환경 설정

| 변수 | 용도 |
|---|---|
| MOA_DATABASE_URL | `postgresql+asyncpg://...` |
| MOA_INTERNAL_TOKEN | 백엔드→AI 서비스 토큰; 비어 있으면 내부 요청 차단 |
| MOA_ALLOWED_TENANTS | 해당 서비스 토큰이 접근 가능한 테넌트 목록; 기본 syncnote |
| MOA_GEMINI_API_KEY | Google AI Studio 키 |
| MOA_GEMINI_MODEL | 본인 계정에서 사용 가능한 Gemini 모델 ID |
| MOA_GEMINI_TIMEOUT_MS | 모델 HTTP 제한 시간 |
| MOA_BACKEND_BASE_URL | 고정된 백엔드 주소; 요청 본문에서 URL을 받지 않음 |
| MOA_BACKEND_SERVICE_TOKEN | AI→백엔드 콜백 토큰 |

Gemini 무료 사용 가능 여부·한도는 선택한 모델/계정의 현재 조건을 확인합니다. 이 초기 프로젝트는 자동으로 API를 호출하지 않습니다.
벡터 DB는 지정하지 않았으며 PostgreSQL에 임베딩 확장 설치를 강제하지 않습니다.
여러 고객사에 배포할 때는 토큰별 테넌트 범위를 분리하는 인증 저장소로 확장해야 합니다.

## 백엔드 연결

| AI가 제공하는 API | 용도 |
|---|---|
| POST /internal/v1/pipeline-runs | 전체 파이프라인 실행 요청 |
| GET /internal/v1/pipeline-runs/{run_id} | 상태·결과 조회 |
| POST /internal/v1/rag/sources | 자료 인덱싱 요청 |
| POST /internal/v1/rag/source-statuses | 소스 유효성 반영 |
| GET /internal/v1/rag/sources/{source_id}/versions/{version} | 소스 상태 조회 |

공통 헤더: `Authorization: Bearer <service token>`, `X-Tenant-ID: syncnote`.
본문 tenant_id와 헤더를 대조하고, 허용 목록 밖의 테넌트는 거절합니다.
실행 요청은 `Idempotency-Key` 필수. 멱등 저장 동작은 후속 repository 구현 대상입니다.
백엔드가 구현할 콜백 수신 경로: `POST /internal/v1/pipeline-results`.
백엔드 저장소에는 현재 라우트 뼈대만 있으므로 콜백 수신 기능 구현 후 연결합니다.

## 개발 순서

1. DB repository와 durable worker를 연결하고 실행·멱등·재시작 복구 구현.
2. 문의 분석 → 정보 부족 → 유사 문의 → RAG → 근거 검증 → 초안 → 별도 검수 구현.
3. 선택한 벡터 DB·임베딩 어댑터와 자료 상태/revision 필터 연결.
4. 결과 저장·콜백 전달 이력·실패 복구 연결. 최종 발송/그룹 배정은 백엔드 책임.

## 검증

```bash
ruff check .
ruff format --check .
pytest
alembic upgrade head
alembic check
```

테스트는 외부 Gemini API를 호출하지 않습니다. CI에서는 PostgreSQL migration도 실행합니다.
계약 참고: [docs/APISpec.md](docs/APISpec.md), [docs/InterfaceRule.md](docs/InterfaceRule.md).
Gemini 어댑터 근거: [공식 Python SDK](https://googleapis.github.io/python-genai/).
