# 直播插件许可说明

直播助手插件 neko_live 自 v0.3.2 起采用[《N.E.K.O. 直播助手社区源码许可证 1.0》](../../LICENSE)：源码可见，个人自用和普通主播直播免费；把代码搬到其他项目、独立发行或做商业产品与服务，须事先取得书面授权。

许可方是插件作者 上九天揽星辰（GitHub: CN-Zephyr）。授权申请邮箱 3920517962@qq.com，也可以在仓库 [Issues](https://github.com/CN-Zephyr/n.e.k.o_plugin_neko_live/issues) 联系。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| [LICENSE](../../LICENSE) | 许可证正文，唯一权威文本 |
| [NOTICE](../../NOTICE) | 版权、历史版本、第三方组件和参考来源声明 |
| [USAGE.md](USAGE.md) | 主播、开发者、其他项目及服务商的常见场景说明 |
| [COMMERCIAL-AUTHORIZATION.md](COMMERCIAL-AUTHORIZATION.md) | 商业或跨项目书面授权的模板 |
| [CONTRIBUTOR-AUTHORIZATION.md](CONTRIBUTOR-AUTHORIZATION.md) | 向本项目提交贡献的授权条款 |

说明文档与 LICENSE 不一致时，以 LICENSE 为准。

## 适用范围

| 内容 | 处理 |
| --- | --- |
| 插件仓库中的代码、面板、文档、数据、测试与工具 | 适用本许可证 |
| `vendor/douyin_bridge/windows-amd64/` | douyinLive，保留原 MIT 许可 |
| `modules/bili_live_ingest/livedanmaku.py` | 移植自 MagicalDanmaku（GPL-3.0），不适用本许可证，计划以独立实现替换 |
| N.E.K.O. 主程序、SDK、UI kit | 按主项目自身许可证处理，不受本许可证影响 |
| Python 运行时依赖（宿主提供，不随包分发） | 按各自许可证处理 |
| 观众数据、主播内容、用户输入及生成输出 | 不适用代码许可 |

## 历史版本

插件在 2026-08-13 独立建仓前，作为 N.E.K.O. 主项目的一部分按 MIT（2026-06-15 前）和 Apache-2.0 发布；独立仓库 v0.3.1 及之前的版本没有附带插件自己的许可证文件。依据这些历史版本已合法取得的权利不受影响，新许可证约束 v0.3.2 及之后的版本。

## 发行要求

`.neko-plugin` 发行包、GitHub Release 和插件市场包都必须包含根目录的 `LICENSE`、`NOTICE` 和 `vendor/douyin_bridge/windows-amd64/LICENSE.douyinLive`。本许可证不引入联网验权、遥测、收入检查或远程停用。
