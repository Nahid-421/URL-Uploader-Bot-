import os
import logging
import asyncio
import re
import uuid
import time
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo
from telethon.errors.rpcerrorlist import MessageNotModifiedError
import yt_dlp
from flask import Flask
import threading

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    API_ID = int(os.environ.get("API_ID"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))
except (ValueError, TypeError, KeyError):
    logger.critical("API_ID, API_HASH, BOT_TOKEN, and LOG_CHANNEL must be set correctly.")
    exit(1)

COOKIES_FILE_PATH = "/app/cookies.txt"
MAX_FILE_SIZE = 1.95 * 1024 * 1024 * 1024
UPLOAD_WORKERS = 8

client = TelegramClient('bot_session', API_ID, API_HASH)
user_data = {}
main_loop = None

def cleanup_files(*paths):
    for path in paths:
        if path and os.path.exists(path):
            try: os.remove(path)
            except OSError as e: logger.error(f"Error deleting file {path}: {e}")

def humanbytes(size):
    if not size: return "0B"
    power, n = 1024, 0
    power_labels = {0: 'B', 1: 'KiB', 2: 'MiB', 3: 'GiB', 4: 'TiB'}
    while size >= power and n < len(power_labels) - 1:
        size /= power; n += 1
    return f"{size:.2f} {power_labels[n]}"

def split_file(file_path, chunk_size):
    if os.path.getsize(file_path) <= chunk_size: return [file_path]
    parts, base_name = [], os.path.splitext(file_path)[0]
    with open(file_path, 'rb') as f:
        part_num = 0
        while True:
            chunk = f.read(chunk_size)
            if not chunk: break
            part_num += 1
            part_filename = f"{base_name}.part{str(part_num).zfill(3)}"
            with open(part_filename, 'wb') as part_file: part_file.write(chunk)
            parts.append(part_filename)
    return parts

@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user = await event.get_sender()
    await event.respond(f"👋 হাই {user.first_name}!\n\nআমি একজন শক্তিশালী এবং Universal URL ডাউনলোডার বট।")

@client.on(events.NewMessage(pattern=re.compile(r'https?://')))
async def url_handler(event):
    user_id = event.sender_id
    if user_id in user_data and 'state' in user_data[user_id]:
        await event.respond("আপনার একটি প্রক্রিয়া ইতিমধ্যে চলছে। অনুগ্রহ করে এটি শেষ করুন অথবা /cancel করুন।")
        return
    url = event.text
    user_data[user_id] = {'url': url, 'state': 'waiting_for_format'}
    buttons = [[Button.inline("🎬 ভিডিও", data="video"), Button.inline("📄 ফাইল", data="document")]]
    await event.respond("আপনি এই লিঙ্কটি কোন ফরম্যাটে চান?", buttons=buttons)

@client.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    if user_id not in user_data or user_data[user_id]['state'] != 'waiting_for_format':
        try: await event.answer("এই বাটনটি আপনার জন্য নয় অথবা এর মেয়াদ শেষ।", alert=True)
        except MessageNotModifiedError: pass
        return
    choice = event.data.decode('utf-8')
    user_data[user_id]['format'] = choice
    try:
        user_data[user_id]['state'] = 'waiting_for_filename'
        await event.edit("চমৎকার! এখন ফাইলের একটি নাম দিন।\n\nডিফল্ট নাম ব্যবহার করতে চাইলে `/skip` টাইপ করুন।")
    except MessageNotModifiedError:
        logger.warning("Message not modified, likely due to double-click. Ignoring.")

