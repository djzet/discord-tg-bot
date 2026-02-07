import asyncio
import json
import logging
import os
import time
import sys
from typing import Set, Dict, Any
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord.ext import commands
from discord.ui import View, Button
import aiohttp
from dotenv import load_dotenv

storage = None
notifier_obj = None


# Встроенный MessageFormatter
class MessageFormatter:
    @staticmethod
    def format_for_discord(text: str, **kwargs) -> str:
        return MessageFormatter._replace_placeholders(text, kwargs) if text else ""
    
    @staticmethod
    def format_for_telegram(text: str, **kwargs) -> str:
        formatted = MessageFormatter._replace_placeholders(text, kwargs)
        return formatted[:4096] if len(formatted) > 4096 else formatted
    
    @staticmethod
    def _replace_placeholders(text: str, replacements: Dict[str, Any]) -> str:
        try:
            return text.format(**{k: str(v) for k, v in replacements.items()})
        except KeyError:
            return text


# Загрузка переменных окружения
load_dotenv()

# Константы
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TG_TOKEN = os.getenv("TG_TOKEN")
CHAT_FILE = "chat_ids.json"
SUBS_FILE = "subscribers.json"
MESSAGES_FILE = "messages.json"

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class BotConfig:
    discord_token: str
    telegram_token: str


def validate_tokens():
    if not DISCORD_TOKEN:
        logger.critical("❌ DISCORD_TOKEN не найден!")
        sys.exit(1)
    if not TG_TOKEN:
        logger.critical("❌ TG_TOKEN не найден!")
        sys.exit(1)
    return BotConfig(DISCORD_TOKEN, TG_TOKEN)


class AtomicFileHandler:
    _locks = {}
    
    @classmethod
    async def _get_lock(cls, filename):
        if filename not in cls._locks:
            cls._locks[filename] = asyncio.Lock()
        return cls._locks[filename]
    
    @staticmethod
    async def load(filename, default_factory=set):
        path = Path(filename)
        if not path.exists():
            return default_factory()
        
        lock = await AtomicFileHandler._get_lock(filename)
        async with lock:
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return set(data)
                elif isinstance(data, dict) and 'data' in data:
                    return set(data['data'])
                return default_factory()
            except:
                return default_factory()
    
    @staticmethod
    async def save(filename, data):
        path = Path(filename)
        lock = await AtomicFileHandler._get_lock(filename)
        async with lock:
            try:
                temp = path.with_suffix('.tmp')
                with open(temp, 'w', encoding='utf-8') as f:
                    json.dump({'data': list(data)}, f, indent=2, ensure_ascii=False)
                temp.replace(path)
                return True
            except:
                return False


class DataStorage:
    def __init__(self):
        self.chat_ids: Set[str] = set()
        self.subscribers: Set[int] = set()
        self.bot_start_time = time.time()  # ✅ Время запуска
    
    async def load_all(self):
        self.chat_ids = await AtomicFileHandler.load(CHAT_FILE)
        self.subscribers = await AtomicFileHandler.load(SUBS_FILE)
        logger.info(f"Загружено {len(self.chat_ids)} чатов, {len(self.subscribers)} подписчиков")
    
    async def save_chat_ids(self):
        return await AtomicFileHandler.save(CHAT_FILE, self.chat_ids)
    
    async def save_subscribers(self):
        return await AtomicFileHandler.save(SUBS_FILE, self.subscribers)
    
    async def add_chat_id(self, chat_id: str) -> bool:
        if chat_id not in self.chat_ids:
            self.chat_ids.add(chat_id)
            return await self.save_chat_ids()
        return False
    
    async def toggle_subscription(self, user_id: int, subscribe: bool) -> bool:
        changed = False
        if subscribe and user_id not in self.subscribers:
            self.subscribers.add(user_id)
            changed = True
        elif not subscribe and user_id in self.subscribers:
            self.subscribers.discard(user_id)
            changed = True
        if changed:
            return await self.save_subscribers()
        return False


