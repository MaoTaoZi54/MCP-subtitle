# 中文字幕备选源调研报告

- 调研日期：2026-11-08
- 调研目标：在 SubHD 之外，寻找能稳定、批量获取**简体中文字幕**的补充源，解决 OpenSubtitles 免费额度（5 次/天）不足以补整季的问题
- 调研方式：全部探测在 NAS（飞牛 fnOS，SSH `192.168.11.144`）上执行，出口 IP `112.42.48.26`（辽宁大连移动），与用户家庭网络一致
- 约束遵守：未修改插件业务源码、未修改配置/权限/媒体文件；本报告为唯一产物

---

## 1. 结论摘要

1. **assrt.net（伪射手）是唯一“中文优先 + 已有适配器”的可行补充源**。本次实测发现其域名被 SNI 级链路阻断（TLS 握手直接 RST），但**去掉 SNI 后 HTTPS 可正常返回 API JSON**（`{"errmsg":"invalid token","status":20001}`），说明站点本身可用。补齐免费 token 后可接入；接入需自定义 TLS 连接或 NAS 侧代理。
2. **SubDL 是最适合“批量”的国外 API 源**。免费 key 为 2,000 次搜索 + 50 次下载/天（UTC 0 点重置），官方称简体中文属于其最大语种之一；API 域名可达且错误结构清晰（无 key 返回 `403 not_authorized`）。需要用户注册免费 key。
3. **OpenSubtitles 不必替换，但应“账号化”**：匿名 5 次/天，免费账号约 20 次/天，现有适配器已支持用户名密码，改配置即可，零开发。
4. **SubSource、TVsubtitles、subf2m 实测中文覆盖接近于零，不建议作为中文源**。SubSource 三个样本共 4,326 条字幕，中文 0 条。
5. **zimuku 系（字幕库）中文覆盖强，但已无法程序化访问**：`zimuku.org` 为云锁验证码，`zimuku.net` 为 JS 挑战后 401；不建议为它投入浏览器自动化/打码。
6. **subom 已死、podnapisi 已无 A 记录**，直接从候选清单删除。
7. **SubHD 的 4 个域名（subhd.tv / subhd.me / subhdtw.com / subhd.com）均可访问且返回同样的 60 条结果**，可作为域名级 failover，但不能解决下载人机校验或配额问题。

**推荐组合（免费路线）**：`SubHD（修复后主源）+ assrt（中文主源）+ SubDL（批量 50/天）+ OpenSubtitles 免费账号（20/天）`。

---

## 2. 实测环境与方法

| 项目 | 内容 |
|---|---|
| 执行位置 | NAS fnOS，SSH 登录后在 `/tmp` 执行 Python 3.11 / curl 8 |
| 出口 IP | `112.42.48.26`（大连移动），与用户本机一致 |
| HTTP 客户端 | 系统 `requests`；Cloudflare 站点使用 `/vol1/1003/nas-app-test/.venv` 下的 `curl_cffi`（Chrome TLS 指纹） |
| 测试样本 | 新美剧：The Last of Us、Severance；国产剧：庆余年、繁花、Joy of Life；老片：The Shawshank Redemption / 肖申克的救赎 |
| 判定口径 | 站点可达性、搜索页/API 返回结构、中文条目计数、反爬形态、配额与账号要求、下载链路可程序化程度 |
| 未做 | 需要 token/key 的站点未做真实搜索与下载（见第 8 节补测清单），避免使用他人凭据或消耗用户额度 |

---

## 3. 对比表

> “中文覆盖”列中，**实测**表示本次 NAS 上真实请求得到的计数；**官方口径**表示站点自述；**站点定位**表示按其内容形态判断，未量化。

