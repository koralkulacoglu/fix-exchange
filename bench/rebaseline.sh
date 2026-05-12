#!/bin/bash
# Rerun benchmarks for existing release tags using the current bench methodology.
# Run this once on the AWS machine after setting it up, before the first automated run.
#
# Usage:
#   bash bench/rebaseline.sh                        # all default tags
#   bash bench/rebaseline.sh v1.10.0 v1.11.1        # specific tags
set -e

TAGS="${*:-v1.10.0 v1.11.1 v1.11.2 v1.11.3}"

. .venv/bin/activate

for tag in $TAGS; do
    echo "=== Rebaselining $tag ==="
    git checkout "$tag" -- src/ CMakeLists.txt config/
    cmake -B build -DCMAKE_BUILD_TYPE=Release
    cmake --build build -j$(nproc)
    python3 bench/bench.py --save --version-override "$tag"
    git restore --staged src/ CMakeLists.txt config/
    git restore src/ CMakeLists.txt config/
    echo "--- $tag done ---"
done

echo ""
echo "Done. Commit bench/results.db, then regenerate charts:"
echo "  python3 bench/plot_history.py"
