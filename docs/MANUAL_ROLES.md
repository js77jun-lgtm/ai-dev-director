# 수동 역할 교환

2026-09-14. 8081 작업실의 각 AI 카드 하단 `다음: 역할 ↻` 버튼을 누르면 HEAD → BUILDER → REVIEWER 순서로 순환하며, 선택한 역할의 기존 담당자와 교환한다. 세 공급자는 각각 한 역할을 담당하므로 6개 순열 모두 가능하다. 카드 표의 역할은 마지막/현재 실행 배정이고 버튼은 다음 요청 배정이다.

- 별도 `data/role_settings.json`에 원자적으로 저장. 새 `load_config()`에 반영하며 이미 생성된 Orchestrator의 설정은 변경하지 않는다.
- 다음 HEAD 대화/개발 요청부터 적용. TRIAD 연구의 고유 단계 배정은 변경하지 않는다.
- 수동 선택 모드에서는 공급자 장애 시 다른 AI로 몰래 대체하지 않는다. 다른 역할 조합 선택 또는 해당 CLI 복구가 필요하다.
- Codex/Claude Builder도 제안 JSON → 동일한 Scope Guard → Director 파일 반영/테스트 방식을 사용한다. Claude CLI 결과 envelope를 해제한다.
- Google HEAD/Reviewer는 격리 CLI에서 제공된 계획 컨텍스트·diff·검증 증거를 사용한다. 실제 테스트 실행은 Director가 담당하며 Reviewer가 실행했다고 주장하지 않는다.
- `/ops/roles` 변경에는 기존 localhost 출처 검사, bridge 토큰, JSON/크기 검사, 3개 공급자 중복 금지 검증이 적용된다.
- 내부 공급자 코드와 회사명을 중복 표시하던 제목을 제품명 + 회사명 1회로 정리했다.

검증: 역할 설정/6개 순열/CLI 라우팅/스냅샷 보존/Claude 응답 파싱 테스트 14개 통과. 실제 브라우저 버튼 교환, 새로고침 후 유지, 기본 배정 복원을 확인했다. 인증 없는 POST는 403이다. 기존 resume/policy와 역할 테스트 회귀 묶음 59개 통과(132.13초). 이 묶음은 envelope 테스트 추가 전이므로 이후 14개 역할 테스트를 별도로 실행했다. 실제 CLI smoke에서 Google HEAD의 ORDER 생성, Codex 및 Claude Builder의 add(2,3)=5 구현, Google Reviewer의 JSON 검토 응답을 각각 종료 코드 0으로 확인했다. 6개 조합 모두에 대한 전체 개발 실행을 실측했다는 뜻은 아니다.
