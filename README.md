# AI Continuity Hub

A local-first memory and project handoff toolkit for people working with multiple AI assistants.

把长期个人记忆和项目交接保存在自己的电脑上。切换助手时，先读当前状态，必要时查交接历史。核心只需要 Python，既可以直接用命令，也可以接支持 stdio MCP 的客户端。

## 本地启动

需要 Python 3.10 或更新版本。Windows 如果常用 `py -3`，就用它替代下文的 `python`。Linux 如果命令是 `python3`，同理。

~~~bash
git clone https://github.com/Archie-LYX/AI-Continuity-Hub.git
cd AI-Continuity-Hub
python hub.py projects
python hub.py publish demo --from-file examples/handoff.json
python hub.py status demo
~~~

第一次运行 `projects` 返回空列表。发布第一条后会创建 `data/projects.sqlite3`，并生成 `projects/demo/STATUS.md` 与 `HANDOFF.md`。示例使用虚构数据，实际使用时换成自己的项目代号和内容。

## 项目接力

1. 接手前运行 `python hub.py projects`，然后查 `python hub.py status <项目代号>`。
2. 记录已完成事项、实际验证、阻塞和下一步。写明本次改动与授权范围，参照 [交接样例](examples/handoff.json)。
3. 发布后续交接时，用 `--expected` 填刚读取的当前交接编号。发生冲突先重读，再合并进展。

~~~bash
python hub.py publish demo --expected 1 --from-file examples/handoff.json
python hub.py status demo --id 1
python hub.py export --all
python hub.py backup
~~~

SQLite 是交接记录的主存储，`STATUS.md` 与 `HANDOFF.md` 可以重建。成功发布后会尝试刷新两份 Markdown 并更新当天的本地备份。若返回 `mirror_warning` 或 `backup_warning`，交接已保存，修好权限或存储问题后补做导出或备份。已有交接镜像或备份而数据库缺失时，先恢复数据库，发布会阻止重开历史。

恢复时暂停写入，保留现有数据库副本，将选中的 SQLite 备份复制为 `data/projects.sqlite3`。核对 `projects` 和 `status` 后，运行 `export --all`。

## 个人记忆

在仓库根目录创建 `personal/`，把[个人主题模板](examples/personal-topic.md)复制进去，例如命名为 `interests-and-aesthetics.md`。文件名用小写英文、数字或连字符，正文按 `##` 小节维护。

~~~bash
python hub.py personal-topics
python hub.py personal interests-and-aesthetics
~~~

正文记录偏好、决定和有用的经历。如果时间是经历的一部分，例如观看电影的日期或游戏进度截至哪天，就保留日期。当事人只记得大概时间时，按实际精度记录。协作规则见 [AGENTS.md](AGENTS.md)。

## 接入本机 AI

在支持 stdio MCP 的客户端添加服务器。命令填本机 Python 的路径，参数依次是 Hub 中 `hub.py` 的绝对路径和 `mcp`。以下是 Windows 的 Codex 配置示例。把路径换成自己的，不要把本机配置提交到仓库。

~~~toml
[mcp_servers.continuity_hub]
command = "python"
args = ['C:\path\to\AI-Continuity-Hub\hub.py', "mcp"]
~~~

默认提供项目列表及交接读取。按需添加 `--include-personal` 读取普通个人主题，或添加 `--allow-project-writes` 发布交接。客户端拿到的内容可能被发送给其模型，按自己的授权范围配置。

## 可选接入 Muse

需要手机或远程助手访问 Hub 时，参阅 [Muse 桥接说明](integrations/muse/README.md) 和 [可复制提示词](integrations/muse/PROMPTS.md)。可独立启动 Hub HTTP 适配器，也可以挂到已有的 FastAPI 手机桥上。只使用本机 CLI 或 MCP 时，直接使用核心功能即可。

## 本地数据与许可证

本仓库发布框架、测试和虚构样例。`personal/`、`private/`、`projects/`、`data/` 与本机配置均由使用者自行管理，并在 Git 忽略规则中。测试用 `python -m unittest -v` 运行。

采用 [MIT 许可证](LICENSE)。
