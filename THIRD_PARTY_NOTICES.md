# 第三方依赖与发布授权

项目自有源码采用 Apache-2.0，见 LICENSE。第三方代码、依赖、镜像及工具保留各自许可证；项目许可证不替代它们的许可要求。下表记录核查范围，不代表自动扫描能够确认代码归属。

发布前需确认：所有贡献者同意发布；自有代码归属清楚；依赖及基础镜像允许按计划使用和分发；保留必要的版权、许可证和 NOTICE；移除无发布授权的附件。

| 组成 | 版本与来源依据 | 核查内容 |
|---|---|---|
| Python 后端及测试工具 | `idn-system/requirements.txt` | 直接与传递依赖的许可、版权和分发要求 |
| Vue、Pinia、Vue Router、D3、ECharts、Vite | `frontend/package.json`、`package-lock.json` | 生产 bundle 与开发工具的许可文件 |
| ONOS API、BMv2 驱动、OSGi、Maven 插件 | `onos-pipeconf/pom.xml`、ONOS 镜像中的 Maven 仓库 | Java 依赖与打包产物中的许可和 NOTICE |
| ONOS、Mininet/Stratum、P4 编译器、Python、Node 镜像 | Dockerfile、Compose、`p4/compile.sh` | 镜像内组件、标签或摘要及再分发要求 |
| 图标、文案、提示词、拓扑与示例资料 | 仓库内对应资源 | 来源、贡献者授权及是否包含真实数据 |

生成已安装 Python/Node 依赖的元数据清单：

```bash
python scripts/dependency_inventory.py > .release-audit/dependencies.json
```

元数据可能缺失或不完整，需要结合包内许可证核查。竞赛交付材料（`docs/delivery/`）、本地验收产物（`artifacts/`）和竞赛专用脚本（`tools/`）由 `scripts/export_release.py` 排除，不随公开快照发布；原件保留在私有仓库。
