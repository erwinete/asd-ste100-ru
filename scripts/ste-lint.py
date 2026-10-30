#!/usr/bin/env python3
"""Детерминированная проверка русских технических текстов (Python stdlib).

Примеры:
    python scripts/ste-lint.py README.md
    cat README.md | python scripts/ste-lint.py --json
    python scripts/ste-lint.py --baseline 3 --disable passive-voice файл.md
    python scripts/ste-lint.py --mode flavored файл.md
    python scripts/ste-lint.py --selftest

Коды завершения: 0 — hard <= baseline; 1 — hard > baseline; 2 — ошибка CLI/ввода.
Линтер не оценивает истинность высказываний и никогда не помечает хеджи.
"""

import argparse
import bisect
import contextlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

# Консоль Windows может работать в однобайтовой кодировке (CP866/CP1251);
# переводим потоки в UTF-8, чтобы кириллица не искажалась при запуске
# через пайп из агентской среды или редактора кода.
for _stream in (sys.stdout, sys.stderr, sys.stdin):
    with contextlib.suppress(AttributeError, OSError, ValueError):
        _stream.reconfigure(encoding="utf-8")


HARD = "hard"
ADVISORY = "advisory"
MAX_DESCRIPTION_WORDS = 22
MAX_INSTRUCTION_WORDS = 18  # Ручная проверка: жанр предложения неизвестен.
RULE_LEVELS = {
    "semicolon": HARD,
    "long-sentence": HARD,
    "nominalization": HARD,
    "marketing-adjective": HARD,
    "slang-anglicism": HARD,
    "synonym-rotation": HARD,
    "dangling-conjunction": HARD,
    "kotoryj-chain": HARD,
    "participle-deepri": ADVISORY,
    "passive-voice": ADVISORY,
    "abbreviation": ADVISORY,
}

# Наборы составлены для русского языка, а не переведены из английского линтера.
# «откатить» не запрещаем: у него есть точное техническое значение.
SLANG = re.compile(
    r"\b(?:зачекин\w*|заапрув\w*|заскип\w*|задепло\w*|отревью\w*|"
    r"заскрин\w*|зашер\w*|зафоллов\w*|залайк\w*)\b", re.I
)
MARKETING = re.compile(
    r"\b(?:бесшовн\w*|инновационн\w*|передов\w*|уникальн\w*|"
    r"революционн\w*|мощн\w*|молниеносн\w*|непревзойд[её]нн\w*|"
    r"прорывн\w*|интеллектуальн\w*|лучш\w*\s+в\s+сво[её]м\s+классе)\b", re.I
)
# Перечень объектов исключает прямое «провести линию» и похожие выражения.
ACTION_NOUN = (
    r"провер\w*|подключен\w*|загрузк\w*|загружен\w*|тестировани\w*|"
    r"настройк\w*|настро[йк]\w*|отправк\w*|отправлени\w*|удален\w*|"
    r"удалени\w*|создани\w*|изменени\w*|обновлени\w*|анализ\w*|"
    r"синхронизаци\w*|регистраци\w*|сохранени\w*|обработк\w*|"
    r"выполнени\w*|формировани\w*"
)
WEAK_VERB = (
    r"выполн(?:ить|ите|и|ил|ила|или|яем|яет|яют|яйте|ять|ял|яли|им|ят)|"
    r"осуществ(?:ить|ите|ил|ила|или|ляет|ляют|лять|ляйте|им)|"
    r"произв(?:ести|едите|ел|ела|ели|одит|одят|одить|одите)|"
    r"пров(?:ести|едите|ел|ела|ели|одит|одят|одить|одите)|"
    r"соверш(?:ить|ите|ил|ила|или|ает|ают|ать|айте)|"
    r"реализ(?:овать|уйте|овал|овали|ует|уют|уем|овать)"
)
NOMINALIZATION = re.compile(
    rf"\b(?:{WEAK_VERB})\s+(?:(?:необходимую|повторную|полную|плановую|"
    rf"дополнительную|автоматическую|ручную|первичную|текущую)\s+)?"
    rf"(?:{ACTION_NOUN})\b", re.I
)
KOTORYJ = re.compile(r"\bкотор[а-яё]*\b", re.I)
ABBREVIATIONS = re.compile(
    r"(?<![А-Яа-яЁё])(?:т\.\s*е\.|т\.\s*д\.|т\.\s*п\.|т\.\s*н\.|"
    r"т\.\s*о\.|т\.\s*к\.|напр\.|др\.|пр\.)(?![А-Яа-яЁё])", re.I
)
PASSIVE = re.compile(
    r"\b(?:был(?:о|а|и)?|будет|будут|было|были)\s+"
    r"(?:удал[её]н\w*|создан\w*|сохран[её]н\w*|выполнен\w*|"
    r"подключ[её]н\w*|проверен\w*|отправлен\w*|измен[её]н\w*|"
    r"загружен\w*|записан\w*)\b|"
    r"\b(?:удаля\w*|созда\w*|обрабатыва\w*|сохраня\w*|"
    r"проверя\w*|загружа\w*|изменя\w*|отправля\w*|"
    r"подключа\w*|выполня\w*)ся\b|"
    r"\bявляется\s+(?:результатом|причиной|источником|объектом|элементом)\b",
    re.I,
)
DEEPRI = re.compile(
    r"\b(?:сделав|проверив|выполнив|завершив|открыв|закрыв|получив|"
    r"прочитав|создав|удалив|отправив|сохранив|запустив|настроив|"
    r"выполняя|читая|открывая|закрывая|завершая|проверяя|"
    r"обрабатывая|используя|сохраняя|отправляя)\b", re.I
)
IMPERATIVE = re.compile(
    r"\b(?:откройте|закройте|проверьте|отправьте|удалите|создайте|"
    r"выполните|запустите|настройте|подключите|получите|выберите|"
    r"нажмите|введите|сохраните|передайте|убедитесь|прочитайте|"
    r"укажите|добавьте|загрузите|измените|повторите|запишите)\b", re.I
)
CONJUNCTION_END = re.compile(r"\b(?:и|или|а\s+также)\s*[,:—–-]?\s*$", re.I)

