# WhatSticks MCP

这是 WhatSticks 的只读、stdio MCP Server。它读取 iPhone 通过 WebDAV 发布的
`current.json`、不可变 release、JSON 分片和媒体索引；不会调用 `PUT`、`DELETE`、
`MOVE` 或 `MKCOL`，也不会把图片二进制塞进 MCP 文本输出。

运行时只依赖 Python 3.9+ 标准库，不需要 `pip install`。生产配置保存地址、用户名、
远端目录和缓存目录，第三方应用密码不写入 JSON 配置，而是保存到宿主系统的安全凭据存储。

## 先分清三种位置

这个仓库发布的是一个独立的本地 stdio Server，不是某个 Agent 的插件包，也不规定某个
Agent 的配置文件路径。下面三类位置由不同层负责：

1. **Server 文件**：Release 压缩包解压后的目录。应由用户或 Agent 选择当前运行环境中
   合适的用户级目录；不存在所有 Agent 共用的通用安装目录。
2. **WhatSticks 运行配置**：通过 `server.py --config` 指定的 JSON 文件。它保存 WebDAV
   地址、用户名、远端目录和缓存目录，不是 Agent 的 MCP 注册表，也不保存密码。
3. **Agent 注册信息**：由当前 Agent 自己管理。不同 Agent 可能使用命令、设置页面、插件
   包或远程 HTTPS 连接；本仓库不把其中任何一种写成通用规则。

如果当前 Agent 不支持本地 stdio Server，而只支持远程 MCP，那么仅仅下载并解压这个 ZIP
不能完成接入。Agent 应明确告诉用户需要该产品支持的插件/本地执行方式，或部署一个受支持
的远程 MCP endpoint，不应伪造“已经注册成功”。

## 当前版本兼容性

- Python 3.9 或更高版本。当前实现已在 Python 3.9.6 验证；3.8 及更低版本暂不声明支持。
- macOS 使用 Keychain，Windows 使用 Windows Credential Manager 保存第三方应用密码；密码不写入
  JSON 配置、命令参数或日志。
- v1.0.0 的 `--setup` 暂只支持 macOS 和 Windows。其他系统必须先由 Agent 报告不兼容，不能
  用明文文件或未说明的替代方案绕过安全凭据存储。
- 当前版本是本地 stdio Server。目标 Agent 是否能使用它，必须由 Agent 根据自身官方文档
  和能力判断。

## 给普通用户和 Agent 的安装流程

普通用户只需要在 iPhone 上使用 WhatSticks，并从 App 复制配置提示词。提示词会提供固定
Release 的版本、下载地址、SHA-256、WebDAV 地址、远端目录和（如果用户愿意提供的）WebDAV
用户名；第三方应用密码永远不进入提示词。

Agent 应按以下顺序处理：

1. 先识别自身的产品、版本、运行环境和 MCP 传输能力，并只读取该 Agent 的官方注册说明。
2. 从固定 Release 地址下载 ZIP，校验 SHA-256；不要下载或执行 `main` 分支或其他地址的
   未固定代码。
3. 选择用户级 Server 安装目录，并单独选择运行配置文件路径；不要把其中任何路径冒充成
   Agent 的官方目录。
4. 运行包内正式的 `--setup` 流程。提示词中已给出的 WebDAV 地址、用户名和远端目录，以及 Agent
   选择的用户级缓存目录，应作为非敏感参数传给 `setup`，避免让普通用户重复填写。安装包会根据宿主系统选择受支持的
   安全凭据存储；第三方应用密码只能在 Agent 提供的安全交互式输入界面中输入，不得进入聊天、
   命令参数、Shell 历史、日志或明文配置。
5. 使用当前 Agent 原生支持的 MCP 注册方式；如果它不支持本地 stdio，停止并说明原因。
6. 验证 `initialize`、`tools/list` 和 `whatsticks_status`，最后报告实际运行环境、Server
   目录、运行配置路径、注册结果和验证结果。

如果 Agent 能启动并控制安全的交互式进程，应自动执行下载、校验、解压、setup、MCP 注册和验证，
只在密码输入处暂停。密码输入结束后，Agent 必须等待 setup 进程退出、检查返回码和实际生成的配置，
然后继续注册和验证；用户不需要再回复“setup 完成”。如果 Agent 无法提供安全的交互式输入，
它应明确告诉用户在哪个终端或控制台输入，而不是声称已经完成。

当前版本：`1.0.0`

安装包：
`https://github.com/panAtGitHub/WhatSticksMCP/releases/download/v1.0.0/WhatSticksMCP-v1.0.0.zip`

## 手动运行 setup

下面只是 Server 的运行形态，不是任何 Agent 的注册命令。`<Python 3.9+ executable>`、
`<server.py path>` 和 `<config path>` 都必须由当前 Agent 根据实际环境替换；这段参数示意
不预设操作系统或 Shell：