| 源 | 类型 | 中文覆盖（新美剧 / 国产剧 / 老片） | 接入方式 | 反爬形态 | 配额 / 限速 | 能否复用现有适配器 | 接入成本 | 结论 |
|---|---|---|---|---|---|---|---|---|
| **SubHD**（基准） | 中文站 | 强 / 强 / 强；庆余年实测 60 条结果 | 网页搜索 + 下载 | 下载接口人机校验（另一任务处理中） | 未见明确配额 | 已有 `subhd.py` | — | 保留主源；4 个镜像可做 failover |
| **assrt.net** | 中文站 | 站点定位中文/简繁，三类均覆盖；本次**未量化**（缺 token） | REST API `api.assrt.net/v1/sub/search` | 域名 SNI 阻断；**no-SNI 可绕过**；API 本身无验证码 | 未见公开日限；token 制 | **已有 `shooter.py`** | 低（0.5–1 人日） | **推荐 P0**，需免费 token |
| **zimuku.org / zimuku.net** | 中文站 | 强（站点内容） | 网页 | 云锁验证码（`security_verify_img`）；`.net` JS 挑战后 401 | — | 无 | 高（3–5 人日，需浏览器+打码） | 不建议 |
| **subom.net** | 中文站 | — | 网页 | — | — | 无 | — | **已失效**（TCP 80/443 超时） |
| **OpenSubtitles** | 国际站 | 中 / 弱 / 中；机翻与社区上传混杂 | REST API + 账号 | API key；官网 Anubis PoW | 匿名 5/天；免费账号约 20/天；VIP 更高 | **已有 `opensubtitles.py`** | 极低（仅配置账号） | 保留，建议账号化 |
| **SubDL** | 国际站 | 官方：简体中文属最大语种之一；本次**未量化**（缺 key） | REST API `api.subdl.com/api/v1/subtitles` | 站点 Cloudflare JS 挑战；API 无挑战 | 免费 2,000 搜索 + 50 下载/天；Pro $5/月 30,000 + 2,000/天 | 无（需新适配器） | 中（1–2 人日） | **推荐 P1**，需免费 key |
| **SubSource** | 国际站 | **实测 0 条中文**：TLoU 0/3,716、Shawshank 0/529、Joy of Life 0/81 | 未公开 API（前端 `POST /v1/movie/search`） | 站点可访问，无需 key | 未公开 | 无 | 中（1–2 人日） | 不作为中文源 |
| **podnapisi** | 国际站 | — | 网页/旧 XML | — | — | 无 | — | **已失效**（无 A 记录，仅 NS 存在） |
| **TVsubtitles** | 国际站 | 实测样本无中文字幕链接（仅语言下拉含 cn） | 网页 | 无 | 无 | 无 | 低 | 不作为中文源 |
| **subf2m** | 国际站 | 实测样本页 0 条中文 | 网页抓取 | 可访问 | 无 | 无 | 中（1–2 人日） | 低优先级 |
| **SubtitleCat** | 聚合站 | 中文来自上游 OpenSubtitles，未量化 | 网页抓取 | requests 被 RST；`curl_cffi` 可访问 | 无 | 无 | 中 | 备选，非主力 |
| **PT / 资源站** | 资源侧 | 整季中字包强 | PT 站内搜索 / Jackett | 登录、邀请、做种考核 | 各站不同 | 复用现有 Jackett/media_search | 非代码 | 账号路线，非字幕 API |

---

## 4. 逐源实测记录

### 4.1 SubHD（基准与镜像）

- `https://subhd.tv/search/庆余年` 返回 200，页面含 60 个 `/d/` 结果链接。
- 镜像 `subhd.me`、`subhdtw.com`、`subhd.com` 同样返回 200，**同样 60 个结果**，说明是同一后端的不同入口。
- 价值：当 `subhd.tv` 域名在某个网络环境下不可达时，可做**域名级 failover**；但下载接口的人机校验与配额问题不会因换域名而改变。
- 结论：继续作为主源，等待下载链路修复；镜像仅作可用性兜底。

### 4.2 assrt.net（伪射手）

实测证据：

