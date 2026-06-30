"""
主动聊天插件 - AstrBot 插件

功能：
- 机器人根据设定的提示词主动向群聊发送消息
- 支持随机时间间隔（最小/最大间隔之间随机选取）
- 支持多条自定义提示词，机器人每次随机选择一条
- 双阶段生成：先定聊天意图，再生成自然消息
- 自动注入真实聊天记录，避免 AI 说"我没有历史记录"
- 黑名单过滤 + 自动重试，杜绝"我是AI"等机械回复
- 支持插件 Page 可视化增删改查提示词
- 支持免打扰时段
- 适配 AI 智能表情包插件（astrbot_plugin_ai_sticker）
- 支持 WebUI 配置管理

兼容：AstrBot v4.16+
"""

import asyncio
import json
import random
import re
import sys
from datetime import datetime
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import MessageChain

PLUGIN_NAME = "astrbot_plugin_proactive_chat"

# ---- 聊天意图模板 ----
CHAT_INTENTS = [
    {
        "name": "分享日常",
        "prompt": "你是一个普通人，正在和朋友聊天。请分享一件最近发生的日常小事（可以是吐槽天气、路上看到的有趣事情、吃了什么好吃的等）。语气轻松随意，像微信聊天。控制在30字以内。"
    },
    {
        "name": "关心对方",
        "prompt": "你是一个关心朋友的人。请用温暖但不过分的语气问候对方，比如问对方今天过得怎么样、吃饭了没、最近忙不忙。语气像老朋友，不要像客服。控制在30字以内。"
    },
    {
        "name": "调侃",
        "prompt": "你和对方是很熟的朋友。请用轻松调侃的语气说一句话，可以开个小玩笑，或者幽默地吐槽一下生活。注意分寸，不要冒犯。控制在30字以内。"
    },
    {
        "name": "提问",
        "prompt": "你是一个好奇心旺盛的朋友。请提一个有趣的问题引发对方聊天兴趣，比如问对方是否看过某部电影、听过某首歌、去过某个地方。问题要自然，不要像采访。控制在30字以内。"
    },
    {
        "name": "吐槽",
        "prompt": "你今天遇到了一些小烦恼。请用略带吐槽但幽默的语气说一句话，比如抱怨天气太热、周一太困、快递太慢等。要让人有共鸣感。控制在30字以内。"
    },
    {
        "name": "分享见闻",
        "prompt": "你最近了解到一个有趣的事情。请用分享的语气告诉对方，比如看到的一个冷知识、一个有趣的新闻、一个好玩的梗。要让人觉得新鲜有趣。控制在30字以内。"
    },
    {
        "name": "延续话题",
        "prompt": "请根据最近和对方的聊天内容，自然地继续聊下去。如果最近聊了某个话题，接着往下说；如果没有可延续的话题，就微笑打招呼然后自然地切换到其他话题。控制在30字以内。"
    },
    {
        "name": "打招呼",
        "prompt": "你看到朋友在线，想打个招呼开启对话。请用轻松自然的语气说一句问候，可以带一个 emoji 但不要每句都用。像微信上跟朋友打招呼一样。控制在20字以内。"
    },
]

# ---- 禁止输出的黑名单短语 ----
BLACKLIST_PATTERNS = [
    r"我是AI",
    r"作为AI",
    r"作为人工智能",
    r"我没有历史记录",
    r"我无法查看历史",
    r"我无法访问.*记录",
    r"我记不得.*聊天",
    r"我没有.*记忆",
    r"为了保护隐私",
    r"给我一点提示",
    r"给我.*提示",
    r"我的.*限制",
    r"我是.*机器人",
    r"作为.*语言模型",
    r"我是.*模型",
    r"我.*训练数据",
    r"我不能.*浏览",
    r"我无法.*上网",
    r"我.*知识截止",
    r"我是个AI",
]

MAX_RETRY = 2  # 黑名单命中后最多重试次数

