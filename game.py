import asyncio
import random
from enum import Enum
from dataclasses import dataclass
from typing import Optional

from roles import RoleName, Team, ROLES, get_role_composition


class GamePhase(Enum):
    LOBBY = "lobby"
    NIGHT = "night"
    DAY_DISCUSSION = "day_discussion"
    DAY_VOTING = "day_voting"
    ENDED = "ended"


@dataclass
class Player:
    user_id: int
    name: str
    username: Optional[str]
    role: Optional[RoleName] = None
    alive: bool = True
    self_heal_used: bool = False  # doktor/hamshira can't self-heal twice

    @property
    def mention(self) -> str:
        return f"@{self.username}" if self.username else self.name

    @property
    def role_obj(self):
        return ROLES[self.role] if self.role else None

    @property
    def team(self) -> Optional[Team]:
        return ROLES[self.role].team if self.role else None


class Game:
    def __init__(self, chat_id: int, creator_id: int):
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.players: dict[int, Player] = {}
        self.phase = GamePhase.LOBBY
        self.round = 0

        # Night state (reset each round)
        self.night_kill_target: Optional[int] = None    # active mafia killer's choice
        self.night_doktor_target: Optional[int] = None  # doktor or hamshira
        self.night_komissar_target: Optional[int] = None
        self.night_advokat_target: Optional[int] = None
        self.night_mashuqa_target: Optional[int] = None
        self.night_daydi_target: Optional[int] = None
        self.night_qotil_target: Optional[int] = None
        self.night_xaker_swap: Optional[tuple] = None   # (p1_id, p2_id)
        self.xaker_first: Optional[int] = None          # step-1 buffer
        self.night_blocked: set[int] = set()
        self.night_done: set[int] = set()

        # Day state
        self.day_votes: dict[int, int] = {}
        self.vote_message_id: Optional[int] = None
        self.giyi_target: Optional[int] = None   # g'iybatchi: step-1 chosen player
        self.giyi_used: bool = False             # g'iybatchi: used this game

        # Kamikadze revenge (async flow)
        self.kamikadze_event: Optional[asyncio.Event] = None
        self.kamikadze_revenge: Optional[int] = None
        self.kamikadze_pending_id: Optional[int] = None

        # Lobby
        self.join_message_id: Optional[int] = None

    # ── Player helpers ──────────────────────────────────────────

    def add_player(self, user_id: int, name: str, username: Optional[str]) -> bool:
        if user_id in self.players or len(self.players) >= 15:
            return False
        self.players[user_id] = Player(user_id=user_id, name=name, username=username)
        return True

    def remove_player(self, user_id: int) -> bool:
        if user_id in self.players:
            del self.players[user_id]
            return True
        return False

    def alive_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.alive]

    def mafia_players(self) -> list[Player]:
        return [p for p in self.alive_players() if p.team == Team.MAFIA]

    def civil_players(self) -> list[Player]:
        return [p for p in self.alive_players() if p.team == Team.CIVIL]

    def neutral_players(self) -> list[Player]:
        return [p for p in self.alive_players() if p.team == Team.NEUTRAL]

    def player_by_role(self, role: RoleName) -> Optional[Player]:
        for p in self.players.values():
            if p.role == role and p.alive:
                return p
        return None

    def active_mafia_killer(self) -> Optional[Player]:
        """Who gives the kill order tonight (Don → Mashuqa → Advokat)."""
        for r in (RoleName.DON, RoleName.MASHUQA, RoleName.ADVOKAT):
            p = self.player_by_role(r)
            if p:
                return p
        return None

    def active_checker(self) -> Optional[Player]:
        """Komissar if alive, else Serjant."""
        return self.player_by_role(RoleName.KOMISSAR) or self.player_by_role(RoleName.SERJANT)

    def active_healer(self) -> Optional[Player]:
        """Doktor if alive, else Hamshira."""
        return self.player_by_role(RoleName.DOKTOR) or self.player_by_role(RoleName.HAMSHIRA)

    def player_has_night_action(self, p: Player) -> bool:
        if p.role == RoleName.XAKER and self.round > 3:
            return False
        # Serjant is active only when Komissar is dead
        if p.role == RoleName.SERJANT:
            return self.player_by_role(RoleName.KOMISSAR) is None
        # Hamshira is active only when Doktor is dead
        if p.role == RoleName.HAMSHIRA:
            return self.player_by_role(RoleName.DOKTOR) is None
        return ROLES[p.role].night_action

    # ── Roles ────────────────────────────────────────────────────

    def assign_roles(self):
        ids = list(self.players.keys())
        random.shuffle(ids)
        roles = get_role_composition(len(ids))
        random.shuffle(roles)
        for i, uid in enumerate(ids):
            self.players[uid].role = roles[i]

    # ── Night ────────────────────────────────────────────────────

    def reset_night(self):
        self.night_kill_target = None
        self.night_doktor_target = None
        self.night_komissar_target = None
        self.night_advokat_target = None
        self.night_mashuqa_target = None
        self.night_daydi_target = None
        self.night_qotil_target = None
        self.night_xaker_swap = None
        self.xaker_first = None
        self.night_blocked = set()
        self.night_done = set()
        self.giyi_target = None

    def all_night_done(self) -> bool:
        for p in self.alive_players():
            if self.player_has_night_action(p) and p.user_id not in self.night_done:
                return False
        return True

    def process_night(self) -> dict:
        results = {
            "killed": [],
            "saved": False,
            "qotil_killed": None,
            "daydi_location": None,
            "xaker_swapped": None,
        }

        # Mashuqa blocking
        if self.night_mashuqa_target and self.night_mashuqa_target in self.players:
            self.night_blocked.add(self.night_mashuqa_target)

        # Xaker swap (rounds 1-3 only, xaker not blocked)
        xaker = self.player_by_role(RoleName.XAKER)
        if (self.night_xaker_swap and self.round <= 3
                and xaker and xaker.user_id not in self.night_blocked):
            p1_id, p2_id = self.night_xaker_swap
            p1, p2 = self.players.get(p1_id), self.players.get(p2_id)
            if p1 and p2 and p1.alive and p2.alive and p1.role != p2.role:
                p1.role, p2.role = p2.role, p1.role
                results["xaker_swapped"] = (p1, p2)

        # Healer (doktor/hamshira) — blocked if targeted by Mashuqa
        healer = self.active_healer()
        heal_id = self.night_doktor_target
        if healer and healer.user_id in self.night_blocked:
            heal_id = None

        # Mafia kill
        kill_id = self.night_kill_target
        if kill_id and kill_id in self.players:
            target = self.players[kill_id]
            if target.alive:
                if heal_id == kill_id:
                    results["saved"] = True
                else:
                    target.alive = False
                    results["killed"].append(target)

        # Qotil kill (independent, not blocked by healer unless healer heals same target)
        qotil = self.player_by_role(RoleName.QOTIL)
        if qotil and self.night_qotil_target and qotil.user_id not in self.night_blocked:
            target = self.players.get(self.night_qotil_target)
            if target and target.alive and heal_id != self.night_qotil_target:
                target.alive = False
                results["qotil_killed"] = target

        # Daydi announcement (suppressed if Daydi was blocked by Mashuqa)
        daydi = self.player_by_role(RoleName.DAYDI)
        if (daydi and daydi.user_id not in self.night_blocked
                and self.night_daydi_target and self.night_daydi_target in self.players):
            results["daydi_location"] = self.players[self.night_daydi_target]

        return results

    def komissar_check(self, target_id: int) -> str:
        target = self.players.get(target_id)
        if not target:
            return "❓ Noma'lum"
        if self.night_advokat_target == target_id:
            return "⚪ TINCH"
        if target.role == RoleName.DON:
            return "⚪ TINCH"  # Don always hides
        if target.team == Team.MAFIA:
            return "🔴 QORA — MAFIA!"
        if target.team == Team.NEUTRAL:
            return "🟡 NEYTRAL"
        return "⚪ TINCH"

    # ── Voting ───────────────────────────────────────────────────

    def reset_votes(self):
        self.day_votes = {}

    def process_votes(self) -> Optional[Player]:
        if not self.day_votes:
            return None
        tally: dict[int, int] = {}
        for t in self.day_votes.values():
            tally[t] = tally.get(t, 0) + 1
        max_v = max(tally.values())
        top = [t for t, c in tally.items() if c == max_v]
        if len(top) != 1:
            return None
        pid = top[0]
        if pid in self.players:
            self.players[pid].alive = False
            return self.players[pid]
        return None

    # ── Win condition ─────────────────────────────────────────────

    def check_winner(self) -> Optional[str]:
        alive = self.alive_players()
        mafia = len([p for p in alive if p.team == Team.MAFIA])
        civil = len([p for p in alive if p.team == Team.CIVIL])
        neutral = len([p for p in alive if p.team == Team.NEUTRAL])

        if not alive:
            return "civil"
        if mafia == 0 and civil == 0 and neutral > 0:
            return "qotil"
        if mafia == 0 and neutral == 0:
            return "civil"
        if mafia > 0 and mafia >= civil + neutral:
            return "mafia"
        return None
