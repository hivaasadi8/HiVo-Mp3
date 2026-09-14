<div align="center">

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:7b2ff7,50:00d4ff,100:7b2ff7&height=220&section=header&text=HiVo-MP3&fontSize=80&fontColor=ffffff&animation=twinkling&fontAlignY=32&desc=Convert%20Video%20to%20Music%20in%20Seconds&descAlignY=58&descSize=18&descAlign=50" width="100%" />

<br>

<a href="https://t.me/HiVoMP3Bot">
  <img src="https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white&labelColor=0d1117" alt="Telegram Bot"/>
</a>
<a href="https://github.com/hivaasadi8/HiVo-Mp3">
  <img src="https://img.shields.io/badge/GitHub-Repo-181717?style=for-the-badge&logo=github&logoColor=white&labelColor=0d1117" alt="GitHub Repo"/>
</a>
<img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white&labelColor=0d1117"/>
<img src="https://img.shields.io/badge/FFmpeg-Powered-007808?style=for-the-badge&logo=ffmpeg&logoColor=white&labelColor=0d1117"/>
<img src="https://img.shields.io/badge/GitHub%20Actions-Auto%20Deploy-2088FF?style=for-the-badge&logo=github-actions&logoColor=white&labelColor=0d1117"/>
<img src="https://img.shields.io/badge/License-MIT-yellow?style=for-the-badge&logo=opensourceinitiative&logoColor=white&labelColor=0d1117"/>

<br><br>

<h3>🎵 ربات تلگرامی تبدیل ویدیو به موزیک</h3>
<p><i>سریع • دقیق • باکیفیت • بدون افت صدا</i></p>

<br>

<a href="#-ویژگیها">
  <img src="https://img.shields.io/badge/✨_ویژگیها-انجام_شده-7b2ff7?style=flat-square&labelColor=0d1117"/>
</a>
<a href="#-راهاندازی">
  <img src="https://img.shields.io/badge/🚀_راه‌اندازی-آموزش-00d4ff?style=flat-square&labelColor=0d1117"/>
</a>
<a href="#-ساختار-پروژه">
  <img src="https://img.shields.io/badge/📁_ساختار-معماری-00d47f?style=flat-square&labelColor=0d1117"/>
</a>

</div>

<br>

---

## 🎯 درباره پروژه

**HiVo-MP3** یک ربات تلگرامی حرفه‌ای برای تبدیل ویدیو به موزیک است.
با پشتیبانی از چندین فرمت خروجی، مدیریت کاربران و پریمیوم، پنل ادمین کامل، و اجرای کاملاً خودکار روی GitHub Actions.

طراحی‌شده با **معماری سه‌لایه** (main / store / converter) — قابل توسعه، مقاوم در برابر خطا و آماده برای مقیاس.

<br>

---

## ✨ ویژگی‌ها

<table>
<tr>
<td width="50%" valign="top">

### 🎧 موتور تبدیل قدرتمند

- 🎵 **MP3** — 128 / 192 / 320 kbps + **VBR V0**
- 🎼 **M4A (AAC)** — 128 / 192 / 256 kbps
- 🎤 **ویس تلگرام** — Opus 48k
- 🎼 **FLAC** — بدون افت کیفیت
- 🔊 **نرمال‌سازی صدا** با استاندارد EBU R128
- ✂️ **حذف خودکار سکوت** ابتدا/انتها
- 🖼 **استخراج کاور** هوشمند (چند سطح fallback)
- 📝 **متادیتای خودکار** (title / artist / album)

### ⚡️ زیرساخت مقیاس‌پذیر

- 🚀 **Semaphore** برای تبدیل همزمان
- ⏱ **Rate Limiting** — ۵/۵د برای رایگان، ۳۰/۵د برای پریمیوم
- 🛡 **Graceful Shutdown** روی SIGTERM
- 🔄 **Auto-restart** با ۱۰ تلاش
- 📦 **ذخیره‌سازی اتمیک** روی GitHub JSON
- 🧪 **Smoke Test** قبل از اجرا

