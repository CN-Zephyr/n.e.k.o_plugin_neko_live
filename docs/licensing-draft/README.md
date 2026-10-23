# 直播插件许可迁移提案

> 状态：Proposal / 草案，尚未生效。整理日期：2026-10-23。本目录不为历史版本补发或撤回授权，不因在本地出现而改变当前版本的权利。中文提案暂无其他语言镜像；本轮不改 UI 或 i18n key。

## 拟定方案

本提案仅为直播助手插件 neko_live 起草“N.E.K.O. 直播助手社区源码许可证 1.0”，采用源码可见、个人及普通主播免费、其他项目复用插件受保护代码及商业使用插件另行授权的安排。N.E.K.O. 主项目保持其现行许可证，本提案不要求主项目变更或合入任何许可提案。

团队已确认插件原创和迁入的旧第一方代码权利也属于团队。本提案按该前提设计，不因旧模块迁移本身否定团队另行授权能力；已经授出的历史权利和第三方许可仍须保留。

## 文档职责

| 文件 | 唯一职责 |
| --- | --- |
| [LICENSE.draft.md](LICENSE.draft.md) | 拟定许可正文及权利边界 |
| [USAGE.draft.md](USAGE.draft.md) | 主播、开发者、其他项目及服务商场景说明 |
| [COMMERCIAL-AUTHORIZATION.draft.md](COMMERCIAL-AUTHORIZATION.draft.md) | 商业或跨项目单独授权模板 |
| [CONTRIBUTOR-AUTHORIZATION.draft.md](CONTRIBUTOR-AUTHORIZATION.draft.md) | 未来贡献及另行商业授权所需的入站授权模板 |
| [REVIEW-2026-10-23.md](REVIEW-2026-10-23.md) | 本轮边界复核、修正及场景判断记录；不替代正文或法律审阅 |
| [NOTICE.draft.md](NOTICE.draft.md) | 插件版权、来源及第三方声明候选 |
| 本文 | 插件适用范围登记、迁移步骤、启用前决定和核验记录 |

本目录仅维护直播插件的许可、授权和复核文档，各说明以插件许可证正文为准。主项目的 SDK、接口及历史来源代码仍按其有效许可处理；不与主项目的许可文档建立同步、合并或迁移依赖。

## 已核对的现状（2026-10-23 快照）

- 插件上游为 CN-Zephyr/n.e.k.o_plugin_neko_live，受检 main 提交为 18e3ed416。
- 上游受检树没有插件自己的根目录 LICENSE/NOTICE。对已发布 v0.3.1 的 395 个归档条目检查发现有 douyinLive 的 MIT 文件，没有插件自己的根 LICENSE/NOTICE。此结论只覆盖所检树及该发行包，不代替所有版本和渠道审计。
- 此前本地准备过尚未提交或发布的 Apache LICENSE/NOTICE 及 README 声明。这些本地提案不是已发布许可证；本轮将原文备份到仓库之外的本次恢复目录，并移除 README 中把它写成现行许可的本地新增表述。
- docs/development.md 记录从主项目旧 bilibili_danmaku 迁入连接解析和扫码登录能力。历史源代码核对亦支持该来源关系；应登记模块和来源，而不是因独立仓库没有根许可证就认定全部代码从未被许可。
- vendor/douyin_bridge/windows-amd64/douyinLive.exe 带有独立 MIT 许可证及版本、校验信息，保持原许可。
- 仓库存在其他贡献者的提交。新贡献协议须明确接收主体及商业再授权，不能仅靠更新文档替历史贡献者接受协议。

公开仓库没有根许可证不等于可以任意复制，也不等于其中所有代码都“从未授权”；已有声明、来源许可、有效独立协议和平台既有权利应分别判断。

## 适用范围登记（定稿前填写）

