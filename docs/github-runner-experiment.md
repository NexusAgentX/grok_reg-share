# GitHub Hosted Runner 试验（注册 + mint）

这是一次**对照实验**，不是默认量产路径。

目标：验证「换 GitHub 机房 IP / 临时机器」是否比 Tower 本机更容易过 Turnstile 和 device-auth mint。

## 预期

| 可能结果 | 含义 |
| --- | --- |
| hosted 比本机更好 | 值得继续扩 worker 思路 |
| hosted 更差/全灭 | 说明需要可控住宅代理，而不是机房 IP |
| 注册成、mint 挂 | 与本机类似，瓶颈仍在 mint 风控 |

## 工作流

- 文件：`.github/workflows/runner-register-mint.yml`
- 触发：仅 `workflow_dispatch`（手动）
- 默认：`extra=1`，`registration_mode=browser`
- 产物：artifact（日志、诊断、可能的 `xai-*.json`）

## 需要的 Secrets

在仓库 Settings → Secrets and variables → Actions 配置：

| Secret | 必需 | 说明 |
| --- | --- | --- |
| `CLOUDFLARE_API_BASE` | 是 | 临时邮箱 API，如 `https://mail-api.kagari.app` |
| `CLOUDFLARE_API_KEY` | 是 | 邮箱 API key |
| `RUNNER_PROXY` | 否 | 可选代理。空= runner 直连（机房 IP） |
| `RUNNER_CPA_PROXY` | 否 | mint 专用代理；空则跟随 `RUNNER_PROXY` |

注意：

- **不要**填 `http://host.docker.internal:7890`（runner 上没有 Tower 宿主代理）
- 若要用代理，填 runner 能访问的公网代理地址

## 怎么跑

1. 推送 workflow 到 GitHub
2. 配好 secrets
3. Actions → `runner-register-mint` → Run workflow
4. 先跑 `extra=1`
5. 下载 artifact，看：
   - `summary.txt` 的 `oidc_ok`
   - `runner-batch.log`
   - `diagnostics/`
   - 若有 `cpa_auths/xai-*.json`，再决定是否导入美西

## 成功后如何导入 CPA

**不要**让 runner 直接 SSH 美西（密钥面太大）。推荐：

1. 从 artifact 取出 `xai-*.json`
2. 人工或本机脚本放到 Tower `/var/lib/grok-reg/cpa-outbox`
3. 走现有 `cpa-outbox-sync` 同步到美西

## 安全

- 工作流只读代码权限
- 配置 artifact 会脱敏 key
- 不要把真实 `config.json` 提交进 Git
- 公共仓库请确认 fork PR 不会偷 secrets（本工作流无 `pull_request` 触发）

## 和本机差异（刻意）

- `cpa_mint_browser_reuse=false`
- `recycle_every=1`
- 无 WARP `proxy_url` 轮转（hosted 上没有 warp-1..4 内网）
- 默认可直连（`allow_direct_fallback=true`）

## 结论怎么写

每次试验记三行即可：

```text
日期:
runner IP 类型: hosted直连 / 自备代理
结果: 注册成功 x / mint成功 y / 主要失败原因
```
