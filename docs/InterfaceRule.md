# MOA 인터페이스 규칙 (v0.3 초안)

> 싱크노트(가상 회사)·시드 데이터 ↔ MOA 백엔드·프론트가 맞물리는 지점의 형식 약속.
> **⚠ 결정 필요** 표시가 붙은 항목은 팀 합의 후 값을 채우고 이 문서를 v1.0으로 올린다.
> 데이터 확정일 이후에는 필드 이름·enum을 바꾸지 않는다. 바꿔야 하면 맨 아래 변경 이력에 남긴다.

---

## 0. 역할

| 담당 | 만드는 것 |
|---|---|
| 데이터·싱크노트 담당 | 싱크노트 GitHub Org·레포·간단한 앱, 의도적 버그와 커밋, 시드 데이터, 정답 파일, 급증 재현 스크립트, GitHub 리셋 스크립트 |
| 백엔드 | 시드 import, 문의 API, 분류·그룹화·RAG·영향 분석, GitHub App·webhook 처리, 이슈 생성, DB 리셋 API |
| 프론트 | 어드민(인박스·상세·재검토 큐·그룹), 고객 채팅 위젯, 별점 UI |

---

## 1. 공통 규칙

### 1-1. 인코딩·포맷
- 시드 파일 형식: **JSON** (근거가 여러 개인 필드를 배열로 담기 위해). CSV가 필요하면 JSON에서 변환해서 쓴다.
- 문자 인코딩: UTF-8, BOM 없음
- 날짜·시각: ISO 8601 + 타임존 고정 `2026-11-03T14:05:00+09:00`
  - 타임존 없는 값은 받지 않는다 (UTC로 읽혀 9시간 밀리는 사고 방지)
- 필드 이름: `snake_case`
- 빈 값: 문자열 `""` 대신 `null`

### 1-2. ID 규칙
| 대상 | 형식 | 예 |
|---|---|---|
| 테넌트 | 소문자 영문 | `syncnote` |
| 문의 | `Q-` + 4자리 | `Q-0174` |
| 답변 | `A-` + 4자리 | `A-0052` |
| 우수 답변 | `BA-` + 3자리 | `BA-016` |
| 문서 | `DOC-` + 3자리 (버전은 별도 필드) | `DOC-002` |
| 문의 그룹 | `G-` + 3자리 | `G-001` |
| 고객 | `U-` + 4자리 | `U-1184` |
| 담당자 | `agent_` + 2자리 | `agent_01` |

시연 중 새로 생기는 문의 ID는 백엔드가 발급한다(시드 ID와 겹치지 않게 `Q-1001`부터).

### 1-3. enum (글자·띄어쓰기까지 정확히)
| 필드 | 허용 값 |
|---|---|
| `category` | `이용 방법` · `계정` · `결제` · `기능 오류` · `개선 요청` · `기타` |
| `urgency` | `높음` · `보통` · `낮음` |
| `inquiry_status` | `접수` · `담당자 확인 중` · `추가 정보 필요` · `답변 완료` · `상담 종료` |
| `doc_type` | `정책` · `FAQ` · `기능 명세` · `릴리즈 노트` |
| `doc_status` | `유효` · `폐기` |
| `answer_status` | `유효` · `재검토 필요` · `비활성` |
| `draft_result` | `초안 가능` · `직접 확인 필요` · `추가 질문 필요` |
| `message_sender` | `customer` · `agent` · `ai_auto` (접수 안내·정보 수집 질문만) |
| `customer_reaction` (**labels 전용**) | `해결` · `미해결` · `무응답` |

> `customer_reaction`은 시스템 입력에 넣지 않는다. 시스템은 대화 내용을 읽고 직접 판단해야 하고, 이 값은 그 판단을 채점할 때만 쓴다.