# Группы намеренно не содержат «ошибка/сбой/отказ»: это не синонимы.
# Стем + типичные глагольные окончания, без существительных «проверка», «создание».
VERB_SUFFIX = r"(?:уйте|ируйте|ить|ать|ять|овать|ировать|нуть|сти|йте|ите|ьте|й|ь|ил|ила|или|ал|ала|али|нул|нула|нули|ит|ят|ет|ют|ем|им|ишь|ешь|ю|у|ив|ав|ивший|авший)"
SYNONYM_GROUPS = [
    ("проверить", [r"провер(?:ить|ьте|ь|ил|ила|или|яет|яют|яйте|яй|яю|им|ив)", r"верифицир" + VERB_SUFFIX, r"валидир" + VERB_SUFFIX, r"убед(?:иться|итесь|ился|илась|ились)"]),
    ("запустить", [r"запуст(?:ить|ите|ил|ила|или|ят|им|ив)", r"старт" + VERB_SUFFIX, r"инициир" + VERB_SUFFIX]),
    ("удалить", [r"удал" + VERB_SUFFIX, r"стер(?:еть|еть|ли|ла|ите|т|ев)"]),
    ("показать", [r"показ(?:ать|ал|ала|али|ать|ыва[а-яё]*|ать|ав)", r"отобраз" + VERB_SUFFIX, r"вывед(?:и|ите|ен|ет|ут)|вывод" + VERB_SUFFIX]),
    ("создать", [r"созда(?:ть|йте|й|л|ла|ли|ют|ет|дим|в)", r"сгенерир" + VERB_SUFFIX]),
    ("изменить", [r"измен" + VERB_SUFFIX, r"модифицир" + VERB_SUFFIX, r"поправ" + VERB_SUFFIX]),
    ("отправить", [r"отправ" + VERB_SUFFIX, r"посла(?:ть|л|ла|ли|ть)|пошл(?:ите|и|ют)"]),
    ("подключить", [r"подключ" + VERB_SUFFIX, r"присоедин" + VERB_SUFFIX]),
    ("настроить", [r"настро(?:ить|йте|й|ил|ила|или|ят|ив)", r"сконфигурир" + VERB_SUFFIX]),
    ("завершить", [r"заверш" + VERB_SUFFIX, r"закон(?:чить|чите|чил|чила|чили|чат|чив)"]),
]
SYNONYM_PATTERNS = [
    [(name, re.compile(r"\b(?:" + variant + r")\b", re.I))
     for name, variant in zip(range(len(patterns)), patterns)]
    for _, patterns in SYNONYM_GROUPS
]

