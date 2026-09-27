"""B 站 HTTP / WebSocket 请求共用的浏览器 User-Agent。

2026-04 起 getDanmuInfo 会对过旧的 Chrome 版本 UA 返回 -352（xfgryujk/blivedm#84），
所以所有请求统一从这里取 UA，升级版本号时只改这一处。
"""

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
