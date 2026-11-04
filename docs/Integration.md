# 백엔드 연결 기준

PostgreSQL 구축·테이블·마이그레이션·영속 데이터 관리는 백엔드 담당입니다.
AI 서비스는 분석 요청을 받아 결과를 반환하며 DB를 직접 생성하거나 변경하지 않습니다.
실행 상태·멱등키 등의 영속 저장은 백엔드 담당이 제공하는 계약에 연결하며, 없는 저장 API를 가정해서 호출하지 않습니다.
벡터 DB 선택과 RAG 인터페이스는 AI 담당 범위로 유지합니다.

참조 저장소: https://github.com/MOA-teams/moa-backend
인터페이스 규칙은 v0.3 snapshot을 `InterfaceRule.md`로 보존합니다. snapshot 파일의 내용을 AI 구현 완료로 해석하지 않습니다.
초기 프로젝트에서 실제 제공하는 동작/미구현 상태는 README와 OpenAPI를 확인합니다.

## 최신 규칙 반영

- snake_case, 한글 category/urgency/draft_result, customer/agent/ai_auto 유지.
- `sent_at`은 타임존 필수. `customer_category`는 입력, AI category/urgency는 출력.
- evidence는 docs[{doc_id,version}]/code[{path,commit_sha}] 형식.
- customer_reaction, stale_*, expected_facts_after_change 등 정답은 서비스 입력 모델에 없음.
- `seed/labels`와 `seed/demo`를 검색 인덱스에 import하지 않음. 초기 커밋에는 importer 자체를 넣지 않음.
- DOC-002 환불 v2는 폐기, v3는 유효: 두 문서가 초기 데이터에 존재하는 최신 규칙을 기준으로 함.
- DOC-001 요금 v1만 초기 사용, v2는 시연 중 등록. Team 금액은 변경 대상이 아님.
- GitHub 경로는 루트 상대 경로, commit_sha는 7~40자리 hex 또는 null. null 근거로 코드 변경 추적을 완료했다고 표시하지 않음.
- E-502 수정은 별도 브랜치이며 main의 해결 상태로 간주하지 않음.

원본 InterfaceRule의 ai_auto는 안내/질문 전용이며 APISpec에는 조건부 최종 자동 답변이 포함되어 있습니다.
AI 서비스는 고객 메시지를 직접 발송하지 않으므로 이 차이가 발송 동작을 암묵적으로 바꾸지 않습니다.
최종 발송 여부·표시는 백엔드의 적용 정책을 따릅니다.

## 단계별 구현 위치

| 위치 | 책임 |
|---|---|
| app/api/internal/v1 | 내부 HTTP 계약·권한 검증 |
| app/schemas | 백엔드와 공유할 직렬화 모델 |
| app/services/pipeline | 단계 실행·검증·생성·별도 검수 |
| app/services/llm | Gemini 제공자 어댑터 |
| app/services/rag | 청크·임베딩·검색·벡터 저장소 어댑터 |
| app/services/backend_client.py | 서버 설정 주소로 결과 콜백 |
| app/workers | durable 실행·재시도·복구 구현 진입점 |

공통 실패 형식은 error.code/error.message. 입력 검증 오류에는 고객 원문/토큰을 그대로 반환하지 않습니다.
멱등키나 실행 상태가 아직 저장되지 않으므로 scaffold는 501을 반환합니다. 테스트 성공은 AI 기능 완성이 아닌 계약/연결부 검증을 의미합니다.