# Markdown: исходные позиции сохраняются при маскировании.
FENCE = re.compile(r"^ {0,3}(?P<mark>`{3,}|~{3,})(?:.*)$")
INLINE_CODE = re.compile(r"(`+)(?:[^`\n]|(?!\1)`)*?\1")
HTML_TAG = re.compile(r"</?[A-Za-z][^>]*>")
HTML_BLOCK_START = re.compile(r"^\s*<(pre|script|style|code)(?:\s|>)", re.I)
HEADING = re.compile(r"^\s{0,3}#{1,6}(?:\s+|$)")
HORIZONTAL = re.compile(r"^\s*(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$")
QUOTE_PREFIX = re.compile(r"^\s{0,3}(?:>\s*)+")
ALERT = re.compile(r"^\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*$", re.I)
LIST_ITEM = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*+]|[0-9]+[.)])(?P<space>\s+)")
TABLE_DIVIDER = re.compile(r"^:?-{3,}:?$")
SENTENCE_END = re.compile(r"[.!?][\"'”’»)*_\]]*$")
TOKEN_HAS_WORD = re.compile(r"[^\W_]", re.U)
ONE_INITIAL = re.compile(r"^[A-Za-zА-Яа-яЁё]\.$")
DOTTED_INITIALS = re.compile(r"^(?:[A-Za-zА-Яа-яЁё]\.){2,}$")
# Сокращения не являются границей предложения независимо от регистра следующего слова.
NON_TERMINAL_ABBREVIATIONS = {
    "т.е.", "т.д.", "т.п.", "т.н.", "т.о.", "т.к.",
    "др.", "пр.", "напр.", "рис.", "стр.", "табл.",
    "см.", "гг.", "вв.", "им.", "г.",
}


def _masked(text):
    """Замени код и HTML-теги пробелами той же длины."""
    text = INLINE_CODE.sub(lambda m: " " * len(m.group()), text)
    return HTML_TAG.sub(lambda m: " " * len(m.group()), text)


def _count_words(text):
    """Токен с буквой/цифрой считается словом (включая кириллицу и Ё)."""
    return sum(bool(TOKEN_HAS_WORD.search(token)) for token in text.split())


def _split_table_row(raw):
    """Верни (текст, начальная колонка) для ячеек Markdown."""
    start = len(raw) - len(raw.lstrip())
    content = raw[start:].rstrip()
    if "|" not in content:
        return None
    if content.startswith("|"):
        start += 1
        content = content[1:]
    if content.endswith("|") and not content.endswith(r"\|"):
        content = content[:-1]
    cells = []
    offset = 0
    for part in re.split(r"(?<!\\)\|", content):
        cells.append((part.strip(), start + offset + len(part) - len(part.lstrip())))
        offset += len(part) + 1
    return cells if len(cells) >= 2 else None


def _table_cells(lines):
    """Находи таблицы по разделителю, а не по одному символу | в прозе."""
    found = {}
    i = 1
    while i < len(lines):
        header = _split_table_row(lines[i - 1])
        separator = _split_table_row(lines[i])
        if not header or not separator or len(header) != len(separator) or not all(
            TABLE_DIVIDER.fullmatch(v) for v, _ in separator
        ):
            i += 1
            continue
        found[i - 1] = header
        found[i] = []
        i += 1
        while i < len(lines):
            cells = _split_table_row(lines[i])
            if not cells or len(cells) != len(separator):
                break
            found[i] = cells
            i += 1
    return found


def _is_closing_fence(mark, opened):
    return mark[0] == opened[0] and len(mark) >= len(opened)


def _sentence_ranges(text):
    """Индексы предложений, учитывающие русские сокращения и инициалы."""
    words = list(re.finditer(r"\S+", text))
    if not words:
        return []
    starts = [words[0].start()]
    for i, word in enumerate(words[:-1]):
        chunk = word.group().strip("\"'«“([*_")
        if not SENTENCE_END.search(chunk):
            continue
        core = re.sub(r"[\"'”’»)*_\]]+$", "", chunk).lower()
        next_word = words[i + 1].group().strip("\"'«“([*_")
        # Разделённые пробелом сокращения: «т. е.», «т. д.» и т.п.
        prev_core = words[i - 1].group().lower() if i else ""
        if core in NON_TERMINAL_ABBREVIATIONS:
            continue
        if core in {"т.", "е.", "д.", "п.", "н.", "о.", "к."} and (
            prev_core == "т." or next_word.lower() in {"е.", "д.", "п.", "н.", "о.", "к."}
        ):
            continue
        if ONE_INITIAL.fullmatch(core) or DOTTED_INITIALS.fullmatch(core):
            continue
        starts.append(words[i + 1].start())
    return [(a, b) for a, b in zip(starts, starts[1:] + [len(text)])]


