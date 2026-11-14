# 直播助手发布规则

- 插件版本使用 `MAJOR.MINOR.PATCH`。兼容修复递增 patch，兼容功能递增 minor，不兼容变更递增 major。维护者指定跳号时，在该版本说明记录缘由。`v0.3.5 → v3.5.1` 是一次维护者指定的跳号。
- 发布前同步 `plugin.toml` 和 `pyproject.toml` 的版本，新增该版本的发布说明。发布 tag 使用 `v<版本>`，必须与包内版本一致。
- 验证、发布、Windows 原生检查和 Hosted UI 工作流使用同一个经过复核的 N.E.K.O 完整提交 SHA。更新宿主基线时同步两个共享工作流引用、宿主 checkout 和发行附件校验使用的 SHA，复跑插件测试、面板验证和发行包校验后再打 tag。
- 正式 `.neko-plugin` 安装包保留抖音桥接的来源、许可证和校验资料，排除 `douyinLive.exe`。在[抖音接入契约](../modules/douyin_live_ingest.md)查看源码版运行条件。

共享发布工作流把插件构建和发布分到不同作业。构建使用只读权限，发布作业为安装包和 `market-evidence.json` 生成构建来源证明并上传附件。随后 `verify-published-assets` 只读下载两个附件，验证证明的源提交、tag、签名工作流和宿主提交，并与证据中的安装包 SHA-256 对照。作业摘要给出已验哈希。校验失败时，已上传的附件仍可能存在，应按工作流结果处理。

## 验证证据

- Windows 原生作业每次运行凭据与桥接测试，覆盖 DPAPI 迁移、失败注销、Job Object 生命周期和端口归属。它使用同一个宿主锁文件，预算 20 分钟，保留 JUnit 结果。共享 Ubuntu 作业负责全量回归和打包。
- 本地可以用宿主 CLI 对包做哈希和独立目录安装检查，再用该副本运行 Hosted UI。正式验收另外使用附有构建来源证明的同一包。完整步骤和记录格式见 [Windows 实播验收](../solo-stream-test-guide.md#windows-实播验收)。
