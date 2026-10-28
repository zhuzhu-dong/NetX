#!/usr/bin/env bash
# ================================================================
# 一站式构建: 编译 pipeconf + 复制 OAR 到 Docker 部署目录
# 用法: cd onos-pipeconf && bash build.sh
# ================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

OAR_SRC="$SCRIPT_DIR/target/onos-pipelines-idn-basic-1.0.0-SNAPSHOT.oar"
OAR_DST="$PROJECT_ROOT/docker/onos/karaf-deploy/idn-pipeconf.oar"
OAR_APP_DST="$PROJECT_ROOT/docker/onos/apps/org.onosproject.pipelines.idn-basic/idn-pipeconf.oar"
JAR_SRC="$SCRIPT_DIR/target/onos-pipelines-idn-basic-1.0.0-SNAPSHOT.jar"
JAR_DST="$PROJECT_ROOT/docker/onos/karaf-deploy/onos-pipelines-idn-basic-1.0.0-SNAPSHOT.jar"

echo "=== 编译 pipeconf ==="
cd "$SCRIPT_DIR"
mvn clean package

echo ""
echo "=== 复制 OAR ==="
cp "$OAR_SRC" "$OAR_DST"
cp "$OAR_SRC" "$OAR_APP_DST"
cp "$JAR_SRC" "$JAR_DST"
echo "  $OAR_SRC"
echo "  → $OAR_DST"
echo "  → $OAR_APP_DST"
echo "  $JAR_SRC"
echo "  → $JAR_DST"
echo ""
echo "=== 完成 ==="
