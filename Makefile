# Thin wrapper; on Windows without make use: uv run python tasks.py <target> [KEY=VALUE ...]
PY := uv run python tasks.py

.PHONY: test lint fmt rules instruments upstox-login upstox-probe fyers-login record shadow-live qc backtest paper mine-log self-review report event-arb evening fundamentals swing

test:        ; $(PY) test
lint:        ; $(PY) lint
fmt:         ; $(PY) fmt
rules:       ; $(PY) rules
instruments: ; $(PY) instruments
upstox-login: ; $(PY) upstox-login
upstox-probe: ; $(PY) upstox-probe D30=$(D30)
fyers-login: ; $(PY) fyers-login
record:      ; $(PY) record SIM=$(SIM) MINUTES=$(MINUTES)
shadow-live: ; $(PY) shadow-live SIM=$(SIM) MINUTES=$(MINUTES) SEND=$(SEND)
qc:          ; $(PY) qc DATE=$(DATE) SIM=$(SIM) LAST=$(LAST)
backtest:    ; $(PY) backtest STRAT=$(STRAT) FROM=$(FROM) TO=$(TO)
paper:       ; $(PY) paper STRAT=$(STRAT)
mine-log:    ; uv run --extra mining python tasks.py mine-log FILE="$(FILE)" LABEL=$(LABEL)
self-review: ; $(PY) self-review
report:      ; $(PY) report
event-arb:   ; $(PY) event-arb DATE=$(DATE) SEND=$(SEND)
evening:     ; $(PY) evening DATE=$(DATE)
fundamentals: ; $(PY) fundamentals SYMBOLS=$(SYMBOLS)
swing:       ; $(PY) swing DATE=$(DATE)