# ---- 默认提示词（用户可在 Plugin Page 中自定义） ----
DEFAULT_PROMPTS = [
    {
        "name": "早安问候",
        "content": "现在是早上，请用轻松自然的语气问早安，像朋友一样。控制在25字以内。"
    },
    {
        "name": "晚安问候",
        "content": "现在是晚上，请用温暖简短的话说晚安。控制在20字以内。"
    },
    {
        "name": "日常闲聊",
        "content": "请像朋友一样随便聊点什么，分享日常、吐槽天气、问问对方近况都行。语气自然轻松，控制在30字以内。不要提到自己是AI或机器人。"
    },
    {
        "name": "人格话题",
        "content": "根据人格设定({persona})中的特征或爱好自然引出话题。控制在30字以内。不要提到自己是AI。"
    },
    {
        "name": "热点话题",
        "content": "根据当前日期({current_time})，聊一个轻松的热门话题或节日。控制在30字以内。不要提到自己是AI。"
    },
]

# ---- Web API 兼容层 ----
try:
    from astrbot.api.web import json_response, error_response, request
    _HAS_ASTRBOT_WEB = True
except ImportError:
    from quart import jsonify, request
    _HAS_ASTRBOT_WEB = False

    def json_response(data):
        return jsonify(data)

    def error_response(message, status_code=400):
        return jsonify({"status": "error", "message": message}), status_code

async def _get_json_body(default=None):
    """跨版本获取 JSON 请求体，兼容多种数据格式"""
    if _HAS_ASTRBOT_WEB:
        try:
            body = await request.json(default=default)
            # 如果返回的是字符串，尝试再解析一次
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    pass
            return body if body is not None else default
        except Exception:
            return default
    else:
        try:
            data = await request.get_json(silent=True)
            if data is None:
                # 尝试从 form data 获取
                form = await request.form
                if form:
                    data = {}
                    for key in form:
                        try:
                            data[key] = json.loads(form[key])
                        except (json.JSONDecodeError, TypeError):
                            data[key] = form[key]
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    pass
            return data if data is not None else default
        except Exception:
            return default