### 1-4. 근거 참조 형식
```json
{
  "evidence": {
    "docs": [
      {
        "doc_id": "DOC-002",
        "version": 3
      }
    ],
    "code": [
      {
        "path": "billing/pricing.ts",
        "commit_sha": "a1b2c3d"
      }
    ]
  }
}
```
- `path`는 싱크노트 레포 **루트 기준 상대 경로**. 실제 레포 경로와 100% 일치해야 한다.
- `commit_sha`는 7자리 이상. 시드 작성 시점에 모르면 `null`, 레포 생성 후 채운다.

---

## 2. 시드 파일

### 2-1. 폴더 구조
```
seed/
├─ README.md               버전, 변경 이력
├─ company.json            싱크노트 기본 정보 (온보딩 폼 값)
├─ docs/                   시드에 포함되는 문서 원문 (md + 메타데이터)
│   ├─ DOC-001_v1.md       ← Pro 9,900원 (현재 유효)
│   ├─ DOC-002_v2.md       ← 환불 14일 (폐기, 10/15 이전)
│   ├─ DOC-002_v3.md       ← 환불 7일 (유효, 10/15부터)
│   └─ ...
├─ inquiries.json          과거 문의 + 대화 (입력값만)
├─ answers.json            과거 답변 (입력값만)
├─ best_answers.json       우수 답변 (입력값만)
├─ demo/                   ⛔ 시드 import 금지. 시연 중에 넣는 것
│   ├─ DOC-001_v2.md       ← Pro 12,900원. 시연 때 업로드
│   └─ burst_inquiries.json← 급증 재현용 문의 (상대 시각)
├─ labels/                 ⛔ 채점 전용. 시스템에 import 금지
│   ├─ inquiry_labels.json
│   ├─ answer_labels.json
│   └─ bug_truth.json
└─ scripts/
    ├─ validate.*          무결성 검사 (2-8). 통과해야 태그를 찍는다
    ├─ burst_replay.*      급증 재현
    └─ reset_github.*      GitHub 리셋
```

> **⛔ `labels/` 폴더는 import하지 않는다.** 정답을 넣으면 시스템이 찾아낸 게 아니라 적혀 있던 걸 보여주는 게 된다. 채점 스크립트만 읽는다.

### 2-2. company.json
```json
{
  "tenant_id": "syncnote",
  "service_name": "싱크노트",
  "summary": "폰·태블릿·PC·웹에서 같은 노트를 쓰는 노트 동기화 앱",
  "current_app_version": "2.4.0",
  "plans": [
    { "name": "Free", "monthly_price": 0, "features": ["기기 2대", "1GB", "첨부 10MB", "버전 기록 7일"] },
    { "name": "Pro", "monthly_price": 9900, "annual_price": 95040, "features": ["기기 무제한", "50GB", "첨부 100MB", "버전 기록 30일"] },
    { "name": "Team", "monthly_price_per_user": 14900, "features": ["Pro 전체", "공유 워크스페이스", "권한 관리", "버전 기록 1년"] }
  ],
  "refund_summary": "결제 후 7일 이내, 유료 기능 미사용 시 전액",
  "tone": "존댓말, 간결하게, 인사·맺음말 1문장씩",
  "support_hours": "평일 09:00~22:00"
}
```

### 2-3. 문서 md 형식 (`seed/docs/*.md`)
```markdown
---
doc_id: DOC-002
version: 3
doc_type: 정책
title: 환불 정책
status: 유효
created_at: 2026-10-15T00:00:00+09:00
---
결제일로부터 7일 이내이고 유료 기능 사용 이력이 없으면 전액 환불합니다. ...
```
- 같은 `doc_id`의 새 버전이 업로드되면 이전 버전은 백엔드가 `폐기`로 바꾼다.

