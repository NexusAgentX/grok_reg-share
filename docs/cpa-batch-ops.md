# CPA 账号池批量铸造经验

本文记录 Tower 注册机 `grok-reg` → outbox → 美西 CLIProxyAPI（CPA）账号池的实战量产经验。
目标口径：**可用号 = 含 OIDC 的 `xai-*.json` 数量**，不是 `accounts_cli.txt` 行数。

相关入口：

- 注册机：本仓库 `README.md`
- 美西站点：`/srv/ops/remote-hosts/us-west-vps/sites/new-api/README.md`
- 上游事实：`/srv/ops/new-api/upstreams.md`（渠道 ID 26 `cpa grok`）
- 同步脚本：`/srv/ops/remote-hosts/us-west-vps/sites/new-api/cpa-import/`

## 成功链路

```text
browser 注册
  → accounts_cli.txt（email----password----sso）
  → mint OIDC（device-auth + Chromium）
  → /data/cpa_auths/xai-<email>.json   # 容器内
  → /outbox → /var/lib/grok-reg/cpa-outbox
  → scp 美西 /data/new-api/cpa/auths
  → CPA 热加载
  → New API 渠道 ID 26
```

硬约束：

- **SSO ≠ OIDC**。只有 OIDC 文件才能给 CPA / `grok-4.5`。
- 账号级 `proxy_url` 轮转 `socks5://warp-1..4:1080`，不要优先开全局代理。
- CPA 不开放公网；管理面只走 SSH / EasyTier。

## 量产推荐参数

| 项 | 推荐 | 说明 |
| --- | --- | --- |
| `registration_mode` | `browser` | 量产唯一稳路径 |
| 注册并发 `--threads` | `1` | 可试 `2`；更高易拖垮 mint |
| mint 并发 `--mint-workers` | `1` | `>=2` 时 `invalid_grant` 明显增多 |
| 每批 `--extra` | `3~4` | 失败后好 prune，也避免长时间卡死 |
| 邮箱 | Cloudflare Worker 邮箱 | 当前可用；DuckMail 公共域已被 xAI 拒 |
| `cpa_probe_chat` | `false` | free 号 chat 拒绝不应丢号 |
| 任务结束 | 改回 `auto` | Web 面板默认更友好 |

示例（容器内）：

```bash
docker compose exec -T grok-reg python -u register_cli.py \
  --extra 4 \
  --threads 1 \
  --registration-mode browser \
  --mint-workers 1
```

同步到美西：

```bash
/srv/ops/remote-hosts/us-west-vps/sites/new-api/cpa-import/sync-outbox.sh
ssh us-west-vps 'find /data/new-api/cpa/auths -name "xai-*.json" | wc -l'
```

## 并发对照

| 策略 | 现象 | 结论 |
| --- | --- | --- |
| `threads=3, mint=2` | 注册快，mint 大量 `invalid_grant`，OIDC 转化差 | 不适合冲池 |
| `threads=2, mint=2` | 比单线程快，但仍有 mint 失败潮 | 观察期可用 |
| `threads=1, mint=1` | 慢，但 OIDC 转化最稳 | **量产默认** |

经验公式：

1. 先保证 mint 成功率，再谈注册吞吐。
2. 注册可以略并发；mint 优先串行。
3. 卡住时先降并发，不要硬加线程。

## 可用号判定与 prune

### 可用

`cpa_auths/xai-*.json` 同时有：

- `access_token`
- `refresh_token`
- 通常还有 `id_token`

### 不可用（应删）

- 只有 `accounts_cli.txt` 行，没有对应 `xai-*.json`
- mint 日志：`device auth token error: invalid_grant: Access denied`
- 文件损坏 / 缺 token 字段

### prune 原则

每批结束后对齐三边：

1. 本地账本 `accounts_cli.txt`
2. 本地 CPA `cpa_auths/`
3. 美西 `/data/new-api/cpa/auths`

删掉「账本有、OIDC 无」的邮箱，并清理：

- `emails_used.txt` 中对应行
- `cpa_auths/cpa_auth_failed.txt` 中对应失败记录

不要只看注册成功数；**以 remote OIDC 文件数为准**。

## 失败分类

### 1. 注册失败

- 邮箱提交不跳转
- Turnstile 卡住（token 长度长期 0）
- 资料页填写失败

处理：直接重开，不要回填半成品。

### 2. 注册成功、mint 失败

- 典型 `invalid_grant`
- 账本有 sso，池子没有 OIDC

处理：**立刻 prune**，否则库存虚高。

### 3. chat probe 拒绝

- free 号常见 permission-denied
- 不应因此删除已 mint 成功的 OIDC 文件
- 配置保持 `cpa_probe_chat=false`

## pure / fast 现状（非量产）

`fast` / pure 协议路径已验证：

- Castle 可采
- OTP 可部分打通（需处理 DrissionPage `NoneElement` 误判）
- Profile 表单可到
- **Turnstile / create_user / 稳定全链路仍未量产可用**

因此：

- 研究继续用 `fast`/`auto`
- **冲池只用 `browser`**

已知实现坑：

- `NoneElement` 不能用 `is not None` 判断，要用 truthiness
- OTP 前不要协议 `VerifyEmailValidationCode`，容易吞码
- OTP 优先无横杠、React-safe 填码
- Profile 后 Turnstile 是当前主卡点

## 冲到目标 N 的 checklist

1. 设 `registration_mode=browser`，`cpa_probe_chat=false`
2. 看当前 OIDC 数：`local_cpa` / `remote`
3. `need = N - oidc`
4. 单线程小批量开：`--extra min(need,4) --threads 1 --mint-workers 1`
5. 批后 prune 无 OIDC
6. `sync-outbox.sh`，核对 remote
7. 重复直到 `remote >= N`
8. 改回 `registration_mode=auto`

## 当前生产基线（2026-07-25）

- 注册机：Tower Docker `grok-reg`，Web `http://10.251.0.10:5000`
- 邮箱：Cloudflare Worker 域名
- 账号代理：`warp-1..4` 轮转
- 美西 CPA 池：约 **40** 个 xAI OIDC 文件
- New API：渠道 ID 26 `cpa grok` / 分组 `grok-cpa`，售价 Krill×2

## Turnstile 诊断（2026-07-25）

资料页 Cloudflare 验证常为**隐形 widget**：页面仍是 `Complete your sign up`，
DOM 里等 `cf-turnstile-response` token，肉眼不一定看得到勾选框。

卡住时注册机会写入：

- `/data/diagnostics/*turnstile*.png` 截图
- `/data/diagnostics/*turnstile*.json`（url、token_len、iframe、正文摘要）

实现要点：

- **不要一上来 `turnstile.reset()`**，会打断已在进行的托管验证
- 先 shadow/CDP/容器多路径点击，再晚些才 reset
- `perf_fast` 会跳过普通截图，但 Turnstile 诊断截图始终落盘

## 不该做的事

- 用 `accounts_cli` 的 sso 直接当 CPA 号
- 高 mint 并发硬冲
- 失败半成品长期留在账本
- 把 CPA 当高可用主路径（仍是 Krill 后备）
- 在文档/Git 写入 CPA 管理密码或 API Key