| 范围 | 拟定处理 | 生效所需记录 |
| --- | --- | --- |
| 有权另行授权的插件第一方逻辑、面板、文档和相应修改 | 纳入新许可的候选 | 起始版本、精确提交、文件范围和权利来源 |
| 从旧 B 站插件迁入的连接解析及登录逻辑 | 按团队确认的第一方权利纳入候选 | modules/bili_live_ingest/、adapters/bili_auth_service.py 与来源提交的映射，保留既有权利 |
| 从主项目取得的 SDK、适配代码和必要样例 | 逐项区分原许可与生态例外 | 不能把接口兼容等同于许可整个主项目源码 |
| douyinLive 及其他第三方依赖或素材 | 排除于本项目独占许可范围 | 原许可、版本、来源、校验和及随包声明 |
| 以前已依有效许可授出的代码 | 保留原有权利 | 具体版本、发行包和来源记录 |
| 观众档案、主播内容、用户输入及输出 | 不自动纳入代码许可 | 继续按隐私和相应权利处理 |

## 待生效记录

- 许可法律主体、有权签署人和商业授权联系人：待团队填写。
- 插件首次采用新许可的版本、精确 commit 和发布时间：待团队填写。
- 第一方、历史来源和第三方文件范围：待完成。
- SDK/样例开发者例外及原版分享范围：待完成。
- 新贡献协议接收主体、接受流程和启用日期：待填写。
- 准据法、争议解决、违约、专利及配套授权条款审阅：待完成。

v0.3.1 是现状核验对象，不是已经指定的新许可生效版本。不得直接将版本号或日期填入草案就宣称完成切换。

## 迁移顺序与回滚边界

1. 团队确认上述范围和主体，审阅许可证、普通主播例外、开发者例外、商业及贡献授权。
2. 固定起始版本和提交，保留旧许可及第三方清单；从旧主项目迁入的代码应有来源记录和与旧版权利的明确关系。
3. 在正式迁移变更中设置根 LICENSE/NOTICE，并更新 README、贡献说明、确实适用的新文件标头和发行说明。本轮不执行该切换。
4. 在插件打包工具支持的元数据字段中写明实际许可；不为添加许可而猜造 plugin.toml 字段。文档、8 locale 中若有正式用户可见许可文案，再按项目规范同步；本轮未新增此类 key。
5. 验证 .neko-plugin、GitHub Release 与 Market 实际包包含正式许可、适用范围及 douyinLive 原始 MIT 声明，避免“源码有、安装包没有”。
6. 保持官方安装、普通主播使用和正常插件适配可行；不为许可草案增加联网验权、遥测、收入检查或远程停播，也不在本轮发布包或切换授权。

定稿前可移除草案及入口回退提案；本轮撤下的未发布 Apache 提案有本机恢复副本。正式授出的许可和签署协议不能靠删除文件追溯回滚。

## 本轮核验范围

只形成草案和文档入口，不修改 pipeline、安全门、dispatcher、运行时配置、面板或数据路径。应检查文档链接、草案状态、共享文本一致性及原有改动保留；文档检查不证明协议法律效力，也不代替完整权利链和历史发行渠道审计。

## 参考依据

- [插件上游](https://github.com/CN-Zephyr/n.e.k.o_plugin_neko_live)
- [受检 v0.3.1 发行包所在 Release](https://github.com/CN-Zephyr/n.e.k.o_plugin_neko_live/releases/tag/v0.3.1)
- [插件开发文档及旧模块来源说明](https://github.com/CN-Zephyr/n.e.k.o_plugin_neko_live/blob/main/docs/development.md)
- [douyinLive 随包 MIT 原文](https://github.com/CN-Zephyr/n.e.k.o_plugin_neko_live/blob/main/vendor/douyin_bridge/windows-amd64/LICENSE.douyinLive)
- [主项目历史许可迁移 PR #1723](https://github.com/Project-N-E-K-O/N.E.K.O/pull/1723)
- [Apache-2.0：既有权利和再分发](https://www.apache.org/licenses/LICENSE-2.0)
- [GitHub 条款 D.5、D.6](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service)
- [WIPO：软件版权保护范围](https://www.wipo.int/en/web/copyright/activities/software)
- [OSI：开源定义](https://opensource.org/osd)

草案是项目自拟条款，不冒充 Apache、PolyForm 或 OSI 批准的标准许可证；参考文献不构成法律顾问背书。
