import discord
import requests
import os
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_server():
    server = HTTPServer(('0.0.0.0', 10000), DummyHandler)
    server.serve_forever()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'{client.user} готов!')
    
    server_thread = Thread(target=run_server, daemon=True)
    server_thread.start()
    print("HTTP сервер запущен")

async def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': msg,
        'parse_mode': 'HTML'
    }
    requests.post(url, data=data)

@client.event
async def on_voice_state_update(member, before, after):
    if member.bot: return
    
    guild = member.guild
    
    if before.channel is None and after.channel:
        msg = f"🔊 <b>{member.display_name}</b> зашёл в <b>{after.channel.name}</b>\nСервер: {guild.name}"
    elif before.channel and after.channel is None:
        msg = f"🔇 <b>{member.display_name}</b> вышел из <b>{before.channel.name}</b>\nСервер: {guild.name}"
    else:
        return
    
    await send_telegram(msg)

client.run(DISCORD_TOKEN)