@client.on(events.NewMessage)
async def message_handler(event):
    user_id = event.sender_id
    if user_id not in user_data or 'state' not in user_data[user_id]: return
    state = user_data[user_id]['state']
    if state == 'waiting_for_filename':
        if event.text.strip().lower() == '/skip':
            user_data[user_id]['custom_filename'] = None
            await event.respond("👍 ঠিক আছে, ডিফল্ট নাম ব্যবহার করা হবে।")
        else:
            safe_filename = re.sub(r'[\\/*?:"<>|]', "", event.text)
            user_data[user_id]['custom_filename'] = safe_filename
            await event.respond(f"✅ ফাইলের নাম সেট করা হয়েছে: `{safe_filename}`")
        if user_data[user_id]['format'] == 'video':
            user_data[user_id]['state'] = 'waiting_for_thumbnail'
            await event.respond("এখন ভিডিওর জন্য একটি কাস্টম থাম্বনেইল পাঠান। না চাইলে `/skip` টাইপ করে পাঠান।")
        else:
            user_data[user_id]['state'] = 'processing'
            await event.respond("ডাউনলোড শুরু হচ্ছে...")
            await process_and_upload(event, user_id)
    elif state == 'waiting_for_thumbnail':
        user_data[user_id]['state'] = 'processing'
        if event.photo:
            thumb_path = await client.download_media(event.photo, file=f"downloads/{uuid.uuid4()}.jpg")
            user_data[user_id]['thumbnail_path'] = thumb_path
            await event.respond("✅ থাম্বনেইল পেয়েছি। ডাউনলোড শুরু হচ্ছে...")
        elif event.text.strip().lower() == '/skip':
            user_data[user_id]['thumbnail_path'] = None
            await event.respond("👍 ঠিক আছে, ডিফল্ট থাম্বনেইল ব্যবহার করা হবে। ডাউনলোড শুরু হচ্ছে...")
        else:
            await event.respond("অনুগ্রহ করে একটি ছবি পাঠান অথবা `/skip` টাইপ করুন।")
            user_data[user_id]['state'] = 'waiting_for_thumbnail'
            return
        await process_and_upload(event, user_id)