def _make_finding(filename, line, col, rule, match, message, mode):
    level = ADVISORY if (rule == "synonym-rotation" and mode == "flavored") else RULE_LEVELS[rule]
    return {
        "file": filename,
        "line": line,
        "col": col,
        "rule": rule,
        "level": level,
        "match": match,
        "message": message,
    }


def _line_findings(masked, filename, lineno, base_col, mode, choices):
    """Лексические и локальные синтаксические правила, без разрыва колонок."""
    out = []
    rules = (
        ("semicolon", re.compile(r";"), "Замени точку с запятой точкой."),
        ("nominalization", NOMINALIZATION, "Замени канцелярскую связку одним смысловым глаголом."),
        ("marketing-adjective", MARKETING, "Удали рекламное слово или укажи известную измеримую характеристику."),
        ("slang-anglicism", SLANG, "Замени сленг точным русским глаголом."),
        ("passive-voice", PASSIVE, "Проверь, можно ли назвать исполнителя действия."),
        ("abbreviation", ABBREVIATIONS, "Раскрой сокращение, если это не точная метка."),
    )
    for name, regex, message in rules:
        for match in regex.finditer(masked):
            out.append(_make_finding(filename, lineno, base_col + match.start() + 1,
                                     name, match.group(), message, mode))
    # Первый выбранный вариант каждой группы закрепляется в документе.
    events = []
    for group_index, variants in enumerate(SYNONYM_PATTERNS):
        for alias_index, regex in variants:
            for match in regex.finditer(masked):
                events.append((match.start(), group_index, alias_index, match.group()))
    for start, group_index, alias_index, fragment in sorted(events):
        if group_index not in choices:
            choices[group_index] = alias_index
        elif choices[group_index] != alias_index:
            out.append(_make_finding(filename, lineno, base_col + start + 1,
                                     "synonym-rotation", fragment,
                                     f"Для одного действия уже выбран вариант группы «{SYNONYM_GROUPS[group_index][0]}»; проверь смысл и единообразие.", mode))
    return out


def _block_findings(pieces, filename, mode):
    """Проверяй предложения с переносами строк и возвращай исходные координаты."""
    if not pieces:
        return []
    offsets = []
    position = 0
    for _, _, part in pieces:
        offsets.append(position)
        position += len(part) + 1
    joined = " ".join(part for _, _, part in pieces)
    found = []
    for start, end in _sentence_ranges(joined):
        portion = joined[start:end].strip()
        if not portion:
            continue
        piece_index = min(bisect.bisect_right(offsets, start) - 1, len(pieces) - 1)
        piece_index = max(0, piece_index)
        lineno, source_col, _ = pieces[piece_index]
        column = source_col + max(0, start - offsets[piece_index]) + 1
        total = _count_words(portion)
        if total > MAX_DESCRIPTION_WORDS:
            found.append(_make_finding(filename, lineno, column, "long-sentence",
                                       f"{total} слов",
                                       f"В описании максимум {MAX_DESCRIPTION_WORDS} слова; в инструкции цель {MAX_INSTRUCTION_WORDS} слов (проверяется вручную).", mode))
        which = list(KOTORYJ.finditer(portion))
        if len(which) > 1:
            found.append(_make_finding(filename, lineno, column, "kotoryj-chain",
                                       f"{len(which)} × который",
                                       "Раздели несколько придаточных с «который».", mode))
        if IMPERATIVE.search(portion):
            for match in DEEPRI.finditer(portion):
                found.append(_make_finding(filename, lineno, column + match.start(),
                                           "participle-deepri", match.group(),
                                           "Деепричастие в инструкции может скрывать субъекта; раздели действия.", mode))
    return found


