> DB 담당 범위 정정: PostgreSQL 구축·스키마·마이그레이션·영속 데이터 관리는 백엔드 담당입니다. 아래 AI 실행/자료 저장 항목은 필요한 데이터 계약을 설명하며, AI 저장소에서 PostgreSQL을 구축한다는 의미가 아닙니다. 벡터 저장소 선택과 RAG 검색은 AI 담당 범위로 유지합니다.

# MOA API 명세서 · 확정 역할 분배 반영본

작성일: 2026-11-04. 이 문서를 현재 구현 기준으로 사용한다.
싱크노트 인터페이스의 snake_case·한글 enum·ID·evidence 형식을 유지한다. 예시는 가상 데이터이며 실제 API 구현 또는 연동 테스트 결과가 아니다.

## 1. 역할과 API 경계

| LLM 파이프라인 담당 | 나머지 백엔드 담당 |
|---|---|
| 문의 분석·정보 부족 판단 | 인증·역할별 접근 제어 |
| 유사 문의 탐색 | 문의·채팅·첨부·자동 접수 안내 |
| RAG 검색·근거 검증 | 분석 요청 접수·결과 저장/반영 |
| 초안 생성·생성 후 별도 검수 | 실제 그룹 배정·급증 집계·검증 알림 |
| AI 파이프라인 실행·상태·재시도 관리 | 담당자 승인·수동/자동 발송·답변 평가 |
| 검색용 인덱스 생성·유효성 필터링 | GitHub 연결·자료 수집·webhook·이슈 생성 |

**중요한 경계**

- 파이프라인 내부 단계는 별도 공개 API로 만들지 않는다. `pipeline-runs` 한 번으로 순서대로 실행한다.
- LLM 담당이 단계 순서·AI 호출·재시도·실패 상태를 관리한다. 백엔드는 요청 전달·결과 적용·실제 메시지 발송을 관리한다.
- 유사 문의 탐색은 기존 그룹 후보 추천이다. 실제 그룹 생성·ID 발급·중복 병합·현재 시각 기준 급증 집계는 백엔드가 담당한다.
- LLM 담당은 근거 검증과 별도 검수 결과를 반환한다. 자동 발송의 허용 정책·권한·최신 문맥 검사와 실제 발송은 백엔드가 맡는다.
- GitHub는 백엔드가 접근 권한·보안 필터를 적용해 수집한다. LLM/RAG는 전달받은 자료만 검색한다.
- 일반 LLM 파이프라인은 코드를 바꾸거나 GitHub 이슈를 직접 생성하지 않는다.

## 2. 처리 흐름

```text
고객 → 백엔드: 문의/메시지 저장, 자동 접수 안내
백엔드 → LLM: 파이프라인 실행 요청(현재 고객 메시지·대화 revision)
LLM: 문의 분석 → 정보 부족 판단
  ├─ 부족: 추가 질문 생성 → 결과 반환
  └─ 충분: 유사 문의 탐색 → RAG 검색 → 근거 검증 → 초안 생성 → 별도 검수
LLM → 백엔드: 완료/실패 결과 콜백
백엔드: 최신 요청인지 확인 → 분석·그룹·초안·알림 반영
  ├─ 정보 부족: 안전한 추가 질문 자동 전송
  ├─ 자동 응대 허용 + 검수 통과 + 최신 근거/대화: 자동 답변
  └─ 그 외: 담당자 검토 → 수정·승인·발송
```

- 콜백 유실 복구는 파이프라인 결과 조회 API를 사용한다. 콜백과 조회 결과는 동일한 형식이다.
- 완료 콜백은 LLM의 고정된 서버 설정 URL로만 전송한다. 고객 요청의 임의 URL을 사용하지 않는다.
- 고객 새 메시지 수신 시 백엔드가 inquiry revision을 올리고 최신 run을 기록한다. 실행 접수와 콜백이 경합하면 해당 revision의 실행 등록을 먼저 완료한 뒤 결과를 적용하며, 아직 등록되지 않은 유효 run의 콜백은 재시도 가능한 응답으로 처리한다. 이전 run은 완료돼도 최신 결과를 덮어쓰거나 답변을 발송할 수 없다.
- 별도 검수는 초안 생성 후 별도 LLM 호출/검증 단계로 수행한다. 생성 결과의 자기 평가만으로 통과시키지 않는다.

## 3. 공통 규약

- 백엔드 외부 경로 `/api/v1`. LLM 내부 경로 `/internal/v1`. 콜백은 백엔드 `/internal/v1/pipeline-results`.
- JSON UTF-8, snake_case. 빈 값 null, 빈 배열 []. 날짜는 타임존 포함 ISO 8601, 예시는 +09:00.
- 고객 요청의 tenant_id/customer_id는 인증된 세션과 일치해야 한다. 첨부·문의·문서 등 모든 접근에서 테넌트/소유권을 검사한다.
- 내부 API는 서비스 인증 필수. 콜백도 서비스 인증과 run/tenant/inquiry 대조. API 토큰·GitHub 토큰·비밀번호는 로그에 남기지 않는다.
- 시드 Q-0001 등은 보존하고 신규 문의는 Q-1001부터 중복 없이 발급. 문의 Q-0000, 답변 A-0000, 베스트 BA-000, 문서 DOC-000, 그룹 G-000, 고객 U-0000, 담당자 agent_00 형식을 유지한다.
- 추가 리소스 예시: MSG-000001, DRAFT-000001, RUN-000001, ATT-0001. 버전은 별도 정수 필드.
- 목록은 cursor/limit(기본 20, 최대 100). 메시지는 서버가 발행한 불투명 cursor 사용, 클라이언트는 message_id로 중복 제거. 같은 시각/지연 저장 메시지를 놓치지 않도록 확정된 스트림 순서로 조회한다.
- 메시지 저장·파이프라인 생성·발송·이슈 생성·콜백에 Idempotency-Key. 동일 키/본문은 같은 결과, 동일 키/다른 본문은 409.
- 승인/자동 발송 직전에 최신 고객 메시지·문의 revision·초안 revision·지식 revision·자동 응대 정책을 검사한다. 변하면 재분석 또는 검토 대기로 전환한다.
- 자동/수동 발송은 동일 초안·기준 고객 메시지에 대해 원자적으로 한 번만 허용한다. 전달 완료 상태가 되기 전 발송 성공을 반환하지 않는다.
- 문서·소스 무효화 시 검색 필터/캐시를 즉시 갱신한다. 벡터 물리 삭제는 비동기여도 되지만 검색 차단은 즉시 보장한다.
- 실제 시드 labels/는 import·검색·프롬프트에 넣지 않는다. 목데이터 구현 작업은 API 명세의 범위에 포함하지 않는다.

