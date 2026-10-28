MCSManagerFunction - AstrBot 插件
==================================

简介
----
本插件为 AstrBot 提供一系列 MCSManager 面板的工具函数，使 Agent 能够直接调用 MCSManager 的 API，实现实例管理、文件操作、日志查看、节点终端命令执行等功能。

依赖
----
- Python 3
- aiohttp

安装前请确保已在 AstrBot 中配置好 LLM 模型，并且启用了工具调用功能。

配置
----
在 AstrBot WebUI 的插件管理中，找到 "MCSManagerFunction"，填写以下配置：

- base_url: MCSManager 面板的访问地址，例如 http://localhost:23333，请勿包含末尾斜杠。
- api_token: MCSManager 的 API 密钥，在面板的用户管理中，在 "API 密钥" 中可以生成或查看。
- tool_permissions: 为每个工具单独设置权限（「所有人」「仅管理员」「禁用」），默认「仅管理员」，详见「权限与限制」。
- blocked_instances: 禁止操作的实例（UUID 或名称关键词），详见「权限与限制」。
- bash_timeout: 节点命令的默认超时秒数（5~300，默认 30）。
- bash_max_output: 节点命令返回的输出上限字符数（默认 8000，超出后保留首尾并截断中间）。
- bash_blocked_commands: 禁止执行的命令规则（正则，忽略大小写；正则非法时按子串匹配），默认空。

保存配置后，插件会自动连接并注册工具。

权限与限制
----------
插件在调用 MCSManager API 前会执行以下校验（硬性限制，对 AI 的工具调用同样生效）：

工具权限（tool_permissions）
- 每个工具都可以单独设置为以下三种权限之一：
  - 所有人：任何用户都可以调用。
  - 仅管理员（默认）：仅 AstrBot 管理员可用。
  - 禁用：任何人都不能使用（包括管理员），调用会被拒绝。
- 「管理员」指 AstrBot 全局配置中的管理员（WebUI → 配置 → 管理员 ID）。
- 非管理员调用「仅管理员」工具时，工具会返回权限不足的提示，AI 会据此告知用户。

实例禁止名单（blocked_instances）
- 填写实例 UUID 或名称关键词（子串匹配，忽略大小写），每行一个。
- 被列入的实例会从实例列表中隐藏，且拒绝一切针对它的操作（对管理员同样生效）。
- 禁止名单非空时，针对实例的操作会先查询实例详情以解析名称，无法确认时保守拒绝。
- 填写节点 ID 可封锁该节点：`mcsmanager_node_bash` 会直接拒绝在该节点上执行命令。

节点命令黑名单（bash_blocked_commands）
- 针对 `mcsmanager_node_bash`，每行一条规则，默认空（即不做命令级限制）。
- 规则按正则匹配（忽略大小写），例如 `rm\s+-rf\s+/`、`^(reboot|shutdown)$`；正则非法时退化为子串匹配。
- 命中规则的命令会在发给节点之前被拒绝，不会产生任何执行副作用。

节点终端（mcsmanager_node_bash）的行为说明
- 依赖面板的「节点终端」：daemon 侧是一个固定实例（UUID `global0001`，昵称 `__MCSM_GLOBAL_INSTANCE__`，启动命令 bash，默认 pty 模式）。
- 终端未运行时插件会自动拉起；pty 下的 bash 在初始化阶段会吞掉早到的输入，所以冷启动后会先用无副作用的探针确认终端真的能执行命令，再发实际命令（冷启动约多花 1~2 秒）。
- 每次执行的脚本都会先写入节点 `/tmp/mcsm_ai_<token>.sh`，再在终端里 `source` 它：pty 只会回显这一行，因此输出干净；`source` 在当前 shell 内执行，所以 `cd`、`export` 仍会跨调用保留。执行结束后脚本会被删除。
- 若 API 密钥用户没有该节点的文件读写权限，会自动降级为把脚本直接写进终端（此时 pty 会把命令原文回显进输出）；超过 3500 字符的命令无法降级，会直接报错要求拆分。
- 输出通过「打印起止标记 + 轮询终端日志」的方式捕获，因此不支持交互式命令（如 `vim`、`top`、需要 stdin 的 `read`）。需要长驻进程请自行 `nohup ... &`。
- 命令里执行 `exit` 会让节点终端退出，下次调用会自动重新拉起。

