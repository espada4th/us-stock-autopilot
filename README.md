# US Stock Autopilot — GitHub Pages Deploy

Static dashboard สำหรับ track 13 US tickers + 11 sector ETFs.
Deploy ฟรีบน GitHub Pages, refresh ข้อมูลอัตโนมัติด้วย GitHub Actions ทุกชั่วโมงในช่วง US market hours.

## โครงสร้างไฟล์

```
gh-pages-deploy/
├── index.html              ← หน้า dashboard (โหลด dataset.json ตอนเปิดเว็บ)
├── dataset.json            ← ข้อมูล tickers + market snapshot
├── scripts/
│   └── refresh_data.py     ← script ที่ GitHub Actions รันเพื่ออัพเดต dataset.json
├── .github/workflows/
│   ├── pages.yml           ← deploy ไป GitHub Pages ทุกครั้งที่ push main
│   └── refresh.yml         ← refresh ข้อมูลทุกชั่วโมง (cron + manual)
└── README.md
```

## ขั้นตอน Deploy (ครั้งแรก)

### 1. สร้าง repo ใหม่บน GitHub

ไปที่ https://github.com/new แล้วสร้าง repo:
- **Owner**: TOTHEMOON
- **Repository name**: `us-stock-autopilot` (หรือชื่ออื่นที่ต้องการ)
- **Visibility**: Public (GitHub Pages ฟรีต้อง public)
- **อย่าเลือก** "Add README / .gitignore / license" — เราจะ push จากเครื่องเอง

### 2. Push โฟลเดอร์ขึ้น GitHub

เปิด Terminal แล้ว `cd` เข้ามาในโฟลเดอร์ `gh-pages-deploy` จากนั้นรัน:

```bash
git init
git add .
git commit -m "initial deploy: US stock autopilot dashboard"
git branch -M main
git remote add origin https://github.com/TOTHEMOON/us-stock-autopilot.git
git push -u origin main
```

> เปลี่ยน `us-stock-autopilot` เป็นชื่อ repo ที่สร้างไว้จริง

### 3. เปิด GitHub Pages

1. ไปที่ repo → **Settings** → **Pages**
2. **Source**: เลือก **GitHub Actions**
3. รอ workflow `pages.yml` รันจบ (ประมาณ 1-2 นาที) — ดูได้ที่แท็บ **Actions**
4. URL ของ dashboard จะเป็น: `https://tothemoon.github.io/us-stock-autopilot/`

### 4. (Optional) เปิด Data Refresh อัตโนมัติ

ถ้าอยากให้ dataset.json refresh ข้อมูลจริงทุกชั่วโมง ต้องเพิ่ม API key:

1. สมัคร free tier ที่ https://financialmodelingprep.com/ (250 calls/day ฟรี) — คัดลอก API key มา
2. ไปที่ repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**
3. **Name**: `FMP_API_KEY`
4. **Value**: paste API key
5. ไปที่แท็บ **Actions** → เลือก workflow **Refresh dataset** → กด **Run workflow** เพื่อ test

ถ้าไม่มี `FMP_API_KEY` ตั้งไว้ workflow จะรันผ่านแต่แค่ bump timestamp ไม่ได้ดึงราคาจริง

## ตารางการทำงาน

| Workflow        | ทำอะไร                              | เมื่อไหร่                                        |
|-----------------|-------------------------------------|-------------------------------------------------|
| `pages.yml`     | Deploy HTML/JSON ไป Pages           | ทุกครั้งที่ push ไป `main` + manual dispatch    |
| `refresh.yml`   | ดึงราคาใหม่ → commit dataset.json    | ทุกชั่วโมง 9:15am-5:15pm ET จันทร์-ศุกร์        |

หลังจาก `refresh.yml` commit เสร็จ `pages.yml` จะ trigger เอง → Pages update ภายใน ~1 นาที

## Cron Schedule (refresh.yml)

```
"15 13-21 * * 1-5"
```

หมายความว่า: นาทีที่ 15 ของทุกชั่วโมง ระหว่าง 13:00-21:00 UTC (9:15am-5:15pm ET) วันจันทร์-ศุกร์
รวม 9 รอบ/วัน = ~180 รอบ/เดือน (ต่ำกว่า FMP free tier 250/วัน สบายๆ)

ถ้าอยากแก้ เปิด `.github/workflows/refresh.yml` แล้วแก้ `cron:` field

## Test ที่เครื่องก่อน push (แนะนำ)

```bash
cd gh-pages-deploy
python3 -m http.server 8000
# เปิด http://localhost:8000 ใน Chrome
```

ต้องเปิดผ่าน http server นะ เปิด `index.html` ตรงๆ จะโดน CORS block ไม่ให้ fetch dataset.json

## Troubleshooting

**หน้าเปิดมาแต่ข้อมูลไม่ขึ้น**
- เช็ค DevTools → Console ว่ามี fetch error มั้ย
- เช็ค Network tab ว่า `dataset.json` โหลดสำเร็จ (200 OK)

**Workflow รันแล้ว error**
- ไป **Actions** tab → เปิด run ที่ fail → ดู log
- ส่วนใหญ่ปัญหาคือ secret ไม่ได้ตั้งหรือ branch name ผิด

**Pages URL ไม่เปิด (404)**
- ยืนยัน Settings → Pages → Source เป็น **GitHub Actions**
- รอ `pages.yml` รัน complete รอบแรก

## Data Sources

- **Primary**: Financial Modeling Prep (FMP) — quote endpoint
- **Fallback**: ถ้า FMP ล่ม/ไม่มี key → bump timestamp only (dataset เดิม)

ขยายไปใช้ Bigdata.com / Polygon / Alpha Vantage ได้โดยแก้ `scripts/refresh_data.py`

## License

Private use. ข้อมูลตลาดจาก FMP ต้องปฏิบัติตาม terms ของ FMP