### 2-3-1. 변경 전·후 정책 문서 (변경 감지 검증용)
| 문서 | 변경 전 | 변경 후 | 시드에 넣는 방법 | 검증하는 것 |
|---|---|---|---|---|
| DOC-002 환불 정책 | v2: 결제 후 14일, 사용 여부 무관 전액 | v3: 결제 후 7일 + 미사용 시 전액 (10/15부터) | **둘 다 시드에 포함** (v2 `폐기`, v3 `유효`) | import 시점에 이미 폐기된 문서를 근거로 한 답변을 잡아내는지 |
| DOC-001 요금제 안내 | v1: Pro 월 9,900원 / 연 95,040원 | v2: Pro 월 12,900원 / 연 123,840원 (Free·Team 동일) | v1만 시드, **v2는 `demo/`에 두고 시연 중 업로드** | 새 버전이 올라오는 순간 과거 답변이 재검토로 바뀌는지 |

- 요금 변경은 두 경로로 들어올 수 있다: ① DOC-001 v2 업로드 ② 싱크노트 `release/pricing` PR 머지(`billing/pricing.ts`). 시연은 ②로 하고 ①은 레벨 0(GitHub 연동 없음) 대체 흐름으로 보여준다.
- DOC-001 v2에서는 **Pro 금액만** 바꾼다. Team 요금 답변(14,900원)은 `billing/pricing.ts`와 연결돼 있지만 바뀌지 않아야 정상 → 오탐 검증용

### 2-4. inquiries.json (입력값만)
```json
{
  "inquiry_id": "Q-0174",
  "tenant_id": "syncnote",
  "customer_id": "U-1184",
  "created_at": "2026-11-03T14:11:00+09:00",
  "title": "저장이 안돼요",
  "messages": [
    { "sender": "customer", "text": "방금부터 저장이 안 되고 E502 에러가 뜹니다", "sent_at": "2026-11-03T14:11:00+09:00", "attachments": [] }
  ],
  "customer_category": "기능 오류",
  "env": { "os": "iOS 18", "app_version": "2.4.0", "error_code": "E-502" },
  "status": "접수"
}
```
- 고객이 고른 카테고리는 `customer_category`. **AI 분류 결과 필드는 시드에 없다** (시스템이 만든다).
- `env.error_code`는 고객이 안 썼으면 `null`. 문장에만 있는 경우도 있다(의미 기반 묶기 테스트).
- 담당자 답변은 `messages`에 `sender: "agent"` + `answer_id`로 넣는다. 같은 답변이 `answers.json`에도 있고, 두 곳의 `answer_id`가 일치해야 한다.

### 2-4-1. 별점 없는 상담 (대화로만 반응)
답변 후 고객 반응은 **대화 메시지로만** 넣고, `answers.json`의 `rating`은 `null`로 둔다.

| 케이스 | 마지막 메시지 | `status` | 정답(`labels`) |
|---|---|---|---|
| 해결 | 고객: "해결됐어요 감사합니다" | `상담 종료` | `해결` |
| 미해결 | 고객: "아직 안 돼요" | `담당자 확인 중` (다시 열림) | `미해결` |
| 무응답 | 담당자 답변이 마지막 | `답변 완료` | `무응답` |

예시 (미해결)
```json
{
  "inquiry_id": "Q-0120",
  "status": "담당자 확인 중",
  "messages": [
    { "sender": "customer", "text": "계속 '동기화 중'으로 멈춰 있어요", "sent_at": "2026-10-26T10:39:00+09:00", "attachments": [] },
    { "sender": "ai_auto", "text": "접수됐어요. 담당자가 확인 후 답변드려요.", "sent_at": "2026-10-26T10:39:05+09:00" },
    { "sender": "agent", "answer_id": "A-0110", "text": "앱을 최신 버전으로 업데이트하고 ...", "sent_at": "2026-10-26T11:20:00+09:00" },
    { "sender": "customer", "text": "업데이트하고 재로그인까지 했는데 아직 안 돼요", "sent_at": "2026-10-26T11:42:00+09:00", "attachments": [] }
  ]
}
```
- 비율 ⚠ 결정 필요 (예: 별점 없는 상담 30건 = 해결 12 / 미해결 10 / 무응답 8)
- 고객 반응 문장은 "해결됐어요"처럼 뻔한 것만 넣지 말고 "오 됐네요", "그래도 똑같은데요", "흠..." 같은 애매한 표현도 섞는다 (판단 테스트)
- ⚠ 결정 필요: 별점은 없지만 대화로 "해결"이 확인된 답변을 우수 답변 후보로 볼지. 현재 명세서 7장은 "미평가 = 후보 제외". 바꾸려면 명세서도 같이 고친다