async def process_and_upload(event, user_id):
    user_info = user_data.get(user_id, {})
    url, file_format, thumbnail_path, custom_filename = [user_info.get(k) for k in ['url', 'format', 'thumbnail_path', 'custom_filename']]
    if not url or not file_format:
        await event.respond("কিছু একটা সমস্যা হয়েছে, অনুগ্রহ করে আবার চেষ্টা করুন।"); return
    progress_msg = await event.respond("প্রস্তুতি চলছে...")
    start_time = time.time()
    last_update_time, downloaded_file_path = 0, None
    
    def make_progress_bar(p): return "█" * round(p / 10) + "░" * (10 - round(p / 10))
    def download_progress_hook(d):
        nonlocal last_update_time, downloaded_file_path
        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_update_time > 2:
                p_str, speed, eta = [d.get('_percent_str', '0%').strip(), d.get('_speed_str', 'N/A').strip(), d.get('_eta_str', 'N/A').strip()]
                try: p = float(p_str.strip('%'))
                except ValueError: p = 0
                downloaded, total = [d.get('downloaded_bytes', 0), d.get('total_bytes_estimate') or d.get('total_bytes', 0)]
                text = (f"**📥 Downloading...**\n`[{make_progress_bar(p)}]`\n\n"
                        f"**P:** `{p_str}`\n**Size:** `{humanbytes(downloaded)} of {humanbytes(total)}`\n"
                        f"**Speed:** `{speed}`\n**ETA:** `{eta}`")
                if main_loop: asyncio.run_coroutine_threadsafe(progress_msg.edit(text), main_loop)
                last_update_time = current_time
        elif d['status'] == 'finished': downloaded_file_path = d.get('filename') or d.get('info_dict', {}).get('_filename')
    async def upload_progress_callback(current, total):
        nonlocal last_update_time
        current_time = time.time()
        if current_time - last_update_time > 2:
            p = round((current / total) * 100)
            text = (f"**🚀 Uploading...**\n`[{make_progress_bar(p)}]`\n\n"
                    f"**P:** `{p}%`\n**Size:** `{humanbytes(current)} of {humanbytes(total)}`")
            try: await progress_msg.edit(text)
            except MessageNotModifiedError: pass
            last_update_time = current_time

    async def upload_and_log(file_to_upload, caption, filename, thumb=None):
        is_video = file_to_upload.endswith(('.mp4', '.mkv', '.webm'))
        attributes = [DocumentAttributeVideo(duration=0, w=0, h=0, supports_streaming=True)] if file_format == 'video' and is_video else []
        await client.send_file(event.chat_id, file=file_to_upload, thumb=thumb, attributes=attributes,
                               force_document=(file_format == 'document' or not is_video),
                               caption=caption, file_name=filename, progress_callback=upload_progress_callback,
                               workers=UPLOAD_WORKERS)
        end_time = time.time()
        time_taken = round(end_time - start_time)
        user = await event.get_sender()
        log_message = (f"✅ **সফলভাবে আপলোড সম্পন্ন!**\n\n👤 **ব্যবহারকারী:** [{user.first_name}](tg://user?id={user.id})\n"
                       f"🆔 **ইউজার আইডি:** `{user.id}`\n📄 **ফাইলের নাম:** `{filename}`\n"
                       f"📥 **ডাউনলোড লিঙ্ক:** `{url}`\n⏱️ **সময় লেগেছে:** `{time_taken} সেকেন্ড`")
        if LOG_CHANNEL:
            await client.send_message(LOG_CHANNEL, log_message, link_preview=False)

    output_template = f"downloads/{uuid.uuid4()}/%(title)s.%(ext)s"
    ydl_opts = {'outtmpl': output_template, 'noplaylist': True, 'nocheckcertificate': True,
                'progress_hooks': [download_progress_hook], 'format': 'bestvideo+bestaudio/best',
                'merge_output_format': 'mkv', 'extractor_args': {'youtube': {'player_client': 'android'}},
                'cookiefile': COOKIES_FILE_PATH if os.path.exists(COOKIES_FILE_PATH) else None}
    
    downloaded_parts_to_clean = []
    try:
        if main_loop: await main_loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))
        if not downloaded_file_path or not os.path.exists(downloaded_file_path): raise ValueError("ফাইল ডাউনলোড করা যায়নি।")
        await progress_msg.edit("ডাউনলোড সম্পন্ন! এখন আপলোড করার জন্য প্রস্তুত হচ্ছে...")
        
        file_size, file_ext = [os.path.getsize(downloaded_file_path), downloaded_file_path.split('.')[-1]]
        final_caption = custom_filename or os.path.basename(downloaded_file_path).rsplit('.', 1)[0]
        
        if file_size > MAX_FILE_SIZE:
            await progress_msg.edit(f"ফাইলটি বড় হওয়ায় ({humanbytes(file_size)}), এটিকে {humanbytes(MAX_FILE_SIZE)} খণ্ডে বিভক্ত করা হচ্ছে...")
            split_parts = split_file(downloaded_file_path, int(MAX_FILE_SIZE))
            downloaded_parts_to_clean = split_parts
            for i, part in enumerate(split_parts):
                part_caption = f"**Part {i+1}/{len(split_parts)}**\n\n{final_caption}"
                final_filename = f"{final_caption} - Part {i+1}.{file_ext}"
                await upload_and_log(part, part_caption, final_filename, thumb=thumbnail_path if i == 0 else None)
                await asyncio.sleep(2)
        else:
            final_filename = f"{final_caption}.{file_ext}"
            await upload_and_log(downloaded_file_path, final_caption, final_filename, thumb=thumbnail_path)
        await progress_msg.delete()
    except Exception as e:
        if isinstance(e, MessageNotModifiedError): logger.warning(f"Message not modified for user {user_id}. Ignoring.")
        else:
            logger.error(f"Error for user {user_id}: {e}")
            try: await progress_msg.edit(f"❌ একটি মারাত্মক সমস্যা হয়েছে।\n**ত্রুটি:** `{str(e)[:500]}`")
            except Exception as edit_error: logger.error(f"Could not edit message: {edit_error}")
    finally:
        cleanup_files(downloaded_file_path, *downloaded_parts_to_clean)
        if user_id in user_data: del user_data[user_id]

app = Flask(__name__)
@app.route('/')
def health_check(): return "Bot is running healthily!", 200
def run_flask(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
async def main_async_runner():
    global main_loop
    main_loop = asyncio.get_running_loop()
    os.makedirs("downloads", exist_ok=True)
    threading.Thread(target=run_flask, daemon=True).start()
    await client.start(bot_token=BOT_TOKEN)
    logger.info("Bot has started successfully! Ready to be powerful.")
    await client.run_until_disconnected()

if __name__ == '__main__':
    if all([API_ID, API_HASH, BOT_TOKEN]):
        asyncio.run(main_async_runner())
    else:
        logger.critical("Crucial environment variables are missing.")