### enum

| 필드 | 값 |
|---|---|
| category | 이용 방법 / 계정 / 결제 / 기능 오류 / 개선 요청 / 기타 |
| urgency | 높음 / 보통 / 낮음 |
| inquiry.status | 접수 / 담당자 확인 중 / 추가 정보 필요 / 답변 완료 / 상담 종료 |
| doc_type | 정책 / FAQ / 기능 명세 / 릴리즈 노트 |
| doc_status | 유효 / 폐기 |
| answer_status | 유효 / 재검토 필요 / 비활성 |
| draft_result | 초안 가능 / 직접 확인 필요 / 추가 질문 필요 |
| message.sender | customer / agent / ai_auto |
| message_type | 고객 메시지 / 접수 안내 / 정보 수집 질문 / 최종 답변 |
| run_status | 대기 / 처리 중 / 완료 / 실패 |
| verification.status | 통과 / 실패 / 확인 불가 / 미실행 |
| delivery_mode | 자동 / 담당자 |

현재 합의한 자동 응대 흐름에 따라 ai_auto는 접수 안내·정보 수집 질문·검증을 통과해 발송된 최종 답변에 사용한다. 이는 원본 규칙의 사용 범위를 확장한 부분이다. 역할 이름 customer/agent/admin은 인증용 enum이며 메시지 sender와 구분한다. 최종 답변 자동 전송 허용 범위는 백엔드 설정으로 제한한다.

문서 재검토는 doc_status에 값을 추가하지 않고 review_required=true로 관리한다. answer_status는 답변 재사용 상태이며 파이프라인 run_status와 다르다. 답변 완료는 발송 완료이지 해결 확인이 아니다.

공통 오류 예시:

```json
{
  "error": {
    "code": "STALE_DRAFT",
    "message": "새 메시지가 있어 초안을 다시 확인해야 합니다.",
    "request_id": "REQ-001"
  }
}
```

400 입력 오류, 401 인증 실패, 403 권한 없음, 404 대상 없음, 409 버전/상태 충돌, 413 용량 초과, 415 파일 형식 미지원, 429 호출 제한, 503 의존 서비스 오류.

## 4. 인증 API · 백엔드 담당

### AUTH01 · POST /auth/login

담당: **백엔드**

담당자 로그인. 고객은 싱크노트 앱의 검증된 고객 세션/서명된 위젯 인증을 사용한다. 아래 login은 담당자용이며 사용자/테넌트 생성 기능은 범위에서 제외한다.

요청 JSON:

```json
{
  "email": "agent@example.com",
  "password": "example-password"
}
```

응답 `200`:

```json
{
  "access_token": "access-token-example",
  "refresh_token": "refresh-token-example",
  "expires_in": 3600,
  "user": {
    "user_id": "agent_01",
    "tenant_id": "syncnote",
    "role": "agent"
  }
}
```

### AUTH02 · POST /auth/refresh

담당: **백엔드**

유효한 refresh token으로 갱신. 만료/폐기 토큰 거절, 새 토큰 발급 시 이전 refresh token 폐기.

요청 JSON:

```json
{
  "refresh_token": "refresh-token-example"
}
```

응답 `200`:

```json
{
  "access_token": "new-access-token-example",
  "refresh_token": "new-refresh-token-example",
  "expires_in": 3600
}
```

### AUTH03 · GET /auth/me

담당: **백엔드**

현재 인증 사용자와 역할 조회. 단순히 요청 role 값을 신뢰하지 않는다.

요청 본문 없음.

응답 `200`:

```json
{
  "user_id": "agent_01",
  "tenant_id": "syncnote",
  "role": "agent"
}
```

## 5. 문의·채팅·첨부·평가 API · 백엔드 담당

### C01 · POST /inquiries

담당: **백엔드**

고객 문의/첫 메시지 저장 → 접수 안내 저장 → LLM 실행 요청. 고객에게는 AI 완료를 기다리지 않고 응답한다. LLM 서비스 일시 장애에도 문의는 보존하고, 분석 요청 실패는 담당자 알림과 제한된 재전송으로 복구한다. created_at 직접 지정은 DEMO_MODE에서만 허용하며 급증 집계는 서버 수신 시각 기준.

요청 JSON:

```json
{
  "tenant_id": "syncnote",
  "customer_id": "U-2001",
  "message": "저장할 때 E502가 떠요.",
  "customer_category": "기타",
  "env": {
    "os": "iOS 18",
    "app_version": "2.4.0",
    "error_code": null
  },
  "attachments": [],
  "created_at": null
}
```

