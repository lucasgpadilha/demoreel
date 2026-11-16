"""Match chat: what kind of message each line is and which kill it reacts to.

The classification is a small word list tuned on Brazilian mix chat (laughing, crying about a death, swearing
at somebody, praising a play, good game, server commands). It is meant for banter between friends - a "biggest
cry-baby of the night" ranking - not for moderation: it misses sarcasm and anything it has no word for.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .schema import RoundSummary

REACTION_WINDOW_S = 20.0  # a line this soon after a kill is taken as a reaction to it
KINDS = ("command", "toxic", "whine", "laugh", "praise", "gg", "talk")

_LAUGH = re.compile(r"k{3,}|(?:ky){2,}|(?:ha){2,}|(?:he){3,}|(?:rs){2,}|(?:hu[ae]){2,}|\bxd+\b|\blol+\b|\blmao+\b|\brsrs")
_LAUGH_WHOLE = {"k", "kk", "kkj", "rs", "xd", "ks", "kks"}
# Crying about a death: cheating accusations, luck, the game being unfair.
_WHINE_STEMS = ("hack", "hax", "xit", "cheat", "aimbot", "godmode", "god mode", "denunci", "report", "sortud",
                "lucky", "roubad", "impossiv", "que isso", "q isso", "ta de sacanagem", "nao da", "time ruim",
                "ninguem ajuda", "ngm ajuda", "sempre eu", "desisto", "jogo lixo", "meu deus", "ah pronto",
                "pelo amor", "inacreditav", "bugad", "lagad", "wallhack", "so da ele", "so ele mata", "rabud",
                "cagaca", "nem mirou", "n vale", "nao vale", "q iso", "que iso", "so timing", "contra mim",
                "muita sorte", "na sorte")
_WHINE_WORDS = {"wh", "wall", "lag", "bug", "aff", "afff", "affs", "mds", "sorte", "luck", "comoo", "pqp",
                "ata", "wtf", "vac", "rabudo", "rabuda", "cagada", "cagado"}
_WHINE_RUN = re.compile(r"\?{3,}|\b\d+ ?km\b")  # "?????", "tiros a 3km de distancia"
# Swearing at people.
_TOXIC_STEMS = ("lixo", "noob", "burr", "idiota", "imbecil", "retardad", "arrombad", "otari", "vagabund", "desgrac",
                "lazarent", "filho da", "vai se", "se fode", "se fude", "fuder", "tomar no", "tomanocu", "babaca",
                "merda", "bosta", "cagao", "viad", "bicha", "macac", "safad", "inutil", "horrivel",
                "ruim demais", "jogou bosta", "jogou nada", "cala a boca", "cala boca", "mongol", "palhac", "trouxa",
                "lixa", "gorila", "gordao", "gordo", "baleia", "fregues", "chora mais", "te pego", "toma no",
                "pipoqueir", "bagre", "pereba", "pe de cimento", "joga tremendo", "muito ruim", "mt ruim")
_TOXIC_WORDS = {"fdp", "vsf", "tnc", "puta", "puto", "cu", "bot", "nub", "nb", "corno",
                "ez", "ezz", "eazy", "lixos", "mija", "mijou", "pau"}
_SWEAR_WORDS = {"krl", "crl", "porra", "caralho", "caraio", "pqp", "carai"}  # interjections: a tag, not a kind
_TOXIC_RUN = re.compile(r"\bv+s+f+\b|\bt+n+c+\b|\bf+d+p+\b")
_PRAISE_STEMS = ("nice", "brab", "monstr", "jogou muito", "que bala", "absurd", "sinistr", "craque", "lind", "mito",
                 "boa", "insano", "sniper americano", "que jogada", "bem jogado")
_PRAISE_WORDS = {"ns", "n1", "god", "ace", "nt", "top"}
_PRAISE_RUN = re.compile(r"\bace+\b|\bns+\b")
_GG_WORDS = {"gg", "ggwp", "wp", "gl", "hf", "glhf", "gh", "bg", "gege"}
# Never shown as written, whatever the instance: masked in `display_text`.
_SLUR_STEMS = ("viad", "bicha", "macac", "retardad", "mongol", "travec")


def normalize(text: str) -> str:
    """Lower case, no accents, single spaces."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", plain.lower()).strip()


