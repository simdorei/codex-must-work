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

Codex Must Work는 Windows x64, Linux x64, macOS ARM64를 지원합니다. 저장소를 내려받은 뒤
저장소 루트에서 운영체제에 맞는 명령 하나를 실행하세요.

Windows PowerShell:

```powershell
.\install.ps1
```

Linux 또는 macOS:

```sh
./install.sh
```

위 두 저장소 루트 스크립트가 설치와 업데이트를 위한 유일한 지원 경로입니다.
소스 체크아웃을 이동하거나 삭제하지 마세요. 저장소를 업데이트한 뒤 같은 설치 명령을 다시 실행하면 검증된 새
버전 캐시와 신뢰 설정을 한 번에 갱신합니다. 일반 플러그인 설치나 수동 신뢰를 별도 대안으로
사용하지 않습니다.

설치 또는 업데이트가 끝나면 ChatGPT 데스크톱이나 Codex를 재시작하고 새 스레드를 여세요.
설치 프로그램이 정확히 3개 CMW 생명주기 훅의 신뢰를 기록하므로 `/hooks`에서 따로 승인할 필요는
필요하지 않습니다. 새 설치 버전의 첫 스레드에서는 로컬 기록으로 추천값을 다시 계산하지만
자동 적용하지 않습니다.

심사자는 새 스레드에서 다음 읽기 전용 확인을 실행할 수 있습니다.

```text
$work-on Objective: perform a read-only check that the current working directory exists. Success criteria: confirm it exists, make no file changes, then use the verified-completion path of $work-off and reply WORK_ON_VERIFIED.
```

The expected final reply is `WORK_ON_VERIFIED`; verified completion also disables the task supervisor.

### 지원 Codex 버전과 설치 경로

설치 프로그램은 소스 코드로 검증해 고정한 다음 세 Codex 버전만 허용합니다.

- `0.144.0-alpha.4`
- `0.144.0`
- `0.145.0-alpha.18`

설치가 성공하면 다음 소유 경로만 사용합니다.

- 플러그인: `codex-must-work@codex-must-work-local`
- 마켓플레이스 설정: `[marketplaces.codex-must-work-local]`
- 버전 캐시: `<CODEX_HOME>/plugins/cache/codex-must-work-local/codex-must-work/0.2.0+codex.20260722000000`
- 작업 데이터: `<CODEX_HOME>/plugins/data/codex-must-work-codex-must-work-local`
- Codex 설정: `<CODEX_HOME>/config.toml`

저장소 매니페스트의 버전이 캐시 버전과 항상 같습니다. 예약 버전 `local`인 캐시 또는 설치할
버전보다 더 높은 버전의 로컬 마켓플레이스 캐시가 이미 있으면, Codex가 다른 캐시를 고르는
상황을 막기 위해 설치를 명시적으로 중단합니다.

### 기존 설치 마이그레이션

기존 설정에 `codex-must-work@simdorei` 플러그인 표가 실제로 있을 때만 그 플러그인을
`enabled = false`로 바꿉니다. 기존 캐시, 훅 상태, 작업 데이터와 보정 기록은 삭제하지 않습니다.
기존 `simdorei` 마켓플레이스도 그대로 보존합니다. 예전 설치 표가 없으면 새로 만들지 않습니다.

### 정확한 진단 코드

설치가 실패하면 조용히 다른 방법을 시도하지 않고 다음과 같은 안전한 진단 코드를 그대로
표시합니다.

| 진단 | 의미 |
| --- | --- |
| `unsupported_codex_hook_contract: CMW must be updated for this Codex version` | 설치된 Codex 버전이 고정된 세 버전 밖입니다. |
| `unsupported_codex_marketplace_root` | 해당 Codex가 저장소 루트의 `./` 플러그인을 정확히 읽지 못했습니다. |
| `codex_hooks_disabled` | 유효 설정에서 Codex 훅 기능이 꺼져 있습니다. |
| `codex_plugins_disabled` | 유효 설정에서 플러그인 기능이 꺼져 있습니다. |
| `managed_hooks_only` | 조직 정책이 관리형 훅만 허용합니다. |
| `managed_hook_policy_unverifiable` | 조직의 관리형 훅 정책을 안전하게 판별할 수 없습니다. |
| `cache_selection_conflict` | 예약 캐시나 더 높은 버전 캐시 때문에 정확한 선택을 보장할 수 없습니다. |
| `cache_same_version_mismatch` | 같은 버전 캐시의 내용 또는 보안 메타데이터가 다릅니다. |
| `codex_config_metadata_unsupported` | 기존 설정 파일의 특수 메타데이터를 비관리자 권한으로 보존할 수 없습니다. |

### 권한과 설정 메타데이터 제한

Windows 설치 프로그램은 관리자 권한을 요청하지 않습니다. 기존 `config.toml`의 지원되는
소유자, 그룹, DACL, 무결성 레이블, 리소스 특성, 일반 파일 특성을 보존합니다.
기존 `[notice]` 표는 바이트 단위로 변경하지 않습니다. 설정 파일을 새로 만들 때만
`hide_world_writable_warning = true`와 `hide_full_access_warning = true`를 기본값으로 넣습니다.

감사 ACE가 있는 audit SACL은 보존에 승격 권한이 필요하므로 비관리자 설치 범위 밖입니다.
사용자 지정 audit SACL 보존이 필요한 기업 관리형 `config.toml`에는 이 설치 프로그램을
사용할 수 없습니다. 이 제한을 우회하려고 승격하거나 SACL을 버리지 않고 설치 전에
`codex_config_metadata_unsupported`로 중단합니다.

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

설치 프로그램이 선택된 캐시의 정확한 3개 생명주기 훅 신뢰를 기록하므로 별도 신뢰 승인 단계가
필요하지 않습니다. 설치나 업데이트 후 애플리케이션을 재시작하고 새 스레드를 열어야
`SessionStart` 감시와 설치 버전별 추천이 적용됩니다. 설치 프로그램이 신뢰를 확립하지
못하면 우회하지 않고 오류 코드와 함께 중단합니다.

## 개발 검증

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
```

플러그인 코드는 MIT 라이선스입니다. 포함된 CPython 배포물과 구성 요소의 라이선스는
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)를 확인하세요.
