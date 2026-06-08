from enum import Enum
from dataclasses import dataclass


class Team(Enum):
    MAFIA = "mafia"
    CIVIL = "civil"
    NEUTRAL = "neutral"


class RoleName(Enum):
    # Mafia
    DON = "don"
    MASHUQA = "mashuqa"
    ADVOKAT = "advokat"
    # Civil
    KOMISSAR = "komissar"
    SERJANT = "serjant"
    DOKTOR = "doktor"
    HAMSHIRA = "hamshira"
    GIYIBATCHI = "giyibatchi"
    XAKER = "xaker"
    KAMIKADZE = "kamikadze"
    DAYDI = "daydi"
    TINCH_AHOLI = "tinch_aholi"
    # Neutral
    QOTIL = "qotil"


@dataclass
class Role:
    name: RoleName
    title: str
    emoji: str
    team: Team
    description: str
    night_action: bool = False
    day_action: bool = False
    action_prompt: str = ""


ROLES: dict[RoleName, Role] = {
    RoleName.DON: Role(
        name=RoleName.DON,
        title="Don",
        emoji="🔴",
        team=Team.MAFIA,
        description=(
            "Mafia rahbari. Komissar tekshirganda doim tinch fuqaro sifatida ko'rinadi. "
            "Kechasi mafia nomidan o'ldirish buyrug'ini beradi."
        ),
        night_action=True,
        action_prompt="🔴 Kim o'lsin?",
    ),
    RoleName.MASHUQA: Role(
        name=RoleName.MASHUQA,
        title="Mashuqa",
        emoji="💃",
        team=Team.MAFIA,
        description=(
            "Kechasi bir o'yinchini chalg'itadi — tanlangan o'yinchi o'sha kecha "
            "o'z maxsus qobiliyatini ishlata olmaydi."
        ),
        night_action=True,
        action_prompt="💃 Kimning qobiliyatini bloklaasiz?",
    ),
    RoleName.ADVOKAT: Role(
        name=RoleName.ADVOKAT,
        title="Advokat",
        emoji="⚖️",
        team=Team.MAFIA,
        description=(
            "Kechasi bir o'yinchini himoya qiladi. Himoyalangan o'yinchi "
            "Komissar yoki Serjant tomonidan tekshirilsa, aybsiz fuqaro sifatida ko'rinadi."
        ),
        night_action=True,
        action_prompt="⚖️ Kimni himoya qilasiz?",
    ),
    RoleName.KOMISSAR: Role(
        name=RoleName.KOMISSAR,
        title="Komissar",
        emoji="👮",
        team=Team.CIVIL,
        description=(
            "Har kecha bir o'yinchini tekshiradi — mafia yoki tinch ekanini aniqlaydi. "
            "Don DOIM tinch ko'rinadi."
        ),
        night_action=True,
        action_prompt="👮 Kimni tekshirasiz?",
    ),
    RoleName.SERJANT: Role(
        name=RoleName.SERJANT,
        title="Serjant",
        emoji="🎖️",
        team=Team.CIVIL,
        description=(
            "Komissarning yordamchisi. Komissar vafot etsa, uning o'rnini egallaydi "
            "va kechalari tekshiruv o'tkazadi."
        ),
        night_action=False,  # dynamically True when Komissar dies
        action_prompt="🎖️ Kimni tekshirasiz?",
    ),
    RoleName.DOKTOR: Role(
        name=RoleName.DOKTOR,
        title="Doktor",
        emoji="💊",
        team=Team.CIVIL,
        description=(
            "Har kecha bir o'yinchini davolaydi. Agar mafia o'sha o'yinchini tanlagan bo'lsa, "
            "davolanadi. O'zini faqat 1 marta davolashi mumkin."
        ),
        night_action=True,
        action_prompt="💊 Kimni davollaysiz?",
    ),
    RoleName.HAMSHIRA: Role(
        name=RoleName.HAMSHIRA,
        title="Hamshira",
        emoji="🏥",
        team=Team.CIVIL,
        description=(
            "Doktor vafot etgandan so'ng uning o'rnini egallaydi "
            "va davolash vazifasini davom ettiradi."
        ),
        night_action=False,  # dynamically True when Doktor dies
        action_prompt="🏥 Kimni davollaysiz?",
    ),
    RoleName.GIYIBATCHI: Role(
        name=RoleName.GIYIBATCHI,
        title="G'iybatchi",
        emoji="🗣️",
        team=Team.CIVIL,
        description=(
            "Kun bo'lganida o'yinchilar orasida yolg'on ma'lumot tarqatib o'yinni chalkashtiradi. "
            "Bir marta guruhga soxta Komissar tekshiruvi natijasini e'lon qilishi mumkin."
        ),
        night_action=False,
        day_action=True,
        action_prompt="🗣️ Kim haqida soxta xabar tarqatasiz?",
    ),
    RoleName.XAKER: Role(
        name=RoleName.XAKER,
        title="Xaker",
        emoji="💻",
        team=Team.CIVIL,
        description=(
            "Birinchi 3 kecha ikki o'yinchining rollarini almashtirib qo'ya oladi. "
            "4-kundan boshlab oddiy tinch aholiga aylanadi."
        ),
        night_action=True,
        action_prompt="💻 Birinchi o'yinchini tanlang (rol almashtirish):",
    ),
    RoleName.KAMIKADZE: Role(
        name=RoleName.KAMIKADZE,
        title="Kamikadze",
        emoji="💣",
        team=Team.CIVIL,
        description=(
            "O'ldirilsa yoki ovoz berib haydalsa, "
            "o'zi bilan birga yana bitta o'yinchini ham olib ketishi mumkin."
        ),
        night_action=False,
        action_prompt="💣 O'zingiz bilan kimni olib ketasiz?",
    ),
    RoleName.DAYDI: Role(
        name=RoleName.DAYDI,
        title="Daydi",
        emoji="🚶",
        team=Team.CIVIL,
        description=(
            "Har kecha bir o'yinchining uyida tunaydi. "
            "Ertalab guruhga Daydining qayerda tunaganligi e'lon qilinadi."
        ),
        night_action=True,
        action_prompt="🚶 Kimning uyida tunaysiz?",
    ),
    RoleName.TINCH_AHOLI: Role(
        name=RoleName.TINCH_AHOLI,
        title="Tinch aholi",
        emoji="👤",
        team=Team.CIVIL,
        description="Oddiy fuqaro. Kunduzi ovoz berish orqali mafia a'zolarini aniqlashga yordam beradi.",
        night_action=False,
    ),
    RoleName.QOTIL: Role(
        name=RoleName.QOTIL,
        title="Qotil",
        emoji="🗡️",
        team=Team.NEUTRAL,
        description=(
            "Yolg'iz harakat qiladi, kim ekanini hammadan yashiradi. "
            "Maqsad: hammani o'ldirib yagona tirik qolish."
        ),
        night_action=True,
        action_prompt="🗡️ Keyingi qurboniz kim?",
    ),
}