- DNS：`assrt.net → 75.127.89.2`，`api.assrt.net → 43.133.211.58`。
- 常规 HTTPS（带 SNI）在 ClientHello 后约 160–440ms 被 RST；HTTP/80 同样被重置。**不是站点 403，而是链路层阻断**。
- 使用 `openssl s_client -noservername`（不带 SNI）直连同一 IP，并带 `Host: api.assrt.net` 发送请求，得到：

  ```http
  HTTP/1.1 400 Bad Request
  {"errmsg":"invalid token","status":20001}
  ```

  说明 API 服务本身正常，仅缺 token。
- 同样方式访问 `https://assrt.net/` 首页返回 200（13 KB），页面标题为：
  “首页 - 射手网(伪) - assrt.net - 字幕下载，字幕组，中文字幕，美剧字幕，英剧字幕，双语字幕，新番字幕”。
- `/sub/search?q=庆余年` 在未登录时 302 回首页，说明网页搜索需登录；API 需 token。

接入判断：

- 现有 `shooter.py` 已封装 assrt API，工具名与 sid 路由可复用。
- 需要补两件事：① 免费 token；② **no-SNI 传输**（或 NAS 侧代理）。no-SNI 方式需要关闭证书校验（因为不带 SNI 时证书主机名无法匹配），属于可用性换安全性的折中。
- 中文覆盖：站点定位就是中文/简繁聚合，国产剧、新美剧、老片均应有覆盖；**本次因缺少 token 未能量化结果数与下载成功率**，需补测（见第 8 节）。

### 4.3 zimuku 系（字幕库）

- `zimuku.org/search?q=庆余年`：返回 404 的“网站防火墙”页，页面包含云锁（Yunsuo）脚本、`security_session_verify` 与 `security_verify_img` 验证码入口，需要人工输入验证码。
- `zimuku.net`：返回 JS 加载页，`location.replace()` 跳转到 `http://ww547.zimuku.net/?tkn=...`，该地址返回 `401 Forbidden`（9 字节）。
- 结论：中文资源强，但**服务端无法稳定程序化访问**；浏览器自动化 + 打码的成本与失效风险都高，不建议纳入本插件。

### 4.4 subom.net

- `subom.net` 解析到 `106.187.39.170`，TCP 80/443 均连接超时；首页与搜索页均不可达。
- 公开检索到的内容也以 2015 年前后为主。
- 结论：**已失效**，从候选清单删除。

### 4.5 OpenSubtitles（现有实现，对照组）

- `api.opensubtitles.com/api/v1/subtitles` 在无 key 时返回 Cloudflare 403：
  `You cannot consume this service.`（带无效 key 同样 403）。
- `www.opensubtitles.org` 首页为 Anubis 工作量证明页（“正在确认你是不是机器人”）。
- 配额（官方/社区口径）：匿名 5 次/24h；免费账号约 20 次/24h；VIP/付费更高。用户当前遇到的是 5 次/天的档位。
- 中文质量：以社区上传与机翻为主，热门美剧覆盖尚可，国产剧/老片覆盖弱，简繁混杂。
- 结论：保留现有适配器；**注册免费账号即可把额度从 5 提到约 20/天**，是零开发成本的立即收益；仍不足以作为整季批量的唯一来源。

### 4.6 SubDL

- 无 key 请求 `api.subdl.com/api/v1/subtitles` 返回：

  ```json
  {"status":false,"statusCode":403,"error":"not_authorized","message":"Not Authorized"}
  ```

  说明 API 可达、结构稳定、只缺 key。
- 官网 `subdl.com` 在 `curl_cffi`（Chrome 指纹）下仍是 Cloudflare JS 挑战页；但 API 域名无此挑战。
- 配额（官方页面）：免费 key 2,000 次搜索 + 50 次下载/天，UTC 0 点重置；Pro $5/月为 30,000 次搜索 + 2,000 次下载/天。
- 中文覆盖：官方 About 页把 Chinese (Simplified) 列入其最大语种集合；本次无 key 未做标题级验证。
- 结论：**最值得新增的批量源**。50 次/天足以在一天内补齐一季 12–24 集；需要用户注册免费 key。

### 4.7 SubSource

