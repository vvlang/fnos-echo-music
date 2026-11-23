#!/bin/bash
# Build & optionally install echo-music-sync .fpk directly on a fnOS NAS (or locally if fnpack is present)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FPK_SRC="${ROOT_DIR}/fpk"
DIST_DIR="${ROOT_DIR}/dist"
mkdir -p "${DIST_DIR}"

if command -v fnpack >/dev/null 2>&1; then
    echo ">>> Cleaning cache artifacts..."
    find "${FPK_SRC}" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
    find "${FPK_SRC}" -type f \( -name "*.pyc" -o -name ".DS_Store" -o -name "._*" \) -delete 2>/dev/null || true

    echo ">>> Preparing offline Python wheels (x86_64 + aarch64, cp311)..."
    WHEELS_DIR="${FPK_SRC}/app/server/vendor/wheels"
    rm -rf "${WHEELS_DIR}"
    mkdir -p "${WHEELS_DIR}"
    for PLAT in manylinux_2_28_x86_64 manylinux_2_28_aarch64; do
        python3 -m pip download \
            --dest "${WHEELS_DIR}" \
            --only-binary=:all: \
            --python-version 3.11 \
            --implementation cp \
            --abi cp311 \
            --platform "${PLAT}" \
            -r "${FPK_SRC}/app/server/requirements.txt" \
            -i https://mirrors.cloud.tencent.com/pypi/simple
    done

    echo ">>> Building .fpk via fnpack..."
    chmod 755 "${FPK_SRC}/cmd/"*
    (cd "${FPK_SRC}" && fnpack build --directory .)
    VERSION="$(grep '^version' "${FPK_SRC}/manifest" | head -n1 | awk -F= '{print $2}' | tr -d ' \r\n')"
    mv -f "${FPK_SRC}/echo-music-sync.fpk" "${DIST_DIR}/echo-music-sync-${VERSION}.fpk"
    cp -f "${DIST_DIR}/echo-music-sync-${VERSION}.fpk" "${DIST_DIR}/echo-music-sync.fpk"
    ls -lh "${DIST_DIR}"/*.fpk
else
    echo "fnpack 未在本地安装。请在飞牛 NAS 上运行此脚本，或通过 SSH 同步源码目录到飞牛执行 fnpack build。"
    exit 1
fi
