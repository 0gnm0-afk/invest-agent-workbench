# 하락 초입 장기 재검증

기존 하락 신호63개, 과거 고정 후속6개, 추가12개를 한국·미국 각각 평가한다.
공통15년은2010-10-01~2025-09-30이며 짧은 종목도 최초 평가 가능일부터 포함한다.
모든 보유 이력은 보조 결과다. 운영 데이터·다른 상승 연구는 읽기만 한다.

입력 정본은 `runtime/downtrend-longhistory-20261029/inventory.json`의 `cache_path`이다.
기존7,426개와 연구용 추가57개를 모두 시도한7483개 목록이며, 가격/L1캐시는
기존 복원 자료와 `uptrend-longhistory-20261029/supplemental`을 원위치에서 재사용한다.
원시 가격 이력을 복제하거나 새로 수집하지 않는다. 이전 계산의 수정가격·결측 처리를 유지한다.

재현 환경은 인접 엔진 `.venv/Scripts/python.exe`(numpy/pandas/matplotlib)다.
`prepare.py`는 최초 파생 기록이며 일반 재현 때 실행하지 않는다. 커밋된 runner를 사용한다.
`coverage_audit.py` → `verify_features.py` → `study.py evaluate --workers 3` →
`study.py aggregate-evaluate` → `paths.py --workers 2` →
`infer.py classification` / `infer.py paths` 순서다.
최종 검증·보고서 스크립트와 영수증을 완료 시 함께 제공한다.

L1은 사후 평가표다. 신호·가격경로 대조군에 넣지 않는다. 이미 보았던 과거의 범위 확대이며
독립 새 검증이나 실행 가능한 매도 전략 수익률로 해석하지 않는다.
월별 분류 캐시에는 중복 가격경로 통계열을 저장하지 않고 연도별 통계를 미리 합산하여
같은 충분통계와 float64 정밀도를 유지하면서 저장 공간을 줄인다.
