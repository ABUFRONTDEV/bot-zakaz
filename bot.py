import asyncio
import logging
import os
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import Forbidden, BadRequest

import config
import stats as player_stats
from game import Game, GamePhase, Player
from roles import RoleName, Team, ROLES

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

games: dict[int, Game] = {}
user_game: dict[int, int] = {}
timers: dict[int, asyncio.Task] = {}

# chat_id -> {user_id: (full_name, username)}  — tracks everyone who sends a message
group_members: dict[int, dict[int, tuple]] = {}

NIGHT_PREFIXES = (
    "nkill_", "nmash_", "nadv_",
    "nkomir_", "nserj_",
    "ndokt_", "nhamsh_",
    "ndaydi_", "nqotil_",
    "xswap1_", "xswap2_",
)


# ─────────────────────── helpers ────────────────────────────────

def players_kb(
    game: Game,
    prefix: str,
    exclude: Optional[int] = None,
    exclude_set: Optional[set] = None,
    skip_cb: str = "night_skip",
) -> InlineKeyboardMarkup:
    excluded: set = set()
    if exclude is not None:
        excluded.add(exclude)
    if exclude_set:
        excluded |= exclude_set
    rows = []
    for p in game.alive_players():
        if p.user_id in excluded:
            continue
        label = p.name  # never reveal roles via button labels
        rows.append([InlineKeyboardButton(label, callback_data=f"{prefix}_{p.user_id}")])
    rows.append([InlineKeyboardButton("⏭ O'tkazib yuborish", callback_data=skip_cb)])
    return InlineKeyboardMarkup(rows)


def vote_kb(game: Game) -> InlineKeyboardMarkup:
    rows = []
    for p in game.alive_players():
        cnt = sum(1 for v in game.day_votes.values() if v == p.user_id)
        rows.append([InlineKeyboardButton(
            f"{p.name}  ({cnt} 🗳)", callback_data=f"vote_{p.user_id}"
        )])
    rows.append([InlineKeyboardButton("🚫 Ovoz bermaslik", callback_data="vote_skip")])
    return InlineKeyboardMarkup(rows)


def plist(game: Game, show_roles: bool = False) -> str:
    lines = []
    for i, p in enumerate(game.players.values(), 1):
        icon = "✅" if p.alive else "💀"
        if show_roles and p.role:
            r = ROLES[p.role]
            lines.append(f"{i}. {icon} {p.name} — {r.emoji} {r.title}")
        else:
            lines.append(f"{i}. {icon} {p.name}")
    return "\n".join(lines)


def lobby_text(game: Game) -> str:
    n = len(game.players)
    pl = "\n".join(f"{i}. ✅ {p.name}" for i, p in enumerate(game.players.values(), 1))
    return (
        f"🎮 <b>MAFIA — LOBBY</b>\n\n"
        f"<b>O'yinchilar ({n}/{config.MAX_PLAYERS}):</b>\n{pl}\n\n"
        f"Minimum: {config.MIN_PLAYERS} o'yinchi kerak."
    )


def lobby_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Qo'shilish", callback_data=f"join_{chat_id}"),
         InlineKeyboardButton("❌ Chiqish", callback_data=f"lleave_{chat_id}")],
        [InlineKeyboardButton("🚀 O'yinni boshlash", callback_data=f"gstart_{chat_id}")],
    ])


def mention(p: Player) -> str:
    if p.username:
        return f"@{p.username}"
    return f'<a href="tg://user?id={p.user_id}">{p.name}</a>'


async def cancel_timer(chat_id: int):
    t = timers.pop(chat_id, None)
    if t:
        t.cancel()


def _fmt_countdown(emoji: str, label: str, remaining: int) -> str:
    mins, secs = divmod(max(0, remaining), 60)
    return f"{emoji} <b>{label}</b> — ⏱ {mins:02d}:{secs:02d}"


async def _clear_countdown(context: ContextTypes.DEFAULT_TYPE, game: "Game"):
    if game.countdown_msg_id:
        try:
            await context.bot.delete_message(
                chat_id=game.chat_id, message_id=game.countdown_msg_id
            )
        except Exception:
            pass
        game.countdown_msg_id = None


async def try_dm(context: ContextTypes.DEFAULT_TYPE, user_id: int, **kwargs) -> bool:
    try:
        await context.bot.send_message(chat_id=user_id, **kwargs)
        return True
    except (Forbidden, BadRequest) as e:
        logger.warning("DM fail uid=%s: %s", user_id, e)
        return False


# ─────────────────────── mafia group chat ───────────────────────

async def handle_dm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Forward text messages from mafia members to their teammates during night."""
    chat = update.effective_chat
    user = update.effective_user
    msg = update.message

    if not msg or not msg.text or chat.type != "private" or not user:
        return

    chat_id = user_game.get(user.id)
    if not chat_id:
        return
    game = games.get(chat_id)
    if not game or game.phase != GamePhase.NIGHT:
        return

    player = game.players.get(user.id)
    if not player or not player.alive or player.team != Team.MAFIA:
        return

    teammates = [m for m in game.mafia_players() if m.user_id != user.id]
    if not teammates:
        await msg.reply_text("Siz yolg'iz mafia a'zosisiz.")
        return

    forwarded = 0
    for m in teammates:
        if await try_dm(
            context, m.user_id,
            text=f"🔴 <b>{player.name}:</b> {msg.text}",
            parse_mode="HTML",
        ):
            forwarded += 1

    if forwarded:
        await msg.reply_text(f"✅ Xabar {forwarded} jamoadoshga yetkazildi.", parse_mode="HTML")


# ─────────────────────── member tracker ─────────────────────────

async def track_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record every user who sends a message in a group."""
    chat = update.effective_chat
    user = update.effective_user
    if chat and chat.type in ("group", "supergroup") and user and not user.is_bot:
        bucket = group_members.setdefault(chat.id, {})
        bucket[user.id] = (user.full_name, user.username)


# ─────────────────────── role DMs ───────────────────────────────

