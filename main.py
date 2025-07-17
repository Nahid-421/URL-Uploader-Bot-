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

# --- লগিং সেটআপ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- এনভায়রনমেন্ট ভ্যারিয়েবল থেকে ক্রেডেনশিয়াল সংগ্রহ ---
try:
    API_ID = int(os.environ.get("API_ID"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
except (ValueError, TypeError):
    logger.critical("API_ID, API_HASH, and BOT_TOKEN must be set correctly in environment variables.")
    exit(1)

# --- Telethon ক্লায়েন্ট ইনিশিয়ালাইজেশন ---
client = TelegramClient('bot_session', API_ID, API_HASH)

# --- ইউজারদের অবস্থা এবং মূল ইভেন্ট লুপ ট্র্যাক করা ---
user_data = {}
main_loop = None

# --- Helper Functions ---
def cleanup_files(*paths):
    for path in paths:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                logger.error(f"Error deleting file {path}: {e}")

# --- Command Handlers ---
@client.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    user = await event.get_sender()
    await event.respond(
        f"👋 হাই {user.first_name}!\n\nআমি একজন শক্তিশালী URL ডাউনলোডার বট। আমাকে যেকোনো লিঙ্ক দিন, আমি সেটি **2 GB পর্যন্ত** ফাইল হিসেবে ডাউনলোড করে দেবো।\n\nসাহায্যের জন্য /help কমান্ড ব্যবহার করুন।"
    )

@client.on(events.NewMessage(pattern='/help'))
async def help_handler(event):
    await event.respond(
        "**ℹ️ কিভাবে ব্যবহার করবেন:**\n\n1. আমাকে যেকোনো URL পাঠান।\n2. 'ভিডিও' বা 'ফাইল' ফরম্যাট বেছে নিন।\n3. (ঐচ্ছিক) ভিডিওর জন্য একটি কাস্টম থাম্বনেইল হিসেবে ছবি পাঠান। না চাইলে মেসেজ দিয়ে `/skip` লিখুন।\n\n**⚠️ সীমাবদ্ধতা:**\n- সর্বোচ্চ ফাইলের আকার: **2 GB**\n- বড় ফাইল ডাউনলোড এবং আপলোড করতে সার্ভারের ক্ষমতার উপর নির্ভর করে কিছুটা সময় লাগতে পারে।\n\nযেকোনো পর্যায়ে প্রক্রিয়া বাতিল করতে `/cancel` কমান্ড দিন।"
    )

@client.on(events.NewMessage(pattern='/cancel'))
async def cancel_handler(event):
    user_id = event.sender_id
    if user_id in user_data:
        cleanup_files(user_data[user_id].get('thumbnail_path'))
        del user_data[user_id]
        await event.respond("✅ প্রক্রিয়াটি সফলভাবে বাতিল করা হয়েছে।")
    else:
        await event.respond("এখন কোনো প্রক্রিয়া চালু নেই।")

# --- Core Logic Handlers ---
@client.on(events.NewMessage(pattern=re.compile(r'https?://')))
async def url_handler(event):
    user_id = event.sender_id
    if user_id in user_data and 'state' in user_data[user_id]:
        await event.respond("আপনার একটি প্রক্রিয়া ইতিমধ্যে চলছে। অনুগ্রহ করে এটি শেষ করুন অথবা /cancel করুন।")
        return

    url = event.text
    user_data[user_id] = {'url': url, 'state': 'waiting_for_format'}
    
    buttons = [
        [Button.inline("🎬 ভিডিও (Video)", data="video"), Button.inline("📄 ফাইল (File)", data="document")]
    ]
    await event.respond("আপনি কোন ফরম্যাটে ফাইলটি চান?", buttons=buttons)

@client.on(events.CallbackQuery)
async def callback_handler(event):
    user_id = event.sender_id
    if user_id not in user_data or user_data[user_id]['state'] != 'waiting_for_format':
        try:
            await event.answer("এই বাটনটি আপনার জন্য নয় অথবা এর মেয়াদ শেষ।", alert=True)
        except MessageNotModifiedError:
            pass
        return

    choice = event.data.decode('utf-8')
    user_data[user_id]['format'] = choice
    
    try:
        if choice == 'video':
            user_data[user_id]['state'] = 'waiting_for_thumbnail'
            await event.edit("চমৎকার! এখন ভিডিওর জন্য একটি কাস্টম থাম্বনেইল পাঠান। না চাইলে `/skip` টাইপ করে পাঠান।")
        else:
            await event.edit("✅ ফরম্যাট সিলেক্ট হয়েছে। ডাউনলোড শুরু হচ্ছে...")
            await process_and_upload(event, user_id)
    except MessageNotModifiedError:
        logger.warning("Message not modified, likely due to double-click. Ignoring.")
        pass

@client.on(events.NewMessage)
async def message_handler(event):
    user_id = event.sender_id
    if user_id not in user_data or 'state' not in user_data[user_id]:
        return

    if user_data[user_id]['state'] == 'waiting_for_thumbnail':
        if event.photo:
            thumb_path = await client.download_media(event.photo, file=f"downloads/{uuid.uuid4()}.jpg")
            user_data[user_id]['thumbnail_path'] = thumb_path
            await event.respond("✅ থাম্বনেইল পেয়েছি। ডাউনলোড শুরু হচ্ছে...")
            await process_and_upload(event, user_id)
        elif event.text.strip().lower() == '/skip':
            user_data[user_id]['thumbnail_path'] = None
            await event.respond("👍 ঠিক আছে, ডিফল্ট থাম্বনেইল ব্যবহার করা হবে। ডাউনলোড শুরু হচ্ছে...")
            await process_and_upload(event, user_id)
        else:
            await event.respond("অনুগ্রহ করে একটি ছবি পাঠান অথবা `/skip` টাইপ করুন।")

# --- Download and Upload Function ---
async def process_and_upload(event, user_id):
    user_info = user_data.get(user_id, {})
    url = user_info.get('url')
    file_format = user_info.get('format')
    thumbnail_path = user_info.get('thumbnail_path')
    
    if not url or not file_format:
        await event.respond("কিছু একটা সমস্যা হয়েছে, অনুগ্রহ করে আবার চেষ্টা করুন।")
        return

    progress_msg = await event.respond("প্রস্তুতি চলছে...")
    last_update_time = 0
    downloaded_file_path = None

    def make_progress_bar(percentage):
        filled_blocks = round(percentage / 10)
        empty_blocks = 10 - filled_blocks
        return "█" * filled_blocks + "░" * empty_blocks

    def download_progress_hook(d):
        nonlocal last_update_time
        if d['status'] == 'downloading':
            current_time = time.time()
            if current_time - last_update_time > 2:
                percentage_str = d.get('_percent_str', '0%').strip()
                try:
                    percentage = float(percentage_str.strip('%'))
                except ValueError:
                    percentage = 0
                
                speed = d.get('_speed_str', 'N/A').strip()
                total_size = d.get('total_bytes_estimate') or d.get('total_bytes', 0)
                total_size_str = f"{total_size / 1048576:.2f} MB" if total_size > 0 else "Unknown"
                progress_bar = make_progress_bar(percentage)
                text = (
                    f"📥 **ডাউনলোড হচ্ছে...**\n"
                    f"`[{progress_bar}] {percentage_str}`\n"
                    f"**গতি:** `{speed}` | **ফাইলের আকার:** `{total_size_str}`"
                )
                if main_loop:
                    asyncio.run_coroutine_threadsafe(progress_msg.edit(text), main_loop)
                last_update_time = current_time
        elif d['status'] == 'finished':
            nonlocal downloaded_file_path
            downloaded_file_path = d.get('filename') or d.get('info_dict', {}).get('_filename')

    async def upload_progress_callback(current, total):
        nonlocal last_update_time
        current_time = time.time()
        if current_time - last_update_time > 2:
            percentage = round((current / total) * 100)
            progress_bar = make_progress_bar(percentage)
            text = (
                f"🚀 **আপলোড হচ্ছে...**\n"
                f"`[{progress_bar}] {percentage}%`"
            )
            await progress_msg.edit(text)
            last_update_time = current_time

    output_template = f"downloads/{uuid.uuid4()}/%(title)s.%(ext)s"
    ydl_opts = {
        'outtmpl': output_template,
        'noplaylist': True,
        'nocheckcertificate': True,
        'progress_hooks': [download_progress_hook],
        'format': 'bestvideo[ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    }

    if file_format == 'video':
        ydl_opts['postprocessors'] = [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]

    try:
        if main_loop:
            await main_loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))
        
        if not downloaded_file_path or not os.path.exists(downloaded_file_path):
            logger.warning("Initial download attempt failed. Retrying with a more generic format.")
            ydl_opts['format'] = 'bestvideo+bestaudio/best'
            if main_loop:
                await main_loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=True))

        if not downloaded_file_path or not os.path.exists(downloaded_file_path):
            raise ValueError("ফাইল ডাউনলোড করা যায়নি। অনেক চেষ্টার পরেও লিঙ্কটি থেকে কোনো মিডিয়া পাওয়া যায়নি।")

        await progress_msg.edit("ডাউনলোড সম্পন্ন! এখন আপলোড করা হচ্ছে...")
        last_update_time = 0

        file_attributes = []
        is_video_file = downloaded_file_path.endswith(('.mp4', '.mkv', '.webm'))
        if file_format == 'video' and is_video_file:
            file_attributes.append(DocumentAttributeVideo(duration=0, w=0, h=0, supports_streaming=True))
        
        await client.send_file(
            event.chat_id,
            file=downloaded_file_path,
            thumb=thumbnail_path,
            attributes=file_attributes,
            force_document=(file_format == 'document' or not is_video_file),
            caption=os.path.basename(downloaded_file_path).rsplit('.', 1)[0],
            progress_callback=upload_progress_callback
        )
        await progress_msg.delete()

    except Exception as e:
        logger.error(f"Error processing for user {user_id}: {e}")
        try:
            await progress_msg.edit(f"❌ একটি মারাত্মক সমস্যা হয়েছে।\n\n**ত্রুটি:** `{str(e)[:500]}`")
        except Exception as edit_error:
            logger.error(f"Could not edit progress message: {edit_error}")
    finally:
        cleanup_files(downloaded_file_path, thumbnail_path)
        if user_id in user_data:
            del user_data[user_id]

# --- Flask Web Server & Main Execution ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot is running healthily!", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

async def main_async_runner():
    global main_loop
    main_loop = asyncio.get_running_loop()

    os.makedirs("downloads", exist_ok=True)
    
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    await client.start(bot_token=BOT_TOKEN)
    logger.info("Bot has started successfully and web server is running!")
    await client.run_until_disconnected()

if __name__ == '__main__':
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        logger.critical("One or more environment variables (API_ID, API_HASH, BOT_TOKEN) are missing.")
    else:
        asyncio.run(main_async_runner())
