#!/usr/bin/env python3
"""Pre-deploy validation: imports, config, disk space."""
import sys, os
sys.path.insert(0, '/root/polymarket_toolkit')
os.chdir('/root/polymarket_toolkit')

errors = []

# 1. Import check
modules = [
    'bot', 'weather', 'config',
    'monitoring.quant_metrics', 'monitoring.hrp', 'monitoring.kyle_lambda',
    'utils.risk_manager', 'utils.kalman_forecast',
    'execution.execution_agent',
    'strategies.abandoned_position', 'strategies.cross_platform_arb',
]
for mod in modules:
    try:
        __import__(mod)
    except Exception as e:
        errors.append(f"IMPORT FAIL: {mod} — {e}")

# 2. Config check
try:
    from config import Config
    c = Config()
    alloc = c.weather_pct + c.sniper_pct + c.bond_pct + c.event_pct + c.whale_pct
    if abs(alloc - 1.0) > 0.01:
        errors.append(f"ALLOC ERROR: {alloc*100:.0f}% (deve essere 100%)")
    print(f"Capital: ${c.total_capital} | Max bet: ${c.max_bet_size} | Alloc: {alloc*100:.0f}%")
except Exception as e:
    errors.append(f"CONFIG FAIL: {e}")

# 3. Disk space
import shutil
usage = shutil.disk_usage('/root')
free_gb = usage.free / (1024**3)
if free_gb < 1.0:
    errors.append(f"DISK LOW: {free_gb:.1f}GB free")
print(f"Disk: {free_gb:.1f}GB free")

# Result
if errors:
    print("\n*** VALIDATION FAILED ***")
    for e in errors:
        print(f"  - {e}")
    sys.exit(1)
else:
    print("\nOK: all checks passed")
    sys.exit(0)
