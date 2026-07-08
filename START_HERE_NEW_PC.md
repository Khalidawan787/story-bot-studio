# 🚀 Naye PC pe Story Bot chalane ka aasaan tareeqa

Yeh zip me **poora bot + aapke secrets (YouTube login, API keys) + assets** shaamil hain.
Bas neeche ke steps follow karo.

> ⚠️ **BOHAT ZAROORI:** Yeh zip kisi ko **mat bhejo / share mat karo**. Isme aapka
> YouTube token aur API keys hain — koi aur ise le kar aapka channel istemal kar sakta hai.

---

## Step 1 — Zip extract karo
Zip ko kisi aasaan jagah nikaalo, jaise:
`C:\StoryBot`
(Extract ke baad andar `web_dashboard.py`, `src`, `scripts` waghera dikhne chahiyein.)

## Step 2 — Python install karo (sirf ek dafa)
1. Yeh link kholo: https://www.python.org/downloads/
2. **Python 3.11 ya usse naya** download karke install karo.
3. Install screen pe **"Add Python to PATH"** ka checkbox ZAROOR tick karo.

## Step 3 — Setup script chalao (sab kuch khud kar deta hai)
Folder `C:\StoryBot` khol ke, address bar me `powershell` likh ke Enter karo,
phir yeh paste karo:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\setup_new_pc.ps1
```

Yeh script khud:
- Python virtual environment banata hai
- Saare packages install karta hai
- **FFmpeg** install karta hai (winget se) — video banane ke liye zaroori
- `.env` ready karta hai
- Ek health-check chalata hai

> Agar daily automation (rozana 5 video) bhi chahiye to iske bajaye yeh chalao:
> ```powershell
> powershell.exe -ExecutionPolicy Bypass -File .\scripts\setup_new_pc.ps1 -InstallDailyTask
> ```

## Step 4 — Dashboard kholo
```powershell
.\.venv\Scripts\python.exe web_dashboard.py
```
Phir browser me kholo: **http://127.0.0.1:8000**

Bas! Dashboard chal raha hai. Yahan se video generate / daily / upload sab kar sakte ho.

---

## ✅ Naye features (is version me)

**1. Buffer — kai din aage videos (PC band ho to bhi publish hote rahein):**
```powershell
.\.venv\Scripts\python.exe -m src.cli buffer --count 30 --upload true
```
Yeh 30 videos bana ke YouTube pe *scheduled* kar deta hai. PC 3-4 din band bhi rahe,
YouTube khud time pe publish karta rahega.

**2. Publish times set karna (optional):** `.env` file me yeh line edit karo —
```
YOUTUBE_SCHEDULE_DAILY_SLOTS=10,13,16,19,21
```
(Local time ke hours — buffered videos in waqton pe publish honge.)

**3. Karaoke captions + music ducking** — default ON hain (lafz bolte waqt highlight,
aur awaaz ke waqt music halka). `.env` me band/chalu kar sakte ho:
```
ENABLE_KARAOKE_CAPTIONS=true
ENABLE_MUSIC_DUCKING=true
```

---

## ⚠️ Yaad rakhne wali baatein
- **Internet zaroori hai** (images, voice, aur YouTube upload ke liye).
- **PC on + jagta hua** hona chahiye jab videos ban rahi hon. Sleep me nahi.
- Agar upload pe YouTube login maangey ya fail ho: `token.json` file delete karke
  dobara dashboard se "Connect & verify" karo (naya login khul jayega).
- VS Code ya dashboard window khula rakhna zaroori nahi (scheduled task background me chalta hai).

## ❓ Agar kuch kaam na kare
- **"ffmpeg not found"** → PowerShell band karke naya kholo, ya PC restart karke Step 4 dobara.
- **"python not found"** → Python install karte waqt "Add to PATH" tick nahi hua. Python
  dobara install karo, checkbox tick karo.
- Baaki tafseel ke liye `MOVE_TO_NEW_PC.md` aur `README.md` bhi isi folder me hain.
</content>