@register(PLUGIN_NAME, "AstrBot Community", "机器人主动聊天：根据提示词定时向群聊发起话题，支持联动表情包", "1.0.0")
class ProactiveChatPlugin(Star):
    """主动聊天插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 插件目录
        self.plugin_dir: Path = Path(__file__).parent

        # 每个群的异步任务：umo -> asyncio.Task
        self._tasks: dict[str, asyncio.Task] = {}

        # 表情包插件引用（静默检测，不对外显示状态）
        self._sticker_plugin = None
        self._sticker_checked = False

        # 注册 Web API（供 Plugin Page 使用）
        self._register_web_apis()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self):
        """插件初始化：启动所有主动聊天循环"""
        logger.info("[主动聊天] 插件初始化中...")
        self._start_all_tasks()
        logger.info(f"[主动聊天] 初始化完成，已为 {len(self._tasks)} 个群启动主动聊天")

    async def terminate(self):
        """插件卸载时取消所有任务"""
        self._stop_all_tasks()
        logger.info("[主动聊天] 插件已卸载，所有任务已停止")

    # ------------------------------------------------------------------
    # 任务管理
    # ------------------------------------------------------------------

    def _start_all_tasks(self):
        """为所有目标群启动主动聊天循环"""
        self._stop_all_tasks()

        if not self._is_enabled():
            logger.info("[主动聊天] 插件已禁用，跳过启动")
            return

        group_targets = self._get_target_groups()
        private_targets = self._get_private_targets()
        all_targets = [(t, "群聊") for t in group_targets] + [(t, "私聊") for t in private_targets]

        if not all_targets:
            logger.info("[主动聊天] 未配置任何目标（群聊或私聊），跳过启动")
            return

        for target, target_type in all_targets:
            umo = self._resolve_umo(target, target_type)
            if not umo:
                logger.warning(f"[主动聊天] 无法解析目标: {target}，跳过")
                continue
            task = asyncio.create_task(self._proactive_loop(umo, target))
            self._tasks[umo] = task
            logger.info(f"[主动聊天] 已为 {target_type}:{target} 启动主动聊天循环")

    def _stop_all_tasks(self):
        """取消所有主动聊天任务"""
        for umo, task in list(self._tasks.items()):
            task.cancel()
        self._tasks.clear()
        logger.info("[主动聊天] 所有任务已停止")

    # ------------------------------------------------------------------
    # 配置读取辅助
    # ------------------------------------------------------------------

    def _is_enabled(self) -> bool:
        return bool(self.config.get("enable", True))

    def _get_target_groups(self) -> list[str]:
        val = self.config.get("target_groups", [])
        return val if isinstance(val, list) else []

    def _get_private_targets(self) -> list[str]:
        val = self.config.get("private_targets", [])
        return val if isinstance(val, list) else []

    def _get_min_interval(self) -> int:
        return max(60, int(self.config.get("min_interval_seconds", 1800)))

    def _get_max_interval(self) -> int:
        return max(int(self.config.get("max_interval_seconds", 7200)),
                   self._get_min_interval() + 60)

    def _get_silent_start(self) -> int:
        return bool(self.config.get("enable_sticker_integration", True))

    def _get_silent_start(self) -> int:
        return max(0, min(23, int(self.config.get("silent_hours_start", 0))))

    def _get_silent_end(self) -> int:
        return max(0, min(23, int(self.config.get("silent_hours_end", 0))))

    def _get_custom_prompts(self) -> list[dict]:
        """获取自定义提示词列表（供 Web API 和状态显示使用）"""
        raw = self.config.get("custom_prompts", "[]")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return list(DEFAULT_PROMPTS)

    def _is_sticker_enabled(self) -> bool:
        return bool(self.config.get("enable_sticker_integration", True))

    def _get_silent_end(self) -> int:
        return max(0, min(23, int(self.config.get("silent_hours_end", 0))))

    # ------------------------------------------------------------------
    # 核心循环
    # ------------------------------------------------------------------

    async def _proactive_loop(self, umo: str, target: str):
        """每个群的主动聊天主循环"""
        logger.info(f"[主动聊天] 循环启动: {target} (umo={umo})")

        # 首次启动时等待初始间隔，避免机器人一启动就立刻发消息
        initial_wait = random.randint(30, 120)
        try:
            await asyncio.sleep(initial_wait)
        except asyncio.CancelledError:
            return

        while True:
            try:
                min_sec = self._get_min_interval()
                max_sec = self._get_max_interval()
                wait_seconds = random.randint(min_sec, max_sec)
                logger.info(
                    f"[主动聊天] {target} 下次发送在 {wait_seconds}s 后 "
                    f"(范围: {min_sec}s - {max_sec}s)"
                )

                await asyncio.sleep(wait_seconds)

                # 再次检查插件状态
                if not self._is_enabled():
                    logger.info("[主动聊天] 插件已禁用，退出循环")
                    return

                # 检查目标是否仍在配置中（群聊+私聊）
                all_targets = self._get_target_groups() + self._get_private_targets()
                if target not in all_targets:
                    logger.info(f"[主动聊天] {target} 已从配置移除，退出循环")
                    return

                # 检查免打扰
                if self._is_silent_hours():
                    logger.info("[主动聊天] 当前处于免打扰时段，跳过")
                    continue

                # 发送主动消息
                await self._send_proactive_message(umo, target)

            except asyncio.CancelledError:
                logger.info(f"[主动聊天] {target} 循环被取消")
                return
            except Exception as e:
                logger.error(f"[主动聊天] {target} 循环异常: {e}", exc_info=True)
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    return

    # ------------------------------------------------------------------
    # 消息生成与发送（双阶段 + 黑名单 + 重试）
    # ------------------------------------------------------------------

    async def _send_proactive_message(self, umo: str, target: str):
        """生成并发送一条主动聊天消息（双阶段 + 过滤 + 重试）"""
        logger.info(f"[主动聊天] 开始为 {target} 生成主动消息...")

        # 阶段0: 收集真实上下文
        persona_text = await self._get_persona_context(umo)
        chat_history = await self._get_recent_chat_text(umo)
        time_str = self._current_time_str()

        # 阶段1: 选定聊天意图
        intent = random.choice(CHAT_INTENTS)
        intent_name = intent["name"]
        logger.info(f"[主动聊天] 选定意图: 「{intent_name}」")

        # 阶段2: 根据意图 + 上下文生成消息，带黑名单过滤和重试
        message_text = await self._generate_with_retry(
            intent=intent,
            persona_text=persona_text,
            chat_history=chat_history,
            time_str=time_str,
            umo=umo,
        )

        if not message_text:
            logger.warning("[主动聊天] 生成失败（重试耗尽），跳过发送")
            return

        logger.info(f"[主动聊天] 最终消息: {message_text}")

        # 发送
        import astrbot.api.message_components as Comp
        chain = [Comp.Plain(message_text)]
        sticker_path = await self._try_get_sticker_image(message_text)
        if sticker_path:
            try:
                chain.append(Comp.Image.fromFileSystem(str(sticker_path)))
            except Exception:
                pass

        # 发送（带重试，应对 NapCat 偶发超时）
        SEND_MAX_RETRY = 2
        for send_attempt in range(SEND_MAX_RETRY + 1):
            try:
                message_chain = MessageChain()
                for comp in chain:
                    if isinstance(comp, Comp.Plain):
                        message_chain.message(comp.text)
                    elif isinstance(comp, Comp.Image):
                        file_path = getattr(comp, 'file', None)
                        if file_path:
                            message_chain.file_image(file_path)
                        else:
                            url = getattr(comp, 'url', None)
                            if url:
                                message_chain.image(url)
                await self.context.send_message(umo, message_chain)
                logger.info(f"[主动聊天] ✅ 已向 {target} 发送主动消息")
                break
            except Exception as e:
                err_str = str(e)
                # NapCat 1200 超时等瞬态错误：短暂等待后重试
                if ("1200" in err_str or "Timeout" in err_str or "timeout" in err_str.lower()) \
                   and send_attempt < SEND_MAX_RETRY:
                    logger.warning(f"[主动聊天] 发送超时（第{send_attempt+1}次），3秒后重试...")
                    await asyncio.sleep(3)
                else:
                    logger.error(f"[主动聊天] 发送失败: {e}", exc_info=True)
                    break

    async def _generate_with_retry(
        self, intent: dict, persona_text: str, chat_history: str,
        time_str: str, umo: str
    ) -> str | None:
        """带黑名单过滤和重试的消息生成"""
        for attempt in range(MAX_RETRY + 1):
            prompt = self._build_stage2_prompt(
                intent=intent,
                persona_text=persona_text,
                chat_history=chat_history,
                time_str=time_str,
                is_retry=(attempt > 0),
            )

            msg = await self._generate_message(prompt, umo)
            if not msg:
                continue

            # 黑名单检查
            if self._is_blacklisted(msg):
                logger.warning(
                    f"[主动聊天] 黑名单命中（第{attempt+1}次）: '{msg[:50]}...'，将重试"
                )
                continue

            return msg

        return None

    def _build_stage2_prompt(
        self, intent: dict, persona_text: str, chat_history: str,
        time_str: str, is_retry: bool
    ) -> str:
        """构建阶段2的提示词（直接注入上下文，不让 AI 自己回忆）"""
        parts = []

        # 人格设定
        if persona_text:
            parts.append(f"你的身份：{persona_text[:200]}")
        else:
            parts.append("你是一个普通人，在和朋友聊天。")

        # 关键规则
        parts.append(
            "【重要规则】\n"
            "- 你是真人，不是AI。绝对禁止提到 AI、人工智能、语言模型、机器人等词。\n"
            "- 你是一个普通朋友在微信上聊天。\n"
            "- 回复必须简短（20-40字），像微信消息一样随意。\n"
            "- 偶尔可以用1个emoji，但不要每句都用。\n"
            '- 禁止说 我没有记录、我记不得、给我提示、为了保护隐私 等解释性话语。\n'
            "- 如果不知道说什么，就自然地打招呼或换个话题，不要解释原因。"
        )
        if is_retry:
            parts.append("【再次强调】刚才的回复不合格！绝对不要提到你是AI、模型、或机器人！像真人一样聊天！")

        # 时间和意图
        parts.append(f"当前时间：{time_str}")
        parts.append(f"聊天方向：{intent['name']}——{intent['prompt']}")

        # 真实聊天记录（直接注入，让 AI 有上下文可延续）
        if chat_history:
            parts.append(
                f"以下是你和对方最近的聊天内容，请据此自然地延续对话：\n{chat_history}\n"
                "如果有可延续的话题就接着聊；如果没有就自然地开启新话题。"
            )

        parts.append("请直接输出你要发送的一句话，不要加引号、前缀或任何解释。")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # 黑名单检查
    # ------------------------------------------------------------------

    def _is_blacklisted(self, text: str) -> bool:
        """检查消息是否包含禁止短语"""
        for pattern in BLACKLIST_PATTERNS:
            if re.search(pattern, text):
                return True
        return False

    def _current_time_str(self) -> str:
        weekdays = ["日", "一", "二", "三", "四", "五", "六"]
        wd = weekdays[datetime.now().weekday()]
        return datetime.now().strftime(f"%Y年%m月%d日 %H:%M，星期{wd}")

    async def _get_recent_chat_text(self, umo: str) -> str:
        """获取最近的聊天记录文本（直接返回对话内容，而非提示词）"""
        try:
            conv_mgr = self.context.conversation_manager
            if not conv_mgr:
                return ""

            count = int(self.config.get("past_conversation_count", 3))
            if count <= 0:
                return ""

            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if not curr_cid:
                return ""

            conversation = await conv_mgr.get_conversation(umo, curr_cid)
            if not conversation or not hasattr(conversation, 'history'):
                return ""

            history = conversation.history
            if not history:
                return ""

            recent = history[-count * 2:]
            lines = []
            for msg in recent:
                if hasattr(msg, 'content') and msg.content:
                    for part in msg.content:
                        if hasattr(part, 'text') and part.text:
                            text = part.text[:100]
                            role = "对方" if getattr(msg, 'role', '') == 'user' else "我"
                            lines.append(f"{role}：{text}")

            return "\n".join(lines) if lines else ""
        except Exception as e:
            logger.warning(f"[主动聊天] 获取聊天记录失败: {e}")
            return ""

    async def _generate_message(self, prompt: str, umo: str = "") -> str | None:
        """调用 AI 生成消息"""
        try:
            provider_id = await self._get_provider_id(umo)
            if not provider_id:
                logger.warning("[主动聊天] 无法获取聊天模型 ID")
                return None

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            if not llm_resp:
                return None

            result = llm_resp.completion_text.strip()

            # 清理可能的引号包裹
            if result.startswith('"') and result.endswith('"'):
                result = result[1:-1].strip()
            if result.startswith("'") and result.endswith("'"):
                result = result[1:-1].strip()
            if result.startswith("「") and result.endswith("」"):
                result = result[1:-1].strip()
            if result.startswith("『") and result.endswith("』"):
                result = result[1:-1].strip()

            return result if result else None
        except Exception as e:
            logger.error(f"[主动聊天] AI 调用失败: {e}", exc_info=True)
            return None

    async def _get_provider_id(self, umo: str = "") -> str | None:
        """获取聊天模型 ID"""
        try:
            # 优先使用 AstrBot 标准方法
            if umo:
                pid = await self.context.get_current_chat_provider_id(umo=umo)
                if pid:
                    return pid
            # 兜底：从 provider_manager 获取
            pm = self.context.provider_manager
            if hasattr(pm, 'get_default_provider_id'):
                return await pm.get_default_provider_id()
            if hasattr(pm, 'providers') and pm.providers:
                return next(iter(pm.providers))
            return None
        except Exception as e:
            logger.error(f"[主动聊天] 获取 provider ID 失败: {e}")
            return None

    # ------------------------------------------------------------------
    # 上下文收集
    # ------------------------------------------------------------------

    async def _get_persona_context(self, umo: str) -> str:
        """获取人格设定"""
        try:
            persona_mgr = self.context.persona_manager
            if not persona_mgr:
                return ""
            try:
                dp = await persona_mgr.get_default_persona_v3(umo)
                if dp and hasattr(dp, 'system_prompt') and dp.system_prompt:
                    return dp.system_prompt
            except Exception:
                pass
            try:
                all_p = await persona_mgr.get_all_personas()
                if all_p:
                    for p in all_p:
                        if hasattr(p, 'system_prompt') and p.system_prompt:
                            return p.system_prompt
            except Exception:
                pass
            return ""
        except Exception as e:
            logger.warning(f"[主动聊天] 获取人格失败: {e}")
            return ""

    # ------------------------------------------------------------------
    # 免打扰
    # ------------------------------------------------------------------

    def _is_silent_hours(self) -> bool:
        """检查是否在免打扰时段"""
        start = self._get_silent_start()
        end = self._get_silent_end()
        if start == 0 and end == 0:
            return False
        if start == end:
            return False
        hour = datetime.now().hour
        if start < end:
            return start <= hour < end
        else:
            return hour >= start or hour < end

    # ------------------------------------------------------------------
    # UMO 解析
    # ------------------------------------------------------------------

    def _resolve_umo(self, target: str, target_type: str = "群聊") -> str | None:
        """
        解析 unified_msg_origin。
        自动查找 aiocqhttp 平台的真实 platform_id（而非硬编码名称），
        构造正确格式: {platform_id}:GroupMessage:{id} 或 {platform_id}:FriendMessage:{id}
        """
        target = target.strip()
        if not target:
            return None
        if ":" in target and target.count(":") >= 2:
            return target
        if not target.isdigit():
            logger.warning(f"[主动聊天] 无法解析目标: {target}")
            return None

        # 动态查找 aiocqhttp 平台的真实 ID
        platform_id = self._find_aiocqhttp_platform_id()
        if not platform_id:
            logger.warning("[主动聊天] 未找到 aiocqhttp 平台，无法构造 UMO")
            return None

        if target_type == "私聊":
            return f"{platform_id}:FriendMessage:{target}"
        return f"{platform_id}:GroupMessage:{target}"

    def _find_aiocqhttp_platform_id(self) -> str | None:
        """查找 aiocqhttp 平台的真实唯一 ID"""
        try:
            pm = self.context.platform_manager
            for inst in pm.platform_insts:
                meta = inst.meta()
                if meta.name == "aiocqhttp":
                    logger.info(f"[主动聊天] 找到 aiocqhttp 平台，ID: {meta.id}")
                    return meta.id
        except Exception as e:
            logger.warning(f"[主动聊天] 查找平台 ID 失败: {e}")
        return None

    # ------------------------------------------------------------------
    # Web API（供 Plugin Page 调用）
    # ------------------------------------------------------------------

    def _register_web_apis(self):
        """注册插件 Page 所需的 Web API"""
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/status",
            self._api_status,
            ["GET"],
            "获取插件状态",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/prompts",
            self._api_get_prompts,
            ["GET"],
            "获取所有提示词",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/prompts/add",
            self._api_add_prompt,
            ["POST"],
            "添加提示词",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/prompts/save",
            self._api_save_prompts,
            ["POST"],
            "批量保存提示词",
        )

    # --- API Handlers ---

    async def _api_status(self):
        """返回插件运行状态"""
        prompts = self._get_custom_prompts()
        return json_response({
            "enabled": self._is_enabled(),
            "prompt_count": len(prompts),
            "interval": f"{self._get_min_interval()}s ~ {self._get_max_interval()}s",
            "silent_hours": f"{self._get_silent_start()}:00 - {self._get_silent_end()}:00",
        })

    async def _api_get_prompts(self):
        """返回所有提示词"""
        prompts = self._get_custom_prompts()
        return json_response({"prompts": prompts})

    async def _api_add_prompt(self):
        """添加一条提示词"""
        payload = await _get_json_body(default={})
        name = (payload.get("name") or "").strip()
        content = (payload.get("content") or "").strip()
        if not name or not content:
            return error_response("名称和内容不能为空", status_code=400)

        prompts = self._get_custom_prompts()
        prompts.append({"name": name, "content": content})
        self._save_prompts(prompts)
        logger.info(f"[主动聊天] 已添加提示词「{name}」，共 {len(prompts)} 条")
        return json_response({"prompts": prompts})

    async def _api_save_prompts(self):
        """批量保存提示词"""
        # 优先读原始 body，避免框架层 body 解析问题
        try:
            if _HAS_ASTRBOT_WEB:
                raw = await request.body()
                payload = json.loads(raw) if raw else {}
            else:
                raw = await request.get_data()
                payload = json.loads(raw) if raw else {}
        except Exception:
            payload = await _get_json_body(default={})

        new_prompts = payload.get("prompts")
        if not isinstance(new_prompts, list):
            return error_response("prompts 必须是数组", status_code=400)

        cleaned = []
        for p in new_prompts:
            if isinstance(p, dict) and p.get("name") and p.get("content"):
                cleaned.append({"name": p["name"].strip(), "content": p["content"].strip()})

        self._save_prompts(cleaned)
        logger.info(f"[主动聊天] 已保存 {len(cleaned)} 条提示词")
        return json_response({"prompts": cleaned})

    def _save_prompts(self, prompts: list):
        """将提示词列表保存到配置"""
        self.config["custom_prompts"] = json.dumps(prompts, ensure_ascii=False, indent=2)
        try:
            self.config.save_config()
        except Exception as e:
            logger.error(f"[主动聊天] 保存提示词配置失败: {e}")

    # ------------------------------------------------------------------
    # 表情包适配（静默模式：尝试使用，不可用时静默跳过，不显示状态）
    # ------------------------------------------------------------------

    async def _try_get_sticker_image(self, message_text: str):
        """
        适配表情包插件：从已安装的 astrbot_plugin_ai_sticker 中随机选取一张图片。
        不做概率判断——概率由表情包插件自己的 trigger_probability 控制。
        这里只负责「如果能拿到图就追加」。
        """
        if not self._is_sticker_enabled():
            return None

        sticker = await self._find_sticker_plugin()
        if not sticker:
            return None

        try:
            categories = getattr(sticker, 'categories', [])
            category_images = getattr(sticker, 'category_images', {})
            if not categories or not category_images:
                return None

            category = random.choice(categories)
            images = category_images.get(category, [])
            if not images:
                return None

            img_path = random.choice(images)
            logger.info(f"[主动聊天] 表情包适配「{category}」-> {img_path.name}")
            return img_path
        except Exception:
            return None

    async def _find_sticker_plugin(self):
        """查找表情包插件实例（静默，不打印"未检测到"）"""
        if self._sticker_checked and self._sticker_plugin is not None:
            return self._sticker_plugin
        if self._sticker_checked:
            return None

        self._sticker_checked = True

        try:
            for mod_name, module in list(sys.modules.items()):
                if 'astrbot_plugin_ai_sticker' in mod_name or 'astrbot_plugin_god' in mod_name or 'sticker' in mod_name.lower():
                    for attr_name in dir(module):
                        try:
                            obj = getattr(module, attr_name)
                            if obj is None:
                                continue
                            if 'AISticker' in obj.__class__.__name__:
                                if hasattr(obj, 'categories') and hasattr(obj, 'category_images'):
                                    self._sticker_plugin = obj
                                    logger.info("[主动聊天] 已连接表情包插件，主动消息可搭配表情包")
                                    return obj
                        except Exception:
                            continue

            if hasattr(self.context, 'plugin_manager'):
                pm = self.context.plugin_manager
                for attr in ('plugins', '_plugins', 'plugin_instances'):
                    plugins = getattr(pm, attr, None)
                    if not plugins:
                        continue
                    items = plugins.values() if isinstance(plugins, dict) else plugins
                    for p in items:
                        try:
                            if 'AISticker' in p.__class__.__name__ and hasattr(p, 'categories'):
                                self._sticker_plugin = p
                                logger.info("[主动聊天] 已连接表情包插件，主动消息可搭配表情包")
                                return p
                        except Exception:
                            continue
        except Exception:
            pass

        return None

    # ------------------------------------------------------------------
    # 管理指令
    # ------------------------------------------------------------------

    @filter.command_group("proactive_chat")
    def proactive_chat(self):
        """主动聊天插件管理指令"""
        pass

    @proactive_chat.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看主动聊天插件运行状态"""
        enabled = self._is_enabled()
        groups = self._get_target_groups()
        privates = self._get_private_targets()
        min_sec = self._get_min_interval()
        max_sec = self._get_max_interval()
        active = len(self._tasks)
        prompt_count = len(self._get_custom_prompts())

        lines = [
            "📊 **主动聊天插件状态**",
            f"• 启用: {'✅ 已启用' if enabled else '❌ 已禁用'}",
            f"• 目标群聊: {len(groups)} 个 | 私聊: {len(privates)} 个",
            f"• 活跃任务: {active}",
            f"• 时间间隔: {min_sec}s ~ {max_sec}s",
            f"• 免打扰: {self._get_silent_start()}:00-{self._get_silent_end()}:00",
            f"• 提示词数: {prompt_count} 条（每次随机选择）",
        ]
        if groups:
            lines.append("• 群聊目标:")
            for t in groups:
                umo = self._resolve_umo(t, "群聊")
                mark = "🟢" if (umo and umo in self._tasks) else "🔴"
                lines.append(f"  {mark} {t}")
        if privates:
            lines.append("• 私聊目标:")
            for t in privates:
                umo = self._resolve_umo(t, "私聊")
                mark = "🟢" if (umo and umo in self._tasks) else "🔴"
                lines.append(f"  {mark} {t}")

        yield event.plain_result("\n".join(lines))

    @proactive_chat.command("trigger")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_trigger(self, event: AstrMessageEvent):
        """管理员：在当前会话立即触发一次主动聊天（支持群聊和私聊）"""
        umo = event.unified_msg_origin
        group_id = event.message_obj.group_id
        target = group_id if group_id else event.get_sender_id()
        target_type = "群聊" if group_id else "私聊"

        yield event.plain_result(f"🚀 正在为当前{target_type}生成主动消息...")
        await self._send_proactive_message(umo, target)

    @proactive_chat.command("reload")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_reload(self, event: AstrMessageEvent):
        """管理员：重新加载配置并重启所有任务"""
        self._start_all_tasks()
        yield event.plain_result(f"✅ 已重载！当前为 {len(self._tasks)} 个群启动了主动聊天。")

    @proactive_chat.command("stop")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_stop(self, event: AstrMessageEvent):
        """管理员：停止所有主动聊天任务"""
        self._stop_all_tasks()
        yield event.plain_result("⏹️ 已停止所有主动聊天任务。")

    @proactive_chat.command("start")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_start(self, event: AstrMessageEvent):
        """管理员：启动所有主动聊天任务"""
        self._start_all_tasks()
        yield event.plain_result(f"▶️ 已启动！当前为 {len(self._tasks)} 个群开启了主动聊天。")