응답 `201`:

```json
{
  "inquiry_id": "Q-1001",
  "status": "접수",
  "auto_message": {
    "message_id": "MSG-000002",
    "sender": "ai_auto",
    "message_type": "접수 안내",
    "text": "접수됐어요. 문의를 확인하고 있어요.",
    "sent_at": "2026-11-04T22:00:00+09:00"
  }
}
```

### C02 · POST /inquiries/{inquiry_id}/messages

담당: **백엔드**

customer는 자기 메시지만 추가. 서버가 message_id를 발급하고 revision 증가 후 새 분석 실행. 종료된 문의는 409, 새 문의 생성.

요청 JSON:

```json
{
  "sender": "customer",
  "text": "재실행해도 똑같아요.",
  "attachments": []
}
```

응답 `201`:

```json
{
  "message_id": "MSG-000003",
  "inquiry_id": "Q-1001",
  "inquiry_revision": 2,
  "status": "담당자 확인 중"
}
```

### C03 · GET /inquiries/{inquiry_id}

담당: **백엔드**

3초 폴링. 처음 cursor 없으면 첫 페이지. 기존 since 지원 시 cursor와 동시 사용 금지. 내부 초안·코드·검수 세부 결과는 고객에게 반환하지 않는다.

쿼리 예시(JSON은 설명용):

```json
{
  "cursor": "cursor_002",
  "limit": 100
}
```

응답 `200`:

```json
{
  "inquiry_id": "Q-1001",
  "status": "추가 정보 필요",
  "messages": [
    {
      "message_id": "MSG-000004",
      "sender": "ai_auto",
      "message_type": "정보 수집 질문",
      "text": "오류 화면을 보내주실 수 있나요?",
      "attachments": [],
      "sent_at": "2026-11-04T22:00:00+09:00"
    }
  ],
  "next_cursor": "cursor_004",
  "has_more": false
}
```

### C04 · POST /attachments

담당: **백엔드**

multipart/form-data의 file 바이너리 필드 사용. 아래 요청 JSON은 파일 입력 설명용이며 실제 JSON body가 아니다. PNG/JPEG/PDF, 파일당 10MB. URL/서버 경로 임의 지정 불가. 임시 첨부는 업로더 소유권을 저장하고 문의에 연결 시 검증.

요청 JSON:

```json
{
  "multipart_field": "file",
  "file_name": "error.png",
  "mime": "image/png"
}
```

응답 `201`:

```json
{
  "attachment_id": "ATT-0001",
  "url": "https://storage.example/signed-file",
  "mime": "image/png",
  "size": 182344,
  "expires_at": "2026-11-04T22:05:00+09:00"
}
```

### C05 · GET /attachments/{attachment_id}

담당: **백엔드**

첨부 URL 만료 시 권한 확인 후 재발급. 일반 공개 영구 URL을 반환하지 않는다. 내부 LLM도 허용된 첨부만 접근.

요청 본문 없음.

응답 `200`:

```json
{
  "attachment_id": "ATT-0001",
  "url": "https://storage.example/renewed-signed-file",
  "mime": "image/png",
  "size": 182344,
  "expires_at": "2026-11-04T22:10:00+09:00"
}
```

### C06 · POST /inquiries/{inquiry_id}/feedback

담당: **백엔드**

상담 종료 후 평가. rating/resolved/comment 중 하나 이상. 별점 없어도 해결 여부 기록. 무응답은 미확인.

요청 JSON:

```json
{
  "rating": null,
  "resolved": true,
  "comment": "정상적으로 저장돼요."
}
```

응답 `201`:

```json
{
  "inquiry_id": "Q-1001",
  "rating": null,
  "resolution_status": "해결 확인",
  "recorded_at": "2026-11-04T22:00:00+09:00"
}
```

## 6. 담당자·분석 요청·발송 API · 백엔드 담당

### A01 · GET /admin/inquiries

담당: **백엔드**

같은 테넌트의 상담 목록/필터.

쿼리 예시(JSON은 설명용):

```json
{
  "status": "담당자 확인 중",
  "category": "기능 오류",
  "urgency": "높음",
  "cursor": null,
  "limit": 20
}
```

응답 `200`:

```json
{
  "items": [
    {
      "inquiry_id": "Q-1001",
      "title": "저장 오류",
      "category": "기능 오류",
      "urgency": "높음",
      "status": "담당자 확인 중",
      "processing_stage": "검토 대기",
      "group_id": "G-001"
    }
  ],
  "next_cursor": null
}
```

### A02 · GET /admin/inquiries/{inquiry_id}

담당: **백엔드**

원문·메시지·분류·그룹·최신 파이프라인·초안·검수 조회. 아래 messages는 예시 발췌. 실제 화면에서는 대화를 포함하거나 C03과 같은 페이징 규칙으로 조회한다.

요청 본문 없음.

응답 `200`:

```json
{
  "inquiry_id": "Q-1001",
  "revision": 2,
  "customer_category": "기타",
  "category": "기능 오류",
  "urgency": "높음",
  "status": "담당자 확인 중",
  "processing_stage": "검토 대기",
  "latest_customer_message_id": "MSG-000003",
  "latest_run_id": "RUN-000001",
  "group_id": "G-001",
  "messages": [
    {
      "message_id": "MSG-000003",
      "sender": "customer",
      "message_type": "고객 메시지",
      "text": "재실행해도 똑같아요.",
      "attachments": [],
      "sent_at": "2026-11-04T22:00:00+09:00"
    }
  ],
  "draft": {
    "draft_id": "DRAFT-000001",
    "revision": 1,
    "draft_result": "초안 가능",
    "answer_text": "저장 오류 관련 확인이 필요합니다.",
    "evidence": {
      "docs": [
        {
          "doc_id": "DOC-005",
          "version": 1
        }
      ],
      "code": []
    }
  },
  "verification": {
    "status": "확인 불가",
    "reasons": [
      "기술적 원인 확정 불가"
    ]
  }
}
```