WIN_CONDITIONS = {
    Team.MAFIA:    "Mafia soni tinch aholi soniga teng yoki ko'p bo'lganda 🔴 Mafia g'alaba qiladi.",
    Team.CIVIL:    "Barcha mafia a'zolari yo'q qilinsa ⚪ Tinch aholi g'alaba qiladi.",
    Team.NEUTRAL:  "Faqat 🗡️ Qotil tirik qolganda u yagona g'olib bo'ladi.",
}


async def send_roles(context: ContextTypes.DEFAULT_TYPE, game: Game):
    no_dm: list[str] = []
    for p in game.players.values():
        r = ROLES[p.role]
        win_cond = WIN_CONDITIONS[r.team]

        lines = [
            "┌─────────────────────────┐",
            f"       🎭 SIZNING ROLINGIZ",
            "└─────────────────────────┘",
            "",
            f"{r.emoji}  <b>{r.title.upper()}</b>",
            "",
            f"📋 {r.description}",
            "",
            f"🏆 <b>G'alaba sharti:</b>",
            f"{win_cond}",
        ]

        if r.team == Team.MAFIA:
            mates = [m for m in game.players.values()
                     if m.team == Team.MAFIA and m.user_id != p.user_id]
            if mates:
                lines.append("")
                lines.append("🔴 <b>Jamoangiz:</b>")
                for m in mates:
                    lines.append(f"  {ROLES[m.role].emoji} {m.name} — {ROLES[m.role].title}")
            lines.append("")
            lines.append("💬 <b>Mafia chati:</b> Kecha davomida menga istalgan xabar yozing — jamoadoshlaringizga yetkazaman.")

        text = "\n".join(lines)
        if not await try_dm(context, p.user_id, text=text, parse_mode="HTML"):
            no_dm.append(p.name)

    if no_dm:
        await context.bot.send_message(
            chat_id=game.chat_id,
            text=(
                "⚠️ Quyidagilar botga <b>/start</b> yubormagan, roli yetib bormadi:\n"
                + "\n".join(f"• {n}" for n in no_dm)
                + "\n\nBotga /start yuboring va /join bosing."
            ),
            parse_mode="HTML",
        )


# ─────────────────────── KAMIKADZE ──────────────────────────────

async def kamikadze_revenge(
    context: ContextTypes.DEFAULT_TYPE, game: Game, victim: Player
) -> Optional[Player]:
    """Send revenge DM to kamikadze and wait up to 30s."""
    game.kamikadze_event = asyncio.Event()
    game.kamikadze_pending_id = victim.user_id
    game.kamikadze_revenge = None

    kb = players_kb(game, "kamikaze", exclude=victim.user_id, skip_cb="kamikaze_skip")
    sent = await try_dm(
        context, victim.user_id,
        text="💣 <b>Siz o'ldirilmoqdasiz!</b>\n\nO'zingiz bilan kimni olib ketasiz? (30 soniya)",
        reply_markup=kb,
        parse_mode="HTML",
    )
    if not sent:
        game.kamikadze_event = None
        game.kamikadze_pending_id = None
        return None

    try:
        await asyncio.wait_for(game.kamikadze_event.wait(), timeout=30)
    except asyncio.TimeoutError:
        pass

    game.kamikadze_event = None
    game.kamikadze_pending_id = None

    rid = game.kamikadze_revenge
    game.kamikadze_revenge = None
    if rid and rid in game.players:
        target = game.players[rid]
        if target.alive:
            target.alive = False
            return target
    return None


# ─────────────────────── NIGHT ──────────────────────────────────

async def start_night(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    game.phase = GamePhase.NIGHT
    game.round += 1
    game.reset_night()

    alive_list = "\n".join(f"  {i}. {p.name}" for i, p in enumerate(game.alive_players(), 1))
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🌙🌙🌙 <b>KECHA BOSHLANDI</b> 🌙🌙🌙\n\n"
            f"Shahar uxladi...\n\n"
            f"👥 <b>Tirik o'yinchilar ({len(game.alive_players())}):</b>\n{alive_list}\n\n"
            f"⚠️ Shaxsiy xabarlaringizni tekshiring!"
        ),
        parse_mode="HTML",
    )

    killer = game.active_mafia_killer()
    xaker_active = game.round <= 3

    for p in game.alive_players():
        has_action = game.player_has_night_action(p)

        if not has_action:
            role = ROLES[p.role]
            await try_dm(
                context, p.user_id,
                text=f"🌙 <b>Kecha {game.round}</b>\n\n{role.emoji} <b>{role.title}</b> sifatida uxlaysiz...",
                parse_mode="HTML",
            )
            game.night_done.add(p.user_id)
            continue

        role = ROLES[p.role]

        # ── MAFIA ──
        if p.role == RoleName.DON or (p == killer and p.role in (RoleName.MASHUQA, RoleName.ADVOKAT)):
            mates = [m for m in game.mafia_players() if m.user_id != p.user_id]
            mates_text = "\n".join(f"  {ROLES[m.role].emoji} {m.name}" for m in mates) if mates else "  (Siz yolg'iz mafia a'zosisiz)"
            mafia_ids = {m.user_id for m in game.mafia_players()}
            kb = players_kb(game, "nkill", exclude_set=mafia_ids)
            text = (
                f"🌙 <b>Kecha {game.round}</b>\n\n"
                f"🔴 <b>Mafia sessiyasi</b>\n\n"
                f"👥 <b>Jamoangiz:</b>\n{mates_text}\n\n"
                f"💬 Jamoadoshlar bilan muloqot: menga shunchaki xabar yozing.\n\n"
                f"🎯 <b>Qurbon tanlang:</b>"
            )

        elif p.role == RoleName.MASHUQA:
            kb = players_kb(game, "nmash", exclude=p.user_id)
            text = f"🌙 <b>Kecha {game.round}</b>\n\n{role.action_prompt}"

        elif p.role == RoleName.ADVOKAT:
            kb = players_kb(game, "nadv", exclude=p.user_id)
            text = f"🌙 <b>Kecha {game.round}</b>\n\n{role.action_prompt}"

        # ── CHECKER ──
        elif p.role == RoleName.KOMISSAR:
            kb = players_kb(game, "nkomir", exclude=p.user_id)
            text = f"🌙 <b>Kecha {game.round}</b>\n\n{role.action_prompt}"

        elif p.role == RoleName.SERJANT:
            # Only reaches here if Komissar is dead (player_has_night_action ensures this)
            kb = players_kb(game, "nserj", exclude=p.user_id)
            text = f"🌙 <b>Kecha {game.round}</b>\n\n🎖️ Komissar o'ldi — siz tekshirasiz!\n\n{role.action_prompt}"

        # ── HEALER ──
        elif p.role == RoleName.DOKTOR:
            kb = players_kb(game, "ndokt")
            text = f"🌙 <b>Kecha {game.round}</b>\n\n{role.action_prompt}"

        elif p.role == RoleName.HAMSHIRA:
            # Only reaches here if Doktor is dead
            kb = players_kb(game, "nhamsh")
            text = f"🌙 <b>Kecha {game.round}</b>\n\n🏥 Doktor o'ldi — siz davolaysiz!\n\n{role.action_prompt}"

        # ── OTHERS ──
        elif p.role == RoleName.DAYDI:
            kb = players_kb(game, "ndaydi", exclude=p.user_id)
            text = f"🌙 <b>Kecha {game.round}</b>\n\n{role.action_prompt}"

        elif p.role == RoleName.QOTIL:
            kb = players_kb(game, "nqotil", exclude=p.user_id)
            text = f"🌙 <b>Kecha {game.round}</b>\n\n{role.action_prompt}"

        elif p.role == RoleName.XAKER and xaker_active:
            kb = players_kb(game, "xswap1", exclude=p.user_id, skip_cb="xswap_skip")
            text = (
                f"🌙 <b>Kecha {game.round}</b>\n\n"
                f"💻 {role.action_prompt}\n"
                f"(Qolgan imkoniyat: {3 - (game.round - 1)} kecha)"
            )

        else:
            game.night_done.add(p.user_id)
            continue

        ok = await try_dm(context, p.user_id, text=text, reply_markup=kb, parse_mode="HTML")
        if not ok:
            game.night_done.add(p.user_id)

    await cancel_timer(chat_id)
    timers[chat_id] = asyncio.create_task(_night_timeout(context, chat_id))


