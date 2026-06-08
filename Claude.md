# Mafia Bot

Telegram Mafia o'yin boti. O'ziga xos rollar bilan.

## Fayllar

- `bot.py` — Barcha handler va main
- `game.py` — `Game` / `Player` classlari, o'yin logikasi
- `roles.py` — Rol ta'riflari va tarkibi (count→roles)
- `config.py` — Sozlamalar (timer, min/max o'yinchilar)
- `requirements.txt` / `.env.example`

## O'rnatish

```bash
pip install -r requirements.txt
cp .env.example .env
# .env ga: BOT_TOKEN=...
python3 bot.py
```

## Rollar

| Rol | Jamoa | Qobiliyat |
|-----|-------|-----------|
| 🔴 Don | Mafia | Komissar tekshirganda doim tinch ko'rinadi. Kechasi o'ldiradi |
| 💃 Mashuqa | Mafia | Kechasi bir o'yinchining qobiliyatini blokaydi |
| ⚖️ Advokat | Mafia | Kechasi bir o'yinchini komissar tekshiruvidan himoyalaydi |
| 👮 Komissar | Fuqaro | Kechasi bir o'yinchini tekshiradi (mafia/tinch). Don doim tinch |
| 🎖️ Serjant | Fuqaro | Komissar o'lganda uning o'rniga o'tadi |
| 💊 Doktor | Fuqaro | Kechasi bir o'yinchini qutqaradi. O'zini 1 marta |
| 🏥 Hamshira | Fuqaro | Doktor o'lganda uning o'rniga o'tadi |
| 🗣️ G'iybatchi | Fuqaro | Kunduzi 1 marta guruhga soxta komissar natijasini e'lon qiladi |
| 💻 Xaker | Fuqaro | 1-3 kecha ikki o'yinchi rolini almashtiradi. 4-kundan fuqaro |
| 💣 Kamikadze | Fuqaro | O'ldirilsa/haydalsа birini o'zi bilan olib ketadi (30s) |
| 🚶 Daydi | Fuqaro | Kechasi bir uyda tunaydi. Ertalab guruhga e'lon qilinadi |
| 👤 Tinch aholi | Fuqaro | Oddiy fuqaro |
| 🗡️ Qotil | Neytral | Yolg'iz harakat qiladi. Hammani o'ldirib yagona tirik qolsa g'alaba |

## Buyruqlar

`/newgame` `/join` `/leave` `/startgame` `/endgame` `/vote` `/players` `/roles` `/help`

## Texnik eslatmalar

- Kecha timeout: 60s | Muhokama: 90s | Ovoz: 60s (`config.py`)
- Mafia o'ldirish tartibi: Don → Mashuqa → Advokat (kim tirik bo'lsa)
- Xaker ikki bosqichli: `xswap1_<id>` → `xswap2_<id>`
- Kamikadze o'ch: async 30s timeout `asyncio.Event` bilan
- G'iybatchi: `giyi_p_<id>` → `giyi_result_mafia/tinch`
