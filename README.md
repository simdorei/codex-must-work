# Codex Must Work

한 Codex 스레드에 선택적으로 거는 진행 감시 플러그인입니다. 단순히 UI의 `busy` 표시를
믿지 않고, 로컬 rollout 기록에서 실제 응답·추론·도구 결과가 계속 생기는지 확인합니다.

진행이 오래 보이지 않으면 로컬 하트비트 진단을 남기고, 더 긴 심각 정체가 확인되면
안전 조건을 통과한 경우에만 같은 작업을 재개합니다. `cleanup` 메시지를 선택하면 작업이
끝났는데 남아 있는 작업 소유 프로세스를 정리한 뒤 계속하라고 지시할 수 있습니다.

## OpenAI Build Week

Codex Must Work is an opt-in progress supervisor for one Codex task. It distinguishes a turn that is
merely marked busy from one that is producing real progress, records privacy-safe local diagnostics,
and can resume only the exact managed turn after strict ownership and safety checks. It does not call
a model API at runtime or send prompt, response, or tool contents to an external monitoring service.

### How we used Codex and GPT-5.6

We built and reviewed the project in Codex with GPT-5.6. Codex explored the existing plugin and hook
interfaces, implemented the Python and PowerShell runtime, and repeatedly ran the regression suite,
linting, type checks, and installation smoke tests. GPT-5.6 helped us reason through the hard failure
modes: exact-turn ownership, stale-busy detection, safe manager reuse, path identity across platforms,
activation races, and fail-closed behavior. At runtime the plugin integrates with Codex skills, hooks,
and privacy-filtered local rollout metadata; GPT-5.6 is the development agent, not a hidden runtime API
dependency.

## Installation

Codex Must Work supports Windows x64, Linux x64, and macOS ARM64. Add this repository as a Codex
plugin marketplace, then install the plugin:

```bash
codex plugin marketplace add simdorei/codex-must-work --ref main
codex plugin add codex-must-work@simdorei
```

Restart ChatGPT desktop or Codex, open a new thread, approve the local hook if Codex asks, and run
`$work-on`. Judges can use this read-only smoke test:

```text
$work-on Objective: perform a read-only check that the current working directory exists. Success criteria: confirm it exists, make no file changes, then use the verified-completion path of $work-off and reply WORK_ON_VERIFIED.
```

The expected final reply is `WORK_ON_VERIFIED`; verified completion also disables the task supervisor.

## 사용법

현재 스레드에서 다음 한 줄로 켭니다.

```text
$work-on
```

기본 동작은 다음과 같습니다.

- 하트비트: `10m`
- 심각 정체: `20m`
- 재개 메시지: `cleanup`
- 안전한 자동 재시작: 켜기

native Goal은 선택 사항입니다. Goal이 없어도 자동 재시작은 그대로 켜집니다.

- CMW가 정체된 정확한 턴을 중단한 경우: 같은 작업을 한 번 다시 시작
- 성공 조건을 확인한 경우: Final 직전에 `$work-off --completed`를 호출
- 완료 요청 뒤 작업 턴이 정상 완료된 경우: Final 이후 완료 기록을 남기고 감시 종료
- 완료 요청 없이 작업 턴만 정상 종료된 경우: 성공으로 단정하지 않고 같은 작업을 계속 진행
- 사용자나 다른 클라이언트가 중단한 경우: 자동 재시작 없이 종료
- 턴이 실패한 경우: Final로 처리하지 않고 오류 상태로 종료

사용자가 시간을 직접 말하면 그 값이 항상 우선합니다.

```text
$work-on 하트비트 5분, 심각 정체 15분, continue 사용
```

현재 작업의 감시만 끄려면 다음을 사용합니다.

```text
$work-off
```

## 설치 후 첫 스레드의 추천

플러그인을 설치하거나 업데이트한 뒤 여는 첫 스레드에서 로컬 기록을 한 번 분석합니다.
추천값이 계산되면 자동 적용하지 않고 다음과 같이 적용 여부를 묻습니다.

```text
최근 실제 진행 간격 84개를 분석한 추천은 하트비트 3분, 심각 정체 7분입니다.
이 값을 적용할까요? 권장 답변: 적용
```