### A03 · PATCH /admin/inquiries/{inquiry_id}/classification

담당: **백엔드**

카테고리·긴급도 수동 수정과 이력 저장. 오래된 AI 작업이 수동 수정 결과를 덮어쓰지 않도록 revision 검사.

요청 JSON:

```json
{
  "expected_revision": 2,
  "category": "기능 오류",
  "urgency": "높음",
  "reason": "동일 오류 확인"
}
```

응답 `200`:

```json
{
  "inquiry_id": "Q-1001",
  "revision": 3,
  "category": "기능 오류",
  "urgency": "높음"
}
```

### A04 · POST /admin/inquiries/{inquiry_id}/analysis

담당: **백엔드 접수 → LLM 파이프라인 실행**

담당자 재분석 요청. 백엔드가 최신 스냅샷을 I01로 전달. review_only=true 기본값으로 재분석 도중 자동 발송 방지. 최초 문의/새 고객 메시지는 백엔드가 같은 내부 요청 로직을 자동 실행한다.

요청 JSON:

```json
{
  "expected_inquiry_revision": 3,
  "based_on_message_id": "MSG-000003",
  "review_only": true
}
```

응답 `202`:

```json
{
  "inquiry_id": "Q-1001",
  "run_id": "RUN-000002",
  "run_status": "대기"
}
```

### A05 · POST /admin/inquiries/{inquiry_id}/answers

담당: **백엔드**

최종 수정·담당자 승인·발송. 최신 문의/고객 메시지/초안/지식 revision 검증. 근거는 본 테넌트에서 조회된 자료만 허용. 수동 작성 시 draft_id/draft_revision=null. 자동 발송은 이 담당자 API를 가짜 계정으로 호출하지 않고 동일한 내부 저장·발송 로직을 사용.

요청 JSON:

```json
{
  "draft_id": "DRAFT-000001",
  "draft_revision": 1,
  "expected_inquiry_revision": 3,
  "expected_last_customer_message_id": "MSG-000003",
  "answer_text": "담당자가 확인한 안내입니다.",
  "evidence": {
    "docs": [
      {
        "doc_id": "DOC-005",
        "version": 1
      }
    ],
    "code": []
  }
}
```

응답 `201`:

```json
{
  "answer_id": "A-1001",
  "message_id": "MSG-000005",
  "inquiry_id": "Q-1001",
  "status": "답변 완료",
  "delivery_mode": "담당자",
  "agent_id": "agent_01",
  "answered_at": "2026-11-04T22:00:00+09:00"
}
```

### A06 · POST /admin/inquiries/{inquiry_id}/close

담당: **백엔드**

상담 종료·평가 요청. 종료만으로 해결 성공 처리하지 않는다.

요청 JSON:

```json
{
  "expected_inquiry_revision": 4,
  "resolution_status": "미확인"
}
```

응답 `200`:

```json
{
  "inquiry_id": "Q-1001",
  "status": "상담 종료",
  "resolution_status": "미확인",
  "feedback_requested": true
}
```

자동 발송은 검수 통과만으로 실행하지 않는다. 백엔드의 자동 응대 허용 범위, 추가 정보 확보, 유효한 공개 근거, 충돌 없음, 최신 고객 메시지/초안/지식, 내부 정보 미포함을 모두 검사한다. 하나라도 불명확하면 담당자 검토. 발송 시 delivery_mode=자동, agent_id=null, run_id와 검증 기록 저장. 자동 전송 실패는 성공으로 표시하지 않고 담당자 알림 처리한다. 접수/정보 수집 질문은 별도 제한된 전송 규칙을 적용한다.

## 7. 그룹·검증 알림 API · 백엔드 담당

### G01 · GET /admin/groups

담당: **백엔드**

LLM 추천을 반영해 저장한 그룹 목록. 급증 건수는 현재 서버 수신 시각 기준이며 과거 시드는 제외.

쿼리 예시(JSON은 설명용):

```json
{
  "surging": true,
  "cursor": null,
  "limit": 20
}
```

응답 `200`:

```json
{
  "items": [
    {
      "group_id": "G-001",
      "summary": "저장 시 E-502",
      "count": 16,
      "window_minutes": 5,
      "surging": true,
      "issue_url": null
    }
  ],
  "next_cursor": null
}
```

### G02 · GET /admin/groups/{group_id}

담당: **백엔드**

그룹 구성원·대표 증상·코드 후보. 원인 확정으로 표시하지 않는다.

요청 본문 없음.

응답 `200`:

```json
{
  "group_id": "G-001",
  "inquiry_ids": [
    "Q-1001",
    "Q-1002"
  ],
  "normalized_error_codes": [
    "E-502"
  ],
  "suspected_code": [
    {
      "path": "sync/saveHandler.ts",
      "commit_sha": null,
      "reason": "관련 기능 후보"
    }
  ],
  "issue_url": null
}
```

### G03 · GET /admin/alerts

담당: **백엔드**

검수 실패·근거 충돌·분석 실패·급증 알림. LLM은 원인을 반환하고 백엔드가 알림 레코드를 생성.

쿼리 예시(JSON은 설명용):