class MessageManager:
    def __init__(self, filename=MESSAGES_FILE):
        self.messages = {}
        self._load(filename)
    
    def _load(self, filename):
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                self.messages = json.load(f)
            logger.info("Сообщения загружены")
        except Exception as e:
            logger.error(f"Ошибка сообщений: {e}")
    
    def get(self, *path, default=""):
        result = self.messages
        for key in path:
            if isinstance(result, dict):
                result = result.get(key, default)
            else:
                return default
        return str(result) if result else default


class TelegramAPI:
    def __init__(self, token):
        self.token = token
        self.session = None
        self.last_request = 0
    
    async def _make_request(self, method, data):
        await asyncio.sleep(max(0, 0.3 - (time.time() - self.last_request)))  # ✅ Увеличен delay
        self.last_request = time.time()
        
        try:
            if self.session is None:
                connector = aiohttp.TCPConnector(limit=10)
                self.session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=15))
            
            url = f"https://api.telegram.org/bot{self.token}/{method}"
            async with self.session.post(url, json=data) as resp:
                if resp.status == 200:
                    return await resp.json()
        except:
            pass
        return None
    
    async def send_message(self, chat_id, text):
        if not text or len(text) > 4096:
            return False
        data = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True, "parse_mode": "Markdown"}
        result = await self._make_request("sendMessage", data)
        return bool(result and result.get('ok'))
    
    async def get_updates(self, offset=0):
        data = {"offset": offset, "timeout": 30, "allowed_updates": ["message"]}
        result = await self._make_request("getUpdates", data)
        return result


class TelegramNotifier:
    def __init__(self, api, storage, messages):
        self.api = api
        self.storage = storage
        self.messages = messages
        self.bot = None  # ✅ Ссылка на Discord бота
    
    async def handle_start(self, chat_id):
        added = await self.storage.add_chat_id(chat_id)
        msg = self.messages.get("telegram", "commands", "start", "welcome" if added else "already_registered")
        return await self.api.send_message(chat_id, MessageFormatter.format_for_telegram(msg))
    
    async def handle_status(self, chat_id):
        """✅ Полный статус из messages.json"""
        await self.api.send_message(chat_id, self.messages.get("telegram", "commands", "status", "loading"))
        
        subscribers_list = []
        if self.bot:
            for user_id in self.storage.subscribers:
                user = self.bot.get_user(user_id)
                if user:
                    display_name = user.display_name or user.name
                    subscribers_list.append(f"• **{display_name}** (`{user_id}`)")
                else:
                    subscribers_list.append(f"• `ID {user_id}`")
        
        subs_count = len(subscribers_list)
        if not subscribers_list:
            subs_text = self.messages.get("telegram", "errors", "no_subscribers")
        else:
            subs_text = "\n".join(subscribers_list[:20])
            if subs_count > 20:
                subs_text += f"\n... и ещё `{subs_count-20}`"
        
        uptime = str(timedelta(seconds=int(time.time() - self.storage.bot_start_time)))
        
        status_msg = self.messages.get("telegram", "commands", "status", "title").format(
            uptime=uptime,
            subs_count=subs_count,
            chats_count=len(self.storage.chat_ids),
            subscribers_list=subs_text
        )
        
        return await self.api.send_message(chat_id, MessageFormatter.format_for_telegram(status_msg))
    
    async def process_update(self, update):
        try:
            msg = update.get('message', {})
            text = msg.get('text', '').strip()
            chat_id = str(msg.get('chat', {}).get('id'))
            
            if text == '/start':
                return await self.handle_start(chat_id)
            elif text == '/help':
                msg_text = self.messages.get("telegram", "commands", "help", "title")
                return await self.api.send_message(chat_id, MessageFormatter.format_for_telegram(msg_text))
            elif text == '/status':
                return await self.handle_status(chat_id)
            else:
                error = self.messages.get("telegram", "errors", "invalid_command")
                return await self.api.send_message(chat_id, MessageFormatter.format_for_telegram(error))
        except Exception as e:
            logger.error(f"TG обработка: {e}")
            return False
    
    async def broadcast(self, text):
        if not self.storage.chat_ids or not text:
            logger.warning(f"Пропуск рассылки: чатов={len(self.storage.chat_ids)}, текст='{text[:50]}'")
            return 0
        
        text = MessageFormatter.format_for_telegram(text)
        sent = 0
        for i, chat_id in enumerate(self.storage.chat_ids):
            logger.info(f"Отправка в чат #{i+1}: {chat_id}")
            if await self.api.send_message(chat_id, text):
                sent += 1
            else:
                logger.error(f"❌ ОШИБКА отправки в {chat_id}")
        
        logger.info(f"Рассылка: {sent}/{len(self.storage.chat_ids)}")
        return sent