async def _night_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    total = config.NIGHT_TIME
    remaining = total
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_fmt_countdown("🌙", "Kecha tugashiga", remaining),
            parse_mode="HTML",
        )
        game.countdown_msg_id = msg.message_id
    except Exception:
        pass
    try:
        while remaining > 0:
            step = min(10, remaining)
            await asyncio.sleep(step)
            remaining -= step
            game = games.get(chat_id)
            if not game or game.phase != GamePhase.NIGHT:
                return  # ended early — end_night already cleared countdown
            if remaining > 0 and game.countdown_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=game.countdown_msg_id,
                        text=_fmt_countdown("🌙", "Kecha tugashiga", remaining),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except asyncio.CancelledError:
        return  # cancelled by cancel_timer — end_night will clear countdown
    timers.pop(chat_id, None)  # remove self before end_night to avoid self-cancel
    game = games.get(chat_id)
    if game and game.phase == GamePhase.NIGHT:
        await end_night(context, chat_id)


async def end_night(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game or game.phase != GamePhase.NIGHT:
        return
    game.phase = GamePhase.DAY_DISCUSSION  # guard before first await — prevents double entry
    await cancel_timer(chat_id)
    await _clear_countdown(context, game)

    checker = game.active_checker()  # save before process_night changes alive state
    res = game.process_night()

    # Xaker swap — notify privately before announcement
    if res["xaker_swapped"]:
        for sp in res["xaker_swapped"]:
            r = ROLES[sp.role]
            await try_dm(
                context, sp.user_id,
                text=(
                    f"💻 <b>Xaker sizning rolingizni almashtirib qo'ydi!</b>\n\n"
                    f"Yangi rolingiz: {r.emoji} <b>{r.title}</b>\n\n{r.description}"
                ),
                parse_mode="HTML",
            )

    # Deaths
    dead: list[Player] = list(res["killed"])
    if res["qotil_killed"]:
        dead.append(res["qotil_killed"])

    # Kamikadze revenge
    extra_dead: list[Player] = []
    for victim in dead:
        if victim.role == RoleName.KAMIKADZE:
            rev = await kamikadze_revenge(context, game, victim)
            if rev:
                extra_dead.append(rev)

    all_dead = dead + extra_dead

    # Build morning announcement
    parts = [f"☀️☀️☀️ <b>{game.round}-KUN BOSHLANDI</b> ☀️☀️☀️\n"]

    if not all_dead:
        if res["saved"]:
            parts.append("🌅 Tong otdi. Shahar aholisi ko'chaga chiqdi...\n\n🍀 <b>Mo'jiza!</b> Kecha hech kim o'lmadi — kimdir o'lim changalidan qutqarildi!")
        else:
            parts.append("🌅 Tong otdi. Shahar aholisi ko'chaga chiqdi...\n\n😴 Tinch kecha o'tdi. Hech kim o'lmadi.")
    else:
        parts.append("🌅 Tong otdi. Shahar aholisi ko'chaga chiqdi...\n")
        parts.append("💀 <b>Kecha qurbonlar:</b>")
        for v in dead:
            r = ROLES[v.role]
            parts.append(f"  • <b>{v.name}</b> uyida o'liq holda topildi.\n    Uning roli: {r.emoji} <b>{r.title}</b> edi.")
        for v in extra_dead:
            r = ROLES[v.role]
            parts.append(f"\n💣 <b>Kamikadze!</b> O'lim arafasida <b>{v.name}</b> ham olib ketildi.\n    Uning roli: {r.emoji} <b>{r.title}</b> edi.")

    if res["xaker_swapped"]:
        parts.append("\n💻 <b>Xaker</b> tunda ikki o'yinchining rollarini almashtirib qo'ydi!")

    if res["daydi_location"]:
        loc = res["daydi_location"]
        parts.append(f"\n🚶 <b>Daydi</b> kecha <b>{loc.name}</b>ning uyida tunagan edi.")

    alive_list = "\n".join(f"  {i}. {p.name}" for i, p in enumerate(game.alive_players(), 1))
    parts.append(f"\n👥 <b>Tirik o'yinchilar ({len(game.alive_players())}):</b>\n{alive_list}")

    await context.bot.send_message(chat_id=chat_id, text="\n".join(parts), parse_mode="HTML")

    # Komissar/Serjant result (private; suppressed if blocked by Mashuqa)
    checked_id = game.night_komissar_target
    if (checker and checker.alive and checked_id
            and checker.user_id not in game.night_blocked):
        result = game.komissar_check(checked_id)
        checked_player = game.players.get(checked_id)
        if checked_player:
            await try_dm(
                context, checker.user_id,
                text=f"🔍 Kecha tekshiruv natijasi:\n<b>{checked_player.name}</b> → {result}",
                parse_mode="HTML",
            )

    winner = game.check_winner()
    if winner:
        await finish_game(context, chat_id, winner)
        return
    await start_day(context, chat_id)


# ─────────────────────── DAY ────────────────────────────────────

async def start_day(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    game.phase = GamePhase.DAY_DISCUSSION
    game.reset_votes()

    pl = "\n".join(f"  {i}. {p.name}" for i, p in enumerate(game.alive_players(), 1))

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🗣 <b>MUHOKAMA VAQTI</b>\n\n"
            f"Kim mafiya? Kim begunoh? Bahslashing!\n\n"
            f"👥 <b>Tirik o'yinchilar ({len(game.alive_players())}):</b>\n{pl}\n\n"
            f"⏱ {config.DISCUSSION_TIME} soniya muhokama\n"
            f"<code>/vote</code> — ovoz berishni erta boshlash"
        ),
        parse_mode="HTML",
    )

    # G'iybatchi day action
    giyi = game.player_by_role(RoleName.GIYIBATCHI)
    if giyi and not game.giyi_used:
        kb = players_kb(game, "giyi_p", exclude=giyi.user_id, skip_cb="giyi_skip")
        await try_dm(
            context, giyi.user_id,
            text=(
                "🗣️ <b>G'iybatchi!</b>\n\n"
                "Soxta Komissar tekshiruvi natijasini guruhga e'lon qilishingiz mumkin.\n"
                "Kim haqida yolg'on ma'lumot tarqatasiz?"
            ),
            reply_markup=kb,
            parse_mode="HTML",
        )

    await cancel_timer(chat_id)
    timers[chat_id] = asyncio.create_task(_disc_timeout(context, chat_id))


