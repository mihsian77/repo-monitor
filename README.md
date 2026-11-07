# repo-monitor · 单仓库只读仓库管家

一个「只改自己、不碰别人」的 GitHub 巡检模板：定时读取你关心的仓库动态，按重要程度分级，
把同类消息合并成一张飞书卡片，并把去重水位回写到**本仓库**。

它同时服务两个用途，而且不冲突：

- **你自己用**：真实仓库清单通过 Secrets 注入，仓库里看不到你监控了谁；
- **给别人当模板**：clone 下来就能跑自检、回放、看结构，不需要任何凭证。

## 三条硬边界

| 边界 | 怎么保证 |
| --- | --- |
| **只读** | 所有 GitHub 请求都是 GET；`gh.assert_readonly()` 在任何非 GET 上抛异常，`tools/check_readonly.py` 静态扫描全部源码 |
| **单仓库** | 唯一被写入的文件是本仓库的 `state.json`；`tools/verify_no_pollution.py` 逐仓库核对从未产生写入 |
| **不泄露** | 真实清单不入库；回写的 `state.json` 经隐私投影变成不可逆 token，公开仓库里看不到监控目标 |

## 快速开始

1. **Fork / clone 本模板**，在 `Settings → Secrets and variables → Actions` 填 5 个 Secret：

   | Secret | 作用 |
   | --- | --- |
   | `GH_READ_TOKEN` | 只读 PAT，用于跨仓库采集（Repository access = All repositories，5 项 Read-only） |
   | `FEISHU_WEBHOOK` | 飞书自定义机器人地址 |
   | `FEISHU_SECRET` | 机器人开了签名校验才填，否则留空 |
   | `MONITOR_CONFIG` | 真实仓库清单，一段 JSON 字符串（照 `config/settings.example.json` 填） |
   | `PRIVACY_SALT` | 随机字符串，生成后**固定不要改**，用于状态脱敏 |

2. **手动跑一次**：`Actions → GitHub Monitor (v2 readonly) → Run workflow`。别等 cron，定时任务达成率不稳定。

3. **验收看三个数**：`Run monitor` 日志里 `events > 0`、`watermarks` 有端点数、`Verify no pollution` 通过。
   飞书会补推首轮积压的聚合卡，属正常，不是刷屏。

## 分类合并转发（QQ 合并转发效果）

一轮巡检的多张卡片会打成**一张飞书 interactive 卡片**：按类别分成折叠面板，P0 默认展开、
P1/P2 折叠，卡片颜色随最高级别走（红 / 橙 / 蓝）。群里一轮只多一条消息，点开才看同类明细。

三条兜底：

- **装不下就分片**：飞书单条上限 20KB，先逐级收起明细并如实标注「另 N 行未展开」，仍超就按 P0 优先自动分片，成员卡一张不丢；
- **对方不认就退回**：客户端太老或返回 9499 时自动降级为逐条富文本，逐片独立记账，只有失败那片降级；
- **一键回退**：`config/settings.json` 里 `grouping.enabled = false` 即恢复旧的逐条推送。

「每日在岗确认」始终单独发，不并入汇总卡 —— 它是「管家还活着」的信号，程序也靠它判断当天有没有报到。

## 隐私架构（为什么公开也不会泄露）

```
真实清单 ──Secret 注入──> 内存 cfg ──采集/分级──> 卡片（真名）
                              │
                              └─ state.save() ──隐私投影──> state.json（token，入库）
                                                  ▲
                              state.load() ──反投影──┘ 只有持有同一份清单+salt 才能还原
```

- `config/settings.json` 里只有 `your-org/repo-a` 这类占位值，可直接公开；
- `src/config_loader.py` 只允许覆盖身份类字段，行为参数（阈值、分组、免打扰）仍以仓库内模板为准，防止 Secret 夹带东西改变程序行为；
- `src/privacy.py` 在写盘前把真实标识换成 `HMAC-SHA256(salt, 标识)` 的短 token，读盘时按同一张表还原；**业务代码一行都不用改**，去重与水位照常工作；
- 别人 clone 模板、没有 Secret 时，投影层是 no-op，行为与旧版完全一致。