class DiscordBot(commands.Bot):
    def __init__(self, config, storage, messages, telegram):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        
        super().__init__(command_prefix='/', intents=intents, help_command=None)
        self.config = config
        self.storage = storage
        self.messages = messages
        self.telegram = telegram
        self.start_time = time.time()
    
    async def setup_hook(self):
        self.add_view(SubscribeView(self))
    
    async def on_ready(self):
        logger.info(f'Discord бот {self.user} подключился!')
    
    async def on_voice_state_update(self, member, before, after):
        if member.id not in self.storage.subscribers or before.channel == after.channel:
            return
        
        timestamp = datetime.now()
        
        try:
            if after.channel and before.channel is None:
                # 🟢 ПЕРВЫЙ ВХОД
                template = self.messages.get("telegram", "voice_events", "joined")
                msg = template.format(
                    user_name=member.display_name or member.name,
                    channel_name=after.channel.name,
                    user_id=member.id,
                    time=timestamp.strftime("%H:%M")
                )
                await self.telegram.broadcast(msg)
                
            elif before.channel and after.channel is None:
                # 🔴 ВЫХОД
                template = self.messages.get("telegram", "voice_events", "left")
                msg = template.format(
                    user_name=member.display_name or member.name,
                    channel_name=before.channel.name,
                    user_id=member.id,
                    time=timestamp.strftime("%H:%M")
                )
                await self.telegram.broadcast(msg)
                
            elif before.channel and after.channel and before.channel != after.channel:
                # 🔄 ПЕРЕМЕЩЕНИЕ
                template = self.messages.get("telegram", "voice_events", "moved")
                msg = template.format(
                    user_name=member.display_name or member.name,
                    channel_name=after.channel.name,
                    user_id=member.id,
                    time=timestamp.strftime("%H:%M")
                )
                await self.telegram.broadcast(msg)
                
        except Exception as e:
            logger.error(f"Voice error: {e}")