```json
{
  "unread_only": true,
  "cursor": null,
  "limit": 20
}
```

응답 `200`:

```json
{
  "items": [
    {
      "alert_id": "ALERT-000001",
      "type": "검증 확인 필요",
      "inquiry_id": "Q-1001",
      "run_id": "RUN-000001",
      "message": "기술 원인 확인이 필요합니다.",
      "read_at": null,
      "created_at": "2026-11-04T22:00:00+09:00"
    }
  ],
  "next_cursor": null
}
```

### G04 · PATCH /admin/alerts/{alert_id}

담당: **백엔드**

담당자 알림 읽음 처리.

요청 JSON:

```json
{
  "read": true
}
```

응답 `200`:

```json
{
  "alert_id": "ALERT-000001",
  "read_at": "2026-11-04T22:00:00+09:00"
}
```

## 8. GitHub API · 백엔드 담당

### H01 · POST /admin/integrations/github

담당: **백엔드**

admin 전용. GitHub App 설치 소유권 검증. 허용 경로/제외 패턴 적용 후 자료 수집, I03으로 인덱싱 요청. 레포 이름은 예시. 코드 쓰기 권한 없음.

요청 JSON:

```json
{
  "installation_id": 12345,
  "repository": "syncnote-demo/syncnote-app",
  "watch_branch": "main",
  "security_level": 2,
  "include_paths": [
    "billing/",
    "sync/"
  ],
  "exclude_patterns": [
    ".env*",
    "*.pem",
    "*.key",
    "secrets/",
    "config/prod*"
  ]
}
```

응답 `202`:

```json
{
  "integration_id": "github_syncnote",
  "status": "동기화 중"
}
```

### H02 · GET /admin/integrations/github

담당: **백엔드**

연동/동기화 상태 조회. 현재 GitHub 자료 분석은 외부 LLM을 사용할 수 있으며 로컬 모델 처리는 향후 배포 구상이다. 현재 구현을 로컬 처리로 표시하지 않는다.

요청 본문 없음.

응답 `200`:

```json
{
  "integration_id": "github_syncnote",
  "repository": "syncnote-demo/syncnote-app",
  "watch_branch": "main",
  "security_level": 2,
  "status": "연결 완료",
  "analysis_provider": "external_llm",
  "last_synced_sha": "a1b2c3d",
  "last_synced_at": "2026-11-04T22:00:00+09:00"
}
```

### H03 · POST /integrations/github/webhook

담당: **백엔드**

GitHub 원본 bytes의 X-Hub-Signature-256 검증, X-GitHub-Delivery로 중복 제거. 아래 push payload 발췌. 코드 변경 수집 → 영향받은 근거 검색 제외 → 최신 자료 인덱싱. main push만으로 가격/정책 확정을 단정하지 않는다.

요청 JSON:

```json
{
  "ref": "refs/heads/main",
  "before": "a1b2c3d",
  "after": "b2c3d4e",
  "repository": {
    "full_name": "syncnote-demo/syncnote-app"
  },
  "installation": {
    "id": 12345
  }
}
```

응답 `202`:

```json
{
  "accepted": true,
  "event_id": "EVENT-000001"
}
```

### H04 · POST /admin/groups/{group_id}/github-issues

담당: **백엔드**

급증 자동 생성도 같은 내부 로직 사용. 그룹/장애 episode 기준 중복 방지. bug/moa-auto 라벨, 원본 규칙의 문의 요약·환경·코드 후보·관련 커밋 형식 적용. 개인정보 제외.

요청 JSON:

```json
{
  "episode_id": "episode_001",
  "include_suspected_code": true
}
```

응답 `202`:

```json
{
  "group_id": "G-001",
  "issue_creation_status": "처리 중"
}
```

이슈 생성 결과/실패는 G02와 G03에서 확인한다. 별도 범용 작업 API는 만들지 않는다. GitHub 코드 원문은 내부 조사용으로 구분하고 고객 응답 생성용 외부 LLM에 그대로 넘기지 않는다. 메타데이터만 허용한 보안 레벨은 코드 본문을 수집/임베딩하지 않는다.

## 9. 지식 등록 API · 백엔드 담당

RAG가 사용할 문서 입력과 조회만 남긴다. 서비스 정보는 시드 또는 인증된 서버 설정에서 동일한 소스 인덱싱 경로로 넣는다. 베스트 답변 선정/최신 버전 편집용 별도 화면 API는 이번 분담 문서에서 제외하며, 재사용 승인된 답변을 I03으로 등록하는 계약만 유지한다.

### K01 · POST /admin/documents

담당: **백엔드 원본 저장 → LLM 인덱싱**

doc_id/version/front matter 필드 유지. text/attachment_id 중 하나. 신규 버전 등록 시 기존 버전 폐기·검색 차단 후 인덱싱. 원본 작성 시각 created_at과 업로드 시각 uploaded_at 구분.

요청 JSON:

```json
{
  "doc_id": "DOC-005",
  "version": 1,
  "doc_type": "FAQ",
  "title": "오류 코드 안내",
  "status": "유효",
  "created_at": "2026-10-15T00:00:00+09:00",
  "text": "E-502는 저장 처리 중 발생하는 오류입니다.",
  "attachment_id": null
}
```

응답 `202`:

```json
{
  "doc_id": "DOC-005",
  "version": 1,
  "status": "유효",
  "indexing_status": "대기",
  "uploaded_at": "2026-11-04T22:00:00+09:00"
}
```

### K02 · GET /admin/documents

담당: **백엔드**

현재 문서/인덱싱 상태. 백엔드는 내부 소스 상태 조회로 동기화한다. 폐기/재검토/인덱싱 미완료 자료는 검색 금지.

