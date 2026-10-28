#!/usr/bin/env bash
# ================================================================
# 从 ONOS Docker 镜像提取完整 Karaf system 目录作为本地 Maven 仓库
# ================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ONOS_REPO="$SCRIPT_DIR/onos-maven-repo"

echo "=== 从 ONOS Docker 镜像提取 Maven 依赖 ==="

docker pull onosproject/onos:2.7.0 >/dev/null 2>&1

CID=$(docker create onosproject/onos:2.7.0)
rm -rf "$ONOS_REPO"

# 提取整个 Karaf system 目录 (= Maven 仓库布局)
docker cp "$CID:/root/onos/apache-karaf-4.2.9/system/." "$ONOS_REPO" 2>/dev/null || {
    echo "ERROR: 提取失败"
    docker rm "$CID" >/dev/null 2>&1
    exit 1
}
docker rm "$CID" >/dev/null 2>&1

# 验证关键 JAR
for jar in onos-api onos-incubator-api onos-p4runtime-api onos-drivers-bmv2; do
    found=$(find "$ONOS_REPO" -name "${jar}-2.7.0*.jar" -type f 2>/dev/null)
    if [ -z "$found" ]; then
        echo "WARNING: 未找到 $jar"
    else
        echo "OK: $jar"
    fi
done

echo ""
echo "=== 完成 ==="
echo "Maven 本地仓库: $ONOS_REPO"
echo "现在可以运行: bash build.sh -U"
