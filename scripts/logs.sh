#!/bin/bash
# View BLS Monitor logs on VPS
# Usage: bash logs.sh [lines]
cd /opt/bls-monitor
docker compose logs -f --tail=${1:-100}
