#!/bin/bash
# nova_brain_watchdog.sh — NOVA Brain 헬스 감시
# no_agent=True: 임계값 초과 시만 stdout 출력 → 헤르에게 알림
# 정상 시: 아무것도 출력 안 함 (silent watchdog)
python3 ${HERMES_HOME:-$HOME/.hermes}/bin/nova_brain_cli.py watchdog 2>/dev/null
