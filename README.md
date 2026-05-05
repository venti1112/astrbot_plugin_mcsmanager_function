MCSManagerFunction - AstrBot 插件
==================================

简介
----
本插件为 AstrBot 提供一系列 MCSManager 面板的 LLM 工具函数，使 Agent 能够直接调用 MCSManager 的 API，实现实例管理、文件操作、日志查看等功能。

依赖
----
- Python 3
- aiohttp

安装前请确保已在 AstrBot 中配置好 LLM 模型，并且启用了工具调用功能。

配置
----
在 AstrBot WebUI 的插件管理中，找到 "MCSManagerFunction"，填写以下配置：

- base_url: MCSManager 面板的访问地址，例如 http://localhost:23333，请勿包含末尾斜杠。
- api_token: MCSManager 的 API 密钥，在面板左侧 "API 密钥" 中可以生成或查看。

保存配置后，插件会自动连接并注册工具。

使用方法
--------
插件加载成功后，AI 助手在对话中可自动调用以下工具（无需用户手动输入指令）。用户只需用自然语言提出需求，例如：
  "帮我启动实例 xxx"
  "查看所有实例列表"
  "获取实例日志"
  "列出游戏服务器文件"

工具列表
--------
所有工具均以 "mcsmanager_" 开头，以下是详细列表：

仪表盘 / 节点
- mcsmanager_get_overview
  获取面板全局概览（版本、CPU、内存、节点状态等）
- mcsmanager_get_daemon_list
  获取所有已连接节点列表及基本状态

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