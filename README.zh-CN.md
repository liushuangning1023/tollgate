# TollGate（中文文档）

**本地 AI 调用管家：零部署的 OpenAI 兼容代理——每次调用都计量、跨供应商统一成本口径、自动故障切换、预算护栏。**

```console
$ tollgate serve
[tollgate] listening on http://127.0.0.1:8787
[tollgate] point your app at  base_url = http://127.0.0.1:8787/v1
[tollgate] tag calls with the X-Tollgate-Tag header for cost attribution

$ tollgate report --day --by tag
tollgate report — last day by tag

  tag          calls  ok  retries  in+out tokens   cost      avg latency
  proj-a         143  141       12        2.1M     $1.8420   840ms
  my-site         89   89        2        480K     $0.3915   310ms
  total                                   2.6M     $2.2335
```

TollGate 面向「AI 流量从应用直连供应商」的开发者和小团队——这些人通常
答不出*「今天哪个项目花了多少钱」*、扛不住供应商一次抖动、也拦不住
失控脚本的账单。

## 解决什么

| 痛点 | TollGate 的做法 |
|---|---|
| 成本不可见、用量无法归因 | 每次计量调用都进 SQLite，按 `X-Tollgate-Tag` 打标 |
| 各家计费口径不同，对比失真 | 统一价格表（美元/百万 token 输入+输出）；未知模型显式打标，绝不悄悄按零成本处理 |
| 429 / 5xx / 断供打挂应用 | 指数退避（尊重 `Retry-After`）后切到路由链上的下一家 |
| Key 到处硬编码、花销无上限 | Key 只放一份配置；按标签与全局的日预算，`alert` 或硬 `cutoff` |
| 网关型方案运维太重 | 单文件单进程，标准库 HTTP 服务器，唯一依赖 `requests` |

## 工作原理

```
你的应用 ──HTTP──▶ TollGate (localhost:8787)
                    │  "model" 的路由链：
                    │    1. openai/gpt-4o-mini    ──┐
                    │    2. glm/glm-4-flash       ──┼─▶ 第一个健康的供应商胜出
                    │    3. deepseek/deepseek-chat──┘
                    │  ✔ 计量 → SQLite（标签/模型/token/成本/延迟/重试）
                    │  ✔ 预算：调用前拦截（cutoff）+ 调用后告警
                    └──响应/SSE──▶ 你的应用
```

- **透明接入**：任意 `/v1/*` 请求都透传（chat、completions、embeddings、
  models）。改一下 `base_url` 就完事——不用换 SDK，不用改代码。
- **流式透传**：SSE 按块转发；上游在流里给了 usage 就用它，否则按
  字符数/4 估算并在报表里打 `*` 标。
- **故障切换时改写模型**：`"model": "gpt-4o-mini"` 可落到第 2 家的
  `glm-4-flash`——链怎么排你说了算。
- **先记账后回包**：调用在上游应答的那一刻就落库，报表永远不会因为
  竞态而漏记。

## 快速上手

```bash
git clone https://github.com/liushuangning1023/tollgate tollgate && cd tollgate
pip install .
tollgate init          # 生成 ~/.tollgate/config.json
# 编辑 providers：填入真实 key，定义 routes
tollgate serve
```

任何 OpenAI 兼容客户端指向它即可：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="anything")
client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "hi"}],
    extra_headers={"X-Tollgate-Tag": "my-project"},   # 成本归因
)
```

然后看住钱：

```bash
tollgate status                      # 今天一行汇总
tollgate report --week --by tag      # 谁花了多少
tollgate tail                        # 实时调用日志
```

## 配置

`~/.tollgate/config.json`（JSON，`init` 自动 `chmod 600`；环境变量
`TOLLGATE_CONFIG` 可改路径），完整示例见上方英文文档或
`examples/config.example.json`。规则要点：

- **路由**：精确模型名优先；否则走 `default` 路由；再否则按
  `provider_order` 顺序原样透传。客户端路径里多出的 `/v1` 前缀会被
  自动剥掉（供应商 base_url 自带版本前缀）。
- **预算**：`alert` 过线只告警；`cutoff` 在请求转发**之前**返回 429
  （一分钱都不花）。标签与全局双查，以更严格的为准。
- **价格表**：内置表只是参考值（各家经常调价），请对照账单在
  `pricing_overrides` 里修正；未知模型计量照记、成本打 `*` 标。
- **`local_token`**：设置后客户端须带 `Authorization: Bearer <token>`
  访问 TollGate 本身（默认只绑 127.0.0.1，对外开放端口时务必开启）。

## 命令一览

| 命令 | 作用 |
|---|---|
| `tollgate init` | 生成初始配置（`--force` 覆盖） |
| `tollgate serve [--host H] [--port P]` | 启动代理 |
| `tollgate status` | 今日调用 / token / 花费一行汇总 |
| `tollgate report --day\|--week\|--month --by tag\|model_used\|provider\|status` | 成本报表 |
| `tollgate tail [-n N] [--once]` | 跟踪调用日志 |
| `tollgate budget` | 预算表：上限 / 已花 / 模式 / 状态 |
| `tollgate pricing [--model M]` | 价格表查询 |

## 诚实的局限

- 429/超时的重试会重复请求，超时的那次可能已被上游部分计费；
  `max_attempts` 就是上限——贵模型请调小。
- 流式调用只有上游在流中返回 usage（OpenAI 需
  `stream_options.include_usage`）才有精确值，否则按字符/4 估算并
  标记为估算（`*`）。
- 只讲 OpenAI 兼容方言；原生 Anthropic Messages 等私有协议不做翻译。
- 刻意不做：语义缓存、prompt 改写、Web 面板（见路线图）。

## 设计取舍

- **标准库 HTTP 服务器** + 唯一依赖 `requests`：没有 FastAPI/aiohttp
  依赖树，进程小到可以跑在终端旁边或注册成用户服务。
- **SQLite** 存储：任何工具都能查、直接拷文件、可丢进 DuckDB。
- **先记账后回包**：上游一应答就落库，不存在与客户端的竞态。
- **配置即校验**：路由里引用了未配置的供应商会在加载时被剔除，
  容灾链路永远诚实。
- **61 个离线测试**：起真实本地 HTTP 服务（假 OpenAI 上游 + TollGate
  本体），走真实网络路径覆盖转发/流式/故障切换/预算/鉴权/打标；
  CI 跑 Python 3.9–3.13。

## 路线图

- 周/月预算窗口（当前为日预算）
- Prometheus 抓取端点
- 相同 prompt 的响应缓存（可选开启）
- 单供应商多 Key 池 + 配额错误自动轮换

English docs: [README.md](README.md)。欢迎贡献，见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)
