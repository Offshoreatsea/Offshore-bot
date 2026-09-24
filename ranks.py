"""
Единая логика подбора вакансий по должностям — используется И рассылкой вакансий
подписчикам (main.py), И откликами по email (email_apply.py), чтобы правила были
одинаковыми везде.

Правила (заданы владельцем канала):
  1. Семьи должностей — человек с любой должностью из семьи получает вакансии на всю семью:
       Master = Master / Master DPO / Master SDPO (и наоборот)
       Chief Officer = Chief Officer / DPO / SDPO
       2nd Officer = 2nd Officer / DPO / JDPO / SDPO
       3rd Officer = 3rd Officer / DPO / JDPO
  2. Вакансия получает не только свой тег, но и теги по тексту должности:
       Mate, 2nd Mate, OOW              -> 2nd Officer
       3rd Mate                          -> 3rd Officer
       Chief Mate, C/O                   -> Chief Officer
       Captain, Skipper                  -> Master
       Engineer / EOOW без уточнения     -> 3rd Engineer и 2nd Engineer
  3. Все остальные должности — точное совпадение тега.
"""
import re

RANK_FAMILIES = [
    {"Master", "MasterDPO", "MasterSDPO"},
    {"ChiefOfficer", "ChiefOfficerDPO", "ChiefOfficerSDPO"},
    {"SecondOfficer", "SecondOfficerDPO", "SecondOfficerJDPO", "SecondOfficerSDPO"},
    {"ThirdOfficer", "ThirdOfficerDPO", "ThirdOfficerJDPO"},
]

# (регулярка по тексту должности в вакансии, теги, которые она добавляет)
TITLE_RULES = [
    (r"\b(captain|skipper)\b|(?<!barge )(?<!mooring )(?<!tow )(?<!dredge )(?<!quarter)\bmaster\b",
     {"Master"}),
    (r"\b(chief\s*mate|chief\s*officer|c/o)\b", {"ChiefOfficer"}),
    (r"\b(2nd|second)\s*(mate|officer)\b|\boow\b|officer of the watch"
     r"|(?<!chief )(?<!3rd )(?<!third )(?<!2nd )(?<!second )\bmate\b", {"SecondOfficer"}),
    (r"\b(3rd|third)\s*(mate|officer)\b", {"ThirdOfficer"}),
    (r"\beoow\b|engineer officer of the watch", {"ThirdEngineer", "SecondEngineer"}),
    (r"\b(2nd|second)\s*engineer\b", {"SecondEngineer"}),
    (r"\b(3rd|third)\s*engineer\b", {"ThirdEngineer"}),
    (r"\b(chief\s*engineer|c/e)\b", {"ChiefEngineer"}),
]

# «Engineer» без уточнения (не chief/2nd/3rd/junior/electro/survey/ROV…) = 3rd и 2nd Engineer
GENERIC_ENGINEER = re.compile(
    r"(?<!chief )(?<!2nd )(?<!second )(?<!3rd )(?<!third )(?<!junior )(?<!electro-)(?<!electro )"
    r"(?<!survey )(?<!rov )(?<!subsea )(?<!project )(?<!field )(?<!service )\bengineer\b", re.I)

EXTRA_LABELS = {
    "MasterDPO": "Master / DPO", "ChiefOfficerDPO": "Chief Officer / DPO",
    "SecondOfficerSDPO": "2nd Officer / SDPO", "ThirdOfficerDPO": "3rd Officer / DPO",
}


def family(tag: str) -> set[str]:
    """Вся семья должности (или сама должность, если семьи нет)."""
    for fam in RANK_FAMILIES:
        if tag in fam:
            return set(fam)
    return {tag}


def expand(tags) -> set[str]:
    """Должности человека -> все теги вакансий, которые ему подходят."""
    out: set[str] = set()
    for t in tags:
        if t:
            out |= family(t)
    return out


def vacancy_tags(fields: dict) -> set[str]:
    """Все теги, под которые подходит вакансия: её тег + теги по тексту должности, с семьями."""
    tags = {fields.get("position_tag")} - {None, "", "Other"}
    title = (fields.get("position") or "").lower()
    for pattern, add in TITLE_RULES:
        if re.search(pattern, title, re.I):
            tags |= add
    if GENERIC_ENGINEER.search(title) and not re.search(r"electrical|electro|\beto\b", title):
        tags |= {"ThirdEngineer", "SecondEngineer"}
    return expand(tags)


def matches(person_tags, fields: dict) -> bool:
    """Подходит ли вакансия человеку с этими должностями."""
    return bool(expand(person_tags) & vacancy_tags(fields))