- 站点 `subsource.net` 可访问；其前端 API `https://api.subsource.net` 无需 key 即可工作：
  - `POST /v1/movie/search`，body `{"query":"...","includeSeasons":true,"limit":5}` 可返回影视条目。
  - 该 API 未公开文档，属于前端接口，存在变更风险。
- 中文覆盖实测（解析标题页 `self.__next_f` 数据）：

  | 样本 | 字幕总数 | 中文条数 |
  |---|---:|---:|
  | The Last of Us | 3,716 | **0** |
  | The Shawshank Redemption | 529 | **0** |
  | Joy of Life（庆余年） | 81 | **0** |

- 语种以波斯语、英语、阿拉伯语、越南语、土耳其语为主。
- 结论：站点与 API 都稳定，但**中文覆盖为零**，不作为中文源。

### 4.8 podnapisi

- 公共 DNS（223.5.5.5）查询 `podnapisi.net` 无 A 记录；NS 仍存在（`ns1.podnapisi.net` 等），判断为域名/服务已停止解析。
- 结论：**已失效**。

### 4.9 TVsubtitles

- 站点可访问；剧集页语言筛选含 `cn` 图标，但本次抽查的 3 个剧集页中**没有任何中文字幕链接**。
- 结论：英文/欧洲语系为主，不作为中文源。

### 4.10 subf2m

- 站点可访问（Cloudflare 但不拦 HTML）；搜索与标题页可抓取。
- 本次抽查的 Shawshank / The Last of Us 标题页未发现中文条目。
- 结论：抓取可行但中文收益低，低优先级。

### 4.11 SubtitleCat

- `requests` 直连被 RST；`curl_cffi` 可 200 访问并返回搜索结果页。
- 中文仅出现在语言下拉（`zh-CN` / `zh-TW`），结果数据来自上游 OpenSubtitles，未量化中文条目数。
- 结论：本质是 OpenSubtitles 的再包装，不能解决配额问题，最多作为抓取备选。

### 4.12 资源侧：PT / 资源站 / 字幕组整季包

- 可达但需登录：`hdsky.me`、`hdhome.org`、`cmct.tv`、`ourbits.club`、`pterclub.com` 均返回 302/200（登录页或站点首页）。
- 不可达：`dmhy.org`、`share.dmhy.org`、`nyaa.si` 在本线路超时；`rrys.tv`/`zmz2019.com` 已停放（parklogic 广告页）。
- 生态侧证据：ChineseSubFinder 已明确下线 subhd、zimuku 的直连下载，改为 shooter(assrt)、a4k、SubtitleBest 等来源；其中 SubtitleBest 当前 `api.subtitle.best` 返回 522，`subtitle.best` 无 DNS，已不可用。
- 结论：PT/资源站适合“整季中字包”路线，但依赖账号/邀请与做种，不是可编程字幕 API；可作为 SubHD/assrt 都拿不到时的兜底，复用现有 Jackett 搜索即可，不需要新代码。

---

## 5. 推荐组合与优先级

### 组合 A（推荐，全免费）

| 优先级 | 源 | 角色 | 理由 |
|---|---|---|---|
| P0 | **assrt** | 中文主源 | 中文/简繁定位最匹配；已有适配器；仅缺 token 与 no-SNI 传输 |
| P1 | **SubDL** | 批量补充 | 免费 50 下载/天，一天可补整季；API 稳定、错误结构清晰 |
| P1 | **OpenSubtitles（免费账号）** | 西片补充 | 零开发；5/天 → 约 20/天 |
| P2 | **SubHD 镜像** | 可用性兜底 | 4 个域名同后端，主域名故障时切换 |
| P2 | **PT / 资源站整季包** | 最后兜底 | 整季中字包完整，但需账号/邀请 |

### 组合 B（低成本付费，适合重度补库）

- **SubDL Pro $5/月**：30,000 搜索 + 2,000 下载/天，基本消除批量瓶颈。
- **OpenSubtitles VIP**：按官方档位提升下载额度。
- assrt 仍作为中文主源（免费 token）。

