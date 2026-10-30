#!/bin/bash
# Build the bundle that gets copied to the appliance:
#   ./deploy/package.sh  ->  dist/discovery-appliance.tar.gz
# Then:  scp dist/discovery-appliance.tar.gz root@<APPLIANCE_IP>:/root/
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
tar czf dist/discovery-appliance.tar.gz \
    --transform 's,^,discovery-appliance/,' \
    --exclude='__pycache__' --exclude='*.pyc' \
    requirements.txt web engine collector scripts deploy
echo "Built dist/discovery-appliance.tar.gz"
