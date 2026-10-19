# WhatSticks MCP

这是 WhatSticks 的只读、stdio MCP Server。它读取 iPhone 通过 WebDAV 发布的
`current.json`、不可变 release、JSON 分片和媒体索引；不会调用 `PUT`、`DELETE`、
`MOVE` 或 `MKCOL`，也不会把图片二进制塞进 MCP 文本输出。

运行时只依赖 Python 3.11+ 标准库，不需要 `pip install`。生产配置保存地址、用户名、
远端目录和缓存目录，第三方应用密码保存到 macOS Keychain，不写入 JSON 配置。

## 给普通用户的安装方式

普通用户不需要 WhatSticks iOS 项目源码。Codex 等 Agent 应下载 GitHub Release 中的
固定版本压缩包，校验 SHA-256 后再安装；不要下载或执行 `main` 分支上的未固定代码。

当前版本：`1.0.0`

安装包：
`https://github.com/panAtGitHub/WhatSticksMCP/releases/download/v1.0.0/WhatSticksMCP-v1.0.0.zip`

App 内复制给 Agent 的提示词会同时提供安装包地址、版本和 SHA-256。Agent 应先让用户
确认下载和安装位置，再运行包内的 `server.py`。

## 手动安装

将 Release 压缩包解压到一个固定的用户目录，例如：

```text
~/Library/Application Support/WhatSticks/MCP/1.0.0/
```

确认 Mac 有 Python 3.11 或更高版本，然后运行：

```bash
python3.11 \
  "$HOME/Library/Application Support/WhatSticks/MCP/1.0.0/server.py" \
  --setup \
  --config "$HOME/Library/Application Support/WhatSticks/mcp.json"
```

交互式 setup 会要求输入坚果云 HTTPS WebDAV 地址、用户名、专用目录和第三方应用密码。
密码只交给 macOS `security` 工具写入 Keychain；不要把密码放进 Shell 命令、配置文件或聊天记录。

配置文件形状如下（密码字段故意不存在）：

```json
{
  "webdavUrl": "https://dav.jianguoyun.com/dav/",
  "username": "your-account@example.com",
  "remoteFolder": "WhatSticks",
  "cacheDir": "~/Library/Caches/WhatSticksMCP",
  "keychainService": "com.dusk.WhatSticks.webdav"
}
```

## 注册到 Codex

```bash
codex mcp add whatsticks -- \
  python3.11 \
  "$HOME/Library/Application Support/WhatSticks/MCP/1.0.0/server.py" \
  --config "$HOME/Library/Application Support/WhatSticks/mcp.json"
codex mcp list
```

注册前可以先用 stdio 自检。日志只写 stderr，stdout 只有 JSON-RPC 响应：

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | python3.11 \
      "$HOME/Library/Application Support/WhatSticks/MCP/1.0.0/server.py" \
      --config "$HOME/Library/Application Support/WhatSticks/mcp.json"
```

在 iPhone 的 WhatSticks「更多 → 数据恢复 → WebDAV 数据镜像」中先完成连接测试和一次
镜像发布，再调用 `whatsticks_status`、`whatsticks_search_traces` 或
`whatsticks_daily_stats`。没有可用远端或 Keychain 凭据时，MCP 会返回明确错误，不会伪造空统计。

## 工具与统计口径

- `whatsticks_status`：协议版本、generation、发布设备、实体计数和缓存是否滞后。
- `whatsticks_list_projects`：项目名/备注普通子串搜索，默认排除已删除和已归档项目。
- `whatsticks_search_traces`：中文大小写无关普通子串匹配，可按项目和 UTC 时间过滤。
- `whatsticks_get_trace`：返回完整正文（有长度上限）以及媒体存在状态、内容类型、哈希和关联 ID。
- `whatsticks_project_timeline`：合并痕迹和项目活动事件，时间过滤按 UTC。
- `whatsticks_daily_stats`：`from/to` 使用本地业务日历的 `YYYY-MM-DD` 或 `YYYYMMDD`；
  每个 `practiceID + dayKey` 只取最终 `isCompleted`，完成/撤销/再完成不会累计操作次数。
- `whatsticks_list_daily_practices`：返回日课定义及项目/共享链接入口的展示名。

所有列表都有默认上限和数字 cursor；MCP 不暴露密码、Keychain 内容或本机原图路径。

## 缓存与故障排查

MCP 会按分片 SHA-256 缓存数据。新的 generation 校验失败或网络不可用时，如果存在上一次完整
缓存，查询会继续使用缓存，并在 `whatsticks_status.cache.state` 标记为 `stale`、附带错误原因；
没有完整缓存则直接报错。

- `401`：确认用户名和坚果云第三方应用密码，而不是网页登录密码。
- `403`：确认专用目录权限和地址，避免把其他目录写进配置。
- `current.json` / release 校验失败：先在 iPhone 重新完成一次镜像；旧缓存不会被不完整版本替换。
- 想清理缓存：停止 MCP 后只删除配置中的 `cacheDir`，不会删除坚果云数据；下次查询会重新下载。

## 本地测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

测试使用内存 fixture 和 GET-only mock reader，覆盖 stdio `initialize`、`tools/list`、
`tools/call`、中文搜索、分页、日课最终状态、缓存回退和校验失败。它不等价于真实坚果云
账户联调；真实账户结果必须由当前 Mac MCP 客户端和 iPhone 发布后的 generation 另行记录。
