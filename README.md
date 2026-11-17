# 투자에이전트 워크벤치

사용자가 공부한 자료와 투자 생각을 로컬 DB에 쌓고, Claude Code의 다음 세션이 그 맥락을 이어받는 투자 작업공간이다. 자료·발언·분석·계획을 연결하고, 가격·재무·차트 작도·감시 결과를 함께 읽는다. 종목 선택과 매매 판단은 사람이 한다.

이 공개본은 코드 구조, 모듈 설계, AI 에이전트 협업 방식과 시험 경계를 보여 주는 쇼케이스다. 개인 DB·인증 정보·운영 설정·매매 기록은 포함하지 않는다. 설계 문서는 작성 시점의 계획과 계약을 보존한 자료이며, 문서에 적힌 기능·시험·운영 전환 전체가 완료되었다는 뜻은 아니다.

## 구조

| 경로 | 역할 |
|---|---|
| `src/invest_agent/` | Python 패키지와 CLI 진입점 |
| `scripts/knowledge/`, `scripts/analysis/` | 지식 저장·세션 맥락, 분석·snapshot·논의·계획 |
| `scripts/market/`, `scripts/financials/` | 가격 저장·품질 검사와 재무 출처 어댑터 |
| `scripts/drawings/`, `scripts/scenarios/`, `scripts/alerts/` | 작도·감시 조건·알림 상태 처리 |
| `scripts/accounts/`, `scripts/launch/`, `scripts/web/` | 계좌 관측 경계, 실행 수명, 로컬 HTTP 연결 |
| `dashboard/` | 차트·작도·재무·분석 등의 화면 코드 |
| `tests/` | 합성 fixture, 모듈별 검사 목록, 격리 실행기·가드 |
| `docs/design/` | 전체 설계와 M00~M13 설명서·작업 지시서 |

세션 MCP와 대시보드는 서비스 함수를 공유한다. DB별 쓰기 소유권, 가격 읽기, 작도 해석, MCP·HTTP 변경 담당을 계약으로 나누어 병렬 구현의 충돌을 줄인다. 과거 이력에는 현재 개편에서 제거하거나 보류한 기능도 남아 있다.

## AI 에이전트와 작업하는 방식

Claude Code가 전체 맥락과 통합을 감독하고 Codex가 승인된 모듈을 구현한다. 모듈마다 **설명서 → 작업 지시서 → 구현 → 검수** 순서로 진행하며, 작업 지시서에 수정 범위·금지 대상·입출력 계약·완료 기준을 적는다.

서로 다른 파일을 맡는 모듈은 별도 브랜치·worktree에서 병렬로 구현한다. 공유 인터페이스의 담당자를 정하고 검토를 통과한 작업을 통합한다. 코드 검사와 실제 자료를 사용하는 운영 전환은 별도 단계다.

## 통제 설계

- 증권사 MCP는 조회 전용으로 사용한다. `MERITZ_READ_ONLY=1`, `NHPLUG_ENABLE_TRADING=false`를 유지하며 주문·환전을 에이전트 작업 범위에 넣지 않는다. 실제 MCP 연결·인증 설정은 이 공개본에 없다.
- 키와 인증 정보는 Windows DPAPI 또는 환경변수 경로로 다룬다. 코드·문서·시험 fixture에 실제 자격 증명을 넣지 않는다.
- M00 시험 실행기는 등록된 검사만 선택하고, 임시 공간의 소스 복사본과 합성 설정·DB를 사용한다. 시험 가드는 운영 자료·인증·외부 네트워크 접근을 제한하고 허용한 loopback fixture와 보호 장치를 상속한 자식 프로세스를 별도로 다룬다. 차단·건너뜀·미실행을 통과로 집계하지 않는다.
- 공개 이력은 명시적 허용 목록으로 추출하고 개인정보를 치환한다. 전체 결과 이력에 gitleaks·trufflehog와 경로·이메일·계좌 패턴 검사를 적용한다. README와 설계 문서는 별도 publish 커밋으로 덧붙인다.

## 시험 재현

개발 설치 명세는 Windows의 독립 Python **3.12.10 x64**, 고정 wheel과 해시를 전제로 한다. Node 검사는 `C:/Program Files/nodejs/node.exe`를 사용한다. 설치기는 기존 환경을 덮어쓰지 않고 OS 임시 폴더의 새 환경만 허용한다.

원래 실행 순서는 다음과 같다. `$python`은 명세와 일치하는 독립 Python 실행 파일, `$wheelhouse`는 잠금파일에 고정된 wheel을 미리 준비한 로컬 폴더다. 설치 단계 자체는 `--no-index --require-hashes`로 실행되어 패키지를 네트워크에서 받지 않는다.

```powershell
$testRoot = Join-Path $env:TEMP ('invest-showcase-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
$target = Join-Path $testRoot 'env'
& $python -I -X utf8 scripts/setup_dev_environment.py --wheelhouse $wheelhouse --target $target
if ($LASTEXITCODE -eq 0) {
    & (Join-Path $target 'Scripts/python.exe') -I -B -X utf8 tests/run_isolated.py --module M00
}
```

**현재 공개본의 재현 한계:** `dev-environment.json`의 개인 실행 경로를 중립 경로로 치환했기 때문에 원래 설치기의 정확한 경로 비교에서 `independent_python_mismatch`가 발생할 수 있다. 경로 검사를 통과하더라도 격리 실행기는 공개 대상에서 제외한 루트 실행 파일 `dashboard.ps1`, `daily.ps1`, `scheduled-market.ps1`, `register-morning-task.ps1`를 정적 fixture로 읽는다. 따라서 공개 체크아웃만으로 M00 전체 통과를 보장하지 않는다. 검증 보고에서는 설치·설정 실패와 실제 실행된 시험 결과를 구분한다. 공개용 환경 이식과 fixture 분리는 별도 작업이다.

설계 문서의 개인 요구사항·결정 기록 등 일부 상대 링크는 공개 범위 밖 문서를 가리킨다. 해당 원문은 이 저장소에 포함하지 않았다.

## 출처

이 저장소는 개인 투자 작업공간에서 공개 가능한 코드·설계만 추려 옮긴 것이다. 커밋 날짜는 원래 작업일이며, 개인 자료와 인증 정보는 이력에서 제외했다.

## English summary

A local investment research workspace that carries study notes, analysis and user decisions across Claude Code sessions. Trading decisions remain with the user.

Claude Code supervises integration; Codex implements bounded modules using explicit contracts and isolated tests. This showcase contains selected code and design artifacts, with private data and credentials excluded.

The public snapshot documents its reproducibility limits: redacted local interpreter paths and excluded operational launch fixtures require separate portability work before the complete M00 suite can run unchanged.
