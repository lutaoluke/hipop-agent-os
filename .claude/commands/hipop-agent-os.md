---
description: 点购 Agent OS — 工作台 + 协作 Agent + 工作流触发（hipop/server）
---

点购 Agent OS 工作台（hipop/server）。工作目录：/Users/luke/code/hipop

**权威文档**（`openclaw-skill/` 是单一来源，本命令是项目级入口）：
- `openclaw-skill/agent-os.md` — 快速概览 + 启动 + 架构
- `openclaw-skill/agent-os-tools.md` — 11 个 chat tool + 意图路由 + 四象限行为规则
- `openclaw-skill/agent-os-server.md` — 多租户架构 + 工作流触发链路 + 扩展点 + 调试

> ⚠️ 旧全局命令 `~/.claude/commands/hipop-agent-os.md` 已被本项目命令取代。
> 以 `openclaw-skill/agent-os*.md` 为准，两者有差异时以 openclaw-skill/ 为权威。

## 用法

读取上述三个 skill 文件，回答关于工作台架构、chat 工具调用、workflow 触发链路、多租户配置的问题；或根据需要指导修改 `hipop/server/` 下的代码。
