# Codex Must Work

한 Codex 스레드에 선택적으로 거는 진행 감시 플러그인입니다. UI의 `busy` 표시만 믿지 않고,
로컬 rollout에 기록되는 응답·추론·도구 결과 같은 진행 신호를 확인합니다.

진행이 오래 보이지 않으면 병목 의심과 심각 정체를 진단하고, 설정된 Discord 웹훅으로
상태 메시지를 보냅니다. CMW는 Codex 작업을 중단하거나 자동으로 재시작하지 않습니다.
심각 정체 메시지는 작업 상태 확인만 요청하며 Codex 작업을 제어하지 않습니다.

Codex가 시작하면 CMW MCP 서버 겸 daemon인 Python 프로세스 하나가 시작되어 요청을
기다립니다. `$work-on`, 상태 확인, `$work-settings`, `$work-off`, 완료 요청은 이 프로세스에
메시지만 보냅니다. 별도 Codex app-server, watcher, manager 프로세스는 실행하지 않으며,
CMW가 비활성일 때 daemon은 이벤트를 기다리며 잠듭니다.

## OpenAI Build Week

Codex Must Work is an opt-in progress monitor for one Codex task. It distinguishes a turn that is
merely marked busy from one that is producing real progress, records privacy-safe local diagnostics,
and sends optional Discord lifecycle notifications. It does not control or restart Codex, call a model
API at runtime, or send prompt, response, or tool contents to an external monitoring service.

### How we used Codex and GPT-5.6

We built and reviewed the project in Codex with GPT-5.6. Codex explored the existing plugin and hook
interfaces, implemented the Python and PowerShell runtime, and repeatedly ran the regression suite,
linting, type checks, and installation smoke tests. GPT-5.6 helped us reason through the hard failure
modes: stale-busy detection, threshold calibration, path identity across platforms, activation races,
and fail-closed behavior. At runtime the plugin integrates with Codex skills, hooks, and
privacy-filtered local rollout metadata; GPT-5.6 is the development agent, not a hidden runtime API
dependency.

### Session control capability boundary

Every `cmw.*` control call is bound to an unpredictable capability derived for the current Codex
session. Knowing another session's identifier is not enough to start, inspect, complete, or stop its
CMW task. The master key remains in the protected plugin data root, while the derived capability is
passed only through Codex-owned local session context and MCP arguments and is excluded from
user-visible output and product logs. This boundary does not protect against code running as the same
operating-system user with arbitrary access to Codex transcripts or plugin data, or against a
compromised Codex process.

## Installation

검증형 `simdorei` 설치 프로그램은 Windows x64, Linux x64, macOS ARM64를 지원합니다.
현재 `simdorei` 설치 버튼 경로는 Windows x64를 지원합니다.

### 플러그인 설치 버튼으로 설치

Windows x64에서 Codex 플러그인에 `simdorei/codex-must-work` 마켓플레이스를 추가한 뒤
**Codex Must Work → 설치**를 누르세요. 설치 파일에 포터블 Python 압축본이 포함되어
있으므로 시스템 Python이나 첫 실행 다운로드가 필요하지 않습니다. 설치 직후 또는 새
스레드에서 MCP가 처음 시작될 때 현재 설치 출처에 맞는 전용 데이터 폴더와 보안 키를 만들고
Windows 런타임을 한 번만 풉니다. 이후에는 작은 네이티브 실행기가 준비된 Python을 바로
재사용합니다.

새 스레드에서 Discord 알림 연결 제안을 수락하면 로컬 설정 페이지가 열립니다. 웹훅 주소는
그 페이지에만 붙여넣으며 Codex 대화에는 입력하지 않습니다. 연결 테스트와 저장이 끝나면
설정 페이지를 닫은 뒤 다음 명시적인 `$work-on` 요청부터 알림 설정이 적용됩니다.

플러그인 설치 또는 업데이트 뒤에는 새 스레드를 여세요. 훅 신뢰 확인이 표시되면
`UserPromptSubmit` 훅을 검토해 승인해야 명시적인 `$work-on` 요청에만 일회용
활성화 허가가 발급됩니다. 일반 프롬프트에는 CMW 전역 문맥을 주입하지 않습니다.

### 검증형 `simdorei` 설치 프로그램