async def _disc_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    total = config.DISCUSSION_TIME
    remaining = total
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_fmt_countdown("🗣", "Muhokama tugashiga", remaining),
            parse_mode="HTML",
        )
        game.countdown_msg_id = msg.message_id
    except Exception:
        pass
    try:
        while remaining > 0:
            step = min(15, remaining)
            await asyncio.sleep(step)
            remaining -= step
            game = games.get(chat_id)
            if not game or game.phase != GamePhase.DAY_DISCUSSION:
                return  # voting started — start_voting already cleared countdown
            if remaining > 0 and game.countdown_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=game.countdown_msg_id,
                        text=_fmt_countdown("🗣", "Muhokama tugashiga", remaining),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except asyncio.CancelledError:
        return  # cancelled by cancel_timer — start_voting will clear countdown
    timers.pop(chat_id, None)
    game = games.get(chat_id)
    if game and game.phase == GamePhase.DAY_DISCUSSION:
        await start_voting(context, chat_id)


async def start_voting(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game or game.phase != GamePhase.DAY_DISCUSSION:
        return
    game.phase = GamePhase.DAY_VOTING  # guard before first await — prevents double entry
    await cancel_timer(chat_id)
    await _clear_countdown(context, game)
    game.reset_votes()

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🗳🗳🗳 <b>OVOZ BERISH</b> 🗳🗳🗳\n\n"
            f"Kimni shahardan haydash kerak?\n"
            f"⏱ {config.VOTE_TIME} soniya"
        ),
        reply_markup=vote_kb(game),
        parse_mode="HTML",
    )
    game.vote_message_id = msg.message_id
    timers[chat_id] = asyncio.create_task(_vote_timeout(context, chat_id))


async def _vote_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game:
        return
    total = config.VOTE_TIME
    remaining = total
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=_fmt_countdown("🗳", "Ovoz berish tugashiga", remaining),
            parse_mode="HTML",
        )
        game.countdown_msg_id = msg.message_id
    except Exception:
        pass
    try:
        while remaining > 0:
            step = min(10, remaining)
            await asyncio.sleep(step)
            remaining -= step
            game = games.get(chat_id)
            if not game or game.phase != GamePhase.DAY_VOTING:
                return  # ended early — end_voting already cleared countdown
            if remaining > 0 and game.countdown_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=game.countdown_msg_id,
                        text=_fmt_countdown("🗳", "Ovoz berish tugashiga", remaining),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
    except asyncio.CancelledError:
        return  # cancelled by cancel_timer — end_voting will clear countdown
    timers.pop(chat_id, None)
    game = games.get(chat_id)
    if game and game.phase == GamePhase.DAY_VOTING:
        await end_voting(context, chat_id)


async def end_voting(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game = games.get(chat_id)
    if not game or game.phase != GamePhase.DAY_VOTING:
        return
    game.phase = GamePhase.NIGHT  # guard before first await — prevents double entry
    await cancel_timer(chat_id)
    await _clear_countdown(context, game)

    lynched = game.process_votes()

    if not lynched:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚖️ <b>Ovozlar teng keldi!</b>\n\nShahar qaror qabul qila olmadi. Hech kim haydalmaidi.\n\n🌙 Kecha boshlanadi...",
            parse_mode="HTML",
        )
    else:
        r = ROLES[lynched.role]
        vote_count = sum(1 for v in game.day_votes.values() if v == lynched.user_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚖️ <b>SHAHAR QARORI</b>\n\n"
                f"<b>{lynched.name}</b> shahardan haydaldi! ({vote_count} ovoz)\n\n"
                f"Uning roli: {r.emoji} <b>{r.title}</b>"
            ),
            parse_mode="HTML",
        )
        if lynched.role == RoleName.KAMIKADZE:
            rev = await kamikadze_revenge(context, game, lynched)
            if rev:
                rr = ROLES[rev.role]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"💣 <b>KAMIKADZE!</b>\n{lynched.name} o'lim arafasida <b>{rev.name}</b>ni ham olib ketdi!\nUning roli: {rr.emoji} <b>{rr.title}</b>",
                    parse_mode="HTML",
                )

    winner = game.check_winner()
    if winner:
        await finish_game(context, chat_id, winner)
        return
    await asyncio.sleep(3)
    await start_night(context, chat_id)