### 2-5. answers.json (입력값만)
```json
{
  "answer_id": "A-0052",
  "inquiry_id": "Q-0054",
  "answered_at": "2026-10-15T10:20:00+09:00",
  "agent_id": "agent_01",
  "answer_text": "안녕하세요, 싱크노트 고객지원팀입니다. ...",
  "evidence": {
    "docs": [{ "doc_id": "DOC-001", "version": 1 }, { "doc_id": "DOC-009", "version": 1 }],
    "code": [{ "path": "billing/pricing.ts", "commit_sha": null }]
  },
  "agent_edit_ratio": 0.07,
  "rating": 5,
  "rating_comment": null
}
```
- `rating`은 1~5 정수, 미평가는 `null`.
- `answered_at`은 운영시간(09:00~22:00) 안으로 생성한다.

### 2-6. best_answers.json (입력값만)
```json
{
  "best_id": "BA-016",
  "answer_id": "A-0052",
  "approved_by": "admin_01",
  "approved_at": "2026-10-16T10:00:00+09:00",
  "status": "유효"
}
```
- **시드에서는 전부 `유효`로 넣는다.** BA-001, BA-010(환불 v2 근거)도 `유효`로 넣고, import 직후 시스템이 "근거 문서 DOC-002 v2가 폐기됨"을 보고 직접 `재검토 필요`로 바꿔야 한다 (2-3-1).
- 요금 관련 우수 답변(9,900원 언급 6개)은 시연 중 요금 변경이 들어오는 순간 `재검토 필요`로 바뀌어야 한다.

### 2-7. labels/ (채점 전용)
`inquiry_labels.json`
```json
{ "inquiry_id": "Q-0174", "category": "기능 오류", "urgency": "높음",
  "group_id": "G-001", "draft_result": "초안 가능", "team_label_set": true, "intent": "e502" }
```
`answer_labels.json`
```json
{ "answer_id": "A-0052",
  "customer_reaction": null,
  "stale_at_import": false,
  "stale_after_pricing_change": true,
  "expected_facts_after_change": { "pro_monthly": 12900, "pro_annual": 123840 } }
```
- `customer_reaction`: 별점 없는 상담만 `해결`/`미해결`/`무응답`, 나머지는 `null`
- `stale_at_import`: 폐기된 문서(DOC-002 v2)를 근거로 한 답변 → `true`. import 직후 시스템이 재검토로 바꿔야 정답
- `stale_after_pricing_change`: 요금 변경 후 재검토로 바뀌어야 하는 답변. Team 요금 답변은 `false` (오탐 검증)
- `expected_facts_after_change`: 수정 초안에 들어가야 할 값. 재작성된 초안 채점용
`bug_truth.json`
```json
[
  { "group_id": "G-001", "error_code": "E-502", "buggy_file": "sync/saveHandler.ts",
    "buggy_function": "flushBatch", "bug_commit": null, "fix_commit": null },
  { "group_id": "G-004", "error_code": null, "buggy_file": "preview/androidImageEngine.kt",
    "buggy_function": "decodeThumbnail", "bug_commit": null, "fix_commit": null }
]
```

### 2-8. 무결성 규칙 (`scripts/validate`)
시드를 바꿀 때마다 돌리고, **전부 통과해야 `seed-vN` 태그를 찍는다.** 하나라도 실패하면 import하지 않는다.