```text
<Python 3.9+ executable> \
  <server.py path> \
  --setup \
  --config <config path>
```

交互式 setup 会要求输入尚未通过参数提供的坚果云 HTTPS WebDAV 地址、用户名、专用目录和缓存目录，
以及第三方应用密码，并把密码写入当前系统支持的安全凭据存储。Agent 可以通过下面这些非敏感
参数预填前三项，缓存目录由 Agent 选择；`--password` 故意不存在，密码只能由 setup 在隐藏输入中读取：

```text
--webdav-url <https WebDAV 地址>
--username <WebDAV 用户名>
--remote-folder <WhatSticks 远端目录>
--cache-dir <用户级缓存目录>
```

不要把密码放进 Shell 命令、配置文件或聊天记录。以上参数只是 WhatSticks Server 的参数，
不是任何特定 Agent 的注册命令；实际 Agent 应使用自己的进程启动和安全输入能力。

配置文件形状如下（密码字段故意不存在）：

```json
{
  "webdavUrl": "https://dav.jianguoyun.com/dav/",
  "username": "your-account@example.com",
  "remoteFolder": "WhatSticks",
  "cacheDir": "<platform-appropriate-user-cache-directory>",
  "credentialService": "com.dusk.WhatSticks.webdav"
}
```

## 注册到当前 Agent

本节不提供固定命令或固定配置路径。对支持本地 stdio 的 Agent，注册信息的逻辑形态是：

```text
transport: stdio
command: <Python 3.9 or newer executable>
args:
  - <path to server.py>
  - --config
  - <path to whatsticks-runtime.json>
name: whatsticks
```

实际字段名、设置入口、插件格式和配置位置必须以当前 Agent 的官方文档为准。不要把上述
逻辑形态直接当作某个产品的可复制配置。

注册前可以先用 stdio 自检。日志只写 stderr，stdout 只有 JSON-RPC 响应。下面是 POSIX Shell
示意；Windows 或其他环境应由当前 Agent 使用等价的进程调用方式：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | <Python 3.9+ executable> \
      <server.py path> \
      --config <config path>
```

在 iPhone 的 WhatSticks「更多 → 数据恢复 → WebDAV 数据镜像」中先完成连接测试和一次
镜像发布，再调用 `whatsticks_status`、`whatsticks_search_traces` 或
`whatsticks_daily_stats`。没有可用远端或凭据时，MCP 会返回明确错误，不会伪造空统计。

## 工具与统计口径

- `whatsticks_status`：协议版本、generation、发布设备、实体计数和缓存是否滞后。
- `whatsticks_list_projects`：项目名/备注普通子串搜索，默认排除已删除和已归档项目。
- `whatsticks_search_traces`：中文大小写无关普通子串匹配，可按项目和 UTC 时间过滤。
- `whatsticks_get_trace`：返回完整正文（有长度上限）以及媒体存在状态、内容类型、哈希和关联 ID。
- `whatsticks_project_timeline`：合并痕迹和项目活动事件，时间过滤按 UTC。
- `whatsticks_daily_stats`：`from/to` 使用本地业务日历的 `YYYY-MM-DD` 或 `YYYYMMDD`；
  每个 `practiceID + dayKey` 只取最终 `isCompleted`，完成/撤销/再完成不会累计操作次数。
- `whatsticks_list_daily_practices`：返回日课定义及项目/共享链接入口的展示名。

所有列表都有默认上限和数字 cursor；MCP 不暴露密码、凭据存储内容或本机原图路径。

## 缓存与故障排查

MCP 会按分片 SHA-256 缓存数据。新的 generation 校验失败或网络不可用时，如果存在上一次完整
缓存，查询会继续使用缓存，并在 `whatsticks_status.cache.state` 标记为 `stale`、附带错误原因；
没有完整缓存则直接报错。

- `401`：确认用户名和坚果云第三方应用密码，而不是网页登录密码。
- `403`：确认专用目录权限和地址，避免把其他目录写进配置。
- `current.json` / release 校验失败：先在 iPhone 重新完成一次镜像；旧缓存不会被不完整版本替换。
- 想清理缓存：停止 MCP 后只删除配置中的 `cacheDir`，不会删除坚果云数据；下次查询会重新下载。

## 本地测试

```text
<Python 3.9+ executable> -m unittest discover -s tests -p 'test_*.py'
```

测试使用内存 fixture 和 GET-only mock reader，覆盖 stdio `initialize`、`tools/list`、
`tools/call`、中文搜索、分页、日课最终状态、缓存回退和校验失败。它不等价于真实坚果云
账户联调；真实账户结果必须由当前 Agent 和 iPhone 发布后的 generation 另行记录。