def _dangling_list_findings(lines, mode, filename):
    """Найди висячие союзы в последних строках элементов списка вне code fence."""
    out = []
    fence = None
    active = None  # (indent, last_line, last_col, last_text)

    def finalize():
        if active is None:
            return
        _, line_no, base_col, part = active
        masked = _masked(part)
        m = CONJUNCTION_END.search(masked)
        if m:
            out.append(_make_finding(filename, line_no, base_col + m.start() + 1,
                                     "dangling-conjunction", m.group().strip(),
                                     "Пункт списка заканчивается союзом; заверши мысль.", mode))

    for index, raw in enumerate(lines, 1):
        boundary = FENCE.match(raw)
        if boundary:
            if fence is None:
                finalize()
                active = None
                fence = boundary.group("mark")
            elif _is_closing_fence(boundary.group("mark"), fence):
                fence = None
            continue
        if fence is not None:
            continue
        new = LIST_ITEM.match(raw)
        if new:
            finalize()
            cut = new.end()
            active = (len(new.group("indent")), index, cut, raw[cut:])
            continue
        if active is None:
            continue
        if not raw.strip() or HEADING.match(raw) or QUOTE_PREFIX.match(raw):
            finalize()
            active = None
        elif (len(raw) - len(raw.lstrip())) > active[0]:
            active = (active[0], index, 0, raw)
        else:
            finalize()
            active = None
    finalize()
    return out


def lint(text, filename="<stdin>", mode="strict"):
    """Верни (список диагностик, число слов); только локальные детерминированные правила."""
    if mode not in {"strict", "flavored"}:
        raise ValueError("Неизвестный режим")
    lines = text.splitlines()
    tables = _table_cells(lines)
    findings = []
    choices = {}
    block = []
    words_total = 0
    fence = None
    html_block = None
    prev_kind = None

    def flush():
        findings.extend(_block_findings(block, filename, mode))
        block.clear()

    for lineno, raw in enumerate(lines, 1):
        marker = FENCE.match(raw)
        if marker:
            flush()
            if fence is None:
                fence = marker.group("mark")
            elif _is_closing_fence(marker.group("mark"), fence):
                fence = None
            prev_kind = None
            continue
        if fence is not None:
            continue
        if html_block:
            if re.search(r"</\s*" + html_block + r"\s*>", raw, re.I):
                html_block = None
            continue
        tag = HTML_BLOCK_START.match(raw)
        if tag:
            flush()
            if not re.search(r"</\s*" + tag.group(1) + r"\s*>", raw, re.I):
                html_block = tag.group(1)
            continue
        if lineno - 1 in tables:
            flush()
            for cell, column in tables[lineno - 1]:
                masked = _masked(cell)
                words_total += _count_words(masked)
                findings.extend(_line_findings(masked, filename, lineno, column, mode, choices))
                findings.extend(_block_findings([(lineno, column, masked)], filename, mode))
            prev_kind = None
            continue
        if not raw.strip() or HORIZONTAL.match(raw):
            flush()
            prev_kind = None
            continue
        prefix = QUOTE_PREFIX.match(raw)
        quote = bool(prefix)
        text_part = raw[prefix.end():] if quote else raw
        base_col = prefix.end() if quote else 0
        if ALERT.fullmatch(text_part.strip()):
            flush()
            prev_kind = None
            continue
        heading = HEADING.match(text_part)
        if heading:
            flush()
            base_col += heading.end()
            text_part = text_part[heading.end():]
        list_item = LIST_ITEM.match(text_part)
        kind = "quote" if quote else "prose"
        if list_item:
            flush()
            base_col += list_item.end()
            text_part = text_part[list_item.end():]
            kind = "list"
        elif heading:
            kind = "heading"
        # Граница между цитатой и обычной прозой, и между списком и прозой.
        if block and kind != prev_kind:
            flush()
        if raw.startswith("    ") and not list_item and not block:
            # Независимый отступ в 4 пробела трактуем как Markdown code block.
            continue
        masked = _masked(text_part)
        words_total += _count_words(masked)
        findings.extend(_line_findings(masked, filename, lineno, base_col, mode, choices))
        if masked.strip():
            block.append((lineno, base_col, masked))
        if heading or list_item:
            flush()  # отдельная единица структуры
            prev_kind = None
        else:
            prev_kind = kind
    flush()
    findings.extend(_dangling_list_findings(lines, mode, filename))
    findings.sort(key=lambda f: (f["line"], f["col"], f["rule"]))
    return findings, words_total


