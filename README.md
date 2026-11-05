# MOA AI

고객 문의 분석·RAG 검색·답변 초안 생성·별도 검수를 담당하는 FastAPI 서비스입니다.
백엔드가 AI 서버를 호출하며, AI 서버가 고객 채팅을 직접 저장하거나 발송하지 않습니다.

## 역할과 처리 흐름

- **백엔드:** PostgreSQL, 문의·채팅·첨부, 권한, 그룹 확정, 승인·발송, 답변 평가, GitHub 자료 수집.
- **AI:** 문의 분석·정보 부족 판단, 유사 문의 탐색, RAG, 근거 검증, 초안 생성·검수, 실행 관리.
- **생성 LLM:** AWS Bedrock 또는 Gemini. `MOA_LLM_PROVIDER`로 명시적으로 선택할 수 있습니다.
- **임베딩:** Gemini `gemini-embedding-001`, 기본 768차원. Bedrock을 선택해도 Gemini 키가 필요합니다.
- **벡터 DB:** Chroma `PersistentClient`. `.data/chroma`에 자료·청크·상태를 보존합니다.

문의 접수 → 분석 → 정보 부족 시 추가 질문 반환 → 충분하면 유사 문의·근거 검색 → 근거 검증 → 초안 생성 → 별도 검수 → 결과 저장·백엔드 콜백.
근거가 부족하거나 충돌하면 초안 없이 직접 확인 결과를 반환합니다. 검수에서 근거 없는 문장을 적발하면 제한된 1회 수정·재검수를 수행하고, 여전히 통과하지 못하면 자동 응답을 허용하지 않습니다.

## 설치 및 실행

Python 3.11 이상. 저장소 루트에서 실행합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

기존 `.env`가 있으면 덮어쓰지 마세요. `.env`에 내부 토큰과 사용할 LLM/임베딩 자격 증명을 입력합니다.
실행만 필요한 환경은 `requirements.txt`, 테스트·코드 검사까지 필요한 환경은 `requirements-dev.txt`를 사용합니다.

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

- Swagger: http://127.0.0.1:8001/docs
- 상태 확인: `GET /health`. 서버 응답만 검사하며 LLM 연결 상태를 보장하지 않습니다.
- `main:app` 실행도 지원합니다.
- PostgreSQL·Docker·마이그레이션 설정은 이 저장소에 없습니다.

## 환경 설정

| 변수 | 용도 |
|---|---|
| `MOA_INTERNAL_TOKEN` | 백엔드→AI 서비스 인증. 빈 값이면 내부 요청 차단 |
| `MOA_ALLOWED_TENANTS` | 허용된 회사 ID. 기본 `syncnote`, 쉼표로 구분 |
| `MOA_LLM_PROVIDER` | `bedrock` / `gemini` / `auto`. 자동 대체가 필요할 때만 `auto` 선택 |
| `MOA_AWS_REGION` | Bedrock 리전. `AWS_REGION` 별칭도 지원 |
| `MOA_BEDROCK_MODEL_ID` | Bedrock 모델 ID. `BEDROCK_MODEL_ID` 별칭도 지원 |
| `MOA_BEDROCK_MAX_TOKENS` | Bedrock 출력 토큰 상한 |
| `MOA_BEDROCK_API_KEY` | 선택적인 Bedrock Bearer 키. EC2 IAM Role 등 SDK 자격 증명도 지원 |
| `MOA_BEDROCK_SSH_HOST` / `MOA_BEDROCK_SSH_USER` / `MOA_BEDROCK_SSH_KEY_PATH` | 로컬 AWS 자격 증명이 없을 때 사용하는 선택적 개발용 SSH 중계. 원격 `$HOME/venv/bin/python`과 boto3 필요 |
| `MOA_GEMINI_API_KEY` | Gemini 생성 또는 임베딩 키. 복수 키는 쉼표로 구분 |
| `MOA_GEMINI_MODEL` | Gemini 생성 모델 |
| `MOA_GEMINI_FALLBACK_MODELS` | Gemini 대체 모델 목록. 예시 설정은 빈 값으로 불필요한 모델 순회를 끔 |
| `MOA_GEMINI_MAX_RETRIES` | 모델/키당 호출 시도 수. 기본 2이며 추가 재시도 횟수가 아님 |
| `MOA_GEMINI_TIMEOUT_MS` | Gemini 호출 제한 시간. 현재 Bedrock 제한 시간 계산에도 사용 |
| `MOA_ANALYSIS_MAX_INPUT_CHARS` | 분석 입력 문자 제한. 기본 30,000 |
| `MOA_GEMINI_EMBEDDING_MODEL` / `MOA_EMBEDDING_DIMENSIONS` | 임베딩 모델과 차원 |
| `MOA_CHROMA_PATH` | Chroma 저장 폴더 |
| `MOA_RAG_ENABLED` | RAG 활성화 |
| `MOA_ENABLE_PREVIEW` | 개발용 preview API 노출. 기본 비활성 |
| `MOA_RAG_MIN_SCORE` | 검색 최소 코사인 유사도. 기본 0.5, 정답 확률이 아님 |
| `MOA_BACKEND_BASE_URL` / `MOA_BACKEND_SERVICE_TOKEN` | AI→백엔드 콜백 주소와 토큰 |