**연결**
1. `answers[].inquiry_id` → `inquiries`에 존재
2. `inquiries[].messages[]`의 `answer_id` → `answers`에 존재하고, 두 곳의 답변 문장이 일치
3. `best_answers[].answer_id` → `answers`에 존재, 그 답변의 문의 상태가 `상담 종료`
4. `best_answers` 선정 근거가 명세서 7장 기준과 일치 (★5, 또는 ★4 + 수정률 30% 미만)
5. `evidence.docs[]`의 `doc_id`+`version` → `seed/docs/`에 파일이 존재 (demo/ 문서는 근거로 쓸 수 없음)
6. 답변 시각이 근거 문서 `created_at` 이후 (아직 없던 문서를 참고한 답변 금지)
7. `evidence.code[].path` → 싱크노트 레포 해당 `commit_sha`에 파일이 존재
8. `labels/*`의 모든 ID → 입력 파일에 존재

**분리**
9. 입력 파일(`inquiries`·`answers`·`best_answers`)에 정답 필드(`label_*`, `customer_reaction`, `stale_*`, `intent`, `group_id`, `draft_result`)가 없음
10. `best_answers[].status`가 전부 `유효`
11. `demo/` 파일이 시드 import 대상 목록에 없음

**형식**
12. 모든 시각에 `+09:00` 타임존, enum 값이 1-3 목록 안에 있음, 답변 시각이 운영시간(09:00~22:00) 안

---

## 3. 문의 유입 API

### 3-1. ⚠ 결정 필요: 유입 방식
- [ ] **A. 채팅 위젯** — 싱크노트 웹에 모아 위젯 스크립트를 붙임. 프론트가 위젯 제공, 싱크노트 담당이 삽입
- [ ] **B. API 직접 호출** — 싱크노트 "문의하기" 화면이 아래 API 호출

### 3-2. 문의 생성
```
POST /api/v1/inquiries
Content-Type: application/json
```
```json
{
  "tenant_id": "syncnote",
  "customer_id": "U-2001",
  "message": "저장 누르니까 E-502 오류라고 나옵니다",
  "customer_category": "기능 오류",
  "env": { "os": "Web (Chrome 130)", "app_version": "web", "error_code": "E-502" },
  "attachments": [],
  "created_at": null
}
```
응답
```json
{ "inquiry_id": "Q-1001", "status": "접수",
  "auto_message": { "sender": "ai_auto", "text": "접수됐어요. 담당자가 확인 후 답변드려요." } }
```
- `created_at`: 평소엔 `null`(서버 시각). **데모 모드(`DEMO_MODE=true`)에서만** 값 지정 허용.
- `env`는 싱크노트 앱이 자동으로 채운다(OS, 앱 버전, 마지막 오류 코드).

### 3-3. 메시지 추가 (같은 문의 이어서)
```
POST /api/v1/inquiries/{inquiry_id}/messages
{ "sender": "customer", "text": "...", "attachments": [] }
```

### 3-4. 첨부 업로드
```
POST /api/v1/attachments   (multipart/form-data, file 필드)
→ { "attachment_id": "ATT-0001", "url": "...", "mime": "image/png", "size": 182344 }
```
- 허용: `image/png`, `image/jpeg`, `application/pdf` / 파일당 10MB
- 문의 생성·메시지 추가 시 `attachments: ["ATT-0001"]`로 참조

### 3-5. 고객 채팅 조회 (폴링)
```
GET /api/v1/inquiries/{inquiry_id}?since=2026-11-05T10:00:00+09:00
```
- ⚠ 결정 필요: 폴링 주기 `3초` / SSE 사용 여부

---

## 4. 싱크노트 앱 규칙