명시적으로 동의해야만 추천값을 저장합니다. 거절하거나 답하지 않으면 `10m`/`20m`가
유지됩니다. 같은 설치 버전에서는 다시 스캔하거나 질문하지 않습니다.

계산 규칙은 다음과 같습니다.

- 최근 30일, 최신 세션 파일 최대 100개
- 전체 읽기 최대 64MiB, 파일당 최대 8MiB
- 유효한 실제 진행 간격이 최소 20개일 때만 추천
- 하트비트: 진행 간격 P95를 분 단위로 올림
- 심각 정체: P99와 하트비트의 2배 중 큰 값을 분 단위로 올림
- 사용자 입력, 권한 승인, 도구 실행을 기다린 시간과 turn 경계는 제외

프롬프트, 답변 본문, 도구 입력·출력 내용은 추천 상태나 진단 로그에 저장하지 않습니다.

## `busy`와 실제 진행의 차이

`busy`는 turn이 아직 끝나지 않았다는 뜻일 뿐, 계산이 계속되고 있다는 증거는 아닙니다.
Must Work는 다음과 같은 실제 이벤트가 마지막으로 관찰된 시각을 따로 추적합니다.

- assistant 메시지와 추론 항목
- 스트리밍 delta
- 도구 호출 시작과 결과
- 하위 에이전트 활동
- turn 시작·완료·중단

단, Busy turn 안에 새 채팅 메시지를 안전하게 끼워 넣을 수는 없습니다. 하트비트는
`<CODEX_HOME>/codex-must-work/logs/diagnostic.jsonl`에 내용 본문 없이 기록됩니다.
심각 정체 재개는 Codex가 소유권을 검증한 정확한 turn에만 시도합니다.

## 옵션

| 옵션 | 의미 |
| --- | --- |
| `continue` | 같은 작업을 그대로 계속하라고 지시합니다. |
| `cleanup` | 작업 소유의 남은 런타임을 안전하게 정리한 뒤 계속하라고 지시합니다. |
| 자동 재시작 끄기 | 감시와 로컬 진단만 사용하고 turn을 중단하지 않습니다. |
| `--observe-only` | 재개 메시지와 자동 재시작을 모두 사용하지 않는 진단 전용 모드입니다. |
| Goal companion | 기존 Goal을 별도 작업으로 복제하지 않고 일시정지·재개 상태와 함께 관리합니다. |

자동 재시작은 승인 없이 실행 가능한 Codex 권한 모드와 검증된 resident manager가 필요합니다.
조건이 맞지 않으면 이유를 그대로 알리고 안전하게 활성화를 거부합니다.

## Discord Remote와 함께 사용할 때

상태 조회와 수동 미러링은 함께 사용할 수 있습니다. 다만 Must Work가 같은 스레드의 turn을
소유하는 동안 Discord Remote의 `!stop`이나 steering은 다른 app-server 연결을 사용하므로
신뢰 가능한 동시 제어 수단으로 취급하지 않습니다. 이때는 `$work-off`로 먼저 Must Work의
소유권을 해제합니다.

## 포터블 Python

시스템 Python이나 첫 실행 다운로드가 필요하지 않습니다. 다음 세 CPython 3.12.13 런타임을
플러그인에 압축 상태로 포함하며, 해당 운영체제의 런타임만 `PLUGIN_DATA`에 한 번 풉니다.

- Windows x64
- Linux x64
- macOS ARM64

다른 CPU·운영체제 조합은 조용히 우회하지 않고 지원하지 않는 대상으로 오류를 냅니다.
포함 파일의 출처와 SHA-256은 [`runtime/manifest.json`](runtime/manifest.json)에 있습니다.

## 설치 후 확인

새 hook 정의를 처음 발견한 Codex는 신뢰 여부를 한 번 물을 수 있습니다. 승인한 뒤 새
스레드를 열어야 `SessionStart` 감시와 설치 버전별 추천이 적용됩니다. 업데이트로 설치 캐시
경로가 바뀌면 다시 물을 수 있습니다.

## 개발 검증

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
```

플러그인 코드는 MIT 라이선스입니다. 포함된 CPython 배포물과 구성 요소의 라이선스는
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)를 확인하세요.
