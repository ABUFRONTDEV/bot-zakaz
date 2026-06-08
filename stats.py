import json
import os
from typing import Optional

STATS_FILE = os.path.join(os.path.dirname(__file__), "player_stats.json")
_cache: dict = {}


def _load():
    global _cache
    if os.path.exists(STATS_FILE):
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    else:
        _cache = {}


def _save():
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=2)


def _default(name: str) -> dict:
    return {
        "name": name,
        "games": 0,
        "wins": 0,
        "mafia_games": 0,
        "civil_games": 0,
        "neutral_games": 0,
    }


def get(user_id: int) -> Optional[dict]:
    _load()
    return _cache.get(str(user_id))


def record_game(players, winner: str):
    """Record result for all players. winner: 'mafia' | 'civil' | 'qotil'"""
    _load()
    for p in players:
        uid = str(p.user_id)
        if uid not in _cache:
            _cache[uid] = _default(p.name)
        s = _cache[uid]
        s["name"] = p.name
        s["games"] += 1
        team = p.team.value  # "mafia" | "civil" | "neutral"
        s[f"{team}_games"] = s.get(f"{team}_games", 0) + 1
        won = (
            (winner == "mafia" and team == "mafia")
            or (winner == "civil" and team == "civil")
            or (winner == "qotil" and team == "neutral")
        )
        if won:
            s["wins"] += 1
    _save()


def top(limit: int = 10) -> list:
    """Returns list of (user_id_str, stats_dict) sorted by wins desc."""
    _load()
    ranked = sorted(
        _cache.items(),
        key=lambda x: (x[1].get("wins", 0), x[1].get("games", 0)),
        reverse=True,
    )
    return ranked[:limit]


def win_rate(s: dict) -> float:
    g = s.get("games", 0)
    return round(s.get("wins", 0) / g * 100, 1) if g else 0.0


def format_profile(user_id: int, name: str, username: Optional[str] = None) -> str:
    s = get(user_id)
    if not s:
        mention = f"@{username}" if username else name
        return (
            f"👤 <b>{mention}</b>\n\n"
            f"Hali hech qanday o'yin o'ynamagan.\n"
            f"Guruhda /newgame bilan o'yin boshlang!"
        )

    g = s["games"]
    w = s["wins"]
    l = g - w
    wr = win_rate(s)

    # Win rate progress bar (10 blocks)
    filled = round(wr / 10)
    bar = "🟩" * filled + "⬜" * (10 - filled)

    mention = f"@{username}" if username else name
    lines = [
        f"👤 <b>PROFIL — {mention}</b>",
        "",
        f"🎮 Jami o'yinlar: <b>{g}</b>",
        f"🏆 G'alabalar:    <b>{w}</b>",
        f"💀 Mag'lubiyatlar: <b>{l}</b>",
        "",
        f"📊 G'alaba foizi:",
        f"{bar} <b>{wr}%</b>",
        "",
        "🎭 Jamoa bo'yicha:",
        f"  🔴 Mafia:   {s.get('mafia_games', 0)} o'yin",
        f"  ⚪ Fuqaro:  {s.get('civil_games', 0)} o'yin",
        f"  🟡 Neytral: {s.get('neutral_games', 0)} o'yin",
    ]
    return "\n".join(lines)


def format_top(limit: int = 10) -> str:
    ranking = top(limit)
    if not ranking:
        return "📊 Hali hech qanday statistika yo'q.\nO'yin o'ynab statistika to'plang!"

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = ["🏆 <b>TOP O'YINCHILAR</b>\n"]
    for i, (uid, s) in enumerate(ranking, 1):
        medal = medals.get(i, f"{i}.")
        wr = win_rate(s)
        lines.append(
            f"{medal} <b>{s['name']}</b> — "
            f"{s['wins']} g'alaba "
            f"({s['games']} o'yin, {wr}%)"
        )
    return "\n".join(lines)