- **오류 표시 형식 고정**: 화면에 `E-502`처럼 `E-` + 숫자(또는 `E-PAY-07`)로 항상 같은 형식. 스크린샷 오류 코드 추출이 이걸 읽는다.
- 오류 코드 목록은 `DOC-005 오류 코드 안내`와 일치해야 한다: `E-401`, `E-413`, `E-429`, `E-502`
- **장애 스위치**: 버그를 켜고 끄는 플래그 제공 (예: `?fault=e502` 또는 관리자 토글). 시연 중 실제로 E-502를 띄우기 위함
- 문의 시 `env` 자동 수집: `os`, `app_version`, 직전에 표시된 `error_code`

---

## 5. GitHub 연동

### 5-1. 레포·브랜치
| 항목 | 값 |
|---|---|
| Org | ⚠ 결정 필요 (예: `syncnote-demo`) |
| 레포 | ⚠ 결정 필요 (예: `syncnote-app`) |
| 감시 브랜치 | `main` |
| 요금 변경 PR 브랜치 | `release/pricing` |
| 데모 시작 태그 | `demo-start` |

### 5-2. 코드 경로 (evidence·bug_truth와 일치해야 함)
```
billing/pricing.ts            PRO_MONTHLY = 9900  (데모 PR에서 12900, Team은 그대로)
billing/refund.ts
sync/saveHandler.ts           E-502 (flushBatch)
sync/syncEngine.ts
upload/attachmentLimit.ts     E-413
auth/session.ts               E-401
search/indexer.ts
preview/androidImageEngine.kt G-004
config/prod.env.example       보안 시연용 가짜 키
```

### 5-3. 커밋 규칙
- 메시지 형식: `<type>(<영역>): <내용>` 예) `fix(sync): 저장 묶음 크기 원복`
  - type: `feat` · `fix` · `chore` · `release`
  - 영역: 폴더 이름과 동일 (`billing`, `sync`, `upload`, `auth`, `search`, `preview`)
- 보안 레벨 1(메타데이터만) 시연은 커밋 메시지와 변경 경로만으로 영향 분석한다.
- 커밋 시각은 CSV 타임라인과 맞춘다 (`GIT_AUTHOR_DATE`, `GIT_COMMITTER_DATE` 지정)

| 커밋 | 시각 | 브랜치 |
|---|---|---|
| 환불 정책 14일 → 7일 (`billing/refund.ts`) | 2026-10-15 10:05 | main |
| 묶음 저장 도입 (`sync/saveHandler.ts`) | 2026-10-30 11:30 | main |
| Android 미리보기 엔진 교체 (`preview/androidImageEngine.kt`) → G-004 원인 | 2026-10-30 12:10 | main |
| E-502 유발: 묶음 병합 제거 (`sync/saveHandler.ts`) | 2026-11-03 13:50 | main (태그 `demo-start`) |
| E-502 수정 | 2026-11-03 15:30 | `fix/e502-batch` (머지 안 함) |
| 요금 변경 9,900 → 12,900 (`billing/pricing.ts`) | 2026-11-04 18:00 | `release/pricing` (태그 `pricing-start`, 데모 때 PR 머지) |

전체 커밋 목록은 `싱크노트_개발명세서.md` 11장 기준.

### 5-4. GitHub App·webhook
- 권한: Contents **Read**, Metadata **Read**, Issues **Read & Write**, Pull requests **Read**
- 이벤트: ⚠ 결정 필요 — `push`(main) / `pull_request`(closed + merged)
- webhook URL: ⚠ 결정 필요 (배포 서버 또는 터널 주소)
- App 생성·설치 담당: ⚠ 결정 필요

### 5-5. 모아가 만드는 이슈 형식
- 라벨: `bug`, `moa-auto` (리셋 스크립트가 `moa-auto` 기준으로 닫음)
- 제목: `[MOA] {오류 코드 또는 요약} 문의 {건수}건 급증 ({그룹 ID})`
- 본문:
```markdown
## 요약
- 그룹: G-001 · 문의 16건 (14:05~14:40)
- 대표 문의: "저장 누르니까 E-502 오류라고 나옵니다"
- 영향 환경: iOS 18, Android 15, Web (Chrome 130) · 앱 2.4.0

## 의심 코드 위치 (후보)
1. `sync/saveHandler.ts` — flushBatch
2. ...

## 최근 관련 커밋
- `a1b2c3d` fix(sync): ... (2026-11-03 13:50)

## 근거 문의
- Q-1001, Q-1002, ...

> 모아가 자동 생성한 이슈입니다. 원인 확정이 아닌 후보입니다.
```