생성 모델과 임베딩은 별도 호출입니다. 전체 파이프라인은 여러 LLM 단계를 호출하며, 충돌 재검증·초안 수정·제공자 대체에 따라 호출 수가 늘어날 수 있습니다. `auto`는 다른 제공자로 전환할 수 있으므로 비용·한도는 실제 설정을 기준으로 확인하세요.
키·개인키·실제 고객 자료를 커밋하지 않습니다. Bedrock은 클라우드 LLM이며 SSH 경유도 로컬 LLM 실행은 아닙니다.

## API 및 백엔드 연결

`/health` 외에는 `Authorization: Bearer <MOA_INTERNAL_TOKEN>`과 `X-Tenant-ID: syncnote`가 필요합니다. JSON의 `tenant_id`도 헤더와 같아야 합니다.

| API | 역할 |
|---|---|
| `POST /internal/v1/pipeline-runs` | `Idempotency-Key`로 중복을 검사하고 202 접수·백그라운드 실행 |
| `GET /internal/v1/pipeline-runs/{run_id}` | 진행 단계·완료/실패 결과 조회 |
| `POST /internal/v1/rag/sources` | 텍스트 자료 접수·분할·임베딩·저장 |
| `POST /internal/v1/rag/source-statuses` | 폐기·재검토 상태 일괄 반영 |
| `GET /internal/v1/rag/sources/{source_id}/versions/{version}?source_type=문서` | 인덱싱 상태·검색 가능 여부 조회. 경로 형태의 ID도 지원 |

완료/실패 결과는 설정된 백엔드의 `POST /internal/v1/pipeline-results`로 전달합니다. 콜백 실패와 AI 실행 실패는 별도로 관리합니다. 같은 멱등키·같은 요청은 기존 실행을 반환하며, 같은 키·다른 요청은 409입니다.

백엔드에서 접근 가능한 내부 주소로 AI 서버를 연결하면 됩니다. 같은 호스트는 localhost, 서로 다른 컨테이너·서버는 해당 내부 주소를 사용합니다. 실제 적용·자동 발송은 백엔드가 최신 문의·실행·근거와 권한을 다시 검사합니다.

## 로컬 테스트

다음 명령은 실제 LLM/임베딩 API를 호출합니다. 테스트 문서는 검색 확인용 가상 자료 2개입니다.

```bash
# 서버를 켜기 전에 테스트 자료 등록
.venv/bin/python -m app.cli.retrieve "저장 시 E-502 오류" --sources examples/rag/sources.json
# 문의 분석만 1회 실행. --debug는 키를 가린 오류 진단 추가
.venv/bin/python -m app.cli.analyze examples/analysis/error.json --debug
```

Swagger 테스트는 `.env`의 `MOA_ENABLE_PREVIEW=true` 설정 후 서버를 실행합니다.

1. Authorize에 Gemini 키가 아닌 `MOA_INTERNAL_TOKEN` 값만 입력합니다.
2. `POST /internal/v1/pipeline-preview`에서 회사 ID `syncnote`와 준비된 문의 예시를 선택합니다.
3. `full_pipeline=false`는 분석·검색, `true`는 초안·검수까지 실행합니다. 정보 부족이면 질문으로 먼저 종료합니다.
4. `POST /internal/v1/answer-revision-preview`로 기존 답변의 수정 필요 여부·수정안·검수 결과를 확인합니다.

Preview는 고객 메시지 발송·백엔드 콜백을 하지 않습니다. IDE용 요청 예시는 `test_main.http`에 있습니다.

## 자동 검증

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

자동 테스트는 임시 Chroma와 대체 LLM/임베딩 응답을 사용합니다. 실제 모델의 가용성·판정 정확도 평가와 구분합니다. GitHub Actions에서도 같은 검사를 수행합니다.

## 현재 한계

- 실행 기록·멱등키·콜백 상태는 메모리 저장이며 AI 서버 재시작 시 사라집니다. 영속 저장은 백엔드 제공 계약 연결 대상입니다.
- 같은 Chroma 경로에는 단일 프로세스/worker를 사용합니다. 서버 실행 중 같은 경로에 CLI로 동시에 쓰지 않습니다.
- 인덱싱은 프로세스 내 백그라운드 작업입니다. 서버 종료 시 대기 작업이 자동 복구되는 영속 큐는 없습니다.
- 첨부 ID는 받지만 이미지/PDF 내용을 직접 분석하지 않습니다.
- 문서 변경 영향 분석·답변 수정은 서비스/preview까지 구현돼 있습니다. 변경 이벤트부터 최종 저장까지의 자동 연결은 별도 연동 대상입니다.
- 그룹 확정·CS/개발팀의 처리 권한·승인·실제 발송은 백엔드 정책과 계약을 따릅니다.
- LLM 분류·근거 관련성·검수는 정답률을 보장하지 않습니다. 검색 점수와 자동 테스트 통과를 응답 정확도로 해석하지 않습니다.

## 문서

- [코드 읽기 가이드](docs/CodeGuide.md)
- [백엔드 연동 기준](docs/Integration.md)
- [API 계약](docs/APISpec.md)
- [인터페이스·목데이터 규칙](docs/InterfaceRule.md)