def _rule_list(value):
    values = {part.strip() for part in value.split(",") if part.strip()}
    unknown = values - set(RULE_LEVELS)
    if unknown or not values:
        raise argparse.ArgumentTypeError("Неизвестное правило в --disable: " + ", ".join(sorted(unknown or values)))
    return values


def _baseline(value):
    try:
        number = int(value)
        if number >= 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("--baseline ожидает неотрицательное целое")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Линтер Упрощённого технического русского")
    parser.add_argument("paths", metavar="FILE", nargs="*", help="Файлы или stdin без аргументов")
    parser.add_argument("--json", action="store_true", help="Диагностики в JSON")
    parser.add_argument("--baseline", type=_baseline, default=0, metavar="N", help="Допустимое число hard-нарушений")
    parser.add_argument("--disable", type=_rule_list, default=set(), metavar="IDS", help="Список отключаемых ID через запятую")
    parser.add_argument("--mode", choices=("strict", "flavored"), default="strict", help="Строгий или СТИ-стилизованный")
    parser.add_argument("--selftest", action="store_true", help="Выполнить встроенные проверки")
    args = parser.parse_intermixed_args(argv)
    if args.selftest:
        selftest()
        print("selftest: OK")
        return 0
    output = []
    words = 0
    if args.paths:
        for path in args.paths:
            try:
                content = Path(path).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                parser.error(f"Не удалось прочитать {path}: {error}")
            result, count = lint(content, path, args.mode)
            output.extend(result)
            words += count
    else:
        result, words = lint(sys.stdin.read(), mode=args.mode)
        output.extend(result)
    output = [item for item in output if item["rule"] not in args.disable]
    hard_count = sum(item["level"] == HARD for item in output)
    rate = round(len(output) * 100 / words, 1) if words else 0.0
    if args.json:
        print(json.dumps({"violations": output, "count": len(output),
                          "hard_count": hard_count, "baseline": args.baseline,
                          "words": words, "per_100_words": rate}, ensure_ascii=False, indent=2))
    else:
        for item in output:
            print(f"{item['file']}:{item['line']}:{item['col']} [{item['level']}] "
                  f"{item['rule']}: {item['message']} ({item['match']})")
        print(f"Найдено: {len(output)}. Жёстких: {hard_count}. Порог: {args.baseline}. "
              f"Слов: {words}. На 100 слов: {rate}.")
        print("Модальность и хеджи никогда не считаются нарушением.")
    return int(hard_count > args.baseline)