删除保护
- mcsmanager_delete_files 始终拒绝删除实例根目录（空路径、"/"、"." 等目标）。

使用方法
--------
插件加载成功后，AI 助手在对话中可自动调用以下工具（无需用户手动输入指令）。默认仅 AstrBot
管理员可用，可在配置中调整（见「权限与限制」）。用户只需用自然语言提出需求，例如：
  "帮我启动实例 xxx"
  "查看所有实例列表"
  "获取实例日志"
  "列出游戏服务器文件"
  "在这个节点上建个 python 虚拟环境，把 MCDR 部署到实例目录"
  "实例卡住了，dump 一下 java 堆栈看看"

工具列表
--------
所有工具均以 "mcsmanager_" 开头，以下是详细列表：

仪表盘 / 节点
- mcsmanager_get_overview
  获取面板全局概览（版本、CPU、内存、节点状态等）
- mcsmanager_get_daemon_list
  获取所有已连接节点列表及基本状态

节点终端
- mcsmanager_node_bash
  在节点主机上执行 bash 命令，返回输出与退出码（详见「节点终端的行为说明」）
  参数：daemon_id, command, workdir(可选，绝对路径), timeout(可选，5~300 秒)
  示例：创建虚拟环境 `python3 -m venv .venv`、部署 MCDR、用 `jstack`/`top` 诊断整合包卡顿

实例管理
- mcsmanager_get_instances
  获取指定节点上的实例列表
  参数：daemon_id, page, page_size, instance_name(可选), status(可选)
- mcsmanager_get_instance_detail
  获取实例详细信息（配置、进程信息等）
  参数：daemon_id, uuid
- mcsmanager_start_instance
  启动实例
- mcsmanager_stop_instance
  停止实例
- mcsmanager_restart_instance
  重启实例
- mcsmanager_kill_instance
  强制终止实例进程
- mcsmanager_send_command
  向运行中的实例控制台发送命令
  参数：daemon_id, uuid, command
- mcsmanager_get_instance_log
  获取实例最近的输出日志（纯文本）
  参数：daemon_id, uuid, size(可选，单位KB)

文件管理
- mcsmanager_list_files
  列出实例指定目录下的文件和文件夹
  参数：daemon_id, uuid, target, page, page_size
  注意：target 应使用相对于实例根目录的路径，不要以 "/" 开头，
  根目录传空字符串或 "."
- mcsmanager_read_file
  读取文本文件内容
  参数：daemon_id, uuid, target
- mcsmanager_write_file
  写入或覆盖文件内容
  参数：daemon_id, uuid, target, text
- mcsmanager_delete_files
  删除文件或文件夹（可批量）
  参数：daemon_id, uuid, targets(列表)
- mcsmanager_create_folder
  新建文件夹
  参数：daemon_id, uuid, target
- mcsmanager_compress_files
  压缩文件/文件夹为 zip
  参数：daemon_id, uuid, source, targets(列表)
- mcsmanager_decompress_file
  解压 zip 文件
  参数：daemon_id, uuid, source, targets(字符串)
- mcsmanager_copy_files
  复制文件/文件夹
  参数：daemon_id, uuid, targets(二维列表)
- mcsmanager_move_files
  移动或重命名文件/文件夹
  参数：daemon_id, uuid, targets(二维列表)

更新日志
--------
各版本的变更记录见 [CHANGELOG.md](CHANGELOG.md)。

致谢
----
- 感谢 MCSManager 项目 (https://github.com/MCSManager/MCSManager) 提供优秀的开源游戏服务器管理面板。
- 感谢 AstrBot 项目 (https://github.com/AstrBotDevs/AstrBot) 提供灵活的消息机器人框架。

许可证
------
本项目采用 GNU AFFERO GENERAL PUBLIC LICENSE Version 3 (AGPL-3.0) 协议开源。
详见 LICENSE 文件或 https://www.gnu.org/licenses/agpl-3.0.html。

问题反馈
--------
如遇到问题，请通过 GitHub Issue 反馈，并附上 AstrBot 日志（尤其是 DEBUG 级别日志）以及 MCSManager 版本信息。