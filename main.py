import json
from typing import Optional, Union
import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig

PERMISSION_EVERYONE = "所有人"
PERMISSION_ADMIN = "仅管理员"
PERMISSION_DISABLED = "禁用"
DEFAULT_TOOL_PERMISSION = PERMISSION_ADMIN
TOOL_PERMISSION_OPTIONS = (PERMISSION_EVERYONE, PERMISSION_ADMIN, PERMISSION_DISABLED)


@register("mcsmanager_function", "venti1112", "给 AI 使用的 MCSManager 工具函数", "1.1.0")
class MCSManagerFunction(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = self.config.get("base_url", "").rstrip("/")
        self.api_token = self.config.get("api_token", "")
        logger.info(f"MCSManager 地址: {self.base_url}")
        logger.info("MCSManager API 令牌: 已设置" if self.api_token else "MCSManager API 令牌: 未设置")

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
        raw_text: bool = False,
    ) -> Union[dict, str]:
        """
        统一的 API 请求封装。
        raw_text=True 时直接返回原始文本；否则尝试解析 JSON，失败则返回文本并警告。
        无论何种情况，均会在 DEBUG 级别打印原始响应体。
        """
        url = f"{self.base_url}{path}"
        if params is None:
            params = {}
        params["apikey"] = self.api_token

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json; charset=utf-8",
        }

        query_string = "&".join([f"{k}={v}" for k, v in params.items()])
        full_url = f"{url}?{query_string}"
        logger.debug(f"最终请求地址: {full_url}")
        logger.debug(f"请求头: {headers}")

        async with aiohttp.ClientSession() as session:
            try:
                async with session.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_data,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    # 读取原始响应文本
                    resp_text = await resp.text()
                    logger.debug(f"原始响应体:\n{resp_text}")

                    if resp.status == 200:
                        if raw_text:
                            return resp_text
                        # 尝试按 JSON 解析
                        try:
                            return json.loads(resp_text)
                        except json.JSONDecodeError:
                            logger.warning(
                                f"响应不是 JSON (Content-Type: {resp.content_type})，将返回原始文本"
                            )
                            return resp_text
                    else:
                        return {
                            "error": f"HTTP {resp.status}",
                            "detail": resp_text,
                        }
            except Exception as e:
                logger.error(f"MCSManager API 请求失败: {e}")
                return {"error": str(e)}

    # --- 权限与限制 ---
    def _blocked_instance_keywords(self) -> list[str]:
        blocked = self.config.get("blocked_instances", [])
        if not isinstance(blocked, list):
            return []
        return [str(item).strip().lower() for item in blocked if str(item).strip()]

    def _tool_permission(self, tool_name: str) -> str:
        permissions = self.config.get("tool_permissions", {})
        if not isinstance(permissions, dict):
            return DEFAULT_TOOL_PERMISSION
        permission = permissions.get(tool_name, DEFAULT_TOOL_PERMISSION)
        if permission not in TOOL_PERMISSION_OPTIONS:
            return DEFAULT_TOOL_PERMISSION
        return permission

    def _check_tool_permission(
        self, event: AstrMessageEvent, tool_name: str
    ) -> Optional[str]:
        """根据工具权限配置校验调用者。放行返回 None，否则返回拒绝信息。"""
        permission = self._tool_permission(tool_name)
        if permission == PERMISSION_EVERYONE:
            return None
        sender_id = event.get_sender_id()
        if permission == PERMISSION_DISABLED:
            logger.warning(f"工具 {tool_name} 已被禁用，拒绝用户 {sender_id} 的调用")
            return json.dumps(
                {
                    "error": "Tool disabled",
                    "detail": (
                        f"工具 {tool_name} 已被管理员禁用，任何人（包括管理员）都无法使用。"
                    ),
                },
                ensure_ascii=False,
            )
        if event.is_admin():
            return None
        logger.warning(f"非管理员用户 {sender_id} 尝试调用受限工具 {tool_name}，已拒绝")
        return json.dumps(
            {
                "error": "Permission denied",
                "detail": (
                    f"该操作仅限 AstrBot 管理员使用（当前用户 ID: {sender_id}）。"
                    "请让管理员执行，或由管理员将你的 ID 加入 AstrBot 管理员列表。"
                ),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _extract_instance_name(payload) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        config = data.get("config")
        if isinstance(config, dict) and isinstance(config.get("nickname"), str):
            return config["nickname"]
        nickname = data.get("nickname")
        if isinstance(nickname, str) and nickname:
            return nickname
        return None

    @staticmethod
    def _blocked_instance_message(uuid: str, name: Optional[str] = None) -> str:
        target = f"{name} ({uuid})" if name else uuid
        return json.dumps(
            {
                "error": "Instance blocked",
                "detail": f"实例 {target} 已被列入禁止操作的名单，本次操作已被拒绝。",
            },
            ensure_ascii=False,
        )

    async def _check_instance_allowed(self, daemon_id: str, uuid: str) -> Optional[str]:
        """校验目标实例是否在禁止名单中。放行返回 None，否则返回拒绝信息。"""
        keywords = self._blocked_instance_keywords()
        if not keywords:
            return None
        if uuid and uuid.strip().lower() in keywords:
            return self._blocked_instance_message(uuid)
        detail = await self._request(
            "GET", "/api/instance", params={"daemonId": daemon_id, "uuid": uuid}
        )
        name = self._extract_instance_name(detail)
        if name is None:
            logger.warning(f"无法获取实例 {uuid} 的名称，禁止名单校验失败，保守拒绝本次操作")
            return json.dumps(
                {
                    "error": "Instance check failed",
                    "detail": (
                        f"无法确认实例 {uuid} 是否在禁止名单中，已保守拒绝本次操作。"
                        "请检查 MCSManager 连接后重试。"
                    ),
                },
                ensure_ascii=False,
            )
        lowered = name.strip().lower()
        if any(keyword in lowered for keyword in keywords):
            return self._blocked_instance_message(uuid, name)
        return None

    async def _check_restrictions(
        self,
        event: AstrMessageEvent,
        tool_name: str,
        daemon_id: Optional[str] = None,
        uuid: Optional[str] = None,
    ) -> Optional[str]:
        """工具调用前的统一校验：管理员权限，以及（传入实例时）实例禁止名单。"""
        if err := self._check_tool_permission(event, tool_name):
            return err
        if daemon_id is not None and uuid is not None:
            return await self._check_instance_allowed(daemon_id, uuid)
        return None

    def _filter_blocked_instances(self, payload):
        keywords = self._blocked_instance_keywords()
        if not keywords or not isinstance(payload, dict):
            return payload
        data = payload.get("data")
        if not isinstance(data, dict):
            return payload
        items = data.get("data")
        if not isinstance(items, list):
            return payload
        filtered = []
        for item in items:
            if not isinstance(item, dict):
                filtered.append(item)
                continue
            uuid = str(item.get("instanceUuid") or "").strip().lower()
            config = item.get("config")
            name = ""
            if isinstance(config, dict) and isinstance(config.get("nickname"), str):
                name = config["nickname"].strip().lower()
            if uuid and uuid in keywords:
                continue
            if name and any(keyword in name for keyword in keywords):
                continue
            filtered.append(item)
        data["data"] = filtered
        return payload

    # --- 仪表盘 / 节点 ---
    @filter.llm_tool(name="mcsmanager_get_overview")
    async def mcsmanager_get_overview(self, event: AstrMessageEvent) -> str:
        """获取 MCSManager 面板的全局概览信息，包括版本、CPU/内存、节点状态等。"""
        if err := await self._check_restrictions(event, "mcsmanager_get_overview"):
            return err
        data = await self._request("GET", "/api/overview")
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_get_daemon_list")
    async def mcsmanager_get_daemon_list(self, event: AstrMessageEvent) -> str:
        """获取所有已连接的节点（守护进程）列表及其基本状态。"""
        if err := await self._check_restrictions(event, "mcsmanager_get_daemon_list"):
            return err
        data = await self._request("GET", "/api/overview")
        remotes = data.get("data", {}).get("remote", [])
        return json.dumps(remotes, ensure_ascii=False, indent=2)

    # --- 实例管理 ---
    @filter.llm_tool(name="mcsmanager_get_instances")
    async def mcsmanager_get_instances(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        page: int = 1,
        page_size: int = 10,
        instance_name: Optional[str] = None,
        status: Optional[str] = None,
    ) -> str:
        """获取指定节点上的实例列表。

        Args:
            daemon_id(string): 节点 ID（UUID）。
            page(int): 页码，默认 1。
            page_size(int): 每页数量，默认 10。
            instance_name(string, optional): 按实例名称模糊搜索。
            status(string, optional): 按状态过滤（running, stopped 等）。
        """
        if err := await self._check_restrictions(event, "mcsmanager_get_instances"):
            return err
        params = {
            "daemonId": daemon_id,
            "page": page,
            "page_size": page_size,
        }
        if instance_name:
            params["instance_name"] = instance_name
        if status:
            params["status"] = status
        data = await self._request(
            "GET", "/api/service/remote_service_instances", params=params
        )
        data = self._filter_blocked_instances(data)
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_get_instance_detail")
    async def mcsmanager_get_instance_detail(
        self, event: AstrMessageEvent, daemon_id: str, uuid: str
    ) -> str:
        """获取指定实例的详细信息（配置、进程信息、状态等）。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_get_instance_detail", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "GET", "/api/instance", params={"daemonId": daemon_id, "uuid": uuid}
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_start_instance")
    async def mcsmanager_start_instance(
        self, event: AstrMessageEvent, daemon_id: str, uuid: str
    ) -> str:
        """启动一个实例。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_start_instance", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "GET",
            "/api/protected_instance/open",
            params={"daemonId": daemon_id, "uuid": uuid},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_stop_instance")
    async def mcsmanager_stop_instance(
        self, event: AstrMessageEvent, daemon_id: str, uuid: str
    ) -> str:
        """停止一个实例。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_stop_instance", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "GET",
            "/api/protected_instance/stop",
            params={"daemonId": daemon_id, "uuid": uuid},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_restart_instance")
    async def mcsmanager_restart_instance(
        self, event: AstrMessageEvent, daemon_id: str, uuid: str
    ) -> str:
        """重启一个实例。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_restart_instance", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "GET",
            "/api/protected_instance/restart",
            params={"daemonId": daemon_id, "uuid": uuid},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_kill_instance")
    async def mcsmanager_kill_instance(
        self, event: AstrMessageEvent, daemon_id: str, uuid: str
    ) -> str:
        """强制终止实例进程。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_kill_instance", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "GET",
            "/api/protected_instance/kill",
            params={"daemonId": daemon_id, "uuid": uuid},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_send_command")
    async def mcsmanager_send_command(
        self, event: AstrMessageEvent, daemon_id: str, uuid: str, command: str
    ) -> str:
        """向运行中实例的控制台发送一条命令。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            command(string): 要执行的命令。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_send_command", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "GET",
            "/api/protected_instance/command",
            params={"daemonId": daemon_id, "uuid": uuid, "command": command},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_get_instance_log")
    async def mcsmanager_get_instance_log(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        uuid: str,
        size: Optional[int] = None,
    ) -> str:
        """获取实例最近的输出日志（纯文本）。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            size(int, optional): 日志大小（KB），不填则返回全部。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_get_instance_log", daemon_id, uuid
        ):
            return err
        params = {"daemonId": daemon_id, "uuid": uuid}
        if size is not None:
            params["size"] = size
        # raw_text=True 因为该端点返回的是纯文本日志
        data = await self._request(
            "GET", "/api/protected_instance/outputlog", params=params, raw_text=True
        )
        if isinstance(data, dict):
            return json.dumps(data, ensure_ascii=False, indent=2)
        return data

    # --- 文件管理 ---
    @filter.llm_tool(name="mcsmanager_list_files")
    async def mcsmanager_list_files(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        uuid: str,
        target: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> str:
        """列出实例指定目录下的文件和文件夹。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            target(string): 目录路径，相对于实例根目录，例如 "mods" 表示实例根目录下的 mods 文件夹。留空或传 "." 表示根目录。请勿以 "/" 开头。
            page(int): 页码，默认 1。
            page_size(int): 每页数量，默认 50。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_list_files", daemon_id, uuid
        ):
            return err
        # 修复：守护进程要求 target 不能以 / 开头，否则会导致路径重复叠加（如 /data/.../data/...）
        # 将其规范化为相对路径
        target = target.strip()
        if target in ("/", "."):
            target = ""
        elif target.startswith("/"):
            target = target[1:]  # 去掉开头的 /

        data = await self._request(
            "GET",
            "/api/files/list",
            params={
                "daemonId": daemon_id,
                "uuid": uuid,
                "target": target,
                "page": page,
                "page_size": page_size,
            },
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_read_file")
    async def mcsmanager_read_file(
        self, event: AstrMessageEvent, daemon_id: str, uuid: str, target: str
    ) -> str:
        """读取实例中某个文本文件的内容。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            target(string): 文件完整路径。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_read_file", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "PUT",
            "/api/files/",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"target": target},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_write_file")
    async def mcsmanager_write_file(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        uuid: str,
        target: str,
        text: str,
    ) -> str:
        """向实例的文件中写入或更新内容（会覆盖原内容）。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            target(string): 文件完整路径。
            text(string): 要写入的文本内容。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_write_file", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "PUT",
            "/api/files/",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"target": target, "text": text},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_delete_files")
    async def mcsmanager_delete_files(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        uuid: str,
        targets: list[str],
    ) -> str:
        """删除实例中的文件或文件夹（可批量）。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            targets(list): 要删除的文件或文件夹路径列表，例如 ["/a.txt", "/dir"]。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_delete_files", daemon_id, uuid
        ):
            return err
        root_targets = [
            target for target in targets if target.strip().strip("/") in ("", ".")
        ]
        if root_targets:
            return json.dumps(
                {
                    "error": "Dangerous operation rejected",
                    "detail": f"禁止删除实例根目录，已拒绝目标: {root_targets}",
                },
                ensure_ascii=False,
            )
        data = await self._request(
            "DELETE",
            "/api/files",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"targets": targets},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_create_folder")
    async def mcsmanager_create_folder(
        self, event: AstrMessageEvent, daemon_id: str, uuid: str, target: str
    ) -> str:
        """在实例中新建一个文件夹。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            target(string): 要创建的文件夹路径。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_create_folder", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "POST",
            "/api/files/mkdir",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"target": target},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_compress_files")
    async def mcsmanager_compress_files(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        uuid: str,
        source: str,
        targets: list[str],
    ) -> str:
        """将实例中的文件或文件夹压缩为一个 zip 文件。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            source(string): 压缩包保存路径，例如 "/backup.zip"。
            targets(list): 要压缩的文件或文件夹路径列表。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_compress_files", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "POST",
            "/api/files/compress",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"type": 1, "code": "utf-8", "source": source, "targets": targets},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_decompress_file")
    async def mcsmanager_decompress_file(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        uuid: str,
        source: str,
        targets: str,
    ) -> str:
        """将压缩文件解压到指定目录。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            source(string): 压缩包路径，例如 "/backup.zip"。
            targets(string): 解压目标文件夹路径，例如 "/restore/"。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_decompress_file", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "POST",
            "/api/files/compress",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"type": 2, "code": "utf-8", "source": source, "targets": targets},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_copy_files")
    async def mcsmanager_copy_files(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        uuid: str,
        targets: list[list[str]],
    ) -> str:
        """复制文件或文件夹。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            targets(list): 源路径与目标路径的列表，例如 [["/a.txt", "/backup/a.txt"]]。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_copy_files", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "POST",
            "/api/files/copy",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"targets": targets},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    @filter.llm_tool(name="mcsmanager_move_files")
    async def mcsmanager_move_files(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        uuid: str,
        targets: list[list[str]],
    ) -> str:
        """移动或重命名文件/文件夹。

        Args:
            daemon_id(string): 节点 ID。
            uuid(string): 实例 UUID。
            targets(list): 旧路径与新路径的列表，例如 [["/old.txt", "/new.txt"]]。
        """
        if err := await self._check_restrictions(
            event, "mcsmanager_move_files", daemon_id, uuid
        ):
            return err
        data = await self._request(
            "PUT",
            "/api/files/move",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"targets": targets},
        )
        return json.dumps(data, ensure_ascii=False, indent=2)

    async def initialize(self):
        logger.info("MCSManagerFunction 插件已加载")

    async def terminate(self):
        logger.info("MCSManagerFunction 插件已卸载")