쿼리 예시(JSON은 설명용):

```json
{
  "status": "유효",
  "cursor": null,
  "limit": 20
}
```

응답 `200`:

```json
{
  "items": [
    {
      "doc_id": "DOC-005",
      "version": 1,
      "title": "오류 코드 안내",
      "status": "유효",
      "review_required": false,
      "indexing_status": "완료"
    }
  ],
  "next_cursor": null
}
```

### K03 · PATCH /admin/documents/{doc_id}/versions/{version}/status

담당: **백엔드 → LLM 검색 제외**

문서 enum은 유효/폐기 유지. 재검토는 별도 플래그. I04 적용 성공 또는 동등한 검색 차단 보장 후 성공 응답. 차단 실패는 성공으로 반환하지 않는다.

요청 JSON:

```json
{
  "status": "유효",
  "review_required": true,
  "reason": "관련 코드 변경 확인 필요"
}
```

응답 `200`:

```json
{
  "doc_id": "DOC-005",
  "version": 1,
  "status": "유효",
  "review_required": true,
  "searchable": false
}
```

## 10. LLM 파이프라인 연결 API

### I01 · POST /internal/v1/pipeline-runs

담당: **LLM 담당**

외부 접두어 /api/v1을 붙이지 않는다. 백엔드가 일관된 대화 스냅샷을 전달한다. 실행 접수 후 LLM 담당이 비동기 전체 파이프라인을 관리. inquiry_revision/based_on_message_id와 idempotency key로 실행을 식별. 토큰/고객 데이터는 본 테넌트 범위에서만 사용.

요청 JSON:

```json
{
  "tenant_id": "syncnote",
  "inquiry_id": "Q-1001",
  "inquiry_revision": 2,
  "based_on_message_id": "MSG-000003",
  "customer_category": "기타",
  "messages": [
    {
      "message_id": "MSG-000001",
      "sender": "customer",
      "text": "저장할 때 E502가 떠요.",
      "sent_at": "2026-11-04T22:00:00+09:00",
      "attachments": []
    },
    {
      "message_id": "MSG-000003",
      "sender": "customer",
      "text": "재실행해도 똑같아요.",
      "sent_at": "2026-11-04T22:00:00+09:00",
      "attachments": []
    }
  ],
  "env": {
    "os": "iOS 18",
    "app_version": "2.4.0",
    "error_code": null
  },
  "review_only": false
}
```

응답 `202`:

```json
{
  "run_id": "RUN-000001",
  "inquiry_id": "Q-1001",
  "inquiry_revision": 2,
  "run_status": "대기"
}
```

### I02 · GET /internal/v1/pipeline-runs/{run_id}

담당: **LLM 담당**

대기/처리 중은 current_stage 제공. 완료 결과는 아래 형식. 콜백 누락 시 백엔드가 조회해 같은 결과 적용 로직을 호출. 오래된 run 결과는 보관만 하며 최신 상담을 변경하지 않는다.

요청 본문 없음.

응답 `200`:

```json
{
  "run_id": "RUN-000001",
  "tenant_id": "syncnote",
  "inquiry_id": "Q-1001",
  "inquiry_revision": 2,
  "based_on_message_id": "MSG-000003",
  "run_status": "완료",
  "knowledge_revision": 12,
  "analysis": {
    "category": "기능 오류",
    "urgency": "높음",
    "summary": "재실행 후에도 저장 시 E-502 오류",
    "normalized_error_code": "E-502",
    "missing_fields": []
  },
  "similar_inquiries": {
    "items": [
      {
        "inquiry_id": "Q-0174",
        "group_id": "G-001",
        "score": 0.91
      }
    ],
    "suggested_group_id": "G-001",
    "reason": "오류 코드와 저장 증상이 일치"
  },
  "draft_result": "초안 가능",
  "additional_question": null,
  "draft": {
    "answer_text": "저장 처리 중 오류가 발생한 것으로 보입니다. 추가 확인이 필요합니다.",
    "evidence": {
      "docs": [
        {
          "doc_id": "DOC-005",
          "version": 1
        }
      ],
      "code": []
    },
    "citations": [
      {
        "text": "저장 처리 중 오류",
        "docs": [
          {
            "doc_id": "DOC-005",
            "version": 1
          }
        ]
      }
    ],
    "style_references": []
  },
  "verification": {
    "status": "확인 불가",
    "checks": {
      "evidence_current": "통과",
      "source_conflict": "통과",
      "claims_supported": "통과",
      "internal_data_excluded": "통과",
      "auto_response_scope": "확인 불가"
    },
    "reasons": [
      "실제 장애 상태 확인 필요"
    ],
    "unsupported_sentences": []
  },
  "error": null
}
```

처리 중 응답 예시:

```json
{ "run_id": "RUN-000001", "run_status": "처리 중", "current_stage": "별도 검수", "result": null, "error": null }
```

실패 응답/콜백 예시:

```json
{ "run_id": "RUN-000001", "tenant_id": "syncnote", "inquiry_id": "Q-1001", "inquiry_revision": 2, "based_on_message_id": "MSG-000003", "run_status": "실패", "knowledge_revision": null, "analysis": null, "similar_inquiries": null, "draft_result": null, "additional_question": null, "draft": null, "verification": null, "error": { "code": "LLM_UNAVAILABLE", "message": "모델 호출 실패", "retryable": true } }
```