def get_role_composition(count: int) -> list[RoleName]:
    R = RoleName
    if count == 4:
        return [R.DON, R.KOMISSAR, R.DOKTOR, R.TINCH_AHOLI]
    elif count == 5:
        return [R.DON, R.KOMISSAR, R.DOKTOR, R.TINCH_AHOLI, R.TINCH_AHOLI]
    elif count == 6:
        return [R.DON, R.MASHUQA, R.KOMISSAR, R.DOKTOR, R.TINCH_AHOLI, R.TINCH_AHOLI]
    elif count == 7:
        return [R.DON, R.MASHUQA, R.KOMISSAR, R.DOKTOR, R.KAMIKADZE, R.TINCH_AHOLI, R.TINCH_AHOLI]
    elif count == 8:
        return [R.DON, R.MASHUQA, R.KOMISSAR, R.DOKTOR, R.SERJANT, R.HAMSHIRA, R.TINCH_AHOLI, R.TINCH_AHOLI]
    elif count == 9:
        return [R.DON, R.MASHUQA, R.ADVOKAT, R.KOMISSAR, R.DOKTOR, R.SERJANT, R.HAMSHIRA, R.KAMIKADZE, R.TINCH_AHOLI]
    elif count == 10:
        return [R.DON, R.MASHUQA, R.ADVOKAT, R.KOMISSAR, R.DOKTOR, R.SERJANT, R.HAMSHIRA, R.KAMIKADZE, R.GIYIBATCHI, R.TINCH_AHOLI]
    elif count == 11:
        return [R.DON, R.MASHUQA, R.ADVOKAT, R.KOMISSAR, R.DOKTOR, R.SERJANT, R.HAMSHIRA, R.KAMIKADZE, R.GIYIBATCHI, R.DAYDI, R.TINCH_AHOLI]
    elif count == 12:
        return [R.DON, R.MASHUQA, R.ADVOKAT, R.KOMISSAR, R.DOKTOR, R.SERJANT, R.HAMSHIRA, R.KAMIKADZE, R.GIYIBATCHI, R.DAYDI, R.XAKER, R.TINCH_AHOLI]
    elif count == 13:
        return [R.DON, R.MASHUQA, R.ADVOKAT, R.KOMISSAR, R.DOKTOR, R.SERJANT, R.HAMSHIRA, R.KAMIKADZE, R.GIYIBATCHI, R.DAYDI, R.XAKER, R.QOTIL, R.TINCH_AHOLI]
    else:
        base = [R.DON, R.MASHUQA, R.ADVOKAT, R.KOMISSAR, R.DOKTOR, R.SERJANT, R.HAMSHIRA,
                R.KAMIKADZE, R.GIYIBATCHI, R.DAYDI, R.XAKER, R.QOTIL]
        return base + [R.TINCH_AHOLI] * (count - len(base))