저장소 체크아웃에서 `simdorei` 설치 캐시·훅 신뢰까지 한 번에 고정하려면 저장소 루트에서
운영체제에 맞는 명령을 실행하세요.

Windows PowerShell:

```powershell
.\install.ps1
```

Linux 또는 macOS:

```sh
./install.sh
```

저장소를 업데이트한 뒤 같은 설치 명령을 다시 실행하면 검증된 새 `simdorei` 버전 캐시와
신뢰 설정을 한 번에 갱신합니다.

설치 프로그램은 CMW의 단일 `UserPromptSubmit` 훅을 신뢰하도록 기록하므로 `/hooks`에서
따로 승인할 필요가 없습니다. 설치 뒤 현재 또는 새 스레드에서 정확한 `$work-on` 요청을
보내면 관찰이 시작됩니다. CMW는 Codex 작업이나 app-server를 자동 재시작하지 않습니다.

심사자는 새 스레드에서 다음 읽기 전용 확인을 실행할 수 있습니다.

```text
$work-on Objective: perform a read-only check that the current working directory exists. Success criteria: confirm it exists, make no file changes, then use the verified-completion path of $work-off and reply WORK_ON_VERIFIED.
```

The expected final reply is `WORK_ON_VERIFIED`; verified completion also disables the task supervisor.

### 지원 Codex 버전과 설치 경로

설치 프로그램은 소스 코드로 검증해 고정한 다음 네 Codex 버전만 허용합니다.

- `0.144.0-alpha.4`
- `0.144.0`
- `0.145.0-alpha.18`
- `0.146.0-alpha.3.1`

설치가 성공하면 다음 소유 경로만 사용합니다.

- 플러그인: `codex-must-work@simdorei`
- 마켓플레이스 설정: `[marketplaces.simdorei]`
- 버전 캐시: `<CODEX_HOME>/plugins/cache/simdorei/codex-must-work/0.2.0+codex.20260728073232`
- 작업 데이터: `<CODEX_HOME>/plugins/data/codex-must-work-simdorei`
- Codex 설정: `<CODEX_HOME>/config.toml`

저장소 매니페스트의 버전이 캐시 버전과 항상 같습니다. 예약 버전 `local`인 캐시 또는 설치할
버전보다 더 높은 버전의 `simdorei` 캐시가 이미 있으면, Codex가 다른 캐시를 고르는
상황을 막기 위해 설치를 명시적으로 중단합니다.

### 기존 설치 마이그레이션

설치 과정에서 과거 개발용 식별자가 발견되면 성공한 `simdorei` 전환의 마지막 단계에서
그 설정과 훅 신뢰 항목을 제거합니다. `simdorei` Git 마켓플레이스, 기존 작업 데이터와 보정
기록은 보존합니다.

### 안전하게 제거

저장소 루트의 PowerShell에서 다음 명령을 실행하면 CMW의 현재·과거 설정 등록, 훅 신뢰
항목, 검증된 설치 캐시와 포터블 런타임만 제거합니다.

```powershell
.\uninstall.ps1
```

Discord 알림 설정, 작업 기록과 보정 데이터는 기본 제거에서 보존됩니다. 이 CMW 전용 데이터
폴더까지 지우려는 경우에만 명시적인 옵션을 사용하세요.

```powershell
.\uninstall.ps1 -PurgeData
```

경로가 심볼릭 링크나 정션으로 바뀌었거나 CMW 소유권을 검증할 수 없으면 제거하지 않고 정확한
진단 코드와 함께 중단합니다. 다른 플러그인, 다른 마켓플레이스와 `.env`는 두 명령 모두
수정하지 않습니다.

### 정확한 진단 코드

설치가 실패하면 조용히 다른 방법을 시도하지 않고 다음과 같은 안전한 진단 코드를 그대로
표시합니다.

| 진단 | 의미 |
| --- | --- |
| `unsupported_codex_hook_contract: CMW must be updated for this Codex version` | 설치된 Codex 버전이 고정된 네 버전 밖입니다. |
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

- 병목 의심: `5m`
- 심각 정체: `10m`
- 동작: 로컬 이벤트 진단과 선택적인 Discord 웹훅 전송만 수행
- 자동 재시작·Codex app-server 실행: 없음