</td>
<td width="50%" valign="top">

### 🎨 رابط کاربری تمیز

- 💎 دکمه‌های شیشه‌ای مینیمال
- 📊 نوار پیشرفت زنده (۳۰٪ → ۸۰٪ → ۱۰۰٪)
- 🗂 کارت‌های اطلاعاتی گرافیکی
- 🌐 پشتیبانی کامل از فارسی
- 📱 Inline Mode برای اشتراک‌گذاری

### 🔐 مدیریت و امنیت

- 🔒 **قفل کانال** (عضویت اجباری)
- ⭐️ **سیستم پریمیوم** با انقضا
- 🛠 **پنل ادمین** کامل
- 📢 **پیام همگانی** با شمارش دقیق
- 🔍 **جستجوی کاربر** بین یوزرنیم/آیدی
- ⛔️ **بن / آنبن** کاربران
- 💾 **بکاپ JSON** لحظه‌ای
- 🏆 **لیدربورد** + آمار ۷ روز اخیر

</td>
</tr>
</table>

<br>

---

## 🖼 پیش‌نمایش

<div align="center">

<table>
<tr>
<th>🎬 منوی اصلی</th>
<th>🎚 انتخاب کیفیت</th>
<th>✅ نتیجه نهایی</th>
</tr>
<tr>
<td align="center">
<code>/start</code><br>
منوی گرافیکی
</td>
<td align="center">
<code>cv|mp3_192</code><br>
Preset های متنوع
</td>
<td align="center">
<code>send_audio</code><br>
MP3 + کاور + متادیتا
</td>
</tr>
</table>

</div>

<br>

---

## 🚀 راه‌اندازی

### ۱️⃣ ساخت ربات
از [**@BotFather**](https://t.me/BotFather) یه ربات بساز و توکنش رو بردار.

### ۲️⃣ تنظیم Secretها
توی **Settings → Secrets and variables → Actions**:

| Secret | توضیح | اجباری |
|:---|:---|:---:|
| `BOT_TOKEN` | توکن ربات از BotFather | ✅ |
| `GH_TOKEN` | [Personal Access Token](https://github.com/settings/tokens) با دسترسی `repo` | ✅ |
| `ADMINS` | آیدی عددی ادمین‌ها (با کاما جدا کن) | ✅ |
| `GATE_CHANNEL` | آیدی کانال قفل (اختیاری) | ❌ |

### ۳️⃣ فعال‌سازی
برو تب **Actions** → **🎵 HiVo-MP3 Bot** → **Run workflow**

✅ تمام! ربات آنلاین میشه.

<br>

<details>
<summary><b>⚙️ تنظیمات پیشرفته (اختیاری)</b></summary>

<br>

| متغیر | پیش‌فرض | توضیح |
|:---|:---:|:---|
| `FREE_MAX_MB` | `10` | سقف حجم پلن رایگان |
| `PREMIUM_MAX_MB` | `19` | سقف حجم پلن پریمیوم |
| `MAX_CONCURRENT` | `3` | تعداد تبدیل همزمان |
| `GH_PATH` | `data/db.json` | مسیر دیتابیس |
| `GH_BRANCH` | `main` | برنچ ذخیره‌سازی |

</details>

<br>

---

## 📁 ساختار پروژه

```text
HiVo-Mp3/
├── 📄 main.py                 # 🎮 لایه ربات (هندلرها، UI، پنل ادمین)
├── 💾 store.py                # 🗄 لایه ذخیره‌سازی (GitHub JSON)
├── 🎬 converter.py            # 🔄 لایه تبدیل (FFmpeg)
├── 📦 requirements.txt        # وابستگی‌ها
├── 📖 README.md               # این فایل
├── 📂 data/
│   └── 🗃 db.json             # دیتابیس (auto-managed)
└── 📂 .github/
    └── 📂 workflows/
        └── ⚙️ bot.yml         # اجرای خودکار