### 为什么不选其他源

- **SubSource**：API 虽免费且可用，但实测 4,326 条字幕中中文 0 条，方向不匹配。
- **zimuku**：中文强，但云锁验证码 + JS 挑战，程序化成本高、失效风险高。
- **subom / podnapisi**：已失效。
- **TVsubtitles / subf2m / SubtitleCat**：实测或结构上中文极少，投入产出比低。

### 配额算术（以 12 集一季为例）

- SubDL 免费：50 次/天 → **一季一次补齐**。
- OpenSubtitles 免费账号：约 20 次/天 → 单季可补，但多季需分天。
- assrt：额度未公开，需 token 后实测；作为中文主源优先使用。
- 组合后单日可用额度 ≥ 70 次，已覆盖“补一整季”的核心痛点。

---

## 6. 落地要点（按源）

### 6.1 assrt（复用 `shooter.py`）

1. 配置新增/确认 `assrt_token`，并让 `enabled` 可独立开关。
2. 传输层二选一：
   - **方案 A（无 SNI 直连）**：自定义 `HTTPSConnection`，`ssl` 上下文关闭 `check_hostname` 与证书校验，`wrap_socket(server_hostname=None)`；请求头保留 `Host: api.assrt.net`。优点是不依赖额外组件；缺点是证书不校验、依赖站点默认 vhost，需加灰度和失败回退。
   - **方案 B（NAS 代理/分流）**：在 NAS 或软路由上为 `assrt.net` / `api.assrt.net` 配置代理，插件仍走普通 HTTPS。稳定性更好，但需要用户侧网络配置。
3. token 传递方式以站点实际为准：当前社区文档使用 `Authorization: Bearer <token>`，现有适配器若使用 query `token=`，接入时需实测确认（本次 no-SNI 请求已验证 API 会返回 `invalid token`，说明鉴权入口生效）。
4. 下载后沿用现有字幕落盘、简繁过滤、审计与回滚逻辑，不新增判定分支。

### 6.2 SubDL（新增适配器）

1. 新增 `subdl.py`，实现现有统一协议：`search(query) -> [SubtitleCandidate]`、`fetch(sid) -> (filename, content)`；`sid` 加 `subdl:` 前缀。
2. 搜索：`GET https://api.subdl.com/api/v1/subtitles?api_key=...&film_name=<query>&languages=ZH&subs_per_page=30`。
3. 下载直链需在拿到 key 后按官方文档确认（通常为 `dl.subdl.com` 的 zip/srt 直链），**本次未验证**，接入前必须做一次真实下载与解压校验。
4. 缓存与限流：免费额度按 UTC 日重置，需在本地记录当日已用下载数，避免触发 429；token/key 必须从 URL 与日志中脱敏。

### 6.3 OpenSubtitles（现有适配器）

1. 配置用户名/密码，走已实现的登录链路；免费账号即可把额度提升到约 20/天。
2. 保持现有 `enabled: false` 的占位策略，由用户在配置中开启。
3. 中文判定继续沿用现有简繁/中文轨判定，不因换源改变语义。

### 6.4 SubHD 镜像

1. 将 `subhd.tv`、`subhd.me`、`subhdtw.com`、`subhd.com` 做成可配置的 base domain 列表，按顺序 failover。
2. 仅解决“域名不可达”，不解决下载人机校验；下载链路的修复仍由另一任务负责。

### 6.5 PT / 资源站

1. 不新增字幕适配器；复用现有 Jackett/media_search 能力，搜索关键词增加“简繁 / 中字 / 字幕 / 合集”等模式。
2. 需要用户提供 PT 账号或邀请；注意做种考核与站点规则。

---

## 7. 需要用户配合的事项

