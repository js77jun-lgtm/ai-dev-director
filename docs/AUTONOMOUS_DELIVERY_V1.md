# HEAD 주도 제작 루프

2026-09-14. 기본 설정: Claude HEAD → Antigravity Builder → 자동 검사 → Codex Reviewer → Claude HEAD.

HEAD는 목표를 ORDER로 만들고 구현상의 선택을 맡는다. Builder는 변경 파일의 전체 내용을 JSON으로 제출한다. Director는 모든 제출 경로를 먼저 검사하고 격리 작업본에 반영한다. 기존 보호 패턴은 Python Scope Guard가 처리하므로 Antigravity 네이티브 경로 권한으로 변환할 필요가 없다. Antigravity 호출은 별도 임시 저장소에서 수행하며 업무 저장소를 CLI 작업 디렉터리로 주지 않는다. CLI는 제공된 텍스트에서 구현하며 실제 파일 반영은 Director가 담당한다.

자동 테스트가 실패해도 바로 종료하지 않고 Reviewer와 HEAD에 전달한다. HEAD의 FAIL 및 next_steps가 수정 지시가 되며 같은 작업본에서 BUILDING으로 돌아간다. 기본 최대 수정 3회. 라운드별 리뷰·테스트·HEAD 판단은 repair_history.json에 보존한다. 최종 PASS 조건은 기존 검증과 독립 리뷰를 그대로 사용한다.

HUMAN_REQUIRED는 사용자 판단/외부 권한 필요를 뜻한다. 기존 행동 무단 삭제, 경로 범위 위반, 공급자 인증 실패 등은 자동 수정 대상에서 제외된다. 범위 초과, 실패한 초기 baseline, 잘못된 Builder JSON까지 모두 복구하는 만능 루프는 아니다. 재부팅 후 자동 재개와 원본 프로젝트 자동 반영도 별도 기능이며 이번 변경에 포함되지 않는다.

## 설정

- builder.mode: proposal. 기존 방식은 mode 설정 제거로 선택.
- autonomy.enabled: true
- autonomy.max_repairs: 3

## 검증

- tests/test_v303_proposal.py: 8개 통과. 일반 경로 반영, 보호 glob/상위 경로 차단, 테스트 실패→HEAD 수정→PASS, 테스트 PASS지만 리뷰 수정 필요 시 재작업, 사용자 판단 필요 시 중단, 수정 횟수 제한.
- 기존 resume/policy와 최종 proposal 회귀 묶음 54개 통과 (171.33초).
- scripts/test_autonomous_delivery.py: 실제 세 CLI로 비용 CSV 집계 프로그램을 독립 프로젝트에 제작. 결과는 해당 runs 디렉터리에 저장한다.

공급자 CLI 경로 권한 지원 범위 참고: https://antigravity.google/docs/cli/permissions

## 실제 CLI 실험 기록

첫 실험 `RUN-260914222917-38BC7D30A97141D6A3462735D8EF0138`은 최초 구현과 자동 수정 3회를 실제 Claude/Antigravity/Codex로 수행했다. Python 3.9 호환성, 잘못된 CSV 인용부호, 큰 금액 정밀도 오류를 발견·수정하며 테스트가 31개 통과했다. 그러나 HEAD가 제안한 정수 센트 집계가 원래 Decimal 전용 조건에 위배되어 독립 Reviewer가 거절했고, 예산 소진 후 최종 FAIL을 유지했다. 성공으로 간주하지 않는다.

HEAD 프롬프트에 수정 지시와 원래 승인 조건의 일치 확인을 추가했다. 실험 산출물 두 파일을 내용 변경 없이 데모 프로젝트로 복사하고 별도 후속 주문으로 완성을 재검증한다. 이는 재부팅 자동 재개나 실패 실행 자동 재시작의 증거가 아니다.

후속 실행 `RUN-260914224130-E7203CB1857045DEB6F63A058801A2F7`은 2026-09-14 22:46 최종 PASS. Claude HEAD의 실제 계획, Antigravity 실제 구현, Director의 실제 pytest 실행(기존 30개 → 34개 통과), Codex 독립 리뷰(pass, P0/P1/P2 없음), HEAD 최종 PASS와 Director 최종 증거 게이트를 확인했다. HEAD/Reviewer는 읽기 전용 검수이며 전체 테스트 실행은 Director가 담당했다. 후속 실행은 추가 수정 라운드 없이 통과했고, 앞선 실행에서 자동 수정 루프 3회가 실증되었다.

산출물: `<demo-project>\.director_worktrees\RUN-260914224130-E7203CB1857045DEB6F63A058801A2F7\src\expense_summary.py`. `--csv <demo-project>\example.csv` 직접 실행에서 Food=13.00, Books=20.00, grand_total=33.00, 종료 코드 0을 확인했다. 원본 프로젝트에 최종 결과를 자동 반영하지 않았으므로 실행에는 위 작업본 경로를 사용한다. 기존 the trading sample project 실행을 완료했다는 의미는 아니다.
