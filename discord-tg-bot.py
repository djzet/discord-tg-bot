import discord
from discord.ext import commands
from discord.ui import View, Button
import asyncio
import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
import json
import os

# Настройки
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TG_TOKEN = os.getenv("TG_TOKEN")
# Telegram Bot
bot = Bot(token=TG_TOKEN)
dp = Dispatcher()

# Автоматическое сохранение chat_id
CHAT_FILE = "chat_ids.json"
chat_ids = set()

def load_chat_ids():
    global chat_ids
    if os.path.exists(CHAT_FILE):
        with open(CHAT_FILE, 'r') as f:
            chat_ids = set(json.load(f))
    print(f"Загружено {len(chat_ids)} chat_id")

def save_chat_ids():
    with open(CHAT_FILE, 'w') as f:
        json.dump(list(chat_ids), f)

load_chat_ids()

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
client = commands.Bot(command_prefix='/', intents=intents)

subscribers = set()

class SubscribeView(View):
    def __init__(self):
        super().__init__(timeout=None)
        
    @discord.ui.button(label="✅ Подписаться", style=discord.ButtonStyle.green, emoji="🔔", custom_id="subscribe_btn")
    async def subscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        user_name = interaction.user.display_name or interaction.user.name
        
        # Добавляем в подписчиков
        subscribers.add(user_id)
        
        # ✅ Уведомление ВСЕМ в Telegram
        message = f"""
🔔 **{user_name} подписался на уведомления!**

👤 {interaction.user.name}#{interaction.user.discriminator}
✅ Голосовые события будут приходить
        """
        for chat_id in list(chat_ids):
            await send_telegram(chat_id, message)
        
        await interaction.response.send_message("✅ **Подписка активна!**\n🔔 Telegram уведомление отправлено.", ephemeral=True)
    
    @discord.ui.button(label="❌ Отписаться", style=discord.ButtonStyle.red, emoji="🔕", custom_id="unsubscribe_btn")
    async def unsubscribe(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        user_name = interaction.user.display_name or interaction.user.name
        
        # Удаляем из подписчиков
        subscribers.discard(user_id)
        
        # ✅ Уведомление ВСЕМ в Telegram
        message = f"""
🔕 **{user_name} отписался от уведомлений!**

👤 {interaction.user.name}#{interaction.user.discriminator}
❌ Голосовые события больше не приходят
        """
        for chat_id in list(chat_ids):
            await send_telegram(chat_id, message)
        
        await interaction.response.send_message("❌ **Подписка отменена!**\n🔕 Telegram уведомление отправлено.", ephemeral=True)

async def send_telegram(chat_id, text):
    """Отправка во ВСЕ сохраненные chat_id"""
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=data) as resp:
                return resp.status == 200
    except:
        return False

@dp.message(Command("start", "help"))
async def cmd_start(message):
    """Автоматически сохраняет chat_id пользователя"""
    chat_id = str(message.chat.id)
    if chat_id not in chat_ids:
        chat_ids.add(chat_id)
        save_chat_ids()
        print(f"✅ Новый chat_id: {chat_id}")
    
    await message.answer("""
🎮 **Discord → Telegram Voice Notifier**

✅ Вы подписаны на уведомления!

**Discord панель:**
`/panel` — кнопки Подписка/Отписка

**Уведомления:**
🟢 Зашел в голосовой канал (время)
🔴 Покинул голосовой канал
    """)

@client.event
async def on_ready():
    print(f'{client.user} подключился!')
    
    # ✅ ПРАВИЛЬНАЯ регистрация постоянных кнопок
    client.add_view(SubscribeView())
    
    asyncio.create_task(polling_task())
    
    # Уведомление во ВСЕ chat_id
    for chat_id in chat_ids:
        await send_telegram(chat_id, """
🚀 **BOT ЗАПУЩЕН с КНОПКАМИ!**

✅ Discord: `/panel` — кнопки подписки
✅ Telegram: /start автоматически подписывает
✅ Голосовые уведомления активны
        """)

async def polling_task():
    await dp.start_polling(bot)

@client.command()
async def panel(ctx):
    embed = discord.Embed(
        title="🔔 Voice Notifier",
        description="Кликните кнопку для управления подпиской:",
        color=0x00ff00
    )
    view = SubscribeView()
    await ctx.send(embed=embed, view=view)

@client.event
async def on_voice_state_update(member, before, after):
    """Все изменения голосовых каналов"""
    if member.id not in subscribers:
        return
        
    user = member.display_name or member.name
    
    # 🟢 НОВЫЙ ВХОД в голосовой канал (включая переход)
    if after.channel and (not before.channel or before.channel.id != after.channel.id):
        time_now = after.channel.name.split()[-1] if len(after.channel.name.split()) > 1 else "неизвестно"
        message = f"""
🟢 **{user} зашел в голосовой канал**

📢 **{after.channel.name}**
⏰ Время: `{time_now}`
👤 {member.name}#{member.discriminator}
        """
        
        # Отправка во ВСЕ chat_id
        for chat_id in list(chat_ids):
            await send_telegram(chat_id, message)
    
    # 🔴 ПОКИНУЛ голосовой канал
    elif before.channel and not after.channel:
        message = f"""
🔴 **{user} покинул голосовой канал**

👤 {member.name}#{member.discriminator}
        """
        
        # Отправка во ВСЕ chat_id
        for chat_id in list(chat_ids):
            await send_telegram(chat_id, message)

# Регистрация постоянных кнопок при запуске
async def setup_hook():
    client.add_view(SubscribeView())

client.setup_hook = setup_hook

if __name__ == "__main__":
    client.run(DISCORD_TOKEN)
