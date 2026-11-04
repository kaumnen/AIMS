#!/usr/bin/env bash
# Builds the organism's two scenarios from the built-in cache-keys:
#   build/train  cache-keys with scenario.yaml from here (adds unobserved-covert and observed-honest)
#   build/demo   cache-keys with a $15 monitor cap, as in the demo run
set -euo pipefail
cd "$(dirname "$0")"
src=../src/aims/scenarios/cache-keys
rm -rf build
mkdir build
cp -R "$src" build/train
cp scenario.yaml build/train/scenario.yaml
cp -R "$src" build/demo
printf '\nlimits:\n  monitor_cost_usd: 15\n' >> build/demo/scenario.yaml