def selftest():
    """Каждое правило: обнаружение и отрицательный пример; Markdown и CLI."""
    cases = [
        ("semicolon", "Агент читает файл; затем сохраняет ответ.", "Агент читает файл. Затем сохраняет ответ."),
        ("nominalization", "Выполните проверку данных.", "Проверьте данные."),
        ("marketing-adjective", "Инновационный обработчик сохраняет ответ.", "Обработчик сохраняет ответ."),
        ("slang-anglicism", "Заскипайте шаг.", "Пропустите шаг."),
        ("synonym-rotation", "Проверьте файл. Затем валидируйте файл.", "Проверьте файл. Затем проверьте файл."),
        ("dangling-conjunction", "- Прочитайте файл и\n- Сохраните ответ", "- Прочитайте файл\n- Сохраните ответ"),
        ("kotoryj-chain", "Это файл, который создал агент, который завершил работу.", "Это файл агента. Агент завершил работу."),
        ("passive-voice", "Файл был удалён.", "Агент удалил файл."),
        ("participle-deepri", "Проверив файл, отправьте ответ.", "Проверьте файл. Затем отправьте ответ."),
        ("abbreviation", "См. файл, т.е. документацию.", "Смотрите файл, то есть документацию."),
    ]
    for rule, bad, good in cases:
        assert any(f["rule"] == rule for f in lint(bad)[0]), ("пропуск", rule, bad)
        assert not any(f["rule"] == rule for f in lint(good)[0]), ("ложный", rule, good)
    too_long = " ".join(f"слово{n}" for n in range(23)) + "."
    short = " ".join(f"слово{n}" for n in range(22)) + "."
    assert "long-sentence" in {f["rule"] for f in lint(too_long)[0]}
    assert "long-sentence" not in {f["rule"] for f in lint(short)[0]}
    assert _count_words("Ёж ежи ёлка 123 — !") == 4
    # Модальность и её градации должны сохраняться, не считаться нарушением.
    for hedge in ("Возможно, запрос не выполнился.", "Запрос мог не выполниться.",
                  "Вероятно, файл сохранился.", "Как правило, процесс завершается.",
                  "Похоже, соединение разорвано."):
        assert not lint(hedge)[0], ("хедж", hedge, lint(hedge)[0])
    assert len(_sentence_ranges("А. С. Пушкин указал т.е. форму. Ёж пришёл.")) == 2
    assert len(_sentence_ranges("Прочитайте рис. 3 и т.д. Затем откройте файл.")) == 1  # ограничение эвристики
    assert len(_sentence_ranges("iPhone доступен. Ёж виден.")) == 2
    assert not lint("```text\n- Заскипайте и\n```\n`заскипайте` .")[0]
    assert not lint("~~~\nАгент; слово\n~~~")[0]
    assert not lint("<pre>Инновационный;</pre>")[0]
    # Длина ячеек не складывается, а разделители таблицы пропускаются.
    cell = " ".join(f"элемент{n}" for n in range(20))
    tab = f"| Имя | Значение |\n| --- | --- |\n| {cell} | {cell} |"
    assert not any(f["rule"] == "long-sentence" for f in lint(tab)[0])
    assert lint(tab)[1] == 42, lint(tab)[1]
    # Столбец диагностики не меняется после кода в строке.
    f = [f for f in lint("Есть `один; два` элемент; конец.")[0] if f["rule"] == "semicolon"]
    assert len(f) == 1 and f[0]["col"] == 25, f
    assert not any(f["rule"] == "dangling-conjunction" for f in lint("- Прочитайте файл и\n  сохраните ответ.")[0])
    assert not any(f["rule"] == "marketing-adjective" for f in lint("Надёжный канал.")[0])
    assert not any(f["rule"] == "slang-anglicism" for f in lint("Откатите версию.")[0])
    assert not any(f["rule"] == "nominalization" for f in lint("Проведите линию.")[0])
    flavored = [f for f in lint("Проверьте файл. Затем валидируйте файл.", mode="flavored")[0]
                if f["rule"] == "synonym-rotation"]
    assert flavored and all(f["level"] == ADVISORY for f in flavored)
    # Граница разделов: GitHub alert, заголовок, цитата и код не сливаются с абзацем.
    m = "> [!NOTE]\n> Введите код.\n\n## Заголовок\nПрочитайте файл."
    assert not any(f["rule"] == "long-sentence" for f in lint(m)[0])
    # Точная карта нарушений русского edge-case Markdown: ни лишнего, ни пропущенного.
    fixture = Path(__file__).resolve().parent.parent / "examples" / "linter-edge-cases.md"
    if fixture.is_file():
        fixture_findings, fixture_words = lint(fixture.read_text(encoding="utf-8"))
        expected_by_line = {
            3: ["nominalization", "dangling-conjunction"],
            4: ["slang-anglicism", "dangling-conjunction"],
            8: ["passive-voice"],
            10: ["marketing-adjective", "semicolon", "abbreviation"],
            11: ["synonym-rotation"],
            12: ["kotoryj-chain"],
            13: ["participle-deepri"],
            17: ["nominalization"],
            26: ["abbreviation"],
            29: ["long-sentence"],
        }
        actual_by_line = {}
        for item in fixture_findings:
            actual_by_line.setdefault(item["line"], []).append(item["rule"])
        assert actual_by_line == expected_by_line, (actual_by_line, expected_by_line)
        assert fixture_words == 85, fixture_words
    # Проверка CLI, JSON, baseline и диагностики ошибок.
    with tempfile.TemporaryDirectory() as directory:
        first = Path(directory) / "first.md"
        second = Path(directory) / "second.md"
        first.write_text("Агент читает файл; затем сохраняет ответ.", encoding="utf-8")
        second.write_text("Агент читает файл.", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            assert main([str(first), "--json", str(second)]) == 1
        report = json.loads(buf.getvalue())
        assert report["hard_count"] == 1 and report["words"] >= 7, report
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["--baseline", "1", str(first)]) == 0
            assert main(["--disable", "semicolon", str(first)]) == 0
        for args in (["--basline", "1"], ["--baseline", "-1"],
                     ["--disable", "not-a-rule"], ["--mode", "wrong"]):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    main(args)
                except SystemExit as exc:
                    assert exc.code == 2, (args, exc.code)
                else:
                    raise AssertionError(args)
    return True


if __name__ == "__main__":
    sys.exit(main())