# ─────────────────────── FINISH ─────────────────────────────────

async def finish_game(context: ContextTypes.DEFAULT_TYPE, chat_id: int, winner: str):
    game = games.get(chat_id)
    if not game:
        return
    game.phase = GamePhase.ENDED
    await cancel_timer(chat_id)

    banners = {
        "civil": "🎉🎉🎉 <b>TINCH AHOLI G'ALABA QILDI!</b> 🎉🎉🎉\n\n✅ Barcha mafia a'zolari yo'q qilindi!",
        "mafia": "🔴🔴🔴 <b>MAFIA G'ALABA QILDI!</b> 🔴🔴🔴\n\n💀 Shahar mafia qo'liga o'tdi!",
        "qotil": "🗡️🗡️🗡️ <b>QOTIL G'ALABA QILDI!</b> 🗡️🗡️🗡️\n\n☠️ Yolg'iz qotil hamma ustidan zafar qozondi!",
    }
    roles_reveal = plist(game, show_roles=True)

    # Record stats before clearing game state
    player_stats.record_game(list(game.players.values()), winner)

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{banners[winner]}\n\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🎭 <b>Barcha rollar ochildi:</b>\n{roles_reveal}\n"
            f"━━━━━━━━━━━━━━━━━━━\n\n"
            f"📊 Statistika: /profile\n"
            f"🎮 Yangi o'yin: /newgame"
        ),
        parse_mode="HTML",
    )

    for uid in list(game.players.keys()):
        user_game.pop(uid, None)
    games.pop(chat_id, None)


# ─────────────────────── COMMANDS ───────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        await update.message.reply_text(
            "👋 Salom! Men <b>Mafia</b> o'yin botiman.\n\n"
            "Guruhga qo'shing va /newgame buyrug'ini yuboring!",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("Mafia o'yini uchun /newgame yuboring!")


async def cmd_newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Bu buyruq faqat guruhlarda ishlaydi!")
        return
    ex = games.get(chat.id)
    if ex and ex.phase != GamePhase.ENDED:
        await update.message.reply_text("❌ Allaqachon aktiv o'yin bor. /endgame bilan tugatib yangi o'yin boshlang.")
        return

    game = Game(chat_id=chat.id, creator_id=user.id)
    games[chat.id] = game

    # Add creator
    game.add_player(user.id, user.full_name, user.username)
    user_game[user.id] = chat.id

    # Auto-add all tracked group members
    auto_added: list[Player] = []
    for uid, (name, username) in group_members.get(chat.id, {}).items():
        if uid == user.id:
            continue
        if uid in user_game:
            continue  # already in another game
        if game.add_player(uid, name, username):
            user_game[uid] = chat.id
            auto_added.append(game.players[uid])

    # Build notification text
    lines = [lobby_text(game)]
    if auto_added:
        tags = " ".join(mention(p) for p in auto_added)
        lines.append(
            f"\n📣 Guruh a'zolari chaqirildi: {tags}\n"
            f"Ishtirok etmaslik uchun <b>❌ Chiqish</b> tugmasini bosing."
        )

    msg = await update.message.reply_text(
        "\n".join(lines), reply_markup=lobby_kb(chat.id), parse_mode="HTML"
    )
    game.join_message_id = msg.message_id


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        await update.message.reply_text("❌ Bu buyruq faqat guruhlarda ishlaydi!")
        return
    game = games.get(chat.id)
    if not game or game.phase != GamePhase.LOBBY:
        await update.message.reply_text("❌ Hozir qo'shilish mumkin emas.")
        return
    if user.id in game.players:
        await update.message.reply_text("❌ Siz allaqachon qo'shilgansiz!")
        return
    if not game.add_player(user.id, user.full_name, user.username):
        await update.message.reply_text("❌ O'yin to'la!")
        return
    user_game[user.id] = chat.id
    await update.message.reply_text(f"✅ {user.full_name} o'yinga qo'shildi!")


async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    game = games.get(chat.id)
    if not game or game.phase != GamePhase.LOBBY:
        await update.message.reply_text("❌ Hozir chiqib ketib bo'lmaydi.")
        return
    if user.id not in game.players:
        await update.message.reply_text("❌ Siz o'yinda emassiz.")
        return
    if user.id == game.creator_id:
        await update.message.reply_text("❌ Yaratuvchi lobbydan chiqa olmaydi. O'yinni bekor qilish uchun /endgame dan foydalaning.")
        return
    game.remove_player(user.id)
    user_game.pop(user.id, None)
    await update.message.reply_text(f"👋 {user.full_name} o'yindan chiqdi.")


async def cmd_startgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    game = games.get(chat.id)
    if not game:
        await update.message.reply_text("❌ Hozir o'yin yo'q.")
        return
    if user.id != game.creator_id:
        mem = await context.bot.get_chat_member(chat.id, user.id)
        if mem.status not in ("administrator", "creator"):
            await update.message.reply_text("❌ Faqat o'yin yaratuvchisi boshlashi mumkin!")
            return
    if game.phase != GamePhase.LOBBY:
        await update.message.reply_text("❌ O'yin allaqachon boshlangan!")
        return
    if len(game.players) < config.MIN_PLAYERS:
        await update.message.reply_text(f"❌ Kamida {config.MIN_PLAYERS} o'yinchi kerak!")
        return
    await _launch_game(context, game)


async def cmd_endgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    game = games.get(chat.id)
    if not game:
        await update.message.reply_text("❌ Aktiv o'yin yo'q.")
        return
    mem = await context.bot.get_chat_member(chat.id, user.id)
    if user.id != game.creator_id and mem.status not in ("administrator", "creator"):
        await update.message.reply_text("❌ Faqat yaratuvchi yoki admin tugatishi mumkin.")
        return
    await cancel_timer(chat.id)
    await _clear_countdown(context, game)
    for uid in list(game.players.keys()):
        user_game.pop(uid, None)
    games.pop(chat.id, None)
    await update.message.reply_text("🛑 O'yin tugatildi.")


