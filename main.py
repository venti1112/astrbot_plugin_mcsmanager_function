import asyncio
import json
import re
import shlex
import time
from typing import Optional, Union
from uuid import uuid4
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

# MCSManager 面板的「节点终端」在 daemon 侧是一个固定的特殊实例（type=universal，
# startCommand=bash，cwd=/），uuid 与昵称固定，靠普通实例接口即可驱动。
NODE_TERMINAL_UUID = "global0001"
NODE_TERMINAL_NAME = "__MCSM_GLOBAL_INSTANCE__"

INSTANCE_STATUS_STOP = 0
INSTANCE_STATUS_STOPPING = 1
INSTANCE_STATUS_STARTING = 2
INSTANCE_STATUS_RUNNING = 3

# 节点终端启动等待上限（秒）
NODE_TERMINAL_BOOT_TIMEOUT = 20
# 可直接塞进 URL 查询串的脚本长度上限，超过则先落盘为临时脚本再执行
BASH_INLINE_LIMIT = 3500
# 读取节点终端日志时的窗口大小（MCSManager 侧日志文件上限约 512KB）
BASH_LOG_WINDOW = "512kb"
DEFAULT_BASH_TIMEOUT = 30
DEFAULT_BASH_MAX_OUTPUT = 8000

# ANSI 控制序列（颜色、光标移动、OSC 等），节点终端可能在 pty 模式下运行
ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][0-9A-Za-z]"  # 字符集切换
    r"|\x1b[=>]"  # 单字符转义
)