> `PRIVACY_SALT` 一旦更换，旧 `state.json` 里的 token 就无法还原，等价于重置去重账。
> 确实要换，请先清空 `state.json` 并按首轮 bootstrap 重新建基线。

## 采集水位与零历史回写

- **按端点增量**：提交 / Issue / 评论 / 通知中心各自记独立水位，采集时把 `since` 回退 60 秒，
  避免时间戳边界吞掉同一秒的事件；水位不变时请求 URL 稳定，ETag 304 不耗配额。
  重复到达的事件由 `state.json` 的只追加去重账收敛，不会二次推送。
- **回写不增提交**：`Push state` 用 `git commit --amend` 覆盖唯一的无父根提交，再 force 推回 `main`。
  因此每 30 分钟一轮巡检也不会产生第 2 个提交，公开模板始终只有一个干净的初始提交，
  无需事后手工清理提交记录。写盘的 `state.json` 是隐私投影态，公开也看不到监控清单。

## 本机运行与验证

```bash
python3 monitor.py --self-test          # 自检：配置模式、只读守卫、模块导入
python3 monitor.py --replay            # 合成数据离线回放（不联网、不写状态）
python3 tools/replay.py                # 严格验收断言（35 条，A~F 组）
python3 tools/check_readonly.py        # 静态只读守卫
python3 monitor.py --demo             # 用合成数据发一张分类合并卡（配了 Webhook 就真发）
python3 monitor.py --dry-run          # 真实采集但不推送、不写状态
```

本机要带真实清单运行：把 `config/settings.example.json` 复制为 `config/settings.local.json`
填好即可（该文件已在 `.gitignore`，不会入库）；或设置环境变量 `MONITOR_CONFIG`。

**效果测试**：在 Actions 里 `Run workflow` 并把 `demo` 选成 `true`，会用纯合成数据往飞书群
真发一张可折叠的汇总卡，不采集、不写状态，也不含任何真实仓库信息。

## 目录结构

| 路径 | 作用 |
| --- | --- |
| `monitor.py` | CLI 入口（自检 / 回放 / demo / 正常运行） |
| `src/gh.py` | 只读 GET 客户端、ETag、限流退避 |
| `src/events.py` | 事件源（GitHub / 回放 / Webhook）与真实上游 compare |
| `src/rules.py` | 分级、bot 判定、敏感路径、建议引擎、错误归类 |
| `src/report.py` | 卡片聚合、待办本、沉默检测、每日在岗 |
| `src/digest.py` | 分类合并卡片（折叠面板、字节预算、分片） |
| `src/notify.py` | 飞书推送、签名、合并交付与降级 |
| `src/state.py` | 状态 v2、去重记账、原子写 |
| `src/privacy.py` / `src/config_loader.py` | 隐私投影 / 配置注入 |
| `config/settings.json` | 公开模板（占位值） |
| `tests/` `tools/` | 单测、回放断言、安装与校验脚本 |
| `worker/index.js` | 可选的实时 Webhook 接收端（未部署也不影响使用） |

## 常见问题

- **日志里 `events=0` 但没有报错**：多半是 `GH_READ_TOKEN` 作用域不足（fine-grained PAT 没选 All repositories）。
  GitHub 对未授权仓库返回 404 而非 403，采集器会静默跳过。换好 Secret 后**手动跑一次**，历史事件不会丢。
- **飞书没消息**：看「每日在岗」卡是否还在。卡在 = 今天确实没事；卡也没了 = 管家没上班，去看 Actions 日志。
- **cron 不准时**：GitHub Actions 定时任务排队延迟明显，需要准实时请用外部定时器触发 `workflow_dispatch`。
- **想改监控清单**：只改 Secret `MONITOR_CONFIG`（或本机 `settings.local.json`），不用改代码、不用 push。