병목 시간 설정은 별도 명령으로 확인하거나 바꿉니다.

```text
$work-settings
$work-settings default
$work-settings recommended
$work-settings 7m 15m
```

`default`는 고정 `5m`/`10m`, `recommended`는 이 PC의 로컬 진행 기록으로 계산한 현재
추천값, 두 시간 인자는 사용자 지정값입니다. 저장된 선택은 다음 `$work-on`부터 적용되며,
이미 감시 중인 작업을 재시작하거나 조용히 바꾸지 않습니다.

현재 작업의 감시만 끄려면 다음을 사용합니다.

```text
$work-off
```

## 로컬 기록 기반 추천

`$work-settings recommended`를 요청하면 이 PC의 로컬 기록을 분석해 현재 추천값을
표시하고 저장합니다. 추천값은 사용자가 이 명령을 명시적으로 요청할 때만 선택되며,
사용자가 동의하기 전에는 적용하지 않습니다.

추천값을 선택하지 않으면 기존 설정이 유지됩니다. `$work-settings default`는 고정
`5m`/`10m`로 돌아갑니다.

계산 규칙은 다음과 같습니다.

- 최근 30일, 최신 세션 파일 최대 100개
- 전체 읽기 최대 64MiB, 파일당 최대 8MiB
- 유효한 실제 진행 간격이 최소 20개일 때만 추천
- 병목 의심: 진행 간격 P95를 분 단위로 올림
- 심각 정체: P99와 병목 의심 시간의 2배 중 큰 값을 분 단위로 올림
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
CMW는 이 진단 결과로 turn을 중단하거나 재시작하지 않습니다.

## Discord 웹훅 알림

Discord Remote 없이도 병목 의심, 심각 정체, 진행 회복, 정상 완료를 Discord 채널로 보낼 수 있습니다.
설치 후 첫 스레드에서 Codex가 Discord 알림 연결을 제안하면 `연결`이라고 답하세요. Codex가
`127.0.0.1`로 시작하는 5분짜리 로컬 설정 링크를 하나 보여줍니다. 그 링크를 Chrome에서 열고
Discord 채널 설정에서 복사한 웹훅 주소를 붙여넣은 뒤 `연결 테스트 및 저장`을 누르면 됩니다.
첫 제안을 넘겼어도 나중에 Codex에 `CMW Discord 알림 연결해줘`라고 요청할 수 있습니다.

웹훅 주소는 비밀번호처럼 취급합니다. 설정 페이지는 CMW의 기존 Python daemon 안에서 잠깐만
열리고, 브라우저와 이 PC 안에서만 통신합니다. 주소는 Codex 대화, MCP 도구 인자와 결과,
저장소의 `.env`, `config.toml`, `.mcp.json`에 들어가지 않습니다. 연결 테스트가 성공하면
사용자 전용 플러그인 데이터 폴더에 저장하고 임시 설정 서버를 닫습니다. daemon을 다시
시작할 필요 없이 다음 상태 변화부터 새 설정을 사용합니다.

알림에는 로컬 Codex DB에서 읽기 전용으로 조회한 스레드 제목, Codex 스레드 ID, 상태와 병목
주체만 포함됩니다.
주체는 `메인 에이전트`, `전체 작업` 또는 `Tesla (explorer)`처럼 이름과 역할이 확인된
서브에이전트로 표시합니다. 구버전 기록처럼 이름이 없으면 원본 ID 대신 같은 에이전트를
구분할 수 있는 짧은 해시만 표시합니다. 프롬프트, 답변, 도구 입출력은 전송하지 않으며
Discord 멘션도 비활성화합니다. 제목을 읽지 못하면 조용히 다른 제목을 지어내지 않고
`제목 조회 실패`라고 표시합니다.

Discord로 보내는 JSON 본문은 다음 두 필드뿐입니다.

```json
{
  "allowed_mentions": {"parse": []},
  "content": "<상태>\n스레드: <로컬 제목>\nCodex 스레드 ID: <Codex 스레드 ID>\n대상: <전체 작업·메인·서브에이전트>\n<상세 상태와 경과 초>"
}
```

