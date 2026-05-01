#!/usr/bin/env bash
set -euo pipefail

sudo apt-get install -y cmake libssl-dev build-essential

git clone --depth=1 https://github.com/quickfix/quickfix.git /tmp/quickfix
cmake -B /tmp/quickfix/build -S /tmp/quickfix -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/quickfix/build -j$(nproc)
sudo cmake --install /tmp/quickfix/build

cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j$(nproc)