async def cmd_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, user = update.effective_chat, update.effective_user
    game = games.get(chat.id)
    if not game or game.phase != GamePhase.DAY_DISCUSSION:
        await update.message.reply_text("❌ Hozir ovoz berish vaqti emas.")
        return
    p = game.players.get(user.id)
    if not p or not p.alive:
        return
    await start_voting(context, chat.id)


async def cmd_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    game = games.get(chat.id)
    if not game:
        await update.message.reply_text("❌ Aktiv o'yin yo'q.")
        return
    await update.message.reply_text(
        f"👥 <b>O'yinchilar (tirik: {len(game.alive_players())}):</b>\n{plist(game)}",
        parse_mode="HTML",
    )


async def cmd_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    team_label = {"mafia": "🔴 Qora", "civil": "⚪ Oq", "neutral": "🟡 Neytral"}
    lines = []
    for r in ROLES.values():
        lines.append(f"{r.emoji} <b>{r.title}</b> [{team_label[r.team.value]}]\n{r.description}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🎮 MAFIA — BUYRUQLAR</b>\n\n"
        "/newgame — Yangi o'yin (guruhda)\n"
        "/join — O'yinga qo'shilish\n"
        "/leave — Lobbydan chiqish\n"
        "/startgame — O'yinni boshlash\n"
        "/endgame — O'yinni tugatish\n"
        "/vote — Ovoz berishni boshlash\n"
        "/players — O'yinchilar ro'yxati\n"
        "/roles — Barcha rollar haqida\n"
        "/profile — Mening statistikam\n"
        "/top — Eng yaxshi o'yinchilar\n"
        "/help — Ushbu yordam",
        parse_mode="HTML",
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # Allow /profile @username in groups to view others
    target_user = user
    if context.args and update.effective_chat.type != "private":
        # /profile @someone — not implemented yet, show own
        pass
    text = player_stats.format_profile(
        target_user.id, target_user.full_name, target_user.username
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        player_stats.format_top(10), parse_mode="HTML"
    )


# ─────────────────────── LAUNCH ─────────────────────────────────

async def _launch_game(context: ContextTypes.DEFAULT_TYPE, game: Game):
    game.assign_roles()

    await context.bot.send_message(
        chat_id=game.chat_id,
        text=(
            f"🎮 <b>O'YIN BOSHLANDI!</b>\n\n"
            f"👥 O'yinchilar: <b>{len(game.players)}</b>\n"
            f"🎭 Rollar taqsimlandi!\n\n"
            f"Shaxsiy xabarlaringizni tekshiring. "
            f"Agar xabar kelmagan bo'lsa, botga /start yuboring.\n\n"
            f"5 soniyadan keyin kecha boshlanadi..."
        ),
        parse_mode="HTML",
    )
    await send_roles(context, game)
    await asyncio.sleep(5)
    await start_night(context, game.chat_id)


# ─────────────────────── CALLBACKS ──────────────────────────────

async def cb_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = int(q.data.split("_")[1])
    user = q.from_user
    game = games.get(chat_id)
    if not game or game.phase != GamePhase.LOBBY:
        await q.answer("O'yin topilmadi yoki boshlangan!", show_alert=True)
        return
    if user.id in game.players:
        await q.answer("Siz allaqachon qo'shilgansiz!", show_alert=True)
        return
    if not game.add_player(user.id, user.full_name, user.username):
        await q.answer("O'yin to'la!", show_alert=True)
        return
    user_game[user.id] = chat_id
    await q.answer("✅ Qo'shildingiz!")
    try:
        await q.edit_message_text(lobby_text(game), reply_markup=lobby_kb(chat_id), parse_mode="HTML")
    except BadRequest:
        pass


async def cb_leave_lobby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = int(q.data.split("_")[1])
    user = q.from_user
    game = games.get(chat_id)

    if not game or game.phase != GamePhase.LOBBY:
        await q.answer("Lobby tugagan!", show_alert=True)
        return
    if user.id not in game.players:
        await q.answer("Siz o'yinda emassiz!", show_alert=True)
        return
    if user.id == game.creator_id:
        await q.answer("Yaratuvchi lobbydan chiqa olmaydi!", show_alert=True)
        return

    game.remove_player(user.id)
    user_game.pop(user.id, None)
    await q.answer("❌ Siz lobbydan chiqdingiz.")
    try:
        await q.edit_message_text(lobby_text(game), reply_markup=lobby_kb(chat_id), parse_mode="HTML")
    except BadRequest:
        pass


async def cb_gstart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = int(q.data.split("_")[1])
    user = q.from_user
    game = games.get(chat_id)
    if not game:
        await q.answer("O'yin topilmadi!", show_alert=True)
        return
    if user.id != game.creator_id:
        await q.answer("Faqat o'yin yaratuvchisi boshlashi mumkin!", show_alert=True)
        return
    if game.phase != GamePhase.LOBBY:
        await q.answer("O'yin allaqachon boshlangan!", show_alert=True)
        return
    if len(game.players) < config.MIN_PLAYERS:
        await q.answer(f"Kamida {config.MIN_PLAYERS} o'yinchi kerak!", show_alert=True)
        return
    await q.answer()
    try:
        await q.edit_message_text(f"🎮 O'yin boshlanyapti! {len(game.players)} o'yinchi.", parse_mode="HTML")
    except BadRequest:
        pass
    await _launch_game(context, game)


async def cb_night_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    user = q.from_user

    chat_id = user_game.get(user.id)
    if not chat_id:
        await q.answer("Siz o'yinda emassiz!", show_alert=True)
        return
    game = games.get(chat_id)

    # ── skip ──
    if data in ("night_skip", "xswap_skip"):
        if game and game.phase == GamePhase.NIGHT:
            game.night_done.add(user.id)
            await q.answer("O'tkazib yubordingiz.")
            try:
                await q.edit_message_text("⏭ O'tkazib yubordingiz.")
            except BadRequest:
                pass
            if game.all_night_done():
                await end_night(context, chat_id)
        return

    if not game or game.phase != GamePhase.NIGHT:
        await q.answer("Hozir kecha harakatlari vaqti emas!", show_alert=True)
        return

    player = game.players.get(user.id)
    if not player or not player.alive:
        await q.answer("Siz o'liksiz!", show_alert=True)
        return

    # parse prefix and target
    parts = data.split("_")
    prefix = parts[0]      # e.g. "nkill", "xswap1"
    target_id = int(parts[1]) if len(parts) > 1 else 0

    target = game.players.get(target_id)
    if not target or not target.alive:
        await q.answer("Bu o'yinchi mavjud emas!", show_alert=True)
        return

    # ── mafia kill ──
    if prefix == "nkill":
        if target.team == Team.MAFIA:
            await q.answer("Jamoadoshingizni o'ldira olmaysiz!", show_alert=True)
            return
        game.night_kill_target = target_id
        game.night_done.add(user.id)
        # notify other mafia
        for m in game.mafia_players():
            if m.user_id != user.id:
                await try_dm(
                    context, m.user_id,
                    text=f"🔴 {player.name} <b>{target.name}</b>ni o'ldirish uchun tanladi.",
                    parse_mode="HTML",
                )
        await q.answer(f"✅ {target.name} tanlandi!")
        try:
            await q.edit_message_text(f"🔴 Siz <b>{target.name}</b>ni tanladingiz.", parse_mode="HTML")
        except BadRequest:
            pass

    # ── mashuqa block ──
    elif prefix == "nmash":
        game.night_mashuqa_target = target_id
        game.night_done.add(user.id)
        await q.answer(f"✅ {target.name} bloklandi!")
        try:
            await q.edit_message_text(f"💃 Siz <b>{target.name}</b>ni chalg'itasiz.", parse_mode="HTML")
        except BadRequest:
            pass

    # ── advokat protect ──
    elif prefix == "nadv":
        game.night_advokat_target = target_id
        game.night_done.add(user.id)
        await q.answer(f"✅ {target.name} himoyalandi!")
        try:
            await q.edit_message_text(f"⚖️ Siz <b>{target.name}</b>ni himoya qilyapsiz.", parse_mode="HTML")
        except BadRequest:
            pass

    # ── komissar / serjant check ──
    elif prefix in ("nkomir", "nserj"):
        game.night_komissar_target = target_id
        game.night_done.add(user.id)
        result = game.komissar_check(target_id)
        await q.answer("Natija keldi!")
        try:
            await q.edit_message_text(
                f"🔍 Tekshiruv natijasi:\n<b>{target.name}</b> → {result}",
                parse_mode="HTML",
            )
        except BadRequest:
            pass

    # ── doktor / hamshira heal ──
    elif prefix in ("ndokt", "nhamsh"):
        if target_id == user.id and player.self_heal_used:
            await q.answer("O'zingizni faqat 1 marta davolashingiz mumkin!", show_alert=True)
            return
        if target_id == user.id:
            player.self_heal_used = True
        game.night_doktor_target = target_id
        game.night_done.add(user.id)
        await q.answer(f"✅ {target.name} davolandi!")
        try:
            await q.edit_message_text(f"💊 Siz <b>{target.name}</b>ni davoldingiz.", parse_mode="HTML")
        except BadRequest:
            pass

    # ── daydi ──
    elif prefix == "ndaydi":
        game.night_daydi_target = target_id
        game.night_done.add(user.id)
        await q.answer(f"✅ {target.name}ning uyida tunaysiz.")
        try:
            await q.edit_message_text(f"🚶 Siz <b>{target.name}</b>ning uyida tunaysiz.", parse_mode="HTML")
        except BadRequest:
            pass

    # ── qotil ──
    elif prefix == "nqotil":
        game.night_qotil_target = target_id
        game.night_done.add(user.id)
        await q.answer(f"✅ {target.name} tanlandi!")
        try:
            await q.edit_message_text(f"🗡️ Siz <b>{target.name}</b>ni tanladingiz.", parse_mode="HTML")
        except BadRequest:
            pass

    # ── xaker step 1 ──
    elif prefix == "xswap1":
        game.xaker_first = target_id
        await q.answer(f"1️⃣ {target.name} tanlandi. Endi ikkinchisini tanlang.")
        kb = players_kb(game, "xswap2", exclude=target_id, skip_cb="xswap_skip")
        try:
            await q.edit_message_text(
                f"💻 <b>1-o'yinchi:</b> {target.name}\n\nEndi <b>2-o'yinchi</b>ni tanlang:",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except BadRequest:
            pass
        return  # don't mark done yet

    # ── xaker step 2 ──
    elif prefix == "xswap2":
        if not game.xaker_first:
            await q.answer("Avval birinchi o'yinchini tanlang!", show_alert=True)
            return
        p1 = game.players.get(game.xaker_first)
        if not p1 or not p1.alive:
            await q.answer("Birinchi o'yinchi endi mavjud emas!", show_alert=True)
            game.xaker_first = None
            return
        if target_id == game.xaker_first:
            await q.answer("Bir xil o'yinchini tanlash mumkin emas!", show_alert=True)
            return
        game.night_xaker_swap = (game.xaker_first, target_id)
        game.xaker_first = None
        game.night_done.add(user.id)
        await q.answer(f"✅ {p1.name} va {target.name} rollari almashadi!")
        try:
            await q.edit_message_text(
                f"💻 <b>{p1.name}</b> va <b>{target.name}</b>ning rollari almashinadi.",
                parse_mode="HTML",
            )
        except BadRequest:
            pass

    else:
        await q.answer()
        return

    if game.all_night_done():
        await end_night(context, chat_id)


async def cb_kamikaze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    data = q.data

    chat_id = user_game.get(user.id)
    if not chat_id:
        await q.answer("Siz o'yinda emassiz!", show_alert=True)
        return
    game = games.get(chat_id)
    if not game or not game.kamikadze_event:
        await q.answer("Vaqt o'tdi yoki noto'g'ri buyruq!", show_alert=True)
        return
    if game.kamikadze_pending_id != user.id:
        await q.answer("Bu sizga emas!", show_alert=True)
        return

    if data == "kamikaze_skip":
        game.kamikadze_event.set()
        await q.answer("O'tkazib yubordingiz.")
        try:
            await q.edit_message_text("💣 Hech kimni olib ketmadingiz.")
        except BadRequest:
            pass
        return

    target_id = int(data.split("_")[1])
    target = game.players.get(target_id)
    if not target or not target.alive:
        await q.answer("Bu o'yinchi mavjud emas!", show_alert=True)
        return

    game.kamikadze_revenge = target_id
    game.kamikadze_event.set()
    await q.answer(f"💣 {target.name} bilan birga ketasiz!")
    try:
        await q.edit_message_text(f"💣 Siz <b>{target.name}</b>ni tanladingiz.", parse_mode="HTML")
    except BadRequest:
        pass


async def cb_giyi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    data = q.data

    chat_id = user_game.get(user.id)
    if not chat_id:
        await q.answer("Siz o'yinda emassiz!", show_alert=True)
        return
    game = games.get(chat_id)
    if not game:
        return

    player = game.players.get(user.id)
    if not player or player.role != RoleName.GIYIBATCHI:
        await q.answer("Siz g'iybatchi emassiz!", show_alert=True)
        return

    if data == "giyi_skip":
        await q.answer("O'tkazib yubordingiz.")
        try:
            await q.edit_message_text("🗣️ O'tkazib yubordingiz.")
        except BadRequest:
            pass
        return

    # step 1: choose player
    if data.startswith("giyi_p_"):
        target_id = int(data.split("_")[2])
        target = game.players.get(target_id)
        if not target or not target.alive:
            await q.answer("Bu o'yinchi mavjud emas!", show_alert=True)
            return
        game.giyi_target = target_id
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔴 MAFIA deb e'lon qil", callback_data="giyi_result_mafia")],
            [InlineKeyboardButton("⚪ TINCH deb e'lon qil", callback_data="giyi_result_tinch")],
        ])
        await q.answer()
        try:
            await q.edit_message_text(
                f"🗣️ <b>{target.name}</b> haqida qanday ma'lumot tarqatasiz?",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except BadRequest:
            pass
        return

    # step 2: choose fake result
    if data in ("giyi_result_mafia", "giyi_result_tinch"):
        if game.giyi_used:
            await q.answer("Siz allaqachon foydalangansiz!", show_alert=True)
            return
        if not game.giyi_target:
            await q.answer("Avval o'yinchini tanlang!", show_alert=True)
            return
        target = game.players.get(game.giyi_target)
        if not target:
            await q.answer("O'yinchi topilmadi!", show_alert=True)
            return

        game.giyi_used = True
        result_str = "🔴 QORA — MAFIA!" if data == "giyi_result_mafia" else "⚪ TINCH"
        await q.answer("📢 Xabar yuborildi!")
        try:
            await q.edit_message_text("🗣️ Soxta ma'lumot tarqatildi.")
        except BadRequest:
            pass

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📢 <b>Noma'lum manbadan xabar:</b>\n\n<b>{target.name}</b> tekshirildi → {result_str}",
            parse_mode="HTML",
        )