current_stage 값: 문의 분석/정보 부족 판단/유사 문의 탐색/RAG 검색/근거 검증/초안 생성/별도 검수. 근거 검증은 검색 자료의 유효성·충돌·충분성, 별도 검수는 생성 문장과 근거의 일치·내부 정보 노출을 확인한다. 별도 검수의 auto_response_scope는 참고 결과이며 최종 허용 정책은 백엔드가 검사한다.

정보 부족 결과 예시:

```json
{
  "run_id": "RUN-000002",
  "tenant_id": "syncnote",
  "inquiry_id": "Q-1001",
  "inquiry_revision": 3,
  "based_on_message_id": "MSG-000006",
  "run_status": "완료",
  "knowledge_revision": null,
  "analysis": {
    "category": "기타",
    "urgency": "보통",
    "summary": "구체적인 오류 화면 불명",
    "normalized_error_code": null,
    "missing_fields": [
      "발생 화면"
    ]
  },
  "similar_inquiries": null,
  "draft_result": "추가 질문 필요",
  "additional_question": "어떤 화면에서 문제가 발생했나요?",
  "draft": null,
  "verification": {
    "status": "미실행",
    "checks": {},
    "reasons": [
      "정보 수집 우선"
    ],
    "unsupported_sentences": []
  },
  "error": null
}
```

근거 부족/충돌이면 draft_result=직접 확인 필요, draft=null, verification에 사유를 반환한다. 초안은 만들었으나 검수 실패이면 초안을 담당자 확인용으로 보존할 수 있지만 자동 발송 금지. 정책상 검토가 필요한 것과 초안 생성 불가능은 구분한다. 에러로 파이프라인 실행 자체가 실패하면 run_status=실패, 결과 필드는 null, error={code,message,retryable}. 재시도는 LLM 담당이 제한 횟수 내 관리하고 검증 실패/근거 부족을 기술 오류처럼 무한 재시도하지 않는다.

### I03 · POST /internal/v1/rag/sources

담당: **LLM 담당**

백엔드가 권한 확인·보안 필터 적용·텍스트 추출한 자료를 전달. 자료 종류는 문서/서비스 정보/우수 답변/코드/커밋/GitHub 이슈/문의 요약. 고객 데이터와 평가 정답은 혼합하지 않음. source_id/version/content_hash 기준 멱등. 코드 버전은 commit_sha로 식별.

요청 JSON:

```json
{
  "tenant_id": "syncnote",
  "source_type": "문서",
  "source_id": "DOC-005",
  "version": 1,
  "title": "오류 코드 안내",
  "text": "E-502는 저장 처리 중 발생하는 오류입니다.",
  "visibility": "고객 공개",
  "status": "유효",
  "review_required": false,
  "source_revision": 1,
  "content_hash": "sha256:example",
  "metadata": {
    "doc_type": "FAQ",
    "path": null,
    "commit_sha": null
  },
  "evidence": {
    "docs": [],
    "code": []
  }
}
```

응답 `202`:

```json
{
  "source_id": "DOC-005",
  "version": 1,
  "indexing_status": "대기"
}
```

코드는 visibility=내부 전용으로 고정. 우수 답변은 형식 참고용이며 사실 근거로 사용하지 않는다. 문의 요약에는 inquiry_id/group_id와 권한 범위를 저장해 유사 문의 탐색에 사용한다. 백엔드가 그룹을 확정한 뒤 해당 요약 메타데이터를 같은 source 버전의 멱등 갱신 로직으로 동기화한다. 내용 변경은 새 버전/해시로 등록하고, 코드/문서 폐기는 I04로 전달한다. 인덱싱 완료 전에 검색하지 않는다.

### I04 · POST /internal/v1/rag/source-statuses

담당: **LLM 담당**

현재 상태 일괄 반영·검색 차단. 버전 기반 역순 이벤트 방지를 위해 source 상태 변경 revision을 검사. 전체 요청은 원자 적용하거나 실패 반환. 영향받은 생성 초안/형식 참고는 근거 연결로 검색·발송 검증에서 제외.

요청 JSON:

```json
{
  "tenant_id": "syncnote",
  "event_id": "EVENT-000001",
  "sources": [
    {
      "source_type": "문서",
      "source_id": "DOC-005",
      "version": 1,
      "source_revision": 2,
      "status": "유효",
      "review_required": true
    }
  ],
  "reason": "관련 코드 변경 확인 필요"
}
```

응답 `200`:

```json
{
  "knowledge_revision": 13,
  "updated_count": 1,
  "search_excluded": true
}
```

### I05 · GET /internal/v1/rag/sources/{source_id}/versions/{version}

담당: **LLM 담당**

source_type은 필수 쿼리. tenant는 검증된 서비스 인증 문맥에서 결정. 상태와 인덱싱 완료/실패를 백엔드에 제공.

쿼리 예시(JSON은 설명용):

```json
{
  "source_type": "문서"
}
```

응답 `200`:

```json
{
  "source_id": "DOC-005",
  "version": 1,
  "source_revision": 2,
  "indexing_status": "완료",
  "status": "유효",
  "review_required": true,
  "searchable": false,
  "error": null
}
```

### B01 · POST /internal/v1/pipeline-results

담당: **백엔드 담당**

LLM 완료/실패 콜백. 요청 본문은 I02의 완료/실패 전체 결과와 동일하다. 아래는 완료 예시. 인증된 호출자·run 소유권·tenant·문의 revision·latest_run_id·기준 고객 메시지를 검사. 같은 run 최종 결과 재전송은 중복 저장/발송하지 않는다. 다른 내용으로 재전송하면 409. 오래된 결과는 ignored_stale로 접수하고 사용자 메시지는 생성하지 않는다.

요청 JSON:

```json
{
  "run_id": "RUN-000001",
  "tenant_id": "syncnote",
  "inquiry_id": "Q-1001",
  "inquiry_revision": 2,
  "based_on_message_id": "MSG-000003",
  "run_status": "완료",
  "knowledge_revision": 12,
  "analysis": {
    "category": "기능 오류",
    "urgency": "높음",
    "summary": "재실행 후에도 저장 시 E-502 오류",
    "normalized_error_code": "E-502",
    "missing_fields": []
  },
  "similar_inquiries": {
    "items": [
      {
        "inquiry_id": "Q-0174",
        "group_id": "G-001",
        "score": 0.91
      }
    ],
    "suggested_group_id": "G-001",
    "reason": "오류 코드와 저장 증상이 일치"
  },
  "draft_result": "초안 가능",
  "additional_question": null,
  "draft": {
    "answer_text": "저장 처리 중 오류가 발생한 것으로 보입니다. 추가 확인이 필요합니다.",
    "evidence": {
      "docs": [
        {
          "doc_id": "DOC-005",
          "version": 1
        }
      ],
      "code": []
    },
    "citations": [
      {
        "text": "저장 처리 중 오류",
        "docs": [
          {
            "doc_id": "DOC-005",
            "version": 1
          }
        ]
      }
    ],
    "style_references": []
  },
  "verification": {
    "status": "확인 불가",
    "checks": {
      "evidence_current": "통과",
      "source_conflict": "통과",
      "claims_supported": "통과",
      "internal_data_excluded": "통과",
      "auto_response_scope": "확인 불가"
    },
    "reasons": [
      "실제 장애 상태 확인 필요"
    ],
    "unsupported_sentences": []
  },
  "error": null
}
```

응답 `200`:

```json
{
  "run_id": "RUN-000001",
  "apply_status": "applied",
  "inquiry_id": "Q-1001",
  "status": "담당자 확인 중",
  "draft_id": "DRAFT-000001",
  "group_id": "G-001",
  "delivery_action": "담당자 검토"
}
```

apply_status: applied/duplicate/ignored_stale. delivery_action: 추가 질문 전송/자동 답변 전송/담당자 검토/없음. 응답은 결과와 후속 작업이 영속적으로 반영된 상태를 나타낸다. 실제 메시지 전송이 아직 처리 중이면 전송 성공을 표시하지 말고 delivery_action=없음, 후속 상태를 담당자 조회에 반영한다. LLM 워커는 콜백 실패를 제한 재시도하고, 콜백 전달 상태와 AI run_status를 별개로 관리한다. 실패 콜백도 최신 문의에만 반영하며 검토 대기 상태와 실패 알림을 만들고 고객에게 오류 원문을 노출하지 않는다. 백엔드 재시작 후에도 미적용 최신 run을 I02로 조회해 복구할 수 있어야 한다.

## 11. 개발에 남길 데이터와 최소 검증

**백엔드 저장**: 문의/메시지/revision, 첨부, 최신 run_id, 분류, 그룹 구성/급증, 초안/검수 결과, 최종 답변/evidence/전송 경로, 평가, GitHub 연동/이벤트/이슈, 알림, 문서 원본/버전.

**LLM 담당 저장**: run 상태·단계·기준 입력·결과·재시도·콜백 전달 이력, 검색 청크·임베딩·자료 상태/revision, 근거 참조·검색 기록. 백엔드의 최종 메시지/그룹/권한을 임의로 변경하지 않는다.

**검증 항목**

- 문의 한 건 저장 → 전체 파이프라인 → 결과 반영 → 검토/자동 응답까지 연결.
- 추가 정보 필요 시 질문만 발송하고 고객 응답 후 새 run 실행.
- 근거 부족·충돌·별도 검수 실패 시 자동 발송 차단/알림.
- 새 메시지·수동 분류 변경·근거 갱신 이후 오래된 결과 적용/발송 방지.
- 중복 콜백·메시지·이슈·자동/수동 발송 경합 방지.
- 다른 고객사 자료·첨부 접근 차단, 내부 코드 고객 노출 차단.
- 현재 시각의 문의로 급증 집계, 다른 오류 코드 오병합 확인.
- labels/ 미수입, 별점 미응답을 실패/해결로 단정하지 않음.

## 12. 이번 문서에서 제외한 API

- 문의 분석/유사 문의 탐색/RAG 검색/근거 검증/초안 생성/별도 검수 각각의 개별 HTTP API: 파이프라인 내부 함수로 구현.
- 별도 delivery/evaluate API: 별도 검수는 파이프라인 안에서 수행하고 실제 전송 검사는 백엔드 내부 로직.
- 목데이터 생성·import·정답 조회·데모 reset/resume API.
- 범용 작업 job API: LLM 작업은 pipeline-runs로, GitHub 결과는 그룹/연동 상태와 알림으로 조회.
- 서비스 온보딩 CRUD·자동화 설정 화면 API·베스트 답변 버전 편집/승인/갱신 전용 API: 현재 역할 분배의 필수 연결 API에서 제외. 필요한 초기 설정/승인 자료는 서버 설정·시드·소스 등록 계약으로 공급.
- 코드 변경 영향 분석·베스트 답변 수정 각각의 독립 LLM API는 추가하지 않음. GitHub 담당이 소스 갱신/무효화 계약을 사용하고, 별도 생성 기능이 필요해질 때 현재 파이프라인 범위와 분리해 구현.
- 로컬 LLM 배포·WebSocket·외부 메신저·운영 통계 대시보드 API.

이 문서에는 팀 논의 목록과 목데이터 담당자 요청 목록을 포함하지 않는다.
