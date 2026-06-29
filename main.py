"""
主动聊天插件 - AstrBot 插件

功能：
- 机器人根据设定的提示词主动向群聊发送消息
- 支持随机时间间隔（最小/最大间隔之间随机选取）
- 支持多条自定义提示词，机器人每次随机选择一条
- 支持插件 Page 可视化增删改查提示词
- 支持多种提示词模板：早安晚安、回忆过往聊天、基于人格设定聊天
- 支持免打扰时段
- 适配 AI 智能表情包插件（astrbot_plugin_ai_sticker），静默联动发表情包
- 支持 WebUI 配置管理
- 支持管理指令：/proactive_chat status/trigger/reload/start/stop

兼容：AstrBot v4.16+
"""

import asyncio
import json
import random
import sys
from datetime import datetime
from pathlib import Path

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import MessageChain

PLUGIN_NAME = "astrbot_plugin_proactive_chat"

# ---- 默认提示词 ----
DEFAULT_PROMPTS = [
    {
        "name": "早安问候",
        "content": "现在是早上，请以轻松愉快的语气向群友问早安，结合当天的日期和天气感觉，自然地开启新一天的话题。消息要自然简短，像真人朋友一样。"
    },
    {
        "name": "晚安问候",
        "content": "现在是晚上，请以温暖关心的语气向群友说晚安，可以提醒大家早点休息，或者说一些暖心的话。消息要自然简短。"
    },
    {
        "name": "回忆聊天",
        "content": "请回顾最近的聊天记录，找一个之前聊过但还没结束的话题，自然地继续聊下去。比如之前聊过的电影、音乐、游戏、美食等话题。语气要像朋友间的日常对话。"
    },
    {
        "name": "人格话题",
        "content": "请根据你的人格设定({persona})中的特征、爱好或背景故事，自然地引出相关话题。比如你的人格设定中提到喜欢某样东西，你可以分享相关的趣事或问群友的看法。"
    },
    {
        "name": "日常分享",
        "content": "请像一个普通朋友一样，分享一些日常生活中的小趣事或感悟。可以是对天气的吐槽、对某件事的看法、或者一个有趣的小问题来引发讨论。语气轻松自然。"
    },
    {
        "name": "热点话题",
        "content": "根据当前日期({current_time})，聊聊最近可能的热门话题或节日氛围。可以是最近的节假日安排、季节变化、或者一些轻松有趣的社会话题。引导群友参与讨论。"
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

        targets = self._get_target_groups()
        if not targets:
            logger.info("[主动聊天] 未配置目标群聊，跳过启动")
            return

        for target in targets:
            umo = self._resolve_umo(target)
            if not umo:
                logger.warning(f"[主动聊天] 无法解析目标: {target}，跳过")
                continue
            task = asyncio.create_task(self._proactive_loop(umo, target))
            self._tasks[umo] = task
            logger.info(f"[主动聊天] 已为 {target} 启动主动聊天循环")

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

    def _get_min_interval(self) -> int:
        return max(60, int(self.config.get("min_interval_seconds", 1800)))

    def _get_max_interval(self) -> int:
        return max(int(self.config.get("max_interval_seconds", 7200)),
                   self._get_min_interval() + 60)

    def _get_prompt_template(self) -> str:
        """获取兜底提示词模板（当自定义提示词列表为空时使用）"""
        return self.config.get(
            "prompt_template",
            "你是一个聊天机器人，现在你需要主动发起聊天。\n\n"
            "你的身份设定：\n{persona}\n\n"
            "当前时间：{current_time}\n\n"
            "{extra_context}\n\n"
            "请根据以上信息，自然地向群聊发送一条消息，主动开启话题。"
            "消息要求：\n"
            "1. 语气自然、口语化，像真人朋友聊天\n"
            "2. 消息简短（控制在100字以内）\n"
            "3. 符合你的身份设定\n"
            "4. 不要提及\"主动聊天\"、\"提示词\"等元信息\n\n"
            "请直接输出你要发送的消息内容，不要带任何前缀、引号或解释。",
        )

    def _get_custom_prompts(self) -> list[dict]:
        """获取自定义提示词列表"""
        raw = self.config.get("custom_prompts", "[]")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                logger.warning("[主动聊天] 自定义提示词 JSON 解析失败，使用默认提示词")
        # 返回默认提示词
        return list(DEFAULT_PROMPTS)

    def _pick_random_prompt(self) -> str:
        """从提示词列表中随机选择一条，返回提示词内容文本"""
        prompts = self._get_custom_prompts()
        if not prompts:
            # 兜底：使用旧的 prompt_template
            return self._get_prompt_template()

        picked = random.choice(prompts)
        name = picked.get("name", "未命名")
        content = picked.get("content", "")
        logger.info(f"[主动聊天] 随机选中提示词: 「{name}」")
        return content

    def _is_sticker_enabled(self) -> bool:
        return bool(self.config.get("enable_sticker_integration", True))

    def _get_sticker_probability(self) -> int:
        return max(0, min(100, int(self.config.get("sticker_probability", 40))))

    def _get_silent_start(self) -> int:
        return max(0, min(23, int(self.config.get("silent_hours_start", 0))))

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

                # 检查目标群是否仍在配置中
                if target not in self._get_target_groups():
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
    # 消息生成与发送
    # ------------------------------------------------------------------

    async def _send_proactive_message(self, umo: str, target: str):
        """生成并发送一条主动聊天消息"""
        logger.info(f"[主动聊天] 开始为 {target} 生成主动消息...")

        # 1. 构建提示词
        prompt = await self._build_prompt(umo)
        prompt_preview = prompt[:300].replace("\n", " ")
        logger.info(f"[主动聊天] 提示词预览: {prompt_preview}...")

        # 2. 调用 AI 生成消息
        message_text = await self._generate_message(prompt, umo)
        if not message_text:
            logger.warning("[主动聊天] AI 返回空消息，跳过发送")
            return

        logger.info(f"[主动聊天] AI 生成: {message_text}")

        # 3. 尝试搭配表情包（静默，失败不报错）
        import astrbot.api.message_components as Comp
        chain = [Comp.Plain(message_text)]
        sticker_path = await self._try_get_sticker_image(message_text)
        if sticker_path:
            try:
                chain.append(Comp.Image.fromFileSystem(str(sticker_path)))
            except Exception:
                pass  # 表情包追加失败不影响主流程

        # 4. 发送
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
        except Exception as e:
            logger.error(f"[主动聊天] 发送失败: {e}", exc_info=True)

    async def _build_prompt(self, umo: str) -> str:
        """构建完整的发送提示词，随机选择一条自定义提示词作为骨架"""
        # 随机选择提示词
        selected_prompt = self._pick_random_prompt()
        template = selected_prompt

        # 人格设定
        persona_text = await self._get_persona_context(umo)

        # 时间
        weekdays = ["日", "一", "二", "三", "四", "五", "六"]
        wd = weekdays[datetime.now().weekday()]
        current_time = datetime.now().strftime(f"%Y年%m月%d日 %H:%M，星期{wd}")

        # 额外上下文
        extra_parts = []

        # 时段问候
        if self.config.get("enable_time_greeting", True):
            hour = datetime.now().hour
            if 6 <= hour < 9:
                greeting = self.config.get(
                    "morning_greeting_prompt",
                    "现在是早上，请以轻松愉快的语气向群友问早安。"
                )
                extra_parts.append(f"[时段提示]\n{greeting}")
            elif 21 <= hour < 24:
                greeting = self.config.get(
                    "night_greeting_prompt",
                    "现在是晚上，请以温暖关心的语气向群友说晚安。"
                )
                extra_parts.append(f"[时段提示]\n{greeting}")

        # 过往聊天回忆
        if self.config.get("enable_past_conversation", True):
            past = await self._get_past_conversation_context(umo)
            if past:
                extra_parts.append(f"[过往聊天回忆]\n{past}")

        # 人格参考
        if self.config.get("enable_persona_chat", True) and persona_text:
            extra_parts.append(
                "[人格设定参考]\n你可以根据以上人格设定中的特征、爱好、背景故事来开启话题。"
            )

        extra_context = "\n\n".join(extra_parts) if extra_parts else "（自由发挥，开启一个有趣的话题）"

        prompt = template.replace("{persona}", persona_text or "（未设置人格）")
        prompt = prompt.replace("{current_time}", current_time)
        prompt = prompt.replace("{extra_context}", extra_context)

        return prompt

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

    async def _get_past_conversation_context(self, umo: str) -> str:
        """获取过往聊天记录"""
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
                            text = part.text[:80]
                            role = "用户" if getattr(msg, 'role', '') == 'user' else "机器人"
                            lines.append(f"[{role}]: {text}")

            if lines:
                return "最近聊天记录：\n" + "\n".join(lines)
            return ""
        except Exception as e:
            logger.warning(f"[主动聊天] 获取聊天记录失败: {e}")
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

    def _resolve_umo(self, target: str) -> str | None:
        """
        解析 unified_msg_origin。
        支持：纯数字群号 -> 默认 aiocqhttp:group:{id}
             完整 UMO -> 直接使用
        """
        target = target.strip()
        if not target:
            return None
        if ":" in target and target.count(":") >= 2:
            return target
        if target.isdigit():
            return f"aiocqhttp:group:{target}"
        logger.warning(f"[主动聊天] 无法解析目标: {target}")
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
            f"/{PLUGIN_NAME}/prompts/delete/<int:index>",
            self._api_delete_prompt,
            ["POST", "GET"],
            "删除提示词（index 在 URL 路径中）",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/prompts/move/<int:index>/<int:direction>",
            self._api_move_prompt,
            ["POST", "GET"],
            "移动提示词顺序（index 和 direction 在 URL 路径中）",
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

    async def _api_delete_prompt(self, index: int):
        """删除一条提示词（index 来自 URL 路径）"""
        prompts = self._get_custom_prompts()
        if index < 0 or index >= len(prompts):
            return error_response(f"索引超出范围（0~{len(prompts)-1}），收到: {index}", status_code=400)

        removed = prompts.pop(index)
        self._save_prompts(prompts)
        logger.info(f"[主动聊天] 已删除提示词「{removed.get('name', '')}」，共 {len(prompts)} 条")
        return json_response({"prompts": prompts})

    async def _api_move_prompt(self, index: int, direction: int):
        """移动提示词顺序（参数来自 URL 路径）"""
        prompts = self._get_custom_prompts()
        new_index = index + direction
        if new_index < 0 or new_index >= len(prompts):
            return error_response("无法移动（边界）", status_code=400)

        prompts.insert(new_index, prompts.pop(index))
        self._save_prompts(prompts)
        return json_response({"prompts": prompts})

    async def _api_save_prompts(self):
        """批量保存提示词（前端编辑后提交整个列表）"""
        payload = await _get_json_body(default={})
        new_prompts = payload.get("prompts")
        if not isinstance(new_prompts, list):
            return error_response("prompts 必须是数组", status_code=400)

        # 验证每项格式
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
        """尝试获取一张表情包。成功返回路径，失败返回 None（不报错）。"""
        if not self._is_sticker_enabled():
            return None
        prob = self._get_sticker_probability()
        if prob <= 0:
            return None
        if prob < 100 and random.randint(1, 100) > prob:
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
            logger.info(f"[主动聊天] 表情包「{category}」-> {img_path.name}")
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
                if 'astrbot_plugin_ai_sticker' in mod_name or 'astrbot_plugin_god' in mod_name:
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
        targets = self._get_target_groups()
        min_sec = self._get_min_interval()
        max_sec = self._get_max_interval()
        active = len(self._tasks)
        prompt_count = len(self._get_custom_prompts())

        lines = [
            "📊 **主动聊天插件状态**",
            f"• 启用: {'✅ 已启用' if enabled else '❌ 已禁用'}",
            f"• 目标群数: {len(targets)}",
            f"• 活跃任务: {active}",
            f"• 时间间隔: {min_sec}s ~ {max_sec}s",
            f"• 免打扰: {self._get_silent_start()}:00-{self._get_silent_end()}:00",
            f"• 提示词数: {prompt_count} 条（每次随机选择）",
        ]
        if targets:
            lines.append("• 目标群:")
            for t in targets:
                umo = self._resolve_umo(t)
                mark = "🟢" if (umo and umo in self._tasks) else "🔴"
                lines.append(f"  {mark} {t}")

        yield event.plain_result("\n".join(lines))

    @proactive_chat.command("trigger")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_trigger(self, event: AstrMessageEvent):
        """管理员：在当前群立即触发一次主动聊天"""
        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("⚠️ 此指令仅支持群聊")
            return

        umo = event.unified_msg_origin
        target = group_id
        yield event.plain_result(f"🚀 正在为群 {target} 生成主动消息...")
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

