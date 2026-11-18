# Muse 可选桥接

Muse 或兼容助手要通过 HTTP 读取和写回电脑上的 Hub 时，可以接这个适配器。本机 CLI 和 stdio MCP 不需要它。桥接运行在保存 Hub 数据的电脑上，电脑休眠或关机时需要等待恢复。

## 哪些代码属于桥接

| 文件 | 职责 |
| --- | --- |
| [server.py](server.py) | 可独立运行的 FastAPI 入口。挂载路由并校验 Bearer token。 |
| [hub_status.py](hub_status.py) | 固定项目的状态和写入路由，以及调用 Hub CLI 的公共逻辑。正常接入也可走下面的通用项目路由。 |
| [hub_workspace.py](hub_workspace.py) | 工作区文件、项目列表和交接、个人记忆按小节新增或精确更正。文件读取只允许公开给桥接的工作区入口，后台写入记录和数据库文件不在列表里。 |
| [核心 hub.py](../../hub.py) | 由桥接调用的本地 Hub CLI，负责交接版本检查、SQLite、导出及备份。 |

若已经在使用自己的 FastAPI 手机桥，`bridge_server.py` 一类的宿主负责认证、入口和手机操作。复制两个路由模块到宿主项目，挂载时保留宿主的认证依赖。此目录提供 Hub 适配器和独立运行方式；手机屏幕、点击、ADB 等能力由原手机桥提供。

~~~python
from fastapi import Depends
from hub_status import router as hub_status_router
from hub_workspace import router as hub_workspace_router

app.include_router(hub_status_router, dependencies=[Depends(require_auth)])
app.include_router(hub_workspace_router, dependencies=[Depends(require_auth)])
~~~

## 在本机启动

先在仓库根目录建项目内虚拟环境，安装可选依赖。示例使用 Windows PowerShell。

~~~powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r integrations/muse/requirements.txt
$env:BRIDGE_TOKEN = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
.\.venv\Scripts\python.exe -m uvicorn server:app --app-dir integrations/muse --host 127.0.0.1 --port 8765
~~~

Linux 可用 `python3 -m venv .venv` 创建环境，再用 `.venv/bin/python` 执行对应命令。令牌通过客户端的凭据配置传入，HTTP 请求使用 `Authorization: Bearer <令牌>`。不要把实际令牌写进提示词、文档或 Git。

| 设置 | 用途 |
| --- | --- |
| `HUB_ROOT` | Hub 的绝对路径。独立版默认使用仓库根。复制路由到已有桥接项目时请显式配置。 |
| `HUB_PROJECT` | 兼容接口 `/hub/status` 和 `/hub/handoff` 的项目代号，默认 `demo`。 |
| `BRIDGE_TOKEN` | 独立入口的访问令牌。 |
| `MUSE_HUB_PEER_IPS` | 工作区接口接受的实际客户端 IP，空格分隔。独立版默认允许本机回环。 |
| `HUB_PYTHON` | 可选，用于运行核心 Hub 的 Python 解释器。WSL 桥接若访问由 Windows 写入的 Hub，可配置 Windows Python 的 WSL 可执行路径。 |

先访问 `http://127.0.0.1:8765/health` 测试存活，再用 `/openapi.json` 查看工具规格。跨设备使用时，按自己的环境配置可信的私有网络或身份验证代理。核实代理转发后的实际来源与允许列表，把令牌和可达地址填入客户端的工具配置；桥接提示词本身不会开启网络连接。

## 接口与测试顺序

1. `GET /hub/workspace/projects` 列项目，再用 `GET /hub/workspace/projects/{project}` 查当前交接。写回用 `POST /hub/workspace/projects/{project}/handoffs` 与读到的 `expected_id`。新项目传 `null`。
2. `GET /hub/workspace/files?path=personal` 列普通个人主题，用 `GET /hub/workspace/file?path=personal/<topic>.md` 读取某个主题。私密文件留在 `private/`。
3. `POST /hub/workspace/personal/{topic}/notes` 在已有小节写入。请求含 `entry_id`、`section`、`content`。更正完整旧行时另传 `replace_text`。同一次操作重试沿用原 UUID。
4. 写入后读回目标。项目冲突时先重读当前编号。记忆旧行找不到或多处匹配时先核对。接口会把作者和实际时间记在本机后台，正文只写所提交的内容。

[桥接与日常使用提示词](PROMPTS.md)可以直接复制到支持自定义工具的助手。兼容性取决于客户端是否支持相应的 HTTP 工具和认证方式。