async def cb_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = q.from_user
    data = q.data

    chat_id = user_game.get(user.id)
    if not chat_id:
        await q.answer("Siz o'yinda emassiz!", show_alert=True)
        return
    game = games.get(chat_id)
    if not game or game.phase != GamePhase.DAY_VOTING:
        await q.answer("Hozir ovoz berish vaqti emas!", show_alert=True)
        return

    voter = game.players.get(user.id)
    if not voter or not voter.alive:
        await q.answer("O'liklar ovoz bera olmaydi!", show_alert=True)
        return

    if data == "vote_skip":
        game.day_votes.pop(user.id, None)
        await q.answer("Ovozingiz bekor qilindi.")
    else:
        target_id = int(data.split("_")[1])
        target = game.players.get(target_id)
        if not target or not target.alive:
            await q.answer("Bu o'yinchi mavjud emas!", show_alert=True)
            return
        game.day_votes[user.id] = target_id
        await q.answer(f"✅ {target.name}ga ovoz berdingiz!")

    try:
        await q.edit_message_reply_markup(reply_markup=vote_kb(game))
    except BadRequest:
        pass

    if len(game.day_votes) >= len(game.alive_players()):
        await end_voting(context, chat_id)


# ─────────────────────── ROUTER ─────────────────────────────────

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data

    if data.startswith("join_"):
        await cb_join(update, context)
    elif data.startswith("lleave_"):
        await cb_leave_lobby(update, context)
    elif data.startswith("gstart_"):
        await cb_gstart(update, context)
    elif data == "night_skip" or data == "xswap_skip" or any(data.startswith(p) for p in NIGHT_PREFIXES):
        await cb_night_action(update, context)
    elif data.startswith("kamikaze_"):
        await cb_kamikaze(update, context)
    elif data.startswith("giyi_"):
        await cb_giyi(update, context)
    elif data.startswith("vote_"):
        await cb_vote(update, context)
    else:
        await update.callback_query.answer()


# ─────────────────────── MAIN ───────────────────────────────────

def main():
    token = config.BOT_TOKEN
    if not token:
        raise RuntimeError("BOT_TOKEN topilmadi! .env faylini tekshiring.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newgame", cmd_newgame))
    app.add_handler(CommandHandler("join", cmd_join))
    app.add_handler(CommandHandler("leave", cmd_leave))
    app.add_handler(CommandHandler("startgame", cmd_startgame))
    app.add_handler(CommandHandler("endgame", cmd_endgame))
    app.add_handler(CommandHandler("vote", cmd_vote))
    app.add_handler(CommandHandler("players", cmd_players))
    app.add_handler(CommandHandler("roles", cmd_roles))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CallbackQueryHandler(callback_router))
    # Mafia group chat — forward DMs to teammates during night
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
        handle_dm,
    ))
    # Track group members (runs for every message, non-blocking)
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.ALL & ~filters.COMMAND,
        track_member,
        block=False,
    ))

    logger.info("Bot ishga tushirildi.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