### 5-6. 보안 시연 파일
- 기본 제외 패턴: `.env*`, `*.pem`, `*.key`, `secrets/`, `config/prod*`
- 가짜 키 형식: `FAKE_KEY_moa_demo_XXXX` — 실제 서비스 키 형식(`sk_live_`, `AKIA` 등)은 쓰지 않는다 (GitHub push 차단 방지)

---

## 6. 데모 시간 처리 ⚠ 중요

- 시드 데이터는 과거 날짜(11/03 기준) 그대로 둔다 → 우수 답변·처리 완료 문의용
- **급증은 시드로 재현하지 않는다.** 급증 감지는 "지금부터 N분 안"이라서 과거 데이터로는 안 뜬다.
- `burst_replay` 스크립트가 **현재 시각 기준**으로 문의 API(3-2)를 시간차로 호출한다.
  - E-502 16건 + E-413 4건 + E-401 4건을 섞어서 약 ⚠ 결정 필요(예: 2분) 동안 전송
- 급증 임계값: ⚠ 결정 필요 (예: 5분 안에 같은 그룹 8건) — 스크립트 속도에 맞춰 정한다

---

## 7. 리셋

명령 하나로 아래를 순서대로 실행한다.
1. 모아 DB 초기화 → 시드 재import: `POST /api/v1/admin/demo/reset` (백엔드, `DEMO_MODE`에서만)
2. GitHub: `main`을 `demo-start`로 되돌림, `release/pricing`을 `pricing-start`로 되돌린 뒤 PR 새로 생성(머지된 PR은 다시 열 수 없음), `moa-auto` 라벨 이슈 전부 닫기 (데이터 담당 스크립트)
3. 싱크노트 장애 스위치 OFF

> main 브랜치 보호 규칙은 꺼둔다 (force push 필요).

---

## 8. 일정·버전

- 시드 버전: `seed-v1`, `seed-v2` … 태그로 관리. 바꾸면 `seed/README.md`에 한 줄씩 기록
- 데이터 확정일: ⚠ 결정 필요 — 이후 필드·enum 변경 금지
- 이 문서 변경 시 아래 이력에 추가하고 팀 채널에 공유

---

## 9. 결정 필요 목록 (모아보기)

- [ ] 문의 유입 방식 (위젯 / API 직접 호출)
- [ ] 폴링 주기 / SSE
- [ ] GitHub Org·레포 이름
- [ ] webhook 이벤트 (push / PR merged), URL, App 담당
- [ ] 급증 재현 속도, 급증 임계값
- [ ] 데이터 확정일
- [ ] 별점 없는 상담 건수·비율
- [ ] 대화로 "해결"이 확인된 미평가 답변을 우수 답변 후보로 볼지 (명세서 7장과 연동)

---

## 변경 이력
| 버전 | 날짜 | 내용 |
|---|---|---|
| v0.1 | 2026-11-04 | 초안 |
| v0.3 | 2026-11-04 | 5-3 커밋 표를 싱크노트 개발명세서와 맞춤 (E-502 수정은 별도 브랜치), 리셋에 `pricing-start` 태그 반영 |
| v0.2 | 2026-11-04 | 별점 없는 상담(대화 반응) 추가, 변경 전·후 정책 문서(DOC-001 v1/v2, DOC-002 v2/v3)와 `demo/` 폴더, `answer_id` 메시지 연결, 정답 라벨 확장, 무결성 규칙(2-8) 추가 |