def classify(text: str) -> tuple[str, list[str]]:
    """``(kind, tags)``. ``kind`` is the first of ``KINDS`` that applies; ``tags`` lists every one that does."""
    raw = text.strip()
    if raw[:1] in "!./":
        return "command", ["command"]
    t = normalize(raw)
    words = set(re.findall(r"[a-z0-9]+", t))
    tags: list[str] = []
    if any(s in t for s in _TOXIC_STEMS) or words & _TOXIC_WORDS or _TOXIC_RUN.search(t):
        tags.append("toxic")
    if any(s in t for s in _WHINE_STEMS) or words & _WHINE_WORDS or _WHINE_RUN.search(t):
        tags.append("whine")
    if t in _LAUGH_WHOLE or _LAUGH.search(t):
        tags.append("laugh")
    praise = [s for s in _PRAISE_STEMS if s in t]
    if praise == ["boa"] and len(words) > 2:  # "boa!" is praise; "fez a boa", "na boa mesmo" are not
        praise = []
    if praise or words & _PRAISE_WORDS or _PRAISE_RUN.search(t):
        tags.append("praise")
    if words & _GG_WORDS and not re.search(r"\b(?:sem|no|nao|n) gg\b", t):
        tags.append("gg")
    if words & _SWEAR_WORDS:
        tags.append("swear")
    if raw.isupper() and len(raw) >= 6 and sum(c.isalpha() for c in raw) >= 5:
        tags.append("shouting")
    kind = next((k for k in KINDS if k in tags), "talk")
    return kind, tags


def display_text(text: str) -> str:
    """The line as it may be shown: slurs are masked, the rest stays as typed (it is their own banter)."""
    out = text
    plain = normalize(text)
    for stem in _SLUR_STEMS:
        for m in re.finditer(re.escape(stem) + r"[a-z]*", plain):
            if m.end() <= len(out):  # same offsets unless accents changed the length; then mask the whole line
                out = out[: m.start() + 1] + "*" * (m.end() - m.start() - 1) + out[m.end():]
            else:
                return "***"
    return out


def shareable(text: str, kind: str | None = None) -> bool:
    """Whether a line may be shown outside the match page of an organiser (videos, public pages).

    Chat is free text: people paste phone numbers, addresses and links into it. Only short lines are shown,
    and plain talk (anything the word list did not recognise as banter) only when it has no digits or links."""
    t = text.strip()
    if not t or len(t) > 70 or re.search(r"https?://|www\.|@\w+\.|\d{4,}", t):
        return False
    if kind in (None, "talk"):
        return len(t) <= 40 and not re.search(r"\d", t)
    return True


def annotate(messages: list[dict[str, Any]], rounds: list[RoundSummary], tickrate: float) -> list[dict[str, Any]]:
    """Add to every chat line: ``round``, ``kind``, ``tags`` and, when it comes within 20 s of a kill, which kill
    it reacts to (``kill_tick``, ``kill_attacker``, ``kill_victim``) and how the author relates to it
    (``relation``: ``victim`` - he just died, ``killer`` - he just killed, ``other``)."""
    window = int(REACTION_WINDOW_S * tickrate)
    kills = sorted(((k.tick, k) for r in rounds for k in r.kills if not k.is_suicide), key=lambda x: x[0])
    out = []
    for m in sorted(messages, key=lambda x: int(x.get("tick") or 0)):
        tick = int(m.get("tick") or 0)
        text = str(m.get("text") or "")
        kind, tags = classify(text)
        rnd = next((r.number for r in reversed(rounds) if r.start_tick <= tick), None)
        row = {**m, "round": rnd, "kind": kind, "tags": tags, "kill_tick": None, "kill_attacker": None,
               "kill_victim": None, "relation": None}
        if kind != "command":
            recent = [k for t, k in kills if tick - window <= t <= tick]
            author = m.get("steamid")
            pick, relation = None, None
            for k in reversed(recent):  # the author's own death or kill wins over somebody else's
                if author and k.victim.steamid == author:
                    pick, relation = k, "victim"
                    break
                if author and k.attacker is not None and k.attacker.steamid == author:
                    pick, relation = k, "killer"
                    break
            if pick is None and recent:
                pick, relation = recent[-1], "other"
            if pick is not None:
                row.update(kill_tick=pick.tick, kill_attacker=pick.attacker.steamid if pick.attacker else None,
                           kill_victim=pick.victim.steamid, relation=relation)
        out.append(row)
    return out


def reactions(chat: list[dict[str, Any]], kill_ticks: list[int], limit: int = 3) -> list[dict[str, Any]]:
    """The chat lines that react to these kills, crying and laughing first (commands and empty talk never)."""
    wanted = set(kill_ticks)
    order = {"whine": 0, "toxic": 1, "laugh": 2, "praise": 3, "gg": 5, "talk": 4}
    rows = [m for m in chat if m.get("kill_tick") in wanted and m.get("kind") not in (None, "command")
            and shareable(str(m.get("text") or ""), m.get("kind"))]
    rows.sort(key=lambda m: (0 if m.get("relation") == "victim" else 1, order.get(m.get("kind"), 9), m.get("tick", 0)))
    picked = sorted(rows[:limit], key=lambda m: m.get("tick", 0))
    return [{"tick": m.get("tick"), "name": m.get("name"), "steamid": m.get("steamid"),
             "text": display_text(str(m.get("text") or "")), "kind": m.get("kind"), "relation": m.get("relation")}
            for m in picked]
