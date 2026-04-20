# Finnhub API Setup — คู่มือ signup + ตั้ง GitHub Secret

Scanner จะใช้ Finnhub เพื่อเสริมข้อมูลที่ yfinance ไม่มี:
- News ล่าสุดของแต่ละ ticker (catalyst real-time)
- Real-time quote (แทน delayed quote ของ yfinance)
- Fundamentals (P/E, EPS, analyst target consensus)
- Insider transactions + sentiment

**ถ้าไม่ตั้ง API key — scanner ยังรันได้ปกติ** แค่จะไม่มี news / insider / fundamentals เสริม

---

## 1. Register ที่ finnhub.io (ฟรี, ใช้เวลา ~1 นาที)

1. เปิด https://finnhub.io/register
2. กรอก email + password (ใช้ rertuk@gmail.com ได้ — free tier ไม่ต้องใส่บัตร)
3. ยืนยัน email (จะมี link ส่งไปที่ inbox)
4. Login → ไปหน้า Dashboard
5. Copy **API Key** จากหัวหน้าบน (string ยาว ๆ เริ่มด้วย `c...` หรือ `d...`)

**Free tier limits (ณ 2026):**
- 60 calls / minute
- ~86,400 calls / day
- เข้าถึงได้: quote, news, company profile, basic fundamentals, insider transactions, recommendation trends
- พอใช้สำหรับ top-50 scanner: 50 × 4 endpoints = 200 calls ต่อรอบ (~3 นาที)

---

## 2. ตั้ง GitHub Secret

1. เปิด https://github.com/espada4th/us-stock-autopilot/settings/secrets/actions
2. คลิก **New repository secret**
3. ใส่:
   - **Name:** `FINNHUB_API_KEY`
   - **Secret:** (paste API key ที่ copy จาก Finnhub dashboard)
4. คลิก **Add secret**

เสร็จ! workflow จะอ่าน env var นี้อัตโนมัติในการรันครั้งถัดไป

---

## 3. ทดสอบ local (optional)

ถ้าอยากรัน `scripts/refresh_data.py` บนเครื่องตัวเองโดยใช้ API:

**Windows CMD:**
```cmd
set FINNHUB_API_KEY=your_key_here
python scripts\refresh_data.py
```

**Windows PowerShell:**
```powershell
$env:FINNHUB_API_KEY="your_key_here"
python scripts\refresh_data.py
```

---

## 4. Verify หลัง push

1. Push code ผ่าน `push-only.cmd`
2. ไปที่ https://github.com/espada4th/us-stock-autopilot/actions
3. คลิก Refresh workflow → Run workflow → main
4. รอ ~4-5 นาที (scanner + Finnhub enrichment)
5. ดู log — ควรมี `Finnhub: enriched N/50 tickers`
6. เปิด https://espada4th.github.io/us-stock-autopilot/ticker/ASTS.html หรือ ticker อื่นใน top-50
7. ควรเห็น section **News** (headlines 3-5 ล่าสุด) และ **Insider** (transactions 30 วัน)

---

## 5. ถ้าเจอปัญหา

| อาการ | สาเหตุ + แก้ |
|---|---|
| log ขึ้น `Finnhub: skipped (no API key)` | ยังไม่ตั้ง secret หรือตั้งชื่อผิด — ต้อง `FINNHUB_API_KEY` ตรงเป๊ะ |
| log ขึ้น `401 Unauthorized` | API key ผิด / หมดอายุ — regenerate ที่ finnhub.io dashboard |
| log ขึ้น `429 Too Many Requests` | เกิน rate limit — scanner มี backoff แล้ว แต่ถ้าเจอบ่อยให้ลด TOP_N หรือใส่ sleep เพิ่ม |
| หน้า ticker ไม่มี News section | ticker นั้นไม่มีข่าวใน 7 วันล่าสุด — ปกติ, section จะ skip อัตโนมัติ |

---

## Reference

- Finnhub docs: https://finnhub.io/docs/api
- Endpoints ที่ scanner ใช้:
  - `/quote?symbol=X` — real-time price
  - `/company-news?symbol=X&from=YYYY-MM-DD&to=YYYY-MM-DD` — news
  - `/stock/metric?symbol=X&metric=all` — fundamentals
  - `/stock/insider-transactions?symbol=X` — insider trades
  - `/stock/recommendation?symbol=X` — analyst recommendations