class SubscribeView(View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot
    
    @discord.ui.button(label="🔔 Подписаться", style=discord.ButtonStyle.green, custom_id="subscribe_btn")
    async def subscribe(self, interaction: discord.Interaction, button: Button):
        await self._update_subscription(interaction, True)
    
    @discord.ui.button(label="🔕 Отписаться", style=discord.ButtonStyle.red, custom_id="unsubscribe_btn")
    async def unsubscribe(self, interaction: discord.Interaction, button: Button):
        await self._update_subscription(interaction, False)
    
    async def _update_subscription(self, interaction, subscribe):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # ✅ ПРОВЕРКА ТЕКУЩЕГО СОСТОЯНИЯ
        was_subscribed = user.id in self.bot.storage.subscribers
        action_name = "subscribed" if subscribe else "unsubscribed"
        
        # Если уже в нужном состоянии
        if was_subscribed == subscribe:
            status_key = "already_subscribed" if subscribe else "already_unsubscribed"
            status_msg = self.bot.messages.get("discord", "subscription", status_key, "msg")
            await interaction.followup.send(
                MessageFormatter.format_for_discord(status_msg), 
                ephemeral=True
            )
            logger.info(f"👤 {user.name} повторная попытка ({'🔔' if subscribe else '🔕'})")
            return
        
        # Изменяем подписку
        success = await self.bot.storage.toggle_subscription(user.id, subscribe)
        
        # Discord ответ
        msg = self.bot.messages.get("discord", "subscription", action_name)
        await interaction.followup.send(MessageFormatter.format_for_discord(msg), ephemeral=True)
        
        # Telegram уведомление
        template = self.bot.messages.get("telegram", "subscription", action_name, "content")
        text = template.format(
            user_name=user.display_name or user.name,
            user_id=user.id,
            timestamp=timestamp,
            total_subs=len(self.bot.storage.subscribers)
        )
        await self.bot.telegram.broadcast(text)
        
        logger.info(f"👤 {user.name} ({'🔔' if subscribe else '🔕'}) - {len(self.bot.storage.subscribers)} всего")


async def setup_commands(bot):
    @bot.command()
    async def notifier(ctx):
        embed = discord.Embed(
            title="🔔 Voice Notifier", 
            description="Управление подписками", 
            color=0x00ff88
        )
        msg_stats = bot.messages.get("discord", "notifier", "stats").format(
            subs_count=len(bot.storage.subscribers),
            chats_count=len(bot.storage.chat_ids)
        )
        embed.add_field(name="📊 Статистика", value=msg_stats, inline=False)
        
        telegram_info = bot.messages.get("discord", "notifier", "telegram_info")
        embed.add_field(name="📱 Telegram", value=telegram_info, inline=False)
        
        await ctx.send(embed=embed, view=SubscribeView(bot))


async def telegram_polling(notifier):
    """Telegram long polling"""
    offset = 0
    while True:
        try:
            updates = await notifier.api.get_updates(offset)
            if updates and updates.get('ok'):
                for update in updates.get('result', []):
                    offset = max(offset, update['update_id'] + 1)
                    await notifier.process_update(update)
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(1)

async def keep_alive_task():
    """Keep-Alive для Render"""
    global storage, notifier_obj
    while True:
        try:
            uptime = str(timedelta(seconds=int(time.time() - storage.bot_start_time)))
            logger.info(f"👾 ALIVE | Подписчиков: {len(storage.subscribers)} | TG: {len(storage.chat_ids)} | Uptime: {uptime}")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Keep-alive error: {e}")
            await asyncio.sleep(60)


async def main():
    config = validate_tokens()
    global storage, notifier_obj
    
    storage = DataStorage() 
    messages = MessageManager()
    
    api = TelegramAPI(config.telegram_token)
    notifier = TelegramNotifier(api, storage, messages)
    notifier_obj = notifier
    bot = DiscordBot(config, storage, messages, notifier)
    notifier.bot = bot
    
    await storage.load_all()
    await setup_commands(bot)
    
    try:
        # Startup уведомление
        startup_msg = messages.get("telegram", "system", "bot_started")
        await notifier.broadcast(MessageFormatter.format_for_telegram(
            startup_msg, start_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        
        # Запуск задач
        bot_task = asyncio.create_task(bot.start(config.discord_token))
        polling_task = asyncio.create_task(telegram_polling(notifier))
        keep_alive_task_ = asyncio.create_task(keep_alive_task())
        
        done, pending = await asyncio.wait(
            [bot_task, polling_task, keep_alive_task_], 
            return_when=asyncio.FIRST_COMPLETED
        )
        
        for task in pending:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except:
                pass
                
    except KeyboardInterrupt:
        logger.info("🛑 Ctrl+C получен")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
    finally:
        logger.info("📢 Отправка уведомления об остановке...")
        try:
            shutdown_msg = messages.get("telegram", "system", "bot_stopped")
            await notifier_obj.broadcast(MessageFormatter.format_for_telegram(shutdown_msg))
        except:
            pass
        
        try:
            if api.session and not api.session.closed:
                await api.session.close()
        except:
            pass
        try:
            if not bot.is_closed():
                await bot.close()
        except:
            pass
        logger.info("✅ Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 До свидания!")
