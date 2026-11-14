"""repo-monitor v2：单仓库边界的 GitHub 只读管家。

设计约束（见 README）：
  - 对所有业务仓库只有 GET，绝不写入；
  - 仅两条写白名单：回写本仓库 state.json、向飞书 Webhook POST 消息；
  - 纯标准库，零第三方依赖，默认零 AI 额度。
"""
__version__ = "2.0.0"