@register("mcsmanager_function", "venti1112", "给 AI 使用的 MCSManager 工具函数", "1.0.3")
class MCSManagerFunction(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.base_url = self.config.get("base_url", "").rstrip("/")
        self.api_token = self.config.get("api_token", "")
        # 同一节点上的命令串行执行，避免多个调用共用同一个 shell 时输出互相穿插
        self._bash_locks: dict[str, asyncio.Lock] = {}
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

    # --- 节点终端（bash）---
    def _bash_timeout(self) -> int:
        try:
            value = int(self.config.get("bash_timeout", DEFAULT_BASH_TIMEOUT))
        except (TypeError, ValueError):
            value = DEFAULT_BASH_TIMEOUT
        return max(5, min(value, 300))

    def _bash_max_output(self) -> int:
        try:
            value = int(self.config.get("bash_max_output", DEFAULT_BASH_MAX_OUTPUT))
        except (TypeError, ValueError):
            value = DEFAULT_BASH_MAX_OUTPUT
        return max(500, value)

    def _bash_blocked_patterns(self) -> list[str]:
        patterns = self.config.get("bash_blocked_commands", [])
        if not isinstance(patterns, list):
            return []
        return [str(item).strip() for item in patterns if str(item).strip()]

    def _check_bash_command(self, command: str) -> Optional[str]:
        """按黑名单规则校验命令。命中返回拒绝信息，否则返回 None。

        规则优先按正则匹配（忽略大小写）；正则非法时退化为子串匹配。
        """
        for pattern in self._bash_blocked_patterns():
            try:
                hit = re.search(pattern, command, re.IGNORECASE) is not None
            except re.error:
                hit = pattern.lower() in command.lower()
            if hit:
                logger.warning(f"节点 bash 命令命中黑名单规则 {pattern!r}，已拒绝执行")
                return json.dumps(
                    {
                        "error": "Command blocked",
                        "detail": f"命令命中管理员配置的黑名单规则「{pattern}」，已拒绝执行。",
                    },
                    ensure_ascii=False,
                )
        return None

    def _check_blocked_daemon(self, daemon_id: str) -> Optional[str]:
        """节点是否可以操作。blocked_instances 中填节点 ID 即封锁整个节点。"""
        keywords = self._blocked_instance_keywords()
        if daemon_id and daemon_id.strip().lower() in keywords:
            logger.warning(f"节点 {daemon_id} 已被列入禁止名单，拒绝执行命令")
            return json.dumps(
                {
                    "error": "Daemon blocked",
                    "detail": f"节点 {daemon_id} 已被列入禁止操作的名单，本次操作已被拒绝。",
                },
                ensure_ascii=False,
            )
        return None

    @staticmethod
    def _json_error(error: str, detail: str) -> str:
        return json.dumps({"error": error, "detail": detail}, ensure_ascii=False)

    async def _node_terminal_status(
        self, daemon_id: str, uuid: str
    ) -> tuple[Optional[int], str]:
        """读取节点终端状态。返回 (status, 原始响应摘要)。"""
        payload = await self._request(
            "GET", "/api/instance", params={"daemonId": daemon_id, "uuid": uuid}
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict):
            try:
                return int(data.get("status")), ""
            except (TypeError, ValueError):
                pass
        snippet = (
            json.dumps(payload, ensure_ascii=False)
            if isinstance(payload, dict)
            else str(payload)
        )
        return None, snippet[:500]

    async def _find_node_terminal(self, daemon_id: str) -> Optional[str]:
        """定位节点终端实例的 UUID。优先用固定 UUID，失败时回落到实例列表搜索。"""
        status, _ = await self._node_terminal_status(daemon_id, NODE_TERMINAL_UUID)
        if status is not None:
            return NODE_TERMINAL_UUID
        payload = await self._request(
            "GET",
            "/api/service/remote_service_instances",
            params={"daemonId": daemon_id, "page": 1, "page_size": 200},
        )
        items = payload.get("data", {}).get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("instanceUuid") or "") == NODE_TERMINAL_UUID:
                return NODE_TERMINAL_UUID
            config = item.get("config")
            if isinstance(config, dict) and config.get("nickname") == NODE_TERMINAL_NAME:
                return str(item.get("instanceUuid") or "") or None
        return None

    async def _ensure_node_terminal(
        self, daemon_id: str
    ) -> tuple[Optional[str], Optional[str], bool]:
        """确保节点终端处于运行状态。返回 (uuid, 错误信息, 是否本次冷启动)。"""
        uuid = await self._find_node_terminal(daemon_id)
        if not uuid:
            return None, self._json_error(
                "Node terminal not found",
                "未在该节点上找到「节点终端」（实例 UUID 应为 global0001）。"
                "请确认 MCSManager 版本支持节点终端，且当前 API 密钥的用户有权访问该实例。",
            ), False

        status, raw = await self._node_terminal_status(daemon_id, uuid)
        if status == INSTANCE_STATUS_RUNNING:
            return uuid, None, False
        if status is None:
            return None, self._json_error(
                "Node terminal unavailable",
                f"无法读取节点终端状态，请检查 MCSManager 连接与权限。原始响应: {raw}",
            ), False

        deadline = time.monotonic() + NODE_TERMINAL_BOOT_TIMEOUT
        opened = False
        current = status
        while time.monotonic() < deadline:
            if current == INSTANCE_STATUS_RUNNING:
                return uuid, None, opened
            if current == INSTANCE_STATUS_STOP and not opened:
                resp = await self._request(
                    "GET",
                    "/api/protected_instance/open",
                    params={"daemonId": daemon_id, "uuid": uuid},
                )
                if isinstance(resp, dict) and resp.get("error"):
                    return None, self._json_error(
                        "Node terminal start failed",
                        f"启动节点终端失败: {json.dumps(resp, ensure_ascii=False)}",
                    ), False
                opened = True
                logger.info(f"已请求启动节点 {daemon_id} 的节点终端")
            await asyncio.sleep(0.4)
            current, _ = await self._node_terminal_status(daemon_id, uuid)

        if current == INSTANCE_STATUS_RUNNING:
            return uuid, None, opened
        return None, self._json_error(
            "Node terminal start timeout",
            f"节点终端在 {NODE_TERMINAL_BOOT_TIMEOUT} 秒内未就绪（当前状态码: {current}）。",
        ), False

    async def _read_node_terminal_log(self, daemon_id: str, uuid: str) -> Optional[str]:
        text = await self._request(
            "GET",
            "/api/protected_instance/outputlog",
            params={"daemonId": daemon_id, "uuid": uuid, "size": BASH_LOG_WINDOW},
            raw_text=True,
        )
        if not isinstance(text, str):
            return None
        # 日志文件不存在时，面板会把 daemon 的异常对象序列化成 {} 返回
        if text.strip() in ("", "{}", "null"):
            return None
        return text

    async def _probe_node_terminal(
        self, daemon_id: str, uuid: str, timeout: float = 15.0
    ) -> tuple[bool, int]:
        """确认节点终端真的能接收并执行输入，返回 (是否就绪, 探针次数)。

        节点终端默认跑在 pty 模式：daemon 侧的 pty helper 要过一会儿才接管 stdin，
        交互式 bash 初始化 readline 时还会丢掉已缓冲的输入。刚 open 完立刻写命令
        会整段落空，因此冷启动后必须先用无副作用的探针把终端「叫醒」。
        探针里的标记刻意拆成两段字符串，pty 回显的是 `'__MCSM_READY_' 'xxx__'`，
        只有真正执行后才会出现连续的标记，避免把回显误判成执行成功。
        """
        token = uuid4().hex[:12]
        marker = f"__MCSM_READY_{token}__"
        probe = f"printf '\\n%s%s\\n' '__MCSM_READY_' '{token}__'"
        deadline = time.monotonic() + timeout
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            await self._request(
                "GET",
                "/api/protected_instance/command",
                params={"daemonId": daemon_id, "uuid": uuid, "command": probe},
            )
            echo_deadline = min(deadline, time.monotonic() + 1.2)
            while time.monotonic() < echo_deadline:
                text = await self._read_node_terminal_log(daemon_id, uuid)
                if text and marker in text:
                    logger.info(
                        f"节点 {daemon_id} 的节点终端已就绪（探针第 {attempts} 次命中）"
                    )
                    return True, attempts
                await asyncio.sleep(0.2)
        return False, attempts

    async def _upload_script(
        self, daemon_id: str, uuid: str, token: str, script: str
    ) -> Optional[tuple[str, str]]:
        """把脚本写到节点 /tmp，返回 (执行命令, 脚本路径)；失败返回 None。

        pty 模式会把送进去的每一行原样回显，若把多行脚本直接写进终端，
        输出会被「提示符 + 命令原文」塞满。落盘后用 source 执行，只会回显一行，
        同时 source 在当前 shell 里运行，cd / export 依然能跨调用保留。
        """
        target = f"/tmp/mcsm_ai_{token}.sh"
        written = await self._request(
            "PUT",
            "/api/files/",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"target": target, "text": script},
        )
        failed = None
        if isinstance(written, dict):
            if written.get("error"):
                failed = written
            elif isinstance(written.get("status"), int) and written["status"] != 200:
                failed = written
        if failed is not None:
            logger.warning(
                f"写入节点临时脚本失败，回退为直接执行: {json.dumps(failed, ensure_ascii=False)}"
            )
            return None
        quoted = shlex.quote(target)
        # 命令末尾的 rm 负责兜底清理（命令中途 exit 时也能删掉）
        return f". {quoted}; rm -f {quoted}", target

    async def _delete_temp_script(self, daemon_id: str, uuid: str, target: str) -> None:
        """主动删除临时脚本，避免依赖 shell 里的 rm 带来的时序漂移。"""
        resp = await self._request(
            "DELETE",
            "/api/files",
            params={"daemonId": daemon_id, "uuid": uuid},
            json_data={"targets": [target]},
        )
        if isinstance(resp, dict) and resp.get("error"):
            logger.warning(
                f"删除节点临时脚本失败 {target}: {json.dumps(resp, ensure_ascii=False)}"
            )

    @staticmethod
    def _build_bash_script(token: str, command: str, workdir: Optional[str]) -> str:
        """拼装带起止标记的脚本。

        标记由脚本自己打印，因此不依赖日志偏移量；用户命令保持顶层执行，
        这样 cd / export 等会作用在同一个 shell 会话上，跨调用保留状态。

        标记在命令文本里被拆成两段字符串传给 printf，因为 pty 模式会把命令行
        原样回显到输出里：若标记整体出现在命令文本中，回显也会被识别成标记，
        一旦回显与真实输出交错就会切出错乱甚至空的结果。
        """
        lines = [f"printf '\\n%s%s\\n' '__MCSM_BASH_BEGIN_' '{token}__'"]
        if workdir:
            quoted = shlex.quote(workdir)
            lines.append(
                f"cd -- {quoted} || {{ printf '[mcsmanager] 切换到 %s 失败，继续在当前目录执行\\n' \"$PWD\"; }}"
            )
        lines.append(command)
        lines.append(
            f"printf '\\n%s%s:%s\\n' '__MCSM_BASH_END_' '{token}__' \"$?\""
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_bash_result(text: str, token: str) -> tuple[Optional[str], Optional[int]]:
        begin = f"__MCSM_BASH_BEGIN_{token}__"
        end = f"__MCSM_BASH_END_{token}__:"
        start = text.rfind(begin)
        if start < 0:
            return None, None
        rest = text[start + len(begin) :]
        stop = rest.find(end)
        if stop < 0:
            return None, None
        tail = rest[stop + len(end) :]
        match = re.match(r"\s*(-?\d+)", tail)
        return rest[:stop], int(match.group(1)) if match else None

    @staticmethod
    def _extract_partial_output(text: str, token: str) -> str:
        """超时/中断时取本次命令已产生的输出（从本次的起始标记之后开始）。"""
        begin = f"__MCSM_BASH_BEGIN_{token}__"
        start = text.rfind(begin)
        return text[start + len(begin) :] if start >= 0 else text

    @staticmethod
    def _clean_terminal_text(text: str) -> str:
        text = ANSI_ESCAPE_RE.sub("", text)
        text = text.replace("\r\n", "\n").replace("\r", "")
        # pty 模式会把结束标记那条 printf 命令回显出来（字面量里带单引号），
        # 真实输出只含连续标记，因此带引号的整行都可以丢掉
        if "'__MCSM_BASH_END_'" in text:
            text = "\n".join(
                line
                for line in text.split("\n")
                if "'__MCSM_BASH_END_'" not in line
            )
        return text.strip("\n")

    @staticmethod
    def _truncate_output(text: str, limit: int) -> tuple[str, bool]:
        if len(text) <= limit:
            return text, False
        half = limit // 2
        omitted = len(text) - half * 2
        return (
            f"{text[:half]}\n...（输出过长，中间省略 {omitted} 个字符）...\n{text[-half:]}",
            True,
        )

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

    # --- 节点终端执行 ---
    @filter.llm_tool(name="mcsmanager_node_bash")
    async def mcsmanager_node_bash(
        self,
        event: AstrMessageEvent,
        daemon_id: str,
        command: str,
        workdir: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> str:
        """在节点（守护进程）所在主机上执行 bash 命令，返回标准输出/错误与退出码。
        会将给定的命令保存到 .sh 中执行，因此支持多行脚本，适合一次性完成复杂任务。每次调用复用同一个节点终端 shell 会话，因此 cd、export 等状态会保留到下一次调用。
        请注意，该 shell 为交互式，请确保执行过程中不弹出交互式操作，否则会导致卡住。首次执行可能稍慢，是正常情况。

        Args:
            daemon_id(string): 节点 ID。
            command(string): 要执行的 bash 命令，支持多行脚本。
            workdir(string, optional): 执行前切换到的目录（绝对路径），例如 "/opt/mcdr"。
            timeout(int, optional): 超时秒数，默认取插件配置（30），范围 5~300。
        """
        if err := self._check_tool_permission(event, "mcsmanager_node_bash"):
            return err
        if err := self._check_blocked_daemon(daemon_id):
            return err

        command = (command or "").strip("\n")
        if not command.strip():
            return self._json_error("Empty command", "command 不能为空。")
        if err := self._check_bash_command(command):
            return err

        limit = self._bash_timeout()
        if timeout is not None:
            try:
                limit = max(5, min(int(timeout), 300))
            except (TypeError, ValueError):
                pass

        lock = self._bash_locks.setdefault(daemon_id, asyncio.Lock())
        async with lock:
            return await self._execute_bash(
                daemon_id, command, workdir, limit, event.get_sender_id()
            )

    async def _execute_bash(
        self,
        daemon_id: str,
        command: str,
        workdir: Optional[str],
        limit: int,
        sender_id: str,
    ) -> str:
        call_started = time.monotonic()
        uuid, err, cold_started = await self._ensure_node_terminal(daemon_id)
        if err:
            return err
        if cold_started:
            # pty 的 bash 在初始化阶段会吞掉早到的输入，先探针叫醒再发真命令
            ready, attempts = await self._probe_node_terminal(daemon_id, uuid)
            if not ready:
                return self._json_error(
                    "Node terminal not ready",
                    f"节点终端已启动，但连发 {attempts} 次探针都没有得到回显，"
                    "可能是 daemon 的 pty helper 异常或 shell 启动失败。"
                    "请在面板中手动打开该节点的「节点终端」确认它能正常执行命令。",
                )
            await asyncio.sleep(0.1)

        token = uuid4().hex[:12]
        script = self._build_bash_script(token, command, workdir)
        started = time.monotonic()

        runner, temp_target = "", ""
        uploaded = await self._upload_script(daemon_id, uuid, token, script)
        if uploaded is None:
            # 文件接口不可用（权限/路径限制）：退回把脚本直接写进终端，
            # 代价是 pty 模式会把命令原文回显进输出
            if len(script) > BASH_INLINE_LIMIT:
                return self._json_error(
                    "Script upload failed",
                    "命令过长（超过 "
                    f"{BASH_INLINE_LIMIT} 字符），且写入节点临时脚本失败。"
                    "请检查 API 密钥用户是否拥有该节点的文件读写权限，或将命令拆分为多次调用。",
                )
            runner = script
        else:
            runner, temp_target = uploaded

        sent = await self._request(
            "GET",
            "/api/protected_instance/command",
            params={"daemonId": daemon_id, "uuid": uuid, "command": runner},
        )
        if isinstance(sent, dict) and sent.get("error"):
            if temp_target:
                await self._delete_temp_script(daemon_id, uuid, temp_target)
            return self._json_error(
                "Send command failed",
                f"向节点终端发送命令失败: {json.dumps(sent, ensure_ascii=False)}",
            )

        deadline = started + limit
        output: Optional[str] = None
        exit_code: Optional[int] = None
        log_text = ""
        terminal_alive = True
        polls = 0
        while time.monotonic() < deadline:
            text = await self._read_node_terminal_log(daemon_id, uuid)
            if text:
                log_text = text
                output, exit_code = self._extract_bash_result(text, token)
                if output is not None:
                    break
            polls += 1
            # 每 8 次轮询（约 2.4 秒）确认一次终端是否还活着，避免命令 exit 后干等超时
            if polls % 8 == 0:
                status, _ = await self._node_terminal_status(daemon_id, uuid)
                if status is not None and status != INSTANCE_STATUS_RUNNING:
                    terminal_alive = False
                    break
            await asyncio.sleep(0.3)

        # 无论成功还是超时都清掉临时脚本：bash 已经打开该文件，unlink 不影响它继续执行
        if temp_target:
            await self._delete_temp_script(daemon_id, uuid, temp_target)

        if output is None:
            if terminal_alive:
                detail = (
                    f"命令在 {limit} 秒内没有产生结束标记：可能仍在执行（如 top、服务前台进程），"
                    "或命令执行了 exit 导致节点终端退出。可增大 timeout 或改为后台执行。"
                )
            else:
                detail = (
                    "节点终端已不在运行状态，命令被中断（通常是命令里执行了 exit，或终端被面板停止）。"
                    "下次调用会自动重新拉起节点终端。"
                )
            tail = (
                self._clean_terminal_text(
                    self._extract_partial_output(log_text, token)
                )[-1500:]
                if log_text
                else ""
            )
            return json.dumps(
                {
                    "error": "Command not finished",
                    "daemon_id": daemon_id,
                    "detail": detail,
                    "partial_output": tail,
                },
                ensure_ascii=False,
                indent=2,
            )

        cleaned = self._clean_terminal_text(output)
        truncated_text, truncated = self._truncate_output(cleaned, self._bash_max_output())
        logger.info(
            f"节点 bash 执行完成 daemon={daemon_id} exit={exit_code} "
            f"耗时={time.monotonic() - call_started:.2f}s 用户={sender_id}"
        )
        return json.dumps(
            {
                "daemon_id": daemon_id,
                "workdir": workdir or "",
                "exit_code": exit_code,
                "truncated": truncated,
                "duration_seconds": round(time.monotonic() - call_started, 2),
                "output": truncated_text,
            },
            ensure_ascii=False,
            indent=2,
        )

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