| 事项 | 是否付费 | 用途 | 备注 |
|---|---|---|---|
| assrt 注册并获取 32 位 token | 免费 | assrt 搜索/下载鉴权 | 需要用户到 assrt 用户中心获取；提供后补测 3 个样本 |
| SubDL 注册并获取 API key | 免费（2,000 搜索 + 50 下载/天） | 批量补字幕 | key 泄露会导致额度被他人消耗，需妥善保管 |
| OpenSubtitles 免费账号 | 免费（约 20/天） | 提升现有源额度 | 现有适配器需用户名+密码；VIP 可进一步提升 |
| NAS 侧代理/分流（可选） | 视方案 | 让 assrt 走普通 HTTPS | 若不接受 no-SNI 折中，需要此项 |
| PT 站点账号/邀请（可选） | 视站点 | 整季中字包兜底 | 非必须，属于资源侧路线 |

> 凭据请通过安全方式提供，不要写入仓库、配置模板或日志；本报告不包含任何 token/key/密码。

---

## 8. 未验证项与补测清单

1. **assrt**：拿到 token 后，对“新美剧 / 国产剧 / 老片”各 1 个样本统计结果数与简体条目数，并完成 1 次真实下载 + 落盘校验；同时确认 token 传递方式（Bearer 还是 query）。
2. **SubDL**：拿到 key 后，确认中文语言参数（`ZH` / `ZH-HANS`）、单页条数、下载直链格式、zip 内文件命名与编码；完成 1 次真实下载。
3. **OpenSubtitles**：用免费账号复测 20/天额度是否生效，以及中文结果的简繁比例。
4. **SubSource**：API 为非公开前端接口，若未来要用，需先确认服务条款与稳定性；当前中文为 0，暂不投入。
5. **SubHD 下载链路**：由另一任务负责，本报告不涉及代码修改与验证。
6. **PT/资源站**：需用户提供账号后才能验证“整季中字包”的实际命中率。

---

## 9. 风险与失效判断

| 源 | 失效风险 | 说明 |
|---|---|---|
| assrt | **中高** | SNI 阻断策略可能变化；no-SNI 依赖站点默认 vhost 且不校验证书；token 政策可能调整 |
| SubDL | 中低 | 免费额度可能调整；站点有 Cloudflare；API key 管理与日志脱敏需做好 |
| OpenSubtitles | 低 | 成熟服务；主要风险是配额与中文质量，不是可用性 |
| SubHD | 高 | 人机校验、域名变动、镜像同后端；修复前无法批量 |
| zimuku | 高 | 云锁验证码 + JS 挑战；浏览器自动化易碎 |
| subom / podnapisi | 已失效 | 不再投入 |
| SubSource | 低（但无中文） | 站点稳定，API 未公开，中文覆盖为零 |
| TVsubtitles / subf2m / SubtitleCat | 低 | 可访问，但中文收益低 |
| PT / 资源站 | 中 | 邀请制、做种考核、账号封禁风险 |

---

## 10. 附录：no-SNI 探测片段（仅用于验证思路）

```python
import socket, ssl

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

with socket.create_connection(("43.133.211.58", 443), timeout=20) as sock:
    with ctx.wrap_socket(sock, server_hostname=None) as ss:  # 关键：不发送 SNI
        ss.sendall(
            b"GET /v1/sub/search?q=test HTTP/1.1\r\n"
            b"Host: api.assrt.net\r\n"
            b"User-Agent: Mozilla/5.0\r\n"
            b"Connection: close\r\n\r\n"
        )
        print(ss.recv(4096).decode("utf-8", "replace"))
```

实测输出包含 `{"errmsg":"invalid token","status":20001}`，证明 API 可达；接入时应封装为独立传输层，并保留普通 HTTPS 的回退路径。

---

## 11. 本次调研未做的事

- 未修改 `nas_mcp/subhd.py`、`nas_mcp/subtitle.py`、配置与前端文件。
- 未安装/覆盖任何插件，未修改 fnOS 权限与媒体文件。
- 未使用任何真实 token/key 进行搜索或下载；相关结论均标注为“未量化/待补测”。
- 探测脚本仅存在于 NAS `/tmp`，验证后已清理，不进入仓库。