채널은 웹훅 주소 자체가 정하므로 별도 채널 ID는 보내지 않습니다. `content` 안의 스레드 ID
외에 이벤트 ID, 로컬 진단 코드, 제어 권한 값과 웹훅 주소는 메시지 JSON에 넣지 않습니다.
프롬프트·답변·추론 본문·도구 입력과 출력도 보내지 않습니다. 다만 일반적인 HTTPS 통신과
마찬가지로 Discord 서버에는 송신 IP, 전송 시각, HTTP 메타데이터가 보일 수 있습니다.
전송 실패는 `discord_notification_failed` 진단으로 남기고 CMW의 로컬 감시는 계속합니다.

웹훅을 설정하지 않으면 Discord 네트워크 호출, 추가 스레드, 추가 프로세스가 전혀 생기지
않습니다. 로컬 설정 스레드도 설정 링크가 살아 있는 동안만 하나 생깁니다. 설정한 경우에도
Discord HTTP 요청은 네 상태가 실제로 바뀔 때만 실행됩니다. 감시는 항상 알림 전용이며
별도 Codex app-server를 만들지 않습니다.

## Discord Remote와 함께 사용할 때

상태 조회와 수동 미러링은 함께 사용할 수 있습니다. CMW는 Discord Remote의 명령을
가로채거나 Codex turn의 소유권을 갖지 않습니다. `$work-off`는 CMW 감시만 끄며 현재 Codex
작업을 중단하지 않습니다.

## 포터블 Python

시스템 Python이나 첫 실행 다운로드가 필요하지 않습니다. 다음 세 CPython 3.12.13 런타임을
플러그인에 압축 상태로 포함하며, 첫 실행 때 해당 운영체제의 런타임만 `PLUGIN_DATA`에 한 번
풉니다. `simdorei` 설치 버튼 경로에서는 Windows 네이티브 실행기가 준비된 Python을 바로
재사용하고, 런타임이 없는 최초 준비 때만 PowerShell을 한 번 실행합니다. 검증형 `simdorei`
설치에서는 Linux와 macOS 셸 실행기도 준비 후 Python으로 교체됩니다. 따라서 매 이벤트마다
PowerShell과 Python을 새로 실행하지 않습니다.

- Windows x64
- Linux x64
- macOS ARM64

다른 CPU·운영체제 조합은 조용히 우회하지 않고 지원하지 않는 대상으로 오류를 냅니다.
포함 파일의 출처와 SHA-256은 [`runtime/manifest.json`](runtime/manifest.json)에 있습니다.

## 설치 후 확인

검증형 `simdorei` 설치 프로그램은 선택된 캐시의 정확한 `UserPromptSubmit` 훅만
신뢰하도록 기록하므로 별도 승인 단계가 필요하지 않습니다. 플러그인
설치 버튼을 사용한 경우에는 Codex가 보여주는 훅 신뢰 확인을 한 번 승인하세요. 설치나 업데이트
뒤 정확한 `$work-on` 요청을 보내면 관찰과 선택적 Discord 알림이 적용됩니다.
`UserPromptSubmit` 훅은 원문에 정확한 `$work-on` 토큰이 있을 때만 한 번 쓸 수 있는 짧은
활성화 허가를 만들며, `Stop` 훅은 사용하지 않습니다.

## 리소스 구조

- Codex 실행당 CMW Python MCP/daemon: 1개
- 별도 Codex app-server·watcher·manager: 0개
- 도구 이벤트의 CMW PowerShell 실행: 0회
- 일반 사용자 턴의 CMW PowerShell 실행: 0회
- 새 스레드의 전역 문맥·locator 주입용 `SessionStart` 실행: 0회
- 정확한 `$work-on` 요청의 일회용 허가 발급용 `UserPromptSubmit` 실행: 요청당 1회
- Discord 설정 중 임시 loopback 서버: 기존 daemon 안의 스레드 1개, 성공 또는 5분 뒤 0개
- 진행 확인: 로컬 rollout의 새 기록만 증분 확인

Codex나 PC가 종료되면 daemon도 종료됩니다. PC에 상시 실행되는 Windows 서비스를 만들지
않습니다. Codex를 다시 실행하면 새 daemon이 저장된 감시 상태를 읽어 다시 관찰합니다.

## 개발 검증

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
```

플러그인 코드는 MIT 라이선스입니다. 포함된 CPython 배포물과 구성 요소의 라이선스는
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)를 확인하세요.
