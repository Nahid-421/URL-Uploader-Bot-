# বেস ইমেজ হিসেবে একটি আধুনিক এবং হালকা পাইথন ইমেজ ব্যবহার করা হচ্ছে
FROM python:3.11-slim

# ওয়ার্কিং ডিরেক্টরি সেট করা হচ্ছে
WORKDIR /app

# সিস্টেম প্যাকেজ আপডেট করা এবং FFmpeg ও অন্যান্য প্রয়োজনীয় টুলস ইনস্টল করা
# এখানে FFmpeg এর একটি সম্পূর্ণ সংস্করণ ইনস্টল করা হচ্ছে, যা সব ধরনের এনকোডিং সমর্থন করবে
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt ফাইলটি কপি করা হচ্ছে
COPY requirements.txt .

# পাইথন লাইব্রেরিগুলো ইনস্টল করা হচ্ছে
RUN pip install --no-cache-dir -r requirements.txt

# বাকি অ্যাপ্লিকেশন কোড কপি করা হচ্ছে
COPY . .

# অ্যাপ্লিকেশন চালানোর জন্য কমান্ড
CMD ["python", "main.py"]
