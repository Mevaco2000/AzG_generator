"""Generator Zadań w Streamlit - samodzielna replika strony generator.html projektu Ruda Wiedzy.

Jeden plik bez innych plików projektu: interfejs i eksport odtwarzają script.js, export_utils.js i task_groups.js,
a generatory zadań (DEFINITION i generate() z backend/tasks wraz z potrzebnymi funkcjami z backend/common.py
i backend/region_plot.py) są skopiowane na końcu pliku.

Wymagania:   pip install streamlit sympy   (opcjonalnie cairosvg - rysunki do pobrania jako PNG)
Uruchomienie: streamlit run generator_streamlit.py
Adres ?zadanie=ID wybiera zadanie od razu, jak na stronie.
"""

import base64
import io
import math
import random
import re
import zipfile
from typing import Any, Callable

import streamlit as st
import sympy as sp

try:
    import cairosvg  # opcjonalny - tylko do eksportu rysunków jako PNG
except (ImportError, OSError):  # OSError: pakiet jest, ale brak biblioteki Cairo w systemie
    cairosvg = None

# Kolejność działów na liście zadań (task_groups.js); działy spoza listy trafiają na koniec.
AREA_ORDER = [
    "Liczby zespolone",
    "Macierze",
    "Układy równań",
    "Przestrzenie liniowe",
    "Wartości własne i diagonalizacja",
    "Rozkłady macierzy",
]
FALLBACK_AREA = "Inne"

TEX_PREVIEW_MODES = {"full": "Zadania + rozwiązania", "tasks": "Tylko zadania", "solutions": "Tylko rozwiązania"}
TEX_EXPORTS = {
    "full": {"filename": "ruda-wiedzy-przyklady.tex", "title": "Podgląd .tex: zadania + rozwiązania"},
    "tasks": {"filename": "ruda-wiedzy-zadania.tex", "title": "Podgląd .tex: tylko zadania"},
    "solutions": {"filename": "ruda-wiedzy-rozwiazania.tex", "title": "Podgląd .tex: tylko rozwiązania"},
}
TEX_TITLES = {
    "full": "Ruda Wiedzy -- wygenerowane przykłady",
    "tasks": "Ruda Wiedzy -- zadania",
    "solutions": "Ruda Wiedzy -- rozwiązania",
}
TEX_LAYOUTS = {
    "enumerate": "Lista numerowana (\\begin{enumerate}, każdy przykład jako \\item)",
    "enumerate-inline": "Lista numerowana, wzór w linii (\\item $ równanie $)",
    "sections": "Osobne sekcje dla każdego przykładu",
}


# --- LaTeX i tekst (export_utils.js) ---

def escape_latex(text: str) -> str:
    text = str(text).replace("\\", "\\textbackslash{}")
    text = re.sub(r"([#$%&_{}])", r"\\\1", text)
    return text.replace("^", "\\textasciicircum{}").replace("~", "\\textasciitilde{}")


def latex_text(text: str) -> str:
    return "\\text{" + escape_latex(text).replace("\n", " \\\\ ") + "}"


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|$~<>:])")


def markdown_text(text: str) -> str:
    """Zwykły tekst do st.markdown - bez interpretowania znaków Markdown i wzorów w $...$."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", str(text))


MATH_BREAK_BEFORE = {"xrightarrow", "sim", "Rightarrow", "iff", "qquad", "wedge", "text"}
MATH_MAX_WIDTH = 52

_MATRIX_ENV = re.compile(r"\\begin\{([pbv]?matrix)\}([\s\S]*?)\\end\{\1\}")
_FRAC = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
_XRIGHTARROW = re.compile(r"\\xrightarrow\{([^{}]*)\}")
_INVISIBLE_COMMANDS = re.compile(r"\\(?:left|right|text|operatorname|mathbb|displaystyle)(?![a-zA-Z])")
_TEXT_COMMAND = re.compile(r"\\text\{([^{}]*)\}")


def estimate_math_width(tex: str) -> int:
    def matrix_width(match: re.Match[str]) -> str:
        widest = max(
            sum(estimate_math_width(cell) + 1 for cell in row.split("&")) for row in match.group(2).split("\\\\")
        )
        return "x" * (math.ceil(widest) + 2)

    s = _MATRIX_ENV.sub(matrix_width, tex)
    s = _FRAC.sub(lambda m: "x" * max(len(m.group(1)), len(m.group(2))), s)
    s = _XRIGHTARROW.sub(lambda m: "x" * max(4, estimate_math_width(m.group(1))), s)
    s = _INVISIBLE_COMMANDS.sub("", s)
    s = re.sub(r"\\[a-zA-Z]+", "x", s)
    s = re.sub(r"\\.", "x", s)
    s = re.sub(r"[{}_^]", "", s)
    return len(s)


def split_long_math_text(tex: str) -> str:
    def split(match: re.Match[str]) -> str:
        body = match.group(1)
        if len(body) < 20:
            return match.group(0)
        return "".join(f"\\text{{{chunk}}}" for chunk in re.findall(r"\s*\S+\s*", body) or [body])

    return _TEXT_COMMAND.sub(split, tex)


def _is_letter(ch: str) -> bool:
    return "a" <= ch <= "z" or "A" <= ch <= "Z"


def split_math_atoms(tex: str) -> list[str]:
    atoms: list[str] = []
    depth = 0
    start = 0

    def cut(pos: int) -> None:
        nonlocal start
        if pos > start:
            atoms.append(tex[start:pos])
            start = pos

    i = 0
    while i < len(tex):
        ch = tex[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if ch != "\\":
            i += 1
            continue
        j = i + 1
        if j < len(tex) and _is_letter(tex[j]):
            while j < len(tex) and _is_letter(tex[j]):
                j += 1
        else:
            j += 1
        name = tex[i + 1 : j]
        if name in ("left", "begin"):
            depth += 1
        elif name in ("right", "end"):
            depth -= 1
        elif depth == 0 and name in MATH_BREAK_BEFORE:
            cut(i)
        elif depth == 0 and name == " " and i > 0 and tex[i - 1] in ",;":
            cut(j)
        i = j
    atoms.append(tex[start:])
    return atoms


def wrap_math(tex: str) -> str:
    """Łamie długi wzór na wiersze środowiska aligned (szerokość szacowana jak w export_utils.js)."""
    lines: list[str] = []
    current = ""
    width = 0
    for atom in split_math_atoms(split_long_math_text(tex)):
        atom_width = estimate_math_width(atom)
        if current and width + atom_width > MATH_MAX_WIDTH:
            lines.append(current)
            current = atom
            width = atom_width
        else:
            current += atom
            width += atom_width
    if current:
        lines.append(current)
    if len(lines) <= 1:
        return tex
    body = " \\\\\n".join(f"&{'{}' if index else ''}{line.lstrip()}" for index, line in enumerate(lines))
    return f"\\begin{{aligned}}\n{body}\n\\end{{aligned}}"


def data_uri_bytes(data_uri: str) -> bytes:
    return base64.b64decode(data_uri[data_uri.index(",") + 1 :])


def svg_to_png_bytes(svg: bytes) -> bytes:
    return cairosvg.svg2png(bytestring=svg, scale=2, background_color="#ffffff")


# --- Zadania (task_groups.js; generatory: TASKS na końcu pliku) ---

def task_definitions() -> list[dict[str, Any]]:
    return [definition for definition, _ in TASKS.values()]


def selected_definition() -> dict[str, Any] | None:
    definitions = task_definitions()
    selected_id = st.session_state.get("selected_task_id")
    return next((item for item in definitions if item["id"] == selected_id), definitions[0] if definitions else None)


def area_rank(area: str) -> int:
    return AREA_ORDER.index(area) if area in AREA_ORDER else len(AREA_ORDER)


def group_by_area(definitions: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for definition in definitions:
        groups.setdefault(definition.get("area") or FALLBACK_AREA, []).append(definition)
    return sorted(groups.items(), key=lambda group: area_rank(group[0]))


def task_tooltip(definition: dict[str, Any]) -> str:
    return (
        definition.get("description") or definition.get("examInstruction") or definition.get("example") or definition["name"]
    )


def normalized_config(definition: dict[str, Any], raw_config: dict[str, Any] | None) -> dict[str, Any]:
    """Liczby całkowite przycięte do zakresów parametrów - jak normalized_config w backend/app.py."""
    config = dict(raw_config or {})
    for param in definition["params"]:
        key = param["key"]
        value = config.get(key, param["value"])
        try:
            value = int(value)
        except Exception:
            value = param["value"]
        config[key] = max(param["min"], min(param["max"], value))
    return config


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def task_param_key(definition: dict[str, Any], param: dict[str, Any]) -> str:
    return f"task-param-{definition['id']}-{param['key']}"


def example_param_key(example_id: int, param: dict[str, Any]) -> str:
    return f"example-{example_id}-param-{param['key']}"


def task_config(definition: dict[str, Any]) -> dict[str, Any]:
    config = {}
    for param in definition["params"]:
        value = st.session_state.get(task_param_key(definition, param), param["value"])
        config[param["key"]] = clamp(int(value), param["min"], param["max"])
    return config


def format_config(definition: dict[str, Any], config: dict[str, Any]) -> str:
    return ", ".join(f"{param['label']}: {config.get(param['key'])}" for param in definition["params"])


def create_example(definition: dict[str, Any], config: dict[str, Any], existing_id: int | None = None) -> dict[str, Any]:
    used_config = normalized_config(definition, config)
    data = TASKS[definition["id"]][1](used_config)
    if existing_id is None:
        existing_id = st.session_state.next_example_id
        st.session_state.next_example_id += 1
    return {
        "id": existing_id,
        "definition": definition,
        "config": used_config,
        "task": data.get("task") or "",
        "solution": data.get("solution") or "",
        "taskLatex": data.get("taskLatex") or "",
        "solutionLatex": data.get("solutionLatex") or "",
        "solutionImage": data.get("solutionImage") or "",
    }


def sync_example_inputs(items: list[dict[str, Any]]) -> None:
    """Pola "Ustawienia tego przykładu" pokazują ustawienia, z którymi przykład faktycznie wygenerowano."""
    for item in items:
        for param in item["definition"]["params"]:
            st.session_state[example_param_key(item["id"], param)] = item["config"][param["key"]]


# --- Eksport (script.js) ---

def build_export_text(tasks: list[dict[str, Any]], definition: dict[str, Any]) -> str:
    if not tasks:
        return ""
    lines = [f"Ruda Wiedzy - przykłady: {definition['name']}", "=======================================", ""]
    for index, item in enumerate(tasks, 1):
        lines.append(f"{index}. {item['task']}")
        lines.append(f"   Rozwiązanie: {item['solution']}")
        lines.append("")
    return "\n".join(lines)


def build_export_tex(tasks: list[dict[str, Any]], definition: dict[str, Any], mode: str = "full", layout: str = "enumerate") -> str:
    if not tasks:
        return ""
    include_tasks = mode != "solutions"
    include_solutions = mode != "tasks"
    label_parts = include_tasks and include_solutions
    lines = [
        "\\documentclass[12pt,a4paper]{article}",
        "\\usepackage[T1]{fontenc}",
        "\\usepackage[utf8]{inputenc}",
        "\\usepackage[polish]{babel}",
        "\\usepackage{amsmath,amssymb}",
        "\\usepackage{geometry}",
        "\\geometry{margin=2.5cm}",
        f"\\title{{{TEX_TITLES.get(mode, TEX_TITLES['full'])}}}",
        f"\\author{{Typ zadania: {escape_latex(definition['name'])}}}",
        "\\date{}",
        "\\begin{document}",
        "\\maketitle",
        "",
    ]

    inline = layout == "enumerate-inline"
    use_enumerate = layout == "enumerate" or inline
    if use_enumerate:
        lines.append("\\begin{enumerate}")

    for index, item in enumerate(tasks, 1):
        task_tex = wrap_math(item["taskLatex"]) if item["taskLatex"] else ""
        solution_tex = wrap_math(item["solutionLatex"]) if item["solutionLatex"] else ""
        task_plain = escape_latex(item["task"]).replace("\n", " \\\\ ")
        solution_plain = escape_latex(item["solution"]).replace("\n", " \\\\ ")
        indent = "  " if use_enumerate else ""

        if inline:
            parts = []
            if include_tasks:
                parts.append(f"$ {task_tex} $" if task_tex else task_plain)
            if include_solutions:
                label = "\\textbf{Rozwiązanie:} " if label_parts else ""
                solution_part = label + (f"$ {solution_tex} $" if solution_tex else solution_plain)
                parts.append(f"\\\\ {solution_part}" if include_tasks else solution_part)
            lines.append(f"  \\item {parts[0]}")
            lines.extend(f"  {part}" for part in parts[1:])
            lines.append("")
            continue

        lines.append("  \\item" if use_enumerate else f"\\section*{{Przykład {index}}}")
        if include_tasks:
            if label_parts:
                lines.append(f"{indent}\\textbf{{Treść zadania}}")
            if task_tex:
                lines.extend([f"{indent}\\[", f"{indent}{task_tex}", f"{indent}\\]"])
            else:
                lines.extend([f"{indent}{task_plain}", ""])
        if include_solutions:
            if label_parts:
                lines.append(f"{indent}\\textbf{{Rozwiązanie}}")
            if solution_tex:
                lines.extend([f"{indent}\\[", f"{indent}{solution_tex}", f"{indent}\\]"])
            else:
                lines.extend([f"{indent}{solution_plain}", ""])
        lines.append("")

    if use_enumerate:
        lines.append("\\end{enumerate}")
    lines.append("\\end{document}")
    return "\n".join(lines)


def build_images_zip(tasks: list[dict[str, Any]], image_format: str) -> tuple[bytes, int]:
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for number, item in enumerate(tasks, 1):
            if not item["solutionImage"]:
                continue
            data = data_uri_bytes(item["solutionImage"])
            if image_format == "png":
                data = svg_to_png_bytes(data)
            archive.writestr(f"{number}.{image_format}", data)
            count += 1
    return buffer.getvalue(), count


# --- Akcje (wywoływane przed ponownym przebiegiem skryptu) ---

def set_status(message: str) -> None:
    st.session_state.status = message


def on_select_task(task_id: int) -> None:
    st.session_state.selected_task_id = task_id


def on_generate() -> None:
    state = st.session_state
    state.results_shown = True
    definition = selected_definition()
    if definition is None:
        set_status("Brak wybranego zadania.")
        return
    examples = int(state.examples_count)
    config = task_config(definition)
    try:
        tasks = [create_example(definition, config) for _ in range(examples)]
    except Exception as exc:
        set_status(f"Błąd generowania: {exc}")
        return
    sync_example_inputs(tasks)
    state.tasks = tasks
    state.solutions_visible = bool(state.show_solutions)
    set_status(f"Wygenerowano {examples} przykładów dla: {definition['name']}.")
    state.params_info = f"Użyte ustawienia: {format_config(definition, config)}."


def on_regenerate_example(example_id: int) -> None:
    state = st.session_state
    index = next((i for i, item in enumerate(state.tasks) if item["id"] == example_id), -1)
    if index < 0:
        return
    base = state.tasks[index]
    config = dict(base["config"])
    for param in base["definition"]["params"]:
        value = state.get(example_param_key(example_id, param))
        if isinstance(value, (int, float)):
            config[param["key"]] = clamp(int(value), param["min"], param["max"])
    try:
        item = create_example(base["definition"], config, base["id"])
    except Exception as exc:
        set_status(f"Błąd przeliczania: {exc}")
        return
    sync_example_inputs([item])
    state.tasks[index] = item
    set_status(f"Przeliczono przykład #{index + 1}.")


def on_regenerate_all() -> None:
    state = st.session_state
    if not state.tasks:
        set_status("Najpierw wygeneruj przykłady.")
        return
    definition = selected_definition()
    if definition is None:
        set_status("Brak wybranego zadania.")
        return
    base_config = task_config(definition)
    try:
        tasks = [create_example(item["definition"], base_config, item["id"]) for item in state.tasks]
    except Exception as exc:
        set_status(f"Błąd przeliczania: {exc}")
        return
    sync_example_inputs(tasks)
    state.tasks = tasks
    state.params_info = f"Użyte ustawienia: {format_config(definition, base_config)}."
    set_status("Przeliczono wszystkie przykłady wg bieżących ustawień.")


def on_toggle_solutions() -> None:
    state = st.session_state
    if not state.tasks:
        set_status("Najpierw wygeneruj przykłady.")
        return
    state.solutions_visible = not state.solutions_visible


def on_open_tex_preview() -> None:
    state = st.session_state
    if not state.tasks:
        set_status("Najpierw wygeneruj przykłady.")
        return
    state.tex_mode = state.tex_preview_mode


def on_close_tex_preview() -> None:
    st.session_state.tex_mode = None


# --- Widok ---

def init_state(definitions: list[dict[str, Any]]) -> None:
    state = st.session_state
    state.setdefault("tasks", [])
    state.setdefault("solutions_visible", False)
    state.setdefault("next_example_id", 1)
    state.setdefault("status", "Wygeneruj pierwszy zestaw zadań.")
    state.setdefault("params_info", "")
    state.setdefault("results_shown", False)
    state.setdefault("tex_mode", None)

    # Zadanie dotychczasowe, z adresu ?zadanie=ID (np. z lekcji kursu) albo pierwsze.
    known = {item["id"] for item in definitions}
    if state.get("selected_task_id") not in known:
        try:
            requested = int(st.query_params.get("zadanie", ""))
        except ValueError:
            requested = None
        state.selected_task_id = requested if requested in known else definitions[0]["id"]


def render_task_picker(definitions: list[dict[str, Any]], selected: dict[str, Any]) -> None:
    query = st.text_input(
        "Szukaj zadania",
        placeholder="np. Choleskiego, Gaussa, Jordan...",
        help="Filtruje listę zadań po nazwie, dziale lub przykładzie",
        key="available_search",
    ).strip().lower()
    filtered = [
        item for item in definitions if not query or query in f"{item['name']} {item['area']} {item['example']}".lower()
    ]
    if not filtered:
        st.caption("Brak dopasowanych zadań.")

    for area, items in group_by_area(filtered):
        with st.expander(f"{area} ({len(items)})", expanded=bool(query)):
            columns = st.columns(2)
            for position, item in enumerate(items):
                with columns[position % 2]:
                    st.button(
                        item["name"],
                        key=f"pick-{item['id']}",
                        type="primary" if item["id"] == selected["id"] else "secondary",
                        help=task_tooltip(item),
                        width="stretch",
                        on_click=on_select_task,
                        args=(item["id"],),
                    )
                    st.caption(item["example"])

    st.info(f"{selected['name']}. {selected['example']}")


def render_settings(selected: dict[str, Any]) -> None:
    state = st.session_state
    st.subheader("Ustawienia generowanych przykładów")
    st.number_input(
        "Liczba przykładów",
        min_value=1,
        max_value=20,
        value=3,
        step=1,
        key="examples_count",
        help="Ile przykładów wygenerować naraz (1–20)",
    )

    with st.expander("Ustawienia dla wszystkich przykładów"):
        st.caption(selected["parameterHelp"])
        columns = st.columns(2)
        for position, param in enumerate(selected["params"]):
            columns[position % 2].number_input(
                param["label"],
                min_value=int(param["min"]),
                max_value=int(param["max"]),
                value=int(param["value"]),
                step=1,
                key=task_param_key(selected, param),
            )

    st.checkbox(
        "Pokaż wskazówki/rozwiązania od razu",
        key="show_solutions",
        help="Zaznacz, aby rozwiązania były widoczne od razu po wygenerowaniu (można je też przełączać później)",
    )

    # Akcje na wynikach są widoczne dopiero po pierwszym generowaniu.
    if state.results_shown:
        st.markdown("**Podgląd .tex**")
        mode_column, button_column = st.columns([3, 2], vertical_alignment="bottom")
        mode_column.selectbox(
            "Zakres podglądu TEX",
            options=list(TEX_PREVIEW_MODES),
            format_func=TEX_PREVIEW_MODES.get,
            key="tex_preview_mode",
            label_visibility="collapsed",
            help="Zakres podglądu .tex: zadania z rozwiązaniami, same zadania albo same rozwiązania",
        )
        button_column.button("Podgląd TEX", on_click=on_open_tex_preview, width="stretch")

    generate_column, download_column, images_column = st.columns(3, vertical_alignment="bottom")
    generate_column.button("Generuj przykłady", type="primary", on_click=on_generate, width="stretch")
    if state.results_shown:
        download_column.download_button(
            "Pobierz TXT",
            data=build_export_text(state.tasks, selected) or "",
            file_name="ruda-wiedzy-przyklady.txt",
            mime="text/plain",
            width="stretch",
        )
    if any(item["solutionImage"] for item in state.tasks):
        with images_column:
            formats = ["svg", "png"] if cairosvg else ["svg"]
            image_format = st.selectbox(
                "Format obrazów",
                options=formats,
                format_func=str.upper,
                key="image_export_format",
                help="Format rysunków w pobieranym archiwum ZIP: SVG (wektorowy) lub PNG"
                + ("" if cairosvg else " (PNG wymaga pakietu cairosvg)"),
            )
            archive, count = build_images_zip(state.tasks, image_format)
            st.download_button(
                "Pobierz obrazy (ZIP)",
                data=archive,
                file_name="ruda-wiedzy-obrazy.zip",
                mime="application/zip",
                on_click=set_status,
                args=(f"Pobrano {count} obraz(ów) jako {image_format.upper()}.",),
                width="stretch",
            )


def render_math(latex: str, text: str, wrap: bool = True) -> None:
    """Wzór z LaTeX-a, a gdy generator go nie podał - zwykły tekst."""
    if latex:
        st.latex(wrap_math(latex) if wrap else latex)
    elif text:
        st.markdown(markdown_text(text))


def render_solution_image(data_uri: str) -> None:
    data = data_uri_bytes(data_uri)
    st.image(data.decode("utf-8") if data_uri.startswith("data:image/svg") else data, caption="Wykres rozwiązania")


def render_tex_panel(tasks: list[dict[str, Any]], selected: dict[str, Any]) -> None:
    mode = st.session_state.tex_mode
    if not mode or not tasks:
        return
    with st.container(border=True):
        st.subheader(TEX_EXPORTS[mode]["title"])
        layout = st.selectbox(
            "Układ dokumentu",
            options=list(TEX_LAYOUTS),
            format_func=TEX_LAYOUTS.get,
            key="tex_export_format",
            help="Sposób ułożenia przykładów w dokumencie LaTeX",
        )
        source = build_export_tex(tasks, selected, mode, layout)

        include_tasks = mode != "solutions"
        include_solutions = mode != "tasks"
        label_parts = include_tasks and include_solutions
        inline = layout == "enumerate-inline"
        for index, item in enumerate(tasks, 1):
            st.markdown(f"#### Przykład {index}" if layout == "sections" else f"**{index}.**")
            task_latex = item["taskLatex"] or latex_text(item["task"])
            solution_latex = item["solutionLatex"] or latex_text(item["solution"])
            if inline:
                parts = []
                if include_tasks:
                    parts.append(f"${task_latex}$")
                if include_solutions:
                    parts.append(("**Rozwiązanie:** " if label_parts else "") + f"${solution_latex}$")
                st.markdown("  \n".join(parts))
                continue
            if include_tasks:
                if label_parts:
                    st.markdown("**Treść zadania**")
                render_math(item["taskLatex"], item["task"], wrap=False)
            if include_solutions:
                if label_parts:
                    st.markdown("**Rozwiązanie**")
                render_math(item["solutionLatex"], item["solution"], wrap=False)

        with st.expander("Kod źródłowy .tex"):
            st.code(source, language="latex")
        download_column, close_column, _ = st.columns(3)
        filename = TEX_EXPORTS[mode]["filename"]
        download_column.download_button(
            "Pobierz .tex",
            data=source,
            file_name=filename,
            mime="application/x-tex",
            type="primary",
            on_click=set_status,
            args=(f"Pobrano plik {filename}.",),
            width="stretch",
        )
        close_column.button("Zamknij podgląd", on_click=on_close_tex_preview, width="stretch")


def render_examples(tasks: list[dict[str, Any]], solutions_visible: bool) -> None:
    if not tasks:
        st.write("Brak wygenerowanych przykładów.")
        return
    for index, item in enumerate(tasks, 1):
        definition = item["definition"]
        with st.container(border=True):
            st.markdown(f"**Przykład {index}**")
            with st.expander("Ustawienia tego przykładu"):
                if definition["params"]:
                    columns = st.columns(min(3, len(definition["params"])))
                    for position, param in enumerate(definition["params"]):
                        columns[position % len(columns)].number_input(
                            param["label"],
                            min_value=int(param["min"]),
                            max_value=int(param["max"]),
                            step=1,
                            key=example_param_key(item["id"], param),
                        )
                st.button(
                    "Przelicz ten przykład",
                    key=f"regenerate-{item['id']}",
                    on_click=on_regenerate_example,
                    args=(item["id"],),
                )

            render_math(item["taskLatex"], item["task"])
            with st.expander("Podgląd .tex", expanded=True):
                st.code(f"\\[\n{item['taskLatex'] or latex_text(item['task'])}\n\\]", language="latex")
            st.caption(f"{definition['name']} • {definition['area']}", help=task_tooltip(definition))

            if solutions_visible:
                st.markdown("**Rozwiązanie:**")
                render_math(item["solutionLatex"], item["solution"])
                if item["solutionLatex"] and item["solution"]:
                    st.markdown(markdown_text(item["solution"]))
                if item["solutionImage"]:
                    render_solution_image(item["solutionImage"])


def render_results(selected: dict[str, Any]) -> None:
    state = st.session_state
    title_column, toggle_column = st.columns([3, 1], vertical_alignment="bottom")
    title_column.subheader("Wynik")
    toggle_column.button("Pokaż/Ukryj rozwiązania", on_click=on_toggle_solutions, width="stretch")
    (st.error if state.status.startswith("Błąd") else st.info)(state.status)
    if state.params_info:
        st.caption(state.params_info)
    st.button("Przelicz wszystkie wg ustawień", on_click=on_regenerate_all)
    render_tex_panel(state.tasks, selected)
    render_examples(state.tasks, state.solutions_visible)


def main() -> None:
    st.set_page_config(page_title="Ruda Wiedzy - Generator Zadań", page_icon="🧮", layout="wide")
    st.title("Generator Zadań")

    definitions = task_definitions()
    init_state(definitions)
    selected = selected_definition()

    picker_column, settings_column = st.columns([3, 2], gap="large")
    with picker_column:
        render_task_picker(definitions, selected)
    with settings_column:
        render_settings(selected)

    if st.session_state.results_shown:
        st.divider()
        render_results(selected)

    st.divider()
    st.caption("**Ruda Wiedzy** - Wykuwamy bez kucia.")


# =====================================================================================================================
# Generatory zadań skopiowane z backend/ projektu Ruda Wiedzy: DEFINITION i generate() każdego zadania oraz funkcje,
# z których korzystają. Pominięte są challenge() i calculate() (Nauka własna, Kalkulator) - ta strona ich nie używa.
# =====================================================================================================================

# --- Funkcje wspólne generatorów (backend/common.py) ---

VARS = [sp.Symbol(name) for name in ("x", "y", "z", "u", "v", "w", "t", "p", "q")]
Z = sp.Symbol("z")


def mat_text(matrix: sp.Matrix) -> str:
    rows = [[sp.simplify(v) for v in row] for row in matrix.tolist()]
    return "[" + ", ".join("[" + ", ".join(str(v) for v in row) + "]" for row in rows) + "]"


def vec_text(vec: sp.Matrix) -> str:
    row = [sp.simplify(v) for v in list(vec)]
    return "(" + ", ".join(str(v) for v in row) + ")"


def mat_latex(matrix: sp.Matrix) -> str:
    return sp.latex(matrix)


def vec_latex(vec: sp.Matrix) -> str:
    return sp.latex(vec)


def bounds(config: dict[str, Any], fallback: int = 8) -> tuple[int, int]:
    low = int(config.get("coeffLower", -fallback))
    high = int(config.get("coeffUpper", fallback))
    if low > high:
        low, high = high, low
    if low == high:
        high = low + 1
    return low, high


def rint(config: dict[str, Any], key: str, default: int) -> int:
    value = config.get(key, default)
    try:
        return int(value)
    except Exception:
        return default


def rand_non_zero(low: int, high: int) -> int:
    while True:
        value = random.randint(low, high)
        if value != 0:
            return value


def generate_system(n: int, low: int, high: int) -> tuple[sp.Matrix, sp.Matrix, sp.Matrix]:
    while True:
        A = sp.Matrix([[random.randint(low, high) for _ in range(n)] for _ in range(n)])
        if A.det() != 0:
            break
    x = sp.Matrix([random.randint(low, high) for _ in range(n)])
    b = A * x
    return A, b, x


def imag_term(b: int) -> str:
    """Wyraz urojony ze znakiem, bez zbędnej jedynki: +i, -i, +3i, -2i."""
    return "+i" if b == 1 else "-i" if b == -1 else f"{b:+d}i"


def complex_str(a: int, b: int) -> str:
    """Zapis a+bi bez zerowych części (2i zamiast 0+2i, 2 zamiast 2-0i)."""
    if b == 0:
        return str(a)
    if a == 0:
        return imag_term(b).lstrip("+")
    return f"{a}{imag_term(b)}"


def z_minus_center(cx: int, cy: int) -> str:
    """Zapis z-(cx+cy i); dla środka w zerze po prostu z."""
    if cx == 0 and cy == 0:
        return "z"
    if cx and cy:
        return f"z-({complex_str(cx, cy)})"
    value = cx or cy
    unit = str(abs(value)) if cy == 0 else imag_term(abs(value)).lstrip("+")
    return f"z-{unit}" if value > 0 else f"z+{unit}"


def complex_text(value: sp.Expr) -> str:
    return sp.sstr(value).replace("*I", "i").replace("I", "i")


def complex_vec_text(vec: sp.Matrix) -> str:
    return "(" + ", ".join(complex_text(v) for v in vec) + ")"


def clear_denominators(vec: sp.Matrix) -> sp.Matrix:
    denominators = [sp.denom(part) for v in vec for part in (sp.re(v), sp.im(v))]
    scaled = vec * sp.ilcm(*denominators)
    first = next(v for v in scaled if v != 0)
    return -scaled if sp.re(first) < 0 or (sp.re(first) == 0 and sp.im(first) < 0) else scaled


def pmatrix_latex(vec: sp.Matrix) -> str:
    entries = (complex_text(v).replace(" ", "") if v.has(sp.I) else sp.latex(v) for v in vec)
    return "\\begin{pmatrix}" + "\\\\".join(entries) + "\\end{pmatrix}"


def is_gaussian_integer(value: sp.Expr) -> bool:
    value = sp.expand(value)
    return sp.denom(sp.re(value)) == 1 and sp.denom(sp.im(value)) == 1


def entry_latex(value: sp.Expr) -> str:
    if value.has(sp.I) and is_gaussian_integer(value):
        return complex_text(value).replace(" ", "")
    return sp.latex(value)


def rows_matrix_latex(rows: list[sp.Matrix]) -> str:
    body = "\\\\".join("&".join(entry_latex(v) for v in row) for row in rows)
    return "\\begin{pmatrix}" + body + "\\end{pmatrix}"


def row_combination(terms: list[tuple[sp.Expr, str]], latex: bool) -> str:
    out = ""
    for coeff, name in terms:
        re_part, im_part = sp.re(coeff), sp.im(coeff)
        negative = re_part < 0 or (re_part == 0 and im_part < 0)
        mag = -coeff if negative else coeff
        sign = "-" if negative else "+"
        if mag == 1:
            body = name
        elif mag.is_Integer or mag.is_Rational:
            if latex:
                body = f"{sp.latex(mag)}{name}"
            else:
                body = f"{mag}{name}" if mag.is_Integer else f"({mag}){name}"
        elif sp.re(mag) == 0 and sp.im(mag).is_Integer:
            imag = "" if sp.im(mag) == 1 else str(sp.im(mag))
            body = f"{imag}i{name}" if latex else f"{imag}i*{name}"
        else:
            text = complex_text(mag).replace(" ", "")
            body = f"\\left({text}\\right){name}" if latex else f"({text}){name}"
        out += (("-" if sign == "-" else "") + body) if not out else f" {sign} {body}"
    return out


def step(label: str, latex: str) -> dict[str, str]:
    """Krok rozwiązania pokazywany w pliku z rozwiązaniami egzaminu (opis + wzór w LaTeX)."""
    return {"label": label, "latex": latex}


def matrix_latex(matrix: sp.Matrix, split: int | None = None) -> str:
    """Macierz w LaTeX; split = liczba kolumn przed kreską pionową (macierz rozszerzona [A|b])."""
    body = "\\\\".join("&".join(entry_latex(matrix[r, c]) for c in range(matrix.cols)) for r in range(matrix.rows))
    if split is None:
        return "\\begin{pmatrix}" + body + "\\end{pmatrix}"
    columns = "c" * split + "|" + "c" * (matrix.cols - split)
    return "\\left(\\begin{array}{" + columns + "}" + body + "\\end{array}\\right)"


def elimination_steps(
    matrix: sp.Matrix,
    reduced: bool = False,
    split: int | None = None,
    pivot_cols: int | None = None,
    fraction_free: bool = False,
) -> tuple[sp.Matrix, list[dict[str, str]], int]:
    """Eliminacja Gaussa na wierszach z zapisem kolejnych kroków.

    Zwraca (macierz końcowa, kroki, liczba zamian wierszy). Bez reduced: postać schodkowa (operacje
    w_r -> w_r - c*w_p nie zmieniają wyznacznika). Z reduced: postać zredukowana (jedynki wiodące, zera nad nimi).
    fraction_free (tylko bez reduced): w_r -> p*w_r - a*w_p na liczbach całkowitych - nie zmienia rzędu,
    ale zmienia wyznacznik, więc nie nadaje się do jego liczenia.
    pivot_cols ogranicza kolumny z elementami wiodącymi (np. bez kolumny b w [A|b]).
    """
    state = sp.Matrix(matrix)
    steps: list[dict[str, str]] = []
    swaps = 0
    pivot_row = 0
    for col in range(state.cols if pivot_cols is None else pivot_cols):
        if pivot_row >= state.rows:
            break
        candidates = [r for r in range(pivot_row, state.rows) if state[r, col] != 0]
        if not candidates:
            continue
        # Najmniejszy co do modułu element wiodący (najlepiej ±1) daje najprostsze rachunki.
        best = min(candidates, key=lambda r: (abs(state[r, col]) if state[r, col].is_number else 0, r))
        ops: list[str] = []
        if best != pivot_row:
            state.row_swap(pivot_row, best)
            swaps += 1
            ops.append(f"w_{{{pivot_row + 1}}}\\leftrightarrow w_{{{best + 1}}}")
        pivot_name = f"w_{{{pivot_row + 1}}}"
        if reduced and state[pivot_row, col] != 1:
            scale = 1 / state[pivot_row, col]
            state[pivot_row, :] = scale * state[pivot_row, :]
            ops.append(f"{pivot_name}\\to {row_combination([(scale, pivot_name)], True)}")
        targets = range(state.rows) if reduced else range(pivot_row + 1, state.rows)
        for r in targets:
            if r == pivot_row or state[r, col] == 0:
                continue
            name = f"w_{{{r + 1}}}"
            pivot, value = state[pivot_row, col], state[r, col]
            if fraction_free and not reduced and (value / pivot).is_integer is False:
                g = sp.gcd(pivot, value)
                scale_row, scale_pivot = pivot / g, value / g
                if scale_row < 0:
                    scale_row, scale_pivot = -scale_row, -scale_pivot
                state[r, :] = scale_row * state[r, :] - scale_pivot * state[pivot_row, :]
                ops.append(f"{name}\\to {row_combination([(scale_row, name), (-scale_pivot, pivot_name)], True)}")
                continue
            factor = value / pivot
            state[r, :] = state[r, :] - factor * state[pivot_row, :]
            ops.append(f"{name}\\to {row_combination([(sp.Integer(1), name), (-factor, pivot_name)], True)}")
        if ops:
            arrow = "\\xrightarrow{\\substack{" + "\\\\".join(ops) + "}}"
            steps.append(step(f"Kolumna {col + 1}", arrow + matrix_latex(state, split)))
        pivot_row += 1
    return state, steps, swaps


def eigen_steps(A: sp.Matrix, eigenvects: list | None = None) -> list[dict[str, str]]:
    """Wielomian charakterystyczny, wartości własne i przestrzenie własne macierzy kwadratowej."""
    lam = sp.Symbol("lambda")
    n = A.rows
    poly = sp.expand((A - lam * sp.eye(n)).det())
    factored = sp.factor(poly)
    # Postać iloczynowa tylko wtedy, gdy wielomian naprawdę rozkłada się na czynniki.
    nontrivial = sum(m for f, m in sp.factor_list(poly)[1] if f.has(lam)) > 1
    poly_latex = sp.latex(poly) + ("=" + sp.latex(factored) if nontrivial else "")
    steps = [step("Wielomian charakterystyczny", f"\\det(A-\\lambda I)={poly_latex}")]
    eig = eigenvects if eigenvects is not None else A.eigenvects()
    # Pierwiastki wielomianu stopnia 3-4 bywają ogromnymi wyrażeniami (wzory Cardana) - wtedy przybliżenia.
    messy = any(sp.count_ops(v) > 12 for v, _, _ in eig)
    show = (lambda e: sp.latex(e.evalf(5, chop=True))) if messy else sp.latex
    relation = "\\approx" if messy else "="
    values = ",\\ ".join(
        f"\\lambda{relation}{show(v)}" + (f"\\ (\\text{{krotność }}{m})" if m > 1 else "") for v, m, _ in eig
    )
    label = "Wartości własne (pierwiastki wielomianu)" + (" - w przybliżeniu" if messy else "")
    steps.append(step(label, values))
    if messy:
        for value, _, vectors in eig:
            basis = ",\\ ".join(matrix_latex(vec.applyfunc(lambda e: e.evalf(4, chop=True))) for vec in vectors)
            steps.append(step(
                f"Wektory własne dla λ ≈ {value.evalf(5, chop=True)} (rozwiązanie (A-λI)v=0, w przybliżeniu)",
                f"v\\approx\\operatorname{{span}}\\left\\{{{basis}\\right\\}}",
            ))
        return steps
    for value, mult, vectors in eig:
        M = A - value * sp.eye(n)
        cleaned = []
        for vec in vectors:
            rational = all(isinstance(v, sp.Rational) for v in vec)
            cleaned.append(clear_denominators(vec) if rational else vec)
        basis = ",\\ ".join(matrix_latex(v) for v in cleaned)
        if value == 0:
            shifted = "A"
        elif value.is_Integer:
            shifted = f"A{'-' if value > 0 else '+'}{abs(value)}I"
        else:
            shifted = f"A-\\left({sp.latex(value)}\\right)I"
        steps.append(
            step(
                f"Przestrzeń własna dla λ={sp.sstr(value)}: rozwiązujemy (A-λI)v=0",
                f"{shifted}={matrix_latex(M)},\\ \\operatorname{{rz}}={n - len(vectors)},\\ "
                f"v\\in\\operatorname{{span}}\\left\\{{{basis}\\right\\}}",
            )
        )
    return steps


def diagonalization_steps(A: sp.Matrix, D: sp.Matrix, P: sp.Matrix, jordan: bool) -> list[dict[str, str]]:
    """Kroki dla A = P·D·P⁻¹ (D diagonalna albo forma Jordana J)."""
    eig = A.eigenvects()
    steps = eigen_steps(A, eig)
    form = "J" if jordan else "D"
    if jordan:
        lacking = [(v, m, len(vs)) for v, m, vs in eig if len(vs) < m]
        detail = "; ".join(f"λ={sp.sstr(v)}: krotność {m}, wymiar przestrzeni własnej {g}" for v, m, g in lacking)
        verdict = (
            f"Brakuje wektorów własnych ({detail}), więc macierz nie jest diagonalizowalna. "
            "Kolumny P: wektory własne oraz wektor uogólniony w spełniający (A-λI)w = v"
        )
    else:
        verdict = "Liczba niezależnych wektorów własnych = n, więc macierz jest diagonalizowalna. Kolumny P: wektory własne"
    steps.append(step(verdict, f"{form}={matrix_latex(D)},\\ P={matrix_latex(P)}"))
    steps.append(step("Sprawdzenie", f"P^{{-1}}={matrix_latex(P.inv())},\\ P{form}P^{{-1}}={matrix_latex(P * D * P.inv())}=A"))
    return steps


def random_diag_jordan_matrix(n: int, low: int, high: int, jordan: bool) -> tuple[sp.Matrix, sp.Matrix, sp.Matrix]:
    # A = P*D*P^-1 z unimodularną P: forma diagonalna/Jordana i P są całkowite,
    # a zera stanowią mniej niż 20% elementów A.
    distinct_needed = n - 1 if jordan else n
    pool = list(range(low, high + 1))
    extra = high + 1
    while len(pool) < distinct_needed:
        pool.append(extra)
        extra += 1
    max_zeros = (n * n) // 5
    limit = max(9, abs(low), abs(high))
    for _ in range(20000):
        values = random.sample(pool, distinct_needed)
        if jordan:
            values.append(random.choice(values))
        values.sort()
        D = sp.diag(*values)
        if jordan:
            first = next(i for i in range(n - 1) if values[i] == values[i + 1])
            D[first, first + 1] = 1
        P = sp.eye(n)
        for _ in range(random.randint(n, 2 * n)):
            i, j = random.sample(range(n), 2)
            P[i, :] = P[i, :] + random.choice([-1, 1]) * P[j, :]
        perm = list(range(n))
        random.shuffle(perm)
        P = P.extract(perm, list(range(n)))
        A = P * D * P.inv()
        if max(abs(v) for v in A) > limit:
            continue
        if sum(1 for v in A if v == 0) > max_zeros:
            continue
        if all(A[i, j] == 0 for i in range(n) for j in range(n) if i != j):
            continue
        return A, D, P
    raise ValueError("Nie udało się wylosować macierzy o całkowitej formie diagonalnej/Jordana.")



# --- Rysowanie obszarów płaszczyzny zespolonej jako SVG (backend/region_plot.py) ---

_X, _Y = sp.symbols("x y", real=True)
_Z_XY = _X + sp.I * _Y
_Z_SYMBOL = sp.Symbol("z")
_NAN = float("nan")
_ABS_REAL = sp.Function("absr", real=True, nonnegative=True)


def _arg(w: complex) -> float:
    w = complex(w)
    return math.atan2(w.imag, w.real)


def _sign(v: complex) -> float:
    v = complex(v)
    if v.imag != 0:
        raise ValueError("sign dla liczby zespolonej")
    return (v.real > 0) - (v.real < 0)


_NUMERIC_FUNCS: dict[str, Any] = {
    "Abs": abs,
    "absr": math.hypot,
    "arg": _arg,
    "re": lambda w: complex(w).real,
    "im": lambda w: complex(w).imag,
    "conjugate": lambda w: complex(w).conjugate(),
    "sign": _sign,
}


def _sympy_locals(z_value: Any) -> dict[str, Any]:
    if z_value is _Z_XY:
        abs_func: Any = lambda a: _ABS_REAL(sp.re(a), sp.im(a))
    else:
        abs_func = sp.Abs
    return {
        "x": _X,
        "y": _Y,
        "z": z_value,
        "i": sp.I,
        "I": sp.I,
        "pi": sp.pi,
        "e": sp.E,
        "E": sp.E,
        "Abs": abs_func,
        "abs": abs_func,
        "Re": sp.re,
        "re": sp.re,
        "Im": sp.im,
        "im": sp.im,
        "arg": sp.arg,
        "conj": sp.conjugate,
        "conjugate": sp.conjugate,
        "sqrt": sp.sqrt,
        "exp": sp.exp,
        "log": sp.log,
        "ln": sp.log,
        "sin": sp.sin,
        "cos": sp.cos,
        "tan": sp.tan,
    }


_ALLOWED_NAMES = set(_sympy_locals(None)) | {"Eq"}
_OPERATORS = ("<=", ">=", "!=", "=", "<", ">")
_LATEX_OPS = {"<=": "\\le", ">=": "\\ge", "<": "<", ">": ">", "=": "=", "!=": "\\ne"}


class Relation:
    """Pojedyncza relacja g(x, y) {>=, >, =, !=} 0 wraz z funkcją liczbową."""

    def __init__(self, op: str, g_expr: sp.Expr, latex: str) -> None:
        self.op = op
        self.strict = op in ("<", ">", "!=")
        self.latex = latex
        self.numbers = [abs(float(n)) for n in g_expr.atoms(sp.Number) if n.is_real]
        self._fn: Callable[..., Any] = sp.lambdify((_X, _Y), g_expr, modules=[_NUMERIC_FUNCS, "math"])

    def value(self, x: float, y: float) -> float:
        try:
            v = self._fn(x, y)
            if isinstance(v, complex):
                if abs(v.imag) > 1e-12:
                    return _NAN
                v = v.real
            return float(v)
        except Exception:
            return _NAN

    def holds(self, v: float) -> bool:
        if v != v:
            return False
        if self.op == "!=":
            return v != 0
        return v >= 0


def _normalize(text: str) -> str:
    text = text.strip()
    text = text.replace("−", "-").replace("≤", "<=").replace("≥", ">=").replace("≠", "!=")
    text = text.replace("==", "=").replace("^", "**").replace("⩽", "<=").replace("⩾", ">=")
    return re.sub(r"\|([^|]+)\|", r"Abs(\1)", text)


def _insert_multiplication(expr: str) -> str:
    expr = re.sub(r"(?<![A-Za-z_\d.])(\d+(?:\.\d+)?)\s*(?=[A-Za-z(])", r"\1*", expr)
    return re.sub(r"\)\s*(?=[A-Za-z\d(])", ")*", expr)


def _split_conditions(text: str) -> list[str]:
    parts = re.split(r"[;\n&]|\boraz\b|\band\b|\bi\b(?=\s+[A-Za-z|(])", text)
    return [p.strip() for p in parts if p.strip()]


def _unwrap_eq(chunk: str) -> str:
    match = re.fullmatch(r"Eq\((.*)\)", chunk)
    if not match:
        return chunk
    depth = 0
    for idx, ch in enumerate(match.group(1)):
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            inner = match.group(1)
            return f"{inner[:idx]}={inner[idx + 1:]}"
    raise ValueError("Eq wymaga dwóch argumentów, np. Eq(z, conj(z)).")


def _parse_operand(raw: str, z_value: Any) -> sp.Expr:
    prepared = _insert_multiplication(raw.strip())
    if not prepared:
        raise ValueError("Brak wyrażenia po jednej ze stron nierówności.")
    names = set(re.findall(r"[A-Za-z_]\w*", prepared))
    unknown = names - _ALLOWED_NAMES
    if unknown:
        raise ValueError(f"Nieznane oznaczenie: {', '.join(sorted(unknown))}.")
    try:
        return sp.sympify(prepared, locals=_sympy_locals(z_value), evaluate=True)
    except Exception as exc:
        raise ValueError(f"Nie udało się odczytać wyrażenia: {raw.strip()}") from exc


def parse_conditions(text: str) -> list[Relation]:
    """Parsuje warunki, np. 'Abs(z-(1-2*i))<=3; Re(z)>0' albo '1<=Im(z)<=3'."""
    relations: list[Relation] = []
    for chunk in _split_conditions(_normalize(text)):
        chunk = _unwrap_eq(chunk)
        tokens = re.split(r"(<=|>=|!=|=|<|>)", chunk)
        if len(tokens) < 3:
            raise ValueError(f"Warunek '{chunk}' musi zawierać znak =, <, >, <=, >= lub !=.")
        operands = tokens[0::2]
        ops = tokens[1::2]
        if len(tokens) % 2 == 0 or any(o not in _OPERATORS for o in ops):
            raise ValueError(f"Nie udało się odczytać warunku: {chunk}")
        for left_raw, op, right_raw in zip(operands, ops, operands[1:]):
            left = _parse_operand(left_raw, _Z_XY)
            right = _parse_operand(right_raw, _Z_XY)
            left_z = _parse_operand(left_raw, _Z_SYMBOL)
            right_z = _parse_operand(right_raw, _Z_SYMBOL)
            relations.append(_build_relation(op, left, right, left_z, right_z))
    if not relations:
        raise ValueError("Wpisz warunek opisujący obszar.")
    if len(relations) > 6:
        raise ValueError("Podaj najwyżej 6 warunków.")
    return relations


def _build_relation(op: str, left: sp.Expr, right: sp.Expr, left_z: sp.Expr, right_z: sp.Expr) -> Relation:
    diff = left - right if op in (">=", ">", "=", "!=") else right - left
    real_part = sp.simplify(sp.re(diff))
    imag_part = sp.simplify(sp.im(diff))
    if imag_part != 0:
        if op in ("=", "!=") and real_part == 0:
            g_expr = imag_part
        else:
            raise ValueError("Obie strony nierówności muszą być rzeczywiste (np. |z|, Re(z), Im(z), arg(z)).")
    else:
        g_expr = real_part
    latex = f"{sp.latex(left_z)}{_LATEX_OPS[op]}{sp.latex(right_z)}"
    latex = latex.replace("\\operatorname{re}", "\\operatorname{Re}").replace("\\operatorname{im}", "\\operatorname{Im}")
    return Relation(op, g_expr, latex)


_LADDER = [2, 2.5, 3, 3.5, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30, 40, 50, 60, 80, 100, 150, 200, 300, 500, 1000]


def _ladder(value: float) -> float:
    for item in _LADDER:
        if item >= value:
            return float(item)
    return float(math.ceil(value))


def _tick_step(radius: float) -> float:
    if radius <= 8:
        return 1.0
    for step in (2, 5, 10, 20, 50, 100, 200, 500, 1000):
        if radius / step <= 7.5:
            return float(step)
    return float(radius)


def _fmt(value: float) -> str:
    if abs(value - round(value)) < 0.006:
        text = str(int(round(value)))
    else:
        text = f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return text.replace("-", "−") if text != "−0" else "0"


class _Grid:
    """Wartości relacji w węzłach siatki n x n na kwadracie o środku (cx, cy) i półboku R oraz w środkach komórek."""

    def __init__(self, rel: Relation, cx: float, cy: float, radius: float, n: int, centers: bool) -> None:
        self.n = n
        self.h = 2 * radius / n
        self.ox = cx - radius
        self.oy = cy - radius
        xs = [self.ox + i * self.h for i in range(n + 1)]
        ys = [self.oy + i * self.h for i in range(n + 1)]
        self.vertex = [[rel.value(x, y) for x in xs] for y in ys]
        self.center: list[list[float]] | None = None
        if centers:
            mx = [self.ox + (i + 0.5) * self.h for i in range(n)]
            my = [self.oy + (i + 0.5) * self.h for i in range(n)]
            self.center = [[rel.value(x, y) for x in mx] for y in my]


def _bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), max(xs), min(ys), max(ys)


def _scan_view(relations: list[Relation]) -> tuple[float, float, float] | None:
    """Zwraca (środek x, środek y, półbok) widoku wyśrodkowanego na obszarze albo None, gdy nic nie da się wyśrodkować."""
    scan_radius, n = 35.0, 140
    grids = [_Grid(rel, 0.0, 0.0, scan_radius, n, centers=False) for rel in relations]
    fill_idx = [k for k, rel in enumerate(relations) if rel.op != "="]
    h = 2 * scan_radius / n
    region_pts: list[tuple[float, float]] = []
    boundary_pts: list[tuple[float, float]] = []
    for j in range(n + 1):
        y = -scan_radius + j * h
        for i in range(n + 1):
            x = -scan_radius + i * h
            if fill_idx and all(relations[k].holds(grids[k].vertex[j][i]) for k in fill_idx):
                region_pts.append((x, y))
            for k, rel in enumerate(relations):
                if not _vertex_sign_change(grids[k].vertex, i, j, n):
                    continue
                if all(relations[m].holds(grids[m].vertex[j][i]) for m in fill_idx if m != k):
                    boundary_pts.append((x, y))
                    break

    def bounded(pts: list[tuple[float, float]]) -> bool:
        x0, x1, y0, y1 = _bbox(pts)
        return max(abs(x0), abs(x1), abs(y0), abs(y1)) < scan_radius - 2 * h

    if region_pts and bounded(region_pts):
        pts, factor = region_pts + boundary_pts, 1.0
    elif boundary_pts and bounded(boundary_pts):
        pts, factor = boundary_pts, 1.4
    else:
        return _unbounded_view(relations, boundary_pts, h)
    x0, x1, y0, y1 = _bbox(pts)
    half = (max(x1 - x0, y1 - y0) / 2 + h) * factor
    return (x0 + x1) / 2, (y0 + y1) / 2, half


def _unbounded_view(
    relations: list[Relation], boundary_pts: list[tuple[float, float]], h: float
) -> tuple[float, float, float] | None:
    """Widok dla obszarów nieograniczonych: centruje na skończonych krawędziach brzegu (np. prostych)."""
    window = _heuristic_extent(relations)
    pts = [p for p in boundary_pts if abs(p[0]) <= window and abs(p[1]) <= window]
    if not pts:
        return None
    center = []
    for lo, hi in ((min(p[0] for p in pts), max(p[0] for p in pts)), (min(p[1] for p in pts), max(p[1] for p in pts))):
        touches_low, touches_high = lo <= -window + 2 * h, hi >= window - 2 * h
        if touches_low and touches_high:
            center.append(0.0)
        elif touches_low:
            center.append(hi)
        elif touches_high:
            center.append(lo)
        else:
            center.append((lo + hi) / 2)
    return center[0], center[1], window


def _vertex_sign_change(vertex: list[list[float]], i: int, j: int, n: int) -> bool:
    v = vertex[j][i]
    if v != v:
        return False
    for di, dj in ((1, 0), (0, 1)):
        ii, jj = i + di, j + dj
        if ii <= n and jj <= n:
            w = vertex[jj][ii]
            if w == w and (v >= 0) != (w >= 0):
                return True
    return False


def _heuristic_extent(relations: list[Relation]) -> float:
    numbers = sorted((n for rel in relations for n in rel.numbers), reverse=True)
    scale = sum(numbers[:2]) if numbers else 2.0
    return max(3.0, scale * 1.3)


_CASES = {
    1: [(0, 3)], 2: [(0, 1)], 3: [(3, 1)], 4: [(1, 2)], 6: [(0, 2)], 7: [(3, 2)], 8: [(3, 2)],
    9: [(0, 2)], 11: [(1, 2)], 12: [(3, 1)], 13: [(0, 1)], 14: [(3, 0)],
}


def _contour_segments(grid: _Grid) -> list[tuple[tuple, tuple, tuple, tuple]]:
    """Marching squares dla poziomicy g = 0. Segment: (klucz_a, punkt_a, klucz_b, punkt_b)."""
    n, h, v = grid.n, grid.h, grid.vertex
    segments = []
    for j in range(n):
        row0, row1 = v[j], v[j + 1]
        for i in range(n):
            c00, c10, c11, c01 = row0[i], row0[i + 1], row1[i + 1], row1[i]
            if c00 != c00 or c10 != c10 or c11 != c11 or c01 != c01:
                continue
            index = (c00 > 0) | ((c10 > 0) << 1) | ((c11 > 0) << 2) | ((c01 > 0) << 3)
            if index in (0, 15):
                continue
            if index == 5 or index == 10:
                center_in = (c00 + c10 + c11 + c01) > 0
                if index == 5:
                    pairs = [(0, 1), (3, 2)] if center_in else [(0, 3), (1, 2)]
                else:
                    pairs = [(0, 3), (1, 2)] if center_in else [(0, 1), (3, 2)]
            else:
                pairs = _CASES[index]
            x0, y0 = grid.ox + i * h, grid.oy + j * h

            def edge(e: int) -> tuple[tuple, tuple]:
                if e == 0:
                    t = c00 / (c00 - c10)
                    return ("h", i, j), (x0 + t * h, y0)
                if e == 1:
                    t = c10 / (c10 - c11)
                    return ("v", i + 1, j), (x0 + h, y0 + t * h)
                if e == 2:
                    t = c01 / (c01 - c11)
                    return ("h", i, j + 1), (x0 + t * h, y0 + h)
                t = c00 / (c00 - c01)
                return ("v", i, j), (x0, y0 + t * h)

            for ea, eb in pairs:
                ka, pa = edge(ea)
                kb, pb = edge(eb)
                segments.append((ka, pa, kb, pb))
    return segments


def _chain(segments: list[tuple]) -> list[list[tuple[float, float]]]:
    points: dict[tuple, tuple[float, float]] = {}
    adjacency: dict[tuple, list[tuple]] = {}
    for ka, pa, kb, pb in segments:
        points[ka], points[kb] = pa, pb
        adjacency.setdefault(ka, []).append(kb)
        adjacency.setdefault(kb, []).append(ka)
    used: set[frozenset] = set()
    chains: list[list[tuple[float, float]]] = []

    def walk(start: tuple) -> None:
        path = [start]
        current = start
        while True:
            nxt = next((k for k in adjacency[current] if frozenset((current, k)) not in used), None)
            if nxt is None:
                break
            used.add(frozenset((current, nxt)))
            path.append(nxt)
            current = nxt
        chains.append([points[k] for k in path])

    ends = [k for k, nb in adjacency.items() if len(nb) == 1]
    for key in ends + list(adjacency):
        if any(frozenset((key, k)) not in used for k in adjacency[key]):
            walk(key)
    return chains


def _clip_segments(
    segments: list[tuple], index: int, relations: list[Relation], h: float
) -> list[tuple]:
    others = [(k, rel) for k, rel in enumerate(relations) if k != index]
    if not others:
        return segments
    kept = []
    for seg in segments:
        mx, my = (seg[1][0] + seg[3][0]) / 2, (seg[1][1] + seg[3][1]) / 2
        ok = True
        for _, rel in others:
            v = rel.value(mx, my)
            if v != v:
                ok = False
                break
            slope = abs(rel.value(mx + h, my) - v) + abs(rel.value(mx, my + h) - v)
            slope = slope if slope == slope else 0.0
            if rel.op == "=":
                ok = abs(v) <= 0.5 * slope
            elif rel.op == "!=":
                ok = True
            else:
                ok = v >= -0.5 * slope
            if not ok:
                break
        if ok:
            kept.append(seg)
    return kept


def _axis_crossings(
    relations: list[Relation], cx: float, cy: float, radius: float
) -> tuple[list[float], list[float]]:
    """Punkty, w których brzeg obszaru przecina osie Re i Im (o ile osie są w widoku)."""
    steps = 1600
    xs_marks: list[float] = []
    ys_marks: list[float] = []
    for horizontal, target in ((True, xs_marks), (False, ys_marks)):
        center, other = (cx, cy) if horizontal else (cy, cx)
        if abs(other) > radius:
            continue
        for index, rel in enumerate(relations):
            def at(t: float) -> float:
                return rel.value(t, 0.0) if horizontal else rel.value(0.0, t)

            prev_t = center - radius
            prev_v = at(prev_t)
            for s in range(1, steps + 1):
                t = center - radius + 2 * radius * s / steps
                v = at(t)
                if prev_v == prev_v and v == v and (prev_v >= 0) != (v >= 0):
                    lo, hi = prev_t, t
                    flo = prev_v
                    for _ in range(60):
                        mid = (lo + hi) / 2
                        fm = at(mid)
                        if fm != fm:
                            break
                        if (fm >= 0) == (flo >= 0):
                            lo, flo = mid, fm
                        else:
                            hi = mid
                    root = (lo + hi) / 2
                    scale = max(1.0, abs(prev_v), abs(v))
                    if abs(at(root)) <= 1e-6 * scale:
                        px, py = (root, 0.0) if horizontal else (0.0, root)
                        if all(_holds_with_tol(o, px, py) for k, o in enumerate(relations) if k != index):
                            if all(abs(root - m) > 1e-6 for m in target):
                                target.append(root)
                prev_t, prev_v = t, v
    return xs_marks, ys_marks


def _holds_with_tol(rel: Relation, x: float, y: float) -> bool:
    v = rel.value(x, y)
    if v != v:
        return False
    if rel.op == "=":
        return abs(v) <= 1e-6
    if rel.op == "!=":
        return True
    return v >= -1e-6


def _fill_path(grid_values: list[list[bool]], radius: float, scale: float, left: float, top: float) -> str:
    n = len(grid_values)
    h = 2 * radius / n * scale
    parts = []
    for j, row in enumerate(grid_values):
        y = top + (n - 1 - j) * h
        i = 0
        while i < n:
            if not row[i]:
                i += 1
                continue
            start = i
            while i < n and row[i]:
                i += 1
            x = left + start * h
            parts.append(f"M{x:.1f} {y - 0.3:.1f}h{(i - start) * h + 0.3:.1f}v{h + 0.6:.1f}h-{(i - start) * h + 0.3:.1f}z")
    return "".join(parts)


def _snap(value: float, tolerance: float) -> float:
    """Zaokrągla do najbliższej wielokrotności 0,5, jeśli różnica mieści się w błędzie siatki."""
    nearest = round(value * 2) / 2
    return nearest if abs(value - nearest) <= tolerance else value


def render_region_svg(
    conditions: str | list[Relation],
    *,
    focus: list[tuple[float, float]] | None = None,
    marks_x: list[tuple[float, float | None]] | None = None,
    marks_y: list[tuple[float, float | None]] | None = None,
    points: list[tuple[float, float, str]] | None = None,
    auto_marks: bool = False,
) -> str:
    """Zwraca data-URI z obrazem SVG obszaru; widok jest wyśrodkowany na obszarze.

    focus: punkty, które mają zmieścić się w widoku (środek widoku = środek ich prostokąta otaczającego);
           bez nich widok wyznaczany jest automatycznie z kształtu obszaru
    marks_x: (wartość na osi Re, y punktu do którego prowadzi linia pomocnicza lub None)
    marks_y: (wartość na osi Im, x punktu do którego prowadzi linia pomocnicza lub None)
    """
    relations = parse_conditions(conditions) if isinstance(conditions, str) else conditions
    marks_x = list(marks_x or [])
    marks_y = list(marks_y or [])
    points = list(points or [])

    if focus:
        x0, x1, y0, y1 = _bbox(focus)
        cx, cy, half = (x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0) / 2
    else:
        scanned = _scan_view(relations)
        if scanned is not None:
            cx, cy, half = scanned
        else:
            cx, cy, half = 0.0, 0.0, _heuristic_extent(relations)
    radius = _ladder(max(half * 1.2, 2.0))

    n = 280
    grids = [_Grid(rel, cx, cy, radius, n, centers=True) for rel in relations]
    fill_idx = [k for k, rel in enumerate(relations) if rel.op != "="]
    has_equality = len(fill_idx) < len(relations)

    mask = [[False] * n for _ in range(n)]
    filled_any = False
    if fill_idx and not has_equality:
        for j in range(n):
            for i in range(n):
                if all(relations[k].holds(grids[k].center[j][i]) for k in fill_idx):
                    mask[j][i] = True
                    filled_any = True

    h_cell = 2 * radius / n
    contour_chains: list[tuple[Relation, list[list[tuple[float, float]]]]] = []
    boundary_points: list[tuple[float, float]] = []
    for k, rel in enumerate(relations):
        segs = _contour_segments(grids[k])
        segs = _clip_segments(segs, k, relations, h_cell) if (len(relations) > 1) else segs
        if segs:
            chains = _chain(segs)
            contour_chains.append((rel, chains))
            for chain in chains:
                boundary_points.extend(chain)

    if auto_marks:
        cx_marks, cy_marks = _axis_crossings(relations, cx, cy, radius)
        marks_x += [(v, None) for v in cx_marks]
        marks_y += [(v, None) for v in cy_marks]
        if boundary_points:
            bx0, bx1, by0, by1 = _bbox(boundary_points)
            margin_cells = 3 * h_cell
            bounded = (
                bx0 > cx - radius + margin_cells and bx1 < cx + radius - margin_cells
                and by0 > cy - radius + margin_cells and by1 < cy + radius - margin_cells
            )
            if bounded:
                px_max = max(boundary_points, key=lambda p: p[0])
                px_min = min(boundary_points, key=lambda p: p[0])
                py_max = max(boundary_points, key=lambda p: p[1])
                py_min = min(boundary_points, key=lambda p: p[1])
                tol = 3 * h_cell
                existing_x = [m[0] for m in marks_x]
                existing_y = [m[0] for m in marks_y]
                for p in (px_max, px_min):
                    value = _snap(p[0], 1.5 * h_cell)
                    if all(abs(value - m) > tol for m in existing_x):
                        marks_x.append((value, p[1]))
                        points.append((p[0], p[1], ""))
                for p in (py_max, py_min):
                    value = _snap(p[1], 1.5 * h_cell)
                    if all(abs(value - m) > tol for m in existing_y):
                        marks_y.append((value, p[0]))
                        points.append((p[0], p[1], ""))

    return _compose_svg(
        (cx, cy, radius), mask, filled_any, contour_chains, marks_x, marks_y, points,
        empty=not filled_any and not contour_chains,
    )


_AXIS = "#111827"
_BLUE = "#1d4ed8"
_MARK = "#b91c1c"
_FONT = "font-family=\"Segoe UI, Arial, sans-serif\""


def _ticks_in(low: float, high: float, step: float) -> list[float]:
    first = math.ceil(low / step - 1e-9)
    last = math.floor(high / step + 1e-9)
    return [k * step for k in range(first, last + 1)]


def _compose_svg(
    view: tuple[float, float, float],
    mask: list[list[bool]],
    filled: bool,
    chains: list[tuple[Relation, list[list[tuple[float, float]]]]],
    marks_x: list[tuple[float, float | None]],
    marks_y: list[tuple[float, float | None]],
    points: list[tuple[float, float, str]],
    empty: bool,
) -> str:
    cx, cy, radius = view
    plot, margin = 500, 36
    size = plot + 2 * margin
    left = top = margin
    scale = plot / (2 * radius)
    x_lo, x_hi = cx - radius, cx + radius
    y_lo, y_hi = cy - radius, cy + radius

    def px(x: float) -> float:
        return left + (x - x_lo) * scale

    def py(y: float) -> float:
        return top + (y_hi - y) * scale

    # osie przechodzą przez 0, a gdy 0 jest poza widokiem - przylegają do najbliższej krawędzi
    ox = px(min(max(0.0, x_lo), x_hi))
    oy = py(min(max(0.0, y_lo), y_hi))
    origin_visible = x_lo <= 0 <= x_hi and y_lo <= 0 <= y_hi

    out: list[str] = []
    out.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
    )
    out.append(f'<defs><clipPath id="c"><rect x="{left}" y="{top}" width="{plot}" height="{plot}"/></clipPath></defs>')
    out.append(f'<rect width="{size}" height="{size}" fill="#ffffff"/>')

    step = _tick_step(radius)
    x_ticks = _ticks_in(x_lo, x_hi, step)
    y_ticks = _ticks_in(y_lo, y_hi, step)
    grid_lines = "".join(f'<line x1="{px(t):.1f}" y1="{top}" x2="{px(t):.1f}" y2="{top + plot}"/>' for t in x_ticks)
    grid_lines += "".join(f'<line x1="{left}" y1="{py(t):.1f}" x2="{left + plot}" y2="{py(t):.1f}"/>' for t in y_ticks)
    out.append(f'<g stroke="#e5e7eb" stroke-width="1">{grid_lines}</g>')
    out.append(f'<rect x="{left}" y="{top}" width="{plot}" height="{plot}" fill="none" stroke="#cbd5e1"/>')

    out.append('<g clip-path="url(#c)">')
    if filled:
        out.append(
            f'<path d="{_fill_path(mask, radius, scale, left, top)}" fill="rgba(59,130,246,0.30)" '
            'shape-rendering="crispEdges"/>'
        )
    for rel, rel_chains in chains:
        dash = ' stroke-dasharray="7 5"' if rel.strict else ""
        width = 3 if rel.op == "=" else 2.2
        d = "".join(
            "M" + "L".join(f"{px(x):.1f} {py(y):.1f}" for x, y in chain) for chain in rel_chains if len(chain) > 1
        )
        out.append(
            f'<path d="{d}" fill="none" stroke="{_BLUE}" stroke-width="{width}" stroke-linejoin="round"{dash}/>'
        )

    # linie pomocnicze i punkty
    guides = []
    for value, other in marks_x:
        if other is not None:
            guides.append(f'<line x1="{px(value):.1f}" y1="{oy:.1f}" x2="{px(value):.1f}" y2="{py(other):.1f}"/>')
    for value, other in marks_y:
        if other is not None:
            guides.append(f'<line x1="{ox:.1f}" y1="{py(value):.1f}" x2="{px(other):.1f}" y2="{py(value):.1f}"/>')
    out.append(f'<g stroke="{_MARK}" stroke-width="1.2" stroke-dasharray="4 3" opacity="0.75">{"".join(guides)}</g>')
    out.append("</g>")

    # osie ze strzałkami
    ax_end = left + plot + margin - 8
    ay_end = top - margin + 8
    out.append(
        f'<g stroke="{_AXIS}" stroke-width="1.8" fill="{_AXIS}">'
        f'<line x1="{left}" y1="{oy}" x2="{ax_end - 6}" y2="{oy}"/>'
        f'<polygon points="{ax_end},{oy} {ax_end - 10},{oy - 4.5} {ax_end - 10},{oy + 4.5}" stroke="none"/>'
        f'<line x1="{ox}" y1="{top + plot}" x2="{ox}" y2="{ay_end + 6}"/>'
        f'<polygon points="{ox},{ay_end} {ox - 4.5},{ay_end + 10} {ox + 4.5},{ay_end + 10}" stroke="none"/>'
        "</g>"
    )

    halo = 'stroke="#ffffff" stroke-width="3" paint-order="stroke" stroke-linejoin="round"'
    text: list[str] = []

    def label(x: float, y: float, s: str, anchor: str, color: str = "#374151", bold: bool = False, size_px: int = 12) -> None:
        weight = ' font-weight="700"' if bold else ""
        text.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" font-size="{size_px}" fill="{color}"{weight} '
            f'{_FONT} {halo}>{s}</text>'
        )

    marks_x_sorted = sorted({round(m[0], 6) for m in marks_x if abs(m[0]) > 1e-9 and x_lo <= m[0] <= x_hi})
    marks_y_sorted = sorted({round(m[0], 6) for m in marks_y if abs(m[0]) > 1e-9 and y_lo <= m[0] <= y_hi})

    tick_marks = []
    for t in x_ticks:
        if origin_visible and abs(t) < 1e-9:
            continue
        tick_marks.append(f'<line x1="{px(t):.1f}" y1="{oy - 4:.1f}" x2="{px(t):.1f}" y2="{oy + 4:.1f}"/>')
        if not any(abs(px(t) - px(m)) < 30 for m in marks_x_sorted):
            label(px(t), oy + 18, _fmt(t), "middle")
    for t in y_ticks:
        if origin_visible and abs(t) < 1e-9:
            continue
        tick_marks.append(f'<line x1="{ox - 4:.1f}" y1="{py(t):.1f}" x2="{ox + 4:.1f}" y2="{py(t):.1f}"/>')
        if not any(abs(py(t) - py(m)) < 16 for m in marks_y_sorted):
            label(ox - 9, py(t) + 4, _fmt(t), "end")
    out.append(f'<g stroke="{_AXIS}" stroke-width="1.2">{"".join(tick_marks)}</g>')
    if origin_visible:
        label(ox - 9, oy + 18, "0", "end")

    mark_ticks = []
    last_px = -1e9
    level = 0
    for m in marks_x_sorted:
        level = level + 1 if px(m) - last_px < 34 else 0
        last_px = px(m)
        mark_ticks.append(f'<line x1="{px(m):.1f}" y1="{oy - 6:.1f}" x2="{px(m):.1f}" y2="{oy + 6:.1f}"/>')
        label(px(m), oy + 18 + 15 * (level % 2), _fmt(m), "middle", _MARK, True, 13)
    last_py = 1e9
    level = 0
    for m in marks_y_sorted[::-1]:
        level = level + 1 if last_py - py(m) < 18 else 0
        last_py = py(m)
        mark_ticks.append(f'<line x1="{ox - 6:.1f}" y1="{py(m):.1f}" x2="{ox + 6:.1f}" y2="{py(m):.1f}"/>')
        if level % 2 == 0:
            label(ox - 10, py(m) + 4.5, _fmt(m), "end", _MARK, True, 13)
        else:
            label(ox + 10, py(m) + 4.5, _fmt(m), "start", _MARK, True, 13)
    out.append(f'<g stroke="{_MARK}" stroke-width="2">{"".join(mark_ticks)}</g>')

    dots = []
    for x, y, caption in points:
        if not (x_lo <= x <= x_hi and y_lo <= y <= y_hi):
            continue
        dots.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="3.6" fill="{_MARK}" stroke="#ffffff" stroke-width="1"/>')
        if caption:
            label(px(x) + 7, py(y) - 7, caption, "start", _MARK, True, 13)
    out.extend(dots)

    label(left + plot + margin - 10, oy - 11, "Re", "end", _AXIS, True, 15)
    label(ox + 11, top - margin + 20, "Im", "start", _AXIS, True, 15)
    if empty:
        label(left + plot / 2, top + 22, "Brak punktów spełniających warunki w widocznym obszarze", "middle", "#6b7280", False, 13)

    out.extend(text)
    out.append("</svg>")
    encoded = base64.b64encode("".join(out).encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"



# --- Zadanie 1: Równania zespolone (backend/tasks/task_01_rownania_zespolone.py) ---

DEFINITION_01 = {
    "id": 1,
    "name": "Równania zespolone",
    "area": "Liczby zespolone",
    "example": "Przykład: (z-(2+i))(z-(1-3i))=0.",
    "description": "Dane jest równanie wielomianowe zmiennej zespolonej z. Trzeba wyznaczyć wszystkie jego rozwiązania w zbiorze liczb zespolonych.",
    "parameterHelp": "Ustaw liczbę czynników i zakres liczb w generatorze.",
    "examInstruction": "Rozwiąż równanie w zbiorze liczb zespolonych.",
    "params": [
        {"key": "factors", "label": "Liczba czynników", "type": "number", "min": 2, "max": 6, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współczynnika", "type": "number", "min": -30, "max": 0, "value": -8},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współczynnika", "type": "number", "min": 0, "max": 30, "value": 8},
    ],
}


def generate_01(config: dict[str, Any]) -> dict[str, Any]:
    factors = max(2, rint(config, "factors", 3))
    pool = [sp.Integer(v) for v in (-3, -2, -1, 1, 2, 3)] + [sp.I, -sp.I, 1 + sp.I, 1 - sp.I, 2 + sp.I, 2 - sp.I]
    roots = random.sample(pool, min(factors, len(pool)))
    expr = sp.expand(sp.prod([(Z - r) for r in roots]))
    return {
        "task": f"Rozwiąż równanie: {sp.sstr(expr)} = 0.",
        "solution": "Pierwiastki: " + ", ".join(sp.sstr(r) for r in roots) + ".",
        "taskLatex": f"{sp.latex(expr)}=0",
        "solutionLatex": "z\\in\\left\\{" + ", ".join(sp.latex(r) for r in roots) + "\\right\\}",
        "steps": [
            step(
                "Rozkład wielomianu na czynniki liniowe (pierwiastki można zgadnąć wśród dzielników wyrazu wolnego, "
                "a następnie podzielić wielomian schematem Hornera)",
                f"{sp.latex(expr)}=" + "".join(f"\\left({sp.latex(Z - r)}\\right)" for r in roots) + "=0",
            ),
            step("Każdy czynnik przyrównujemy do zera",
                 ",\\ ".join(f"z_{{{i}}}={sp.latex(r)}" for i, r in enumerate(roots, start=1))),
        ],
    }



# --- Zadanie 2: Postać trygonometryczna liczby zespolonej (backend/tasks/task_02_postac_trygonometryczna.py) ---

DEFINITION_02 = {
    "id": 2,
    "name": "Postać trygonometryczna liczby zespolonej",
    "area": "Liczby zespolone",
    "example": "Przykład: z=3-4i zapisz w postaci trygonometrycznej.",
    "description": "Dana jest liczba zespolona w postaci algebraicznej a+bi. Trzeba zapisać ją w postaci trygonometrycznej |z|(cos φ + i sin φ) z kątem głównym φ.",
    "parameterHelp": "Ustaw dolną i górną granicę współczynników.",
    "examInstruction": "Zapisz liczbę zespoloną w postaci trygonometrycznej (kąt główny).",
    "params": [
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współczynnika", "type": "number", "min": -30, "max": 0, "value": -10},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współczynnika", "type": "number", "min": 0, "max": 30, "value": 10},
    ],
}


def generate_02(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    # Kąty główne z przedziału (-pi, pi]: wielokrotności pi/6 i pi/4 (bez 0 i pi, by liczba nie była rzeczywista).
    fractions = sorted(
        {sp.Rational(k, 6) for k in range(-5, 6)} | {sp.Rational(k, 4) for k in range(-3, 4)}
    )
    q = random.choice([f for f in fractions if f != 0])
    angle = q * sp.pi
    c, s = sp.cos(angle), sp.sin(angle)
    r = random.randint(2, max(2, abs(high)))
    z_val = sp.expand(r * c) + sp.I * sp.expand(r * s)

    angle_latex = sp.latex(angle)
    angle_text = sp.sstr(angle)
    if q > sp.Rational(1, 2):
        reference = (1 - q) * sp.pi
        angle_latex += f"=\\pi-{sp.latex(reference)}"
        angle_text += f" = pi - {sp.sstr(reference)}"
    elif q < -sp.Rational(1, 2):
        reference = (1 + q) * sp.pi
        angle_latex += f"=-\\pi+{sp.latex(reference)}"
        angle_text += f" = -pi + {sp.sstr(reference)}"

    return {
        "task": "Zapisz podaną liczbę zespoloną w postaci trygonometrycznej (kąt główny).",
        "solution": f"|z| = {r}, arg(z) = {angle_text}.",
        "taskLatex": f"z={sp.latex(z_val)}",
        "solutionLatex": (
            f"|z|={r},\\ \\arg(z)={angle_latex},\\ "
            f"z={r}\\left(\\cos\\left({sp.latex(angle)}\\right)+i\\sin\\left({sp.latex(angle)}\\right)\\right)"
            f"={r}\\left({sp.latex(c + sp.I * s)}\\right)"
        ),
        "steps": trig_steps(z_val, r, c, s, angle_latex),
    }


def trig_steps(z_val: sp.Expr, r: int, c: sp.Expr, s: sp.Expr, angle_latex: str) -> list[dict[str, str]]:
    a, b = sp.re(z_val), sp.im(z_val)
    square = sp.expand(a**2 + b**2)
    assert sp.sqrt(square) == r
    quadrant = {(True, True): "I", (False, True): "II", (False, False): "III", (True, False): "IV"}[(a >= 0, b >= 0)]
    return [
        step("Część rzeczywista i urojona", f"a=\\operatorname{{Re}}z={sp.latex(a)},\\ b=\\operatorname{{Im}}z={sp.latex(b)}"),
        step("Moduł", f"|z|=\\sqrt{{a^2+b^2}}=\\sqrt{{{sp.latex(square)}}}={r}"),
        step(
            f"Cosinus i sinus argumentu (punkt leży w ćwiartce {quadrant})",
            f"\\cos\\varphi=\\frac{{a}}{{|z|}}={sp.latex(c)},\\ \\sin\\varphi=\\frac{{b}}{{|z|}}={sp.latex(s)}"
            f"\\ \\Rightarrow\\ \\varphi={angle_latex}",
        ),
    ]



# --- Zadanie 3: Obszar na płaszczyźnie zespolonej (backend/tasks/task_03_obszar_plaszczyzny_zespolonej.py) ---

DEFINITION_03 = {
    "id": 3,
    "name": "Obszar na płaszczyźnie zespolonej",
    "area": "Liczby zespolone",
    "example": "Przykład: |z-(1-2i)| <= 3.",
    "description": "Dany jest warunek na liczbę zespoloną z (moduł, część rzeczywista lub urojona, iloraz modułów). Trzeba opisać zbiór punktów spełniających warunek i naszkicować go na płaszczyźnie zespolonej.",
    "parameterHelp": "Ustaw maksymalny promień oraz granice położenia środka.",
    "examInstruction": "Opisz i naszkicuj na płaszczyźnie zespolonej zbiór liczb z spełniających podane warunki.",
    "params": [
        {"key": "radiusMax", "label": "Maksymalny promień", "type": "number", "min": 1, "max": 20, "value": 6},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współrzędnej środka", "type": "number", "min": -30, "max": 0, "value": -10},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współrzędnej środka", "type": "number", "min": 0, "max": 30, "value": 10},
    ],
}


def region_svg(kind: str, **kwargs: Any) -> str:
    if kind in ("disk", "outside"):
        cx, cy, r = kwargs["cx"], kwargs["cy"], kwargs["r"]
        op = "<=" if kind == "disk" else ">="
        return render_region_svg(
            f"Abs(z-({cx}+({cy})*I)){op}{r}",
            focus=[(cx - r, cy - r), (cx + r, cy + r)],
            marks_x=[(cx, cy), (cx - r, cy), (cx + r, cy)],
            marks_y=[(cy, cx), (cy - r, cx), (cy + r, cx)],
            points=[(cx, cy, "S")],
        )
    if kind == "halfplane_re":
        c = kwargs["c"]
        d = abs(c) + 2
        return render_region_svg(
            f"Re(z){kwargs['sign']}{c}", focus=[(c - d, -d), (c + d, d)], marks_x=[(c, None)]
        )
    if kind == "strip_im":
        low, up = kwargs["low"], kwargs["up"]
        mid = (low + up) / 2
        d = max(abs(mid), (up - low) / 2) + 2
        return render_region_svg(
            f"{low}<=Im(z)<={up}", focus=[(-d, mid - d), (d, mid + d)], marks_y=[(low, None), (up, None)]
        )
    raise ValueError("Nieobsługiwany rodzaj obszaru.")


_RX, _RY = sp.symbols("x y", real=True)
_RZ = sp.Symbol("z")
_REL_LATEX = {"=": "=", "<=": "\\le", ">=": "\\ge", "<": "<", ">": ">"}
_REL_FLIP = {"=": "=", "<=": ">=", ">=": "<=", "<": ">", ">": "<"}


def zshift_text(a: sp.Expr) -> str:
    """Zapis z-a dla całkowitego a (np. z-3-2i); ten sam ciąg pasuje do tekstu i do LaTeX-a."""
    real, imag = int(-sp.re(a)), int(-sp.im(a))
    text = "z"
    if real:
        text += f"{real:+d}"
    if imag:
        text += imag_term(imag)
    return text


def rel_latex(left: sp.Expr, right: sp.Expr, op: str) -> str:
    return f"{sp.latex(left)}{_REL_LATEX[op]}{sp.latex(right)}"


def normalize_poly(num: sp.Expr) -> sp.Expr:
    """Mnoży wielomian przez dodatnią stałą tak, by miał całkowite, względnie pierwsze współczynniki (znak nierówności bez zmian)."""
    poly = sp.Poly(sp.expand(num), _RX, _RY)
    coeffs = [sp.Rational(c) for c in poly.coeffs()]
    scale = sp.ilcm(*[c.q for c in coeffs]) if coeffs else 1
    scaled = [int(c * scale) for c in coeffs]
    divisor = math.gcd(*scaled) if scaled else 1
    return sp.expand(num * scale / divisor)


def describe_region(num: sp.Expr, op: str) -> str | None:
    """Opis (LaTeX) zbioru {num op 0} na płaszczyźnie; None dla przypadków zdegenerowanych."""
    poly = sp.Poly(sp.expand(num), _RX, _RY)
    a_c, b_c, c_c = poly.coeff_monomial(_RX**2), poly.coeff_monomial(_RX * _RY), poly.coeff_monomial(_RY**2)
    d_c, e_c, f_c = poly.coeff_monomial(_RX), poly.coeff_monomial(_RY), poly.coeff_monomial(1)
    border = "bez brzegu" if op in ("<", ">") else "wraz z brzegiem"

    if poly.total_degree() > 2:
        return None

    if a_c == 0 and b_c == 0 and c_c == 0:
        if d_c == 0 and e_c == 0:
            return None
        if e_c == 0:
            rhs, side = -f_c / d_c, (op if d_c > 0 else _REL_FLIP[op])
            line, half = rel_latex(_RX, rhs, "="), rel_latex(_RX, rhs, side)
        else:
            rhs, side = (-d_c * _RX - f_c) / e_c, (op if e_c > 0 else _REL_FLIP[op])
            line, half = rel_latex(_RY, rhs, "="), rel_latex(_RY, rhs, side)
        if op == "=":
            return f"\\text{{Prosta }}{line}"
        return f"\\text{{Półpłaszczyzna }}{half}\\text{{ ({border})}}"

    if b_c == 0 and a_c == c_c:
        cx, cy = -d_c / (2 * a_c), -e_c / (2 * a_c)
        r2 = cx**2 + cy**2 - f_c / a_c
        if r2 <= 0:
            return None
        center = f"\\left({sp.latex(cx)},{sp.latex(cy)}\\right)"
        radius = sp.latex(sp.sqrt(r2))
        effective = op if a_c > 0 else _REL_FLIP[op]
        if op == "=":
            return f"\\text{{Okrąg o środku }}{center}\\text{{ i promieniu }}{radius}"
        if effective in ("<=", "<"):
            return f"\\text{{Koło o środku }}{center}\\text{{ i promieniu }}{radius}\\text{{ ({border})}}"
        return f"\\text{{Zewnętrze okręgu o środku }}{center}\\text{{ i promieniu }}{radius}\\text{{ ({border})}}"

    disc = b_c**2 - 4 * a_c * c_c
    if disc < 0:
        return None
    factors = sp.factor_list(sp.expand(num))[1]
    if sum(m for _, m in factors) == 2 and all(sp.Poly(f, _RX, _RY).total_degree() == 1 for f, _ in factors):
        kind = "dwie proste"
    else:
        kind = "hiperbola" if disc > 0 else "parabola"
    equation = f"{sp.latex(sp.expand(num))}{_REL_LATEX[op]}0"
    if op == "=":
        return f"\\text{{Krzywa: {kind}, }}{equation}"
    return f"\\text{{Obszar wyznaczony przez krzywą ({kind}): }}{equation}\\text{{ ({border})}}"


def complex_plane_ratio_example(low: int, high: int) -> dict[str, Any]:
    span = max(2, min(6, abs(low), abs(high)))
    for _ in range(50):
        a = random.randint(-span, span) + sp.I * random.randint(-span, span)
        b = random.randint(-span, span) + sp.I * random.randint(-span, span)
        if a == b:
            continue
        k = random.choice([1, 1, 1, 2, 3, sp.Rational(1, 2), sp.Rational(1, 3), sp.Rational(3, 2), sp.Rational(2, 3)])
        op = random.choice(["=", "=", "<=", ">=", "<", ">"])
        num = normalize_poly(
            (_RX - sp.re(a)) ** 2 + (_RY - sp.im(a)) ** 2 - k**2 * ((_RX - sp.re(b)) ** 2 + (_RY - sp.im(b)) ** 2)
        )
        description = describe_region(num, op)
        if description is None:
            continue
        za, zb = zshift_text(a), zshift_text(b)
        shifts = [f"(z-({int(sp.re(v))}+({int(sp.im(v))})*I))" for v in (a, b)]
        condition = f"Abs({shifts[0]}/{shifts[1]}){op}{k}"
        try:
            image = render_region_svg(condition)
        except ValueError:
            continue
        return {
            "task": f"Opisz i naszkicuj zbiór: |({za})/({zb})| {op} {k}.",
            "solution": f"Warunek równoważny: |{za}|^2 {op} {k**2}*|{zb}|^2, z != {sp.sstr(b)}.",
            "taskLatex": f"\\left|\\frac{{{za}}}{{{zb}}}\\right|{_REL_LATEX[op]}{sp.latex(k)}",
            "solutionLatex": (
                f"z\\ne {sp.latex(b)},\\ \\left|{za}\\right|^2{_REL_LATEX[op]}{sp.latex(k**2)}\\left|{zb}\\right|^2"
                f"\\iff {sp.latex(num)}{_REL_LATEX[op]}0,\\quad {description}"
            ),
            "solutionImage": image,
        }
    raise ValueError("Nie udało się wylosować zadania z ilorazem modułów.")


def complex_plane_part_example(low: int, high: int) -> dict[str, Any]:
    span = max(2, min(5, abs(low), abs(high)))
    templates = [
        lambda a, b: _RZ**2 + a,
        lambda a, b: _RZ**2 + a * _RZ,
        lambda a, b: _RZ**2 + a * _RZ + b,
        lambda a, b: (_RZ - a) ** 2,
        lambda a, b: sp.I * _RZ**2,
        lambda a, b: 1 / _RZ,
        lambda a, b: 1 / (_RZ - a),
        lambda a, b: (_RZ - a) / (_RZ - b),
    ]
    probes = [(0.3, 0.7), (1.1, -2.2), (-3.4, 0.5), (2.6, 3.1)]
    for _ in range(50):
        a = random.choice([v for v in range(-span, span + 1) if v])
        b = random.choice([v for v in range(-span, span + 1) if v and v != a])
        expr = random.choice(templates)(a, b)
        part_name = random.choice(["Re", "Im"])
        c = random.randint(-4, 4)
        op = random.choice(["=", "=", "<=", ">=", "<", ">"])
        value = _RX + sp.I * _RY
        f_xy = sp.expand(expr.subs(_RZ, value)) if expr.is_polynomial(_RZ) else expr.subs(_RZ, value)
        part = sp.simplify(sp.re(f_xy) if part_name == "Re" else sp.im(f_xy))
        num, den = sp.fraction(sp.together(part - c))
        signs = {math.copysign(1, float(den.subs({_RX: px, _RY: py}))) for px, py in probes}
        if len(signs) != 1:
            continue
        if signs == {-1.0}:
            num = -num
        num = normalize_poly(num)
        description = describe_region(num, op)
        if description is None:
            continue
        expr_text = sp.sstr(expr)
        try:
            image = render_region_svg(f"{part_name}({expr_text}){op}{c}")
        except ValueError:
            continue
        shown = expr_text.replace("**", "^").replace("*", "").replace("I", "i")
        name_latex = f"\\operatorname{{{part_name}}}"
        poles = sp.solve(sp.denom(sp.together(expr)), _RZ) if den != 1 else []
        pole = "" if not poles else ",\\ z\\ne " + ",".join(sp.latex(p) for p in poles)
        return {
            "task": f"Opisz i naszkicuj obszar: {part_name}({shown}) {op} {c}.",
            "solution": f"Dla z=x+iy: {part_name}({shown}) = {sp.sstr(part)}; warunek: {sp.sstr(sp.expand(num))} {op} 0.",
            "taskLatex": f"{name_latex}\\left({sp.latex(expr)}\\right){_REL_LATEX[op]}{c}",
            "solutionLatex": (
                f"z=x+iy,\\ {name_latex}\\left({sp.latex(expr)}\\right)={sp.latex(part)}\\iff "
                f"{sp.latex(sp.expand(num))}{_REL_LATEX[op]}0{pole},\\quad {description}"
            ),
            "solutionImage": image,
        }
    raise ValueError("Nie udało się wylosować zadania z Re/Im wyrażenia.")


_OPS4 = ["<", "<=", ">", ">="]


def _center_text(cx: int, cy: int) -> str:
    return f"({cx},{cy})"


def _region_atom_disk(cx: int, cy: int, r: int, op: str) -> dict[str, Any]:
    zc = z_minus_center(cx, cy)
    names = {
        "<": "wnętrze koła (bez brzegu)",
        "<=": "koło domknięte",
        ">": "zewnętrze okręgu (bez brzegu)",
        ">=": "zewnętrze okręgu wraz z brzegiem",
    }
    return {
        "parse": f"Abs(z-({cx}+({cy})*I)){op}{r}",
        "tex": f"\\left|{zc}\\right|{_REL_LATEX[op]}{r}",
        "plain": f"|{zc}| {op} {r}",
        "desc": f"{names[op]} o środku {_center_text(cx, cy)} i promieniu {r}",
        "rels": 1,
    }


def _region_atom_ring() -> dict[str, Any]:
    cx, cy = random.randint(-3, 3), random.randint(-3, 3)
    r1, r2 = sorted(random.sample(range(1, 5), 2))
    inner = _region_atom_disk(cx, cy, r1, random.choice([">", ">="]))
    outer = _region_atom_disk(cx, cy, r2, random.choice(["<", "<="]))
    return {
        "parse": f"{inner['parse']}; {outer['parse']}",
        "tex": f"{inner['tex']},\\quad {outer['tex']}",
        "plain": f"{inner['plain']}, {outer['plain']}",
        "desc": f"pierścień o środku {_center_text(cx, cy)} i promieniach {r1} oraz {r2}",
        "rels": 2,
    }


def _region_atom_part(name: str) -> dict[str, Any]:
    c, op = random.randint(-4, 4), random.choice(_OPS4)
    side = {"Re": ("na lewo od", "na prawo od"), "Im": ("poniżej", "powyżej")}[name]
    where = side[0] if op in ("<", "<=") else side[1]
    border = "bez brzegu" if op in ("<", ">") else "wraz z brzegiem"
    return {
        "parse": f"{name}(z){op}{c}",
        "tex": f"\\operatorname{{{name}}}(z){_REL_LATEX[op]}{c}",
        "plain": f"{name}(z) {op} {c}",
        "desc": f"półpłaszczyzna {where} prostej {name}(z)={c} ({border})",
        "rels": 1,
    }


def _region_atom_strip() -> dict[str, Any]:
    name = random.choice(["Re", "Im"])
    lo, up = sorted(random.sample(range(-4, 5), 2))
    op1, op2 = random.choice(["<", "<="]), random.choice(["<", "<="])
    kind = "pionowy" if name == "Re" else "poziomy"
    return {
        "parse": f"{lo}{op1}{name}(z){op2}{up}",
        "tex": f"{lo}{_REL_LATEX[op1]}\\operatorname{{{name}}}(z){_REL_LATEX[op2]}{up}",
        "plain": f"{lo} {op1} {name}(z) {op2} {up}",
        "desc": f"pas {kind} między prostymi {name}(z)={lo} i {name}(z)={up}",
        "rels": 2,
    }


def _region_atom_sector() -> dict[str, Any]:
    a, b = random.randint(-2, 2), random.randint(-2, 2)
    d = random.choice([3, 4, 6])
    while True:
        k1, k2 = sorted(random.sample(range(-d + 1, d), 2))
        if k2 - k1 <= d:
            break
    al, be = sp.pi * sp.Rational(k1, d), sp.pi * sp.Rational(k2, d)
    op1, op2 = random.choice(["<", "<="]), random.choice(["<", "<="])
    zc = z_minus_center(a, b)
    return {
        "parse": f"({k1})*pi/{d}{op1}arg(z-({a}+({b})*I)){op2}({k2})*pi/{d}",
        "tex": f"{sp.latex(al)}{_REL_LATEX[op1]}\\arg\\left({zc}\\right){_REL_LATEX[op2]}{sp.latex(be)}",
        "plain": f"{sp.sstr(al)} {op1} arg({zc}) {op2} {sp.sstr(be)}",
        "desc": f"kąt (wycinek płaszczyzny) o wierzchołku {_center_text(a, b)}",
        "rels": 2,
    }


def _region_atom_bisector() -> dict[str, Any]:
    while True:
        pa, pb, qa, qb = (random.randint(-3, 3) for _ in range(4))
        if (pa, pb) != (qa, qb):
            break
    op = random.choice(["<", "<=", ">", ">="])
    zp, zq = z_minus_center(pa, pb), z_minus_center(qa, qb)
    closer = (pa, pb) if op in ("<", "<=") else (qa, qb)
    other = (qa, qb) if op in ("<", "<=") else (pa, pb)
    border = "bez brzegu" if op in ("<", ">") else "wraz z brzegiem"
    return {
        "parse": f"Abs(z-({pa}+({pb})*I)){op}Abs(z-({qa}+({qb})*I))",
        "tex": f"\\left|{zp}\\right|{_REL_LATEX[op]}\\left|{zq}\\right|",
        "plain": f"|{zp}| {op} |{zq}|",
        "desc": (
            f"półpłaszczyzna punktów bliższych {_center_text(*closer)} niż {_center_text(*other)} "
            f"(symetralna odcinka, {border})"
        ),
        "rels": 1,
    }


def complex_plane_multi_example(count: int | None = None) -> dict[str, Any]:
    """Obszar zadany jednocześnie 2–3 warunkami (część wspólna zbiorów)."""
    target = count or random.choice([2, 3])
    sample_points = [(i * 0.25 - 8, j * 0.25 - 8) for i in range(65) for j in range(65)]
    for _ in range(200):
        atoms: list[dict[str, Any]] = []
        used = 0
        while used < target:
            room = target - used
            kinds = [(lambda: _region_atom_disk(random.randint(-3, 3), random.randint(-3, 3),
                                                random.randint(1, 4), random.choice(_OPS4)), 4),
                     (lambda: _region_atom_part("Re"), 2), (lambda: _region_atom_part("Im"), 2),
                     (_region_atom_bisector, 1)]
            if room >= 2:
                kinds += [(_region_atom_ring, 2), (_region_atom_strip, 1), (_region_atom_sector, 2)]
            factories, weights = zip(*kinds)
            atom = random.choices(factories, weights=weights)[0]()
            atoms.append(atom)
            used += atom["rels"]
        random.shuffle(atoms)
        try:
            relations = parse_conditions("; ".join(a["parse"] for a in atoms))
        except ValueError:
            continue
        holds = [[rel.holds(rel.value(x, y)) for x, y in sample_points] for rel in relations]
        inside = [all(col) for col in zip(*holds)]
        share = sum(inside) / len(inside)
        if not 0.01 <= share <= 0.7:
            continue
        redundant = False
        for k in range(len(relations)):
            without = sum(
                all(h for m, h in enumerate(col) if m != k) and not col[k] for col in zip(*holds)
            )
            if without < 15:
                redundant = True
                break
        if redundant:
            continue
        try:
            image = render_region_svg(relations)
        except ValueError:
            continue
        return {
            "task": (
                "Zaznacz na płaszczyźnie zespolonej zbiór liczb z spełniających jednocześnie warunki: "
                + "; ".join(f"({n}) {a['plain']}" for n, a in enumerate(atoms, start=1)) + "."
            ),
            "solution": "Część wspólna: " + "; ".join(a["desc"] for a in atoms) + ".",
            "taskLatex": (
                "\\begin{cases}"
                + "\\\\".join(a["tex"].replace(",\\quad ", "\\\\") for a in atoms)
                + "\\end{cases}"
            ),
            "solutionLatex": "\\text{Część wspólna: " + "; ".join(a["desc"] for a in atoms) + "}",
            "solutionImage": image,
        }
    raise ValueError("Nie udało się wylosować zadania z kilkoma warunkami.")


def generate_03(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    cx, cy = random.randint(low, high), random.randint(low, high)
    radius = random.randint(1, max(1, rint(config, "radiusMax", 6)))
    if random.random() < 0.4:
        return complex_plane_multi_example()
    variant = random.randint(1, 6)
    if variant == 5:
        return complex_plane_ratio_example(low, high)
    if variant == 6:
        return complex_plane_part_example(low, high)
    if variant == 1:
        return {
            "task": f"Opisz i naszkicuj obszar: |{z_minus_center(cx, cy)}| <= {radius}.",
            "solution": "Jest to domknięte koło o podanym środku i promieniu.",
            "taskLatex": f"\\left|{z_minus_center(cx, cy)}\\right|\\le {radius}",
            "solutionLatex": f"\\text{{Koło domknięte o środku }}({cx},{cy})\\text{{ i promieniu }}{radius}",
            "solutionImage": region_svg("disk", cx=cx, cy=cy, r=radius),
        }
    if variant == 2:
        return {
            "task": f"Opisz i naszkicuj obszar: |{z_minus_center(cx, cy)}| >= {radius}.",
            "solution": "To obszar na zewnątrz (z brzegiem) okręgu.",
            "taskLatex": f"\\left|{z_minus_center(cx, cy)}\\right|\\ge {radius}",
            "solutionLatex": f"\\text{{Zewnętrze okręgu o środku }}({cx},{cy})",
            "solutionImage": region_svg("outside", cx=cx, cy=cy, r=radius),
        }
    if variant == 3:
        c = random.randint(low, high)
        sign = random.choice([">=", "<="])
        return {
            "task": f"Opisz i naszkicuj obszar: Re(z) {sign} {c}.",
            "solution": "To półpłaszczyzna ograniczona prostą pionową.",
            "taskLatex": f"\\operatorname{{Re}}(z){'\\ge' if sign == '>=' else '\\le'}{c}",
            "solutionLatex": "\\text{Półpłaszczyzna ograniczona prostą }\\operatorname{Re}(z)=\\text{const}",
            "solutionImage": region_svg("halfplane_re", c=c, sign=sign),
        }
    lo = random.randint(low, max(low, high - 1))
    up = random.randint(lo + 1, high)
    return {
        "task": f"Opisz i naszkicuj obszar: {lo} <= Im(z) <= {up}.",
        "solution": "To poziomy pas między dwiema prostymi.",
        "taskLatex": f"{lo}\\le\\operatorname{{Im}}(z)\\le{up}",
        "solutionLatex": "\\text{Poziomy pas między prostymi }\\operatorname{Im}(z)=\\text{const}",
        "solutionImage": region_svg("strip_im", low=lo, up=up),
    }



# --- Zadanie 4: Macierz o zadanym wyznaczniku (backend/tasks/task_04_wyznacznik.py) ---

DEFINITION_04 = {
    "id": 4,
    "name": "Macierz o zadanym wyznaczniku",
    "area": "Macierze",
    "example": "Przykład: oblicz det([[2,1],[5,3]]).",
    "description": "Dana jest macierz kwadratowa A, wylosowana tak, by miała ustalony wyznacznik. Trzeba obliczyć det(A).",
    "parameterHelp": "Ustaw rozmiar i granice elementów.",
    "examInstruction": "",
    "params": [
        {"key": "size", "label": "Rozmiar macierzy (n x n)", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość elementu", "type": "number", "min": -30, "max": 0, "value": -8},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość elementu", "type": "number", "min": 0, "max": 30, "value": 8},
    ],
}


def generate_04(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, rint(config, "size", 3))
    A = sp.Matrix([[random.randint(low, high) for _ in range(n)] for _ in range(n)])
    det_a = sp.simplify(A.det())
    return {
        "task": f"Dla macierzy A={mat_text(A)} oblicz det(A).",
        "solution": f"det(A) = {det_a}.",
        "taskLatex": f"A={mat_latex(A)},\\ \\text{{oblicz }}\\det(A)",
        "solutionLatex": f"\\det(A)={sp.latex(det_a)}",
        "steps": det_steps(A, det_a),
    }


def _factor(value: sp.Expr) -> str:
    return f"\\left({sp.latex(value)}\\right)" if value < 0 else sp.latex(value)


def det_steps(A: sp.Matrix, det_a: sp.Expr) -> list[dict[str, str]]:
    """2x2: ad-bc; 3x3: reguła Sarrusa; większe: postać trójkątna (w_r -> w_r - c*w_p nie zmienia wyznacznika)."""
    if A.rows == 2:
        return [step(
            "Wyznacznik macierzy 2x2: ad - bc",
            f"\\det(A)={_factor(A[0, 0])}\\cdot {_factor(A[1, 1])}-{_factor(A[0, 1])}\\cdot {_factor(A[1, 0])}"
            f"={sp.latex(det_a)}",
        )]
    if A.rows == 3:
        diag = [(0, 1, 2), (1, 2, 0), (2, 0, 1)]
        anti = [(2, 1, 0), (0, 2, 1), (1, 0, 2)]
        plus = [A[0, a] * A[1, b] * A[2, c] for a, b, c in diag]
        minus = [A[0, a] * A[1, b] * A[2, c] for a, b, c in anti]
        products = lambda triples: "+".join(  # noqa: E731
            "\\cdot ".join(_factor(A[r, col]) for r, col in enumerate(t)) for t in triples
        )
        assert sum(plus) - sum(minus) == det_a
        return [
            step("Reguła Sarrusa: iloczyny wzdłuż przekątnych (z plusem) i antyprzekątnych (z minusem)",
                 f"\\det(A)={products(diag)}-\\left({products(anti)}\\right)"),
            step("Wartości iloczynów",
                 "\\det(A)=" + "+".join(_factor(v) for v in plus) + "-\\left(" + "+".join(_factor(v) for v in minus)
                 + f"\\right)={sp.latex(sum(plus))}-\\left({sp.latex(sum(minus))}\\right)={sp.latex(det_a)}"),
        ]
    upper, steps, swaps = elimination_steps(A)
    steps.insert(0, step("Macierz początkowa", f"A={matrix_latex(A)}"))
    diagonal = [upper[i, i] for i in range(A.rows)]
    product = "\\cdot ".join(_factor(v) for v in diagonal)
    sign = "" if swaps % 2 == 0 else "-"
    note = f" (liczba zamian wierszy: {swaps}, każda zmienia znak)" if swaps else ""
    if any(v == 0 for v in diagonal):
        note += "; zero na przekątnej, więc wyznacznik jest równy 0"
    steps.append(step(f"Iloczyn elementów na przekątnej{note}", f"\\det(A)={sign}{product}={sp.latex(det_a)}"))
    assert sp.simplify((-1) ** swaps * sp.prod(diagonal) - det_a) == 0
    return steps



# --- Zadanie 5: Macierz o zadanym rzędzie (backend/tasks/task_05_rzad_macierzy.py) ---

DEFINITION_05 = {
    "id": 5,
    "name": "Macierz o zadanym rzędzie",
    "area": "Macierze",
    "example": "Przykład: wyznacz rząd [[1,2],[2,4]].",
    "description": "Dana jest macierz A, wylosowana tak, by miała ustalony rząd. Trzeba wyznaczyć jej rząd.",
    "parameterHelp": "Ustaw liczbę wierszy, kolumn i granice elementów.",
    "examInstruction": "",
    "params": [
        {"key": "rows", "label": "Liczba wierszy", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "cols", "label": "Liczba kolumn", "type": "number", "min": 2, "max": 5, "value": 4},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość elementu", "type": "number", "min": -30, "max": 0, "value": -8},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość elementu", "type": "number", "min": 0, "max": 30, "value": 8},
    ],
}


def generate_05(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    rows = max(2, rint(config, "rows", 3))
    cols = max(2, rint(config, "cols", 4))
    A = sp.Matrix([[random.randint(low, high) for _ in range(cols)] for _ in range(rows)])
    rank = int(A.rank())
    echelon, steps, _ = elimination_steps(A, fraction_free=True)
    nonzero = sum(1 for r in range(echelon.rows) if any(v != 0 for v in echelon.row(r)))
    assert nonzero == rank
    steps.insert(0, step("Macierz początkowa", f"A={matrix_latex(A)}"))
    steps.append(
        step("Rząd = liczba niezerowych wierszy w postaci schodkowej", f"\\operatorname{{rz}}(A)={rank}")
    )
    return {
        "task": f"Wyznacz rząd macierzy A={mat_text(A)}.",
        "solution": f"Rząd macierzy wynosi rz(A) = {rank}.",
        "taskLatex": f"A={mat_latex(A)},\\ \\text{{wyznacz }}\\operatorname{{rz}}(A)",
        "solutionLatex": f"\\operatorname{{rz}}(A)={rank}",
        "steps": steps,
    }



# --- Zadanie 6: Wartości i wektory własne macierzy symetrycznej (backend/tasks/task_06_wartosci_wlasne_symetryczna.py) ---

DEFINITION_06 = {
    "id": 6,
    "name": "Wartości i wektory własne macierzy symetrycznej",
    "area": "Wartości własne i diagonalizacja",
    "example": "Przykład: A=[[4,1],[1,3]], policz wartości własne.",
    "description": "Dana jest macierz symetryczna A. Trzeba wyznaczyć jej wartości własne i odpowiadające im wektory własne.",
    "parameterHelp": "Ustaw rozmiar i granice elementów.",
    "examInstruction": "",
    "params": [
        {"key": "size", "label": "Rozmiar macierzy", "type": "number", "min": 2, "max": 4, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość elementu", "type": "number", "min": -20, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość elementu", "type": "number", "min": 0, "max": 20, "value": 6},
    ],
}


def generate_06(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(4, rint(config, "size", 3)))
    M = sp.Matrix([[random.randint(low, high) for _ in range(n)] for _ in range(n)])
    A = (M + M.T) / 2
    eig = A.eigenvects()
    # Pierwiastki wielomianu stopnia 3-4 bywają ogromnymi wyrażeniami (wzory Cardana) - wtedy przybliżenia.
    messy = any(sp.count_ops(val) > 12 for val, _, _ in eig)
    approx = (lambda e: e.evalf(5, chop=True)) if messy else (lambda e: e)
    rel_text, rel_latex = ("≈", "\\approx") if messy else ("=", "=")
    eig_text, eig_latex = [], []
    for val, mult, vectors in eig:
        shown = [v.applyfunc(approx) for v in vectors]
        vec_texts = ", ".join(sp.sstr(v) for v in shown)
        eig_text.append(f"λ{rel_text}{sp.sstr(approx(val))} (krotność {mult}), wektory: {vec_texts}")
        multiplicity = f"\\ (\\text{{krotność }}{mult})" if mult > 1 else ""
        basis = ",\\ ".join(matrix_latex(v) for v in shown)
        eig_latex.append(
            f"\\lambda{rel_latex}{sp.latex(approx(val))}{multiplicity}:\\ v\\in\\operatorname{{span}}\\left\\{{{basis}\\right\\}}"
        )
    return {
        "task": f"Wyznacz wartości i wektory własne macierzy symetrycznej A={mat_text(A)}.",
        "solution": "; ".join(eig_text),
        "taskLatex": f"A={mat_latex(A)},\\ \\text{{wyznacz wartości i wektory własne}}",
        # \qquad - wrapMath (eksport .tex) łamie długie wzory właśnie w tym miejscu.
        "solutionLatex": ";\\qquad ".join(eig_latex),
        "steps": eigen_steps(A, eig),
    }



# --- Zadanie 7: Cztery przestrzenie fundamentalne macierzy (backend/tasks/task_07_przestrzenie_fundamentalne.py) ---

DEFINITION_07 = {
    "id": 7,
    "name": "Cztery przestrzenie fundamentalne macierzy",
    "area": "Przestrzenie liniowe",
    "example": "Przykład: znajdź Col(A), Row(A), N(A), N(A^T).",
    "description": "Dana jest macierz A. Trzeba wyznaczyć bazy czterech przestrzeni fundamentalnych: przestrzeni kolumn Col(A), wierszy Row(A), jądra N(A) i jądra macierzy transponowanej N(Aᵀ).",
    "parameterHelp": "Ustaw liczbę wierszy, kolumn, wymiar Col(A) i Row(A) (rząd) oraz granice elementów. Wymiary N(A) i N(A^T) wynikają z rzędu.",
    "examInstruction": "Wyznacz bazy przestrzeni Col(A), Row(A), N(A) i N(A^T).",
    "params": [
        {"key": "rows", "label": "Liczba wierszy", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "cols", "label": "Liczba kolumn", "type": "number", "min": 2, "max": 5, "value": 4},
        {"key": "rank", "label": "Wymiar Col(A) i Row(A) (rząd)", "type": "number", "min": 1, "max": 5, "value": 2},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość elementu", "type": "number", "min": -20, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość elementu", "type": "number", "min": 0, "max": 20, "value": 6},
    ],
}


def random_matrix_of_rank(rows: int, cols: int, rank: int, low: int, high: int) -> sp.Matrix:
    rank = max(1, min(rank, rows, cols))
    full = rank == min(rows, cols)
    small_low, small_high = max(low, -3), min(high, 3)
    if small_low >= small_high:
        small_low, small_high = low, high
    candidate = None
    for _ in range(600):
        if full:
            A = sp.Matrix([[random.randint(low, high) for _ in range(cols)] for _ in range(rows)])
        else:
            B = sp.Matrix([[random.randint(small_low, small_high) for _ in range(rank)] for _ in range(rows)])
            C = sp.Matrix([[random.randint(small_low, small_high) for _ in range(cols)] for _ in range(rank)])
            A = B * C
        if A.rank() != rank:
            continue
        candidate = A
        if all(low <= v <= high for v in A):
            return A
    if candidate is not None:
        return candidate
    A = sp.zeros(rows, cols)
    for i in range(rank):
        A[i, i] = 1
    return A


def integer_basis(vectors: list[sp.Matrix]) -> list[sp.Matrix]:
    result = []
    for v in vectors:
        denominators = [int(sp.fraction(sp.together(x))[1]) for x in v]
        scaled = v * math.lcm(*denominators)
        divisor = math.gcd(*[int(x) for x in scaled])
        result.append(scaled / divisor if divisor > 1 else scaled)
    return result


def space_latex(name: str, basis: list[sp.Matrix]) -> str:
    if not basis:
        return name + "=\\left\\{\\mathbf{0}\\right\\}"
    return name + "=\\operatorname{span}\\left\\{" + ",\\ ".join(sp.latex(v) for v in basis) + "\\right\\}"


def space_text(name: str, basis: list[sp.Matrix]) -> str:
    if not basis:
        return f"{name}: {{0}} (wymiar 0)"
    return f"{name}: {', '.join(vec_text(v) for v in basis)} (wymiar {len(basis)})"


def generate_07(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    rows = max(2, rint(config, "rows", 3))
    cols = max(2, rint(config, "cols", 4))
    A = random_matrix_of_rank(rows, cols, rint(config, "rank", 2), low, high)
    col_basis = A.columnspace()
    row_basis = A.T.columnspace()
    null_basis = integer_basis(A.nullspace())
    left_null = integer_basis(A.T.nullspace())
    return {
        "task": f"Dla macierzy A={mat_text(A)} wyznacz Col(A), Row(A), N(A), N(A^T).",
        "solution": (
            f"rz(A)={len(col_basis)}. "
            f"{space_text('Col(A)', col_basis)}; "
            f"{space_text('Row(A)', row_basis)}; "
            f"{space_text('N(A)', null_basis)}; "
            f"{space_text('N(A^T)', left_null)}."
        ),
        "taskLatex": f"A={mat_latex(A)}",
        "solutionLatex": (
            f"\\operatorname{{rz}}(A)={len(col_basis)},\\ "
            + space_latex("\\operatorname{Col}(A)", col_basis) + ",\\ "
            + space_latex("\\operatorname{Row}(A)", row_basis) + ",\\ "
            + space_latex("N(A)", null_basis) + ",\\ "
            + space_latex("N(A^T)", left_null)
        ),
        "steps": fundamental_steps(A, col_basis, row_basis, null_basis, left_null),
    }


def _pivots(reduced: sp.Matrix) -> list[int]:
    return [next(c for c in range(reduced.cols) if reduced[r, c] != 0)
            for r in range(reduced.rows) if any(v != 0 for v in reduced.row(r))]


def fundamental_steps(A, col_basis, row_basis, null_basis, left_null) -> list[dict[str, str]]:
    steps = [step("Sprowadzamy A do postaci zredukowanej", f"A={matrix_latex(A)}")]
    reduced, ops, _ = elimination_steps(A, reduced=True)
    steps += ops
    pivots = _pivots(reduced)
    assert len(pivots) == len(col_basis)
    numbers = ", ".join(str(p + 1) for p in pivots)
    steps.append(step(
        f"Kolumny wiodące: {numbers}, więc rz(A)={len(pivots)}; Col(A) rozpinają kolumny A o tych numerach, "
        "a N(A) odczytujemy z postaci zredukowanej (zmienne wolne = parametry)",
        space_latex("\\operatorname{Col}(A)", col_basis) + ",\\quad " + space_latex("N(A)", null_basis),
    ))
    steps.append(step("To samo dla macierzy transponowanej", f"A^T={matrix_latex(A.T)}"))
    reduced_t, ops_t, _ = elimination_steps(A.T, reduced=True)
    steps += ops_t
    pivots_t = ", ".join(str(p + 1) for p in _pivots(reduced_t))
    steps.append(step(
        f"Row(A) = Col(A^T) rozpinają kolumny A^T (wiersze A) o numerach {pivots_t}; N(A^T) z postaci zredukowanej A^T",
        space_latex("\\operatorname{Row}(A)", row_basis) + ",\\quad " + space_latex("N(A^T)", left_null),
    ))
    return steps



# --- Zadanie 8: Układ równań metodą Cramera (backend/tasks/task_08_uklad_cramera.py) ---

DEFINITION_08 = {
    "id": 8,
    "name": "Układ równań metodą Cramera",
    "area": "Układy równań",
    "example": "Przykład: 2x+3y=7, x-y=1.",
    "description": "Dany jest układ równań liniowych z macierzą o niezerowym wyznaczniku. Trzeba go rozwiązać wzorami Cramera (przez wyznaczniki).",
    "parameterHelp": "Ustaw liczbę niewiadomych i granice współczynników.",
    "examInstruction": "Rozwiąż układ równań metodą Cramera.",
    "params": [
        {"key": "variables", "label": "Liczba niewiadomych", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współczynnika", "type": "number", "min": -30, "max": 0, "value": -8},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współczynnika", "type": "number", "min": 0, "max": 30, "value": 8},
    ],
}


def generate_08(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, rint(config, "variables", 3))
    A, b, x = generate_system(n, low, high)
    eq = [sp.Eq(sum(A[i, j] * VARS[j] for j in range(n)), b[i], evaluate=False) for i in range(n)]
    det_a = A.det()
    det_info = []
    steps = [step("Wyznacznik główny (różny od zera, więc układ jest oznaczony)",
                  f"A={matrix_latex(A)},\\ \\det(A)={sp.latex(det_a)}")]
    for i in range(n):
        Ai = A.copy()
        Ai[:, i] = b
        det_ai = Ai.det()
        det_info.append(f"{VARS[i]}={sp.simplify(det_ai / det_a)} (det(A_{i+1})={det_ai})")
        var = sp.latex(VARS[i])
        steps.append(step(
            f"Kolumnę {i + 1} zastępujemy wektorem b",
            f"A_{{{i + 1}}}={matrix_latex(Ai)},\\ \\det(A_{{{i + 1}}})={sp.latex(det_ai)},\\ "
            f"{var}=\\frac{{\\det(A_{{{i + 1}}})}}{{\\det(A)}}=\\frac{{{sp.latex(det_ai)}}}{{{sp.latex(det_a)}}}={sp.latex(x[i])}",
        ))
    return {
        "task": "Rozwiąż metodą Cramera układ:\n" + "\n".join(sp.sstr(e) for e in eq),
        "solution": f"det(A)={det_a}; " + "; ".join(det_info) + ".",
        "taskLatex": "\\begin{cases}" + " \\\\ ".join(sp.latex(e) for e in eq) + "\\end{cases}",
        "solutionLatex": f"\\det(A)={sp.latex(det_a)},\\ " + ",\\ ".join(sp.latex(sp.Eq(VARS[i], x[i])) for i in range(n)),
        "steps": steps,
    }



# --- Zadanie 9: Układ równań metodą Gaussa (backend/tasks/task_09_uklad_gaussa.py) ---

DEFINITION_09 = {
    "id": 9,
    "name": "Układ równań metodą Gaussa",
    "area": "Układy równań",
    "example": "Przykład: 3x-2y=5, 6x+y=4.",
    "description": "Dany jest układ równań liniowych. Trzeba go rozwiązać metodą eliminacji Gaussa.",
    "parameterHelp": "Ustaw liczbę niewiadomych i granice współczynników.",
    "examInstruction": "Rozwiąż układ równań metodą eliminacji Gaussa.",
    "params": [
        {"key": "variables", "label": "Liczba niewiadomych", "type": "number", "min": 2, "max": 6, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współczynnika", "type": "number", "min": -30, "max": 0, "value": -8},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współczynnika", "type": "number", "min": 0, "max": 30, "value": 8},
    ],
}


def generate_09(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, rint(config, "variables", 3))
    A, b, x = generate_system(n, low, high)
    eq = [sp.Eq(sum(A[i, j] * VARS[j] for j in range(n)), b[i], evaluate=False) for i in range(n)]
    aug = A.row_join(b)
    rref, _ = aug.rref()
    reduced, steps, _ = elimination_steps(aug, reduced=True, split=n, pivot_cols=n)
    assert reduced == rref
    steps.insert(0, step("Macierz rozszerzona układu", f"[A|b]={matrix_latex(aug, n)}"))
    steps.append(step(
        "Z postaci zredukowanej odczytujemy rozwiązanie",
        ",\\ ".join(f"{sp.latex(VARS[i])}={sp.latex(x[i])}" for i in range(n)),
    ))
    return {
        "task": "Rozwiąż metodą Gaussa układ:\n" + "\n".join(sp.sstr(e) for e in eq),
        "solution": f"Postać zredukowana [A|b]: {mat_text(rref)}. Rozwiązanie: {vec_text(x)}.",
        "taskLatex": "\\begin{cases}" + " \\\\ ".join(sp.latex(e) for e in eq) + "\\end{cases}",
        "solutionLatex": f"[A|b]_{{rref}}={sp.latex(rref)},\\ x={vec_latex(x)}",
        "steps": steps,
    }



# --- Zadanie 10: Układ równań metodą Choleskiego (backend/tasks/task_10_uklad_choleskiego.py) ---

DEFINITION_10 = {
    "id": 10,
    "name": "Układ równań metodą Choleskiego",
    "area": "Układy równań",
    "example": "Przykład: Ax=b dla A dodatnio określonej.",
    "description": "Dany jest układ Ax=b z macierzą A symetryczną i dodatnio określoną. Trzeba go rozwiązać przez rozkład A=LLᵀ (metoda Choleskiego).",
    "parameterHelp": "Ustaw rozmiar i granice współczynników.",
    "examInstruction": "Rozwiąż układ równań Ax=b metodą Choleskiego.",
    "params": [
        {"key": "size", "label": "Rozmiar macierzy", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współczynnika", "type": "number", "min": -12, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współczynnika", "type": "number", "min": 0, "max": 12, "value": 6},
    ],
}


def generate_10(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, rint(config, "size", 3))
    L = sp.Matrix(
        [
            [random.randint(2, 4) if i == j else (random.randint(max(-2, low), min(2, high)) if j < i else 0) for j in range(n)]
            for i in range(n)
        ]
    )
    A = L * L.T
    x = sp.Matrix([random.randint(low, high) or 1 for _ in range(n)])
    b = A * x
    y = L.lower_triangular_solve(b)
    x_calc = L.T.upper_triangular_solve(y)
    eq = [sp.Eq(sum(A[i, j] * VARS[j] for j in range(n)), b[i], evaluate=False) for i in range(n)]
    return {
        "task": "Rozwiąż metodą Choleskiego układ:\n" + "\n".join(sp.sstr(e) for e in eq),
        "solution": f"L={mat_text(L)}, y={vec_text(y)}, x={vec_text(x_calc)}.",
        "taskLatex": f"A={sp.latex(A)},\\ b={sp.latex(b)}",
        "solutionLatex": f"L={sp.latex(L)},\\ y={sp.latex(y)},\\ x={sp.latex(x_calc)}",
    }



# --- Zadanie 11: Ortogonalizacja Grama-Schmidta (backend/tasks/task_11_gram_schmidt.py) ---

DEFINITION_11 = {
    "id": 11,
    "name": "Ortogonalizacja Grama-Schmidta",
    "area": "Przestrzenie liniowe",
    "example": "Przykład: v1=(1,1), v2=(1,-1).",
    "description": "Dane są wektory. Trzeba je zortogonalizować metodą Grama-Schmidta, czyli wyznaczyć układ wektorów ortogonalnych rozpinający tę samą przestrzeń.",
    "parameterHelp": "Ustaw liczbę wektorów, wymiar i granice współrzędnych.",
    "examInstruction": "Zortogonalizuj podane wektory metodą Grama-Schmidta.",
    "params": [
        {"key": "vectors", "label": "Liczba wektorów", "type": "number", "min": 2, "max": 4, "value": 3},
        {"key": "dimension", "label": "Wymiar przestrzeni", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współrzędnej", "type": "number", "min": -12, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współrzędnej", "type": "number", "min": 0, "max": 12, "value": 6},
    ],
}


def generate_11(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    vectors = max(2, rint(config, "vectors", 3))
    dim = max(vectors, rint(config, "dimension", 3))
    raw = [sp.Matrix([random.randint(low, high) for _ in range(dim)]) for _ in range(vectors)]
    ortho = sp.GramSchmidt(raw, orthonormal=False)
    return {
        "task": "Zastosuj Grama-Schmidta do wektorów: " + ", ".join(sp.sstr(v) for v in raw) + ".",
        "solution": "Baza ortogonalna: " + ", ".join(sp.sstr(v) for v in ortho) + ".",
        "taskLatex": "\\text{Wektory: }" + ",\\ ".join(sp.latex(v) for v in raw),
        "solutionLatex": "\\text{Baza ortogonalna: }" + ",\\ ".join(sp.latex(v) for v in ortho),
        "steps": gram_schmidt_steps(raw, ortho),
    }


def gram_schmidt_steps(raw: list[sp.Matrix], ortho: list[sp.Matrix]) -> list[dict[str, str]]:
    """u_k = v_k - sum_j <v_k,u_j>/<u_j,u_j> u_j, z wartościami iloczynów skalarnych."""
    steps = [step("Pierwszy wektor bez zmian", f"u_1=v_1={matrix_latex(raw[0])}")]
    us = [raw[0]]
    for k in range(1, len(raw)):
        v = raw[k]
        terms_formula, terms_values = [], []
        u = v
        for j, uj in enumerate(us, start=1):
            coeff = v.dot(uj) / uj.dot(uj)
            u = u - coeff * uj
            terms_formula.append(f"\\frac{{\\langle v_{{{k + 1}}},u_{{{j}}}\\rangle}}{{\\langle u_{{{j}}},u_{{{j}}}\\rangle}}u_{{{j}}}")
            terms_values.append(f"\\left({sp.latex(coeff)}\\right){matrix_latex(uj)}")
        u = u.applyfunc(sp.simplify)
        us.append(u)
        steps.append(step(
            f"Wektor u{k + 1}: odejmujemy rzuty v{k + 1} na poprzednie wektory",
            f"u_{{{k + 1}}}=v_{{{k + 1}}}-" + "-".join(terms_formula)
            + f"={matrix_latex(v)}-" + "-".join(terms_values) + f"={matrix_latex(u)}",
        ))
    assert all(sp.simplify(a - b) == sp.zeros(*a.shape) for a, b in zip(us, ortho))
    return steps



# --- Zadanie 12: Dekompozycja LU (backend/tasks/task_12_dekompozycja_lu.py) ---

DEFINITION_12 = {
    "id": 12,
    "name": "Dekompozycja LU",
    "area": "Rozkłady macierzy",
    "example": "Przykład: A=[[2,1],[4,3]], znajdź LU.",
    "description": "Dana jest macierz kwadratowa A. Trzeba rozłożyć ją na iloczyn A=LU macierzy dolnotrójkątnej L i górnotrójkątnej U.",
    "parameterHelp": "Ustaw rozmiar i granice elementów.",
    "examInstruction": "Wykonaj dekompozycję LU macierzy A.",
    "params": [
        {"key": "size", "label": "Rozmiar macierzy", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość elementu", "type": "number", "min": -12, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość elementu", "type": "number", "min": 0, "max": 12, "value": 6},
    ],
}


def lu_integer_matrices(n: int, low: int, high: int) -> tuple[sp.Matrix, sp.Matrix, sp.Matrix]:
    limit = 3 * n * max(abs(low), abs(high), 1)
    for attempt in range(3000):
        L = sp.eye(n)
        for i in range(n):
            for j in range(i):
                L[i, j] = random.randint(low, high)
        U = sp.zeros(n, n)
        for i in range(n):
            for j in range(i, n):
                U[i, j] = rand_non_zero(low, high) if i == j else random.randint(low, high)
        A = L * U
        off_diagonal = sum(1 for i in range(n) for j in range(i) if L[i, j] != 0)
        if off_diagonal < max(1, (n * (n - 1) // 2) // 2 + 1):
            continue
        if attempt < 2500 and max(abs(v) for v in A) > limit:
            continue
        return L, U, A
    raise ValueError("Nie udało się wylosować macierzy do dekompozycji LU.")


def generate_12(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(5, rint(config, "size", 3)))
    L, U, A = lu_integer_matrices(n, low, high)
    ops_text: list[str] = []
    steps_latex = ""
    state = A.copy()
    for k in range(n - 1):
        ops_t, ops_l = [], []
        for i in range(k + 1, n):
            m = L[i, k]
            if m == 0:
                continue
            state[i, :] = state[i, :] - m * state[k, :]
            terms = [(sp.Integer(1), f"w{i + 1}"), (-m, f"w{k + 1}")]
            ops_t.append(f"w{i + 1} -> {row_combination(terms, False)}")
            ops_l.append(
                f"w_{i + 1}\\to {row_combination([(c, nm.replace('w', 'w_')) for c, nm in terms], True)}"
            )
        if ops_t:
            ops_text.append(", ".join(ops_t) + ": " + mat_text(state))
            ops_joined = ",\\ ".join(ops_l)
            steps_latex += f"\\xrightarrow{{{ops_joined}}}{rows_matrix_latex([state.row(r) for r in range(n)])}"
    assert state == U
    solution_text = (
        "Eliminacja Gaussa bez zamiany wierszy; mnożniki zapisujemy w macierzy L (pod przekątną jedynek). "
        + (" | ".join(ops_text) + ". " if ops_text else "")
        + f"U={mat_text(U)}, L={mat_text(L)}, A=LU."
    )
    solution_latex = (
        f"A={rows_matrix_latex([A.row(r) for r in range(n)])}{steps_latex}=U"
        f"\\qquad L={rows_matrix_latex([L.row(r) for r in range(n)])}"
        f",\\ U={rows_matrix_latex([U.row(r) for r in range(n)])},\\ A=LU"
    )
    return {
        "task": f"Wykonaj dekompozycję LU dla macierzy A={mat_text(A)}.",
        "solution": solution_text,
        "taskLatex": f"A={sp.latex(A)}",
        "solutionLatex": solution_latex,
    }



# --- Zadanie 13: Dekompozycja Choleskiego (backend/tasks/task_13_dekompozycja_choleskiego.py) ---

DEFINITION_13 = {
    "id": 13,
    "name": "Dekompozycja Choleskiego",
    "area": "Rozkłady macierzy",
    "example": "Przykład: A=[[6,2],[2,5]], znajdź L.",
    "description": "Dana jest macierz symetryczna, dodatnio określona A. Trzeba wyznaczyć macierz dolnotrójkątną L, dla której A=LLᵀ.",
    "parameterHelp": "Ustaw rozmiar i granice współczynników.",
    "examInstruction": "Wykonaj dekompozycję Choleskiego macierzy A.",
    "params": [
        {"key": "size", "label": "Rozmiar macierzy", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współczynnika", "type": "number", "min": -12, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współczynnika", "type": "number", "min": 0, "max": 12, "value": 6},
    ],
}


def generate_13(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, rint(config, "size", 3))
    L = sp.Matrix(
        [[random.randint(1, 5) if i == j else (random.randint(max(-5, low), min(5, high)) if j < i else 0) for j in range(n)] for i in range(n)]
    )
    A = L * L.T
    Lc = A.cholesky()
    return {
        "task": f"Wykonaj dekompozycję Choleskiego macierzy A={mat_text(A)}.",
        "solution": f"L={mat_text(Lc)} i A=LL^T.",
        "taskLatex": f"A={sp.latex(A)}",
        "solutionLatex": f"L={sp.latex(Lc)}",
        "steps": cholesky_steps(A, Lc),
    }


def cholesky_steps(A: sp.Matrix, expected: sp.Matrix) -> list[dict[str, str]]:
    """Elementy L kolumnami: l_jj = sqrt(a_jj - sum l_jk^2), l_ij = (a_ij - sum l_ik l_jk) / l_jj."""
    n = A.rows
    L = sp.zeros(n, n)
    steps = []
    for j in range(n):
        lines = []
        sub = sum((L[j, k] ** 2 for k in range(j)), sp.Integer(0))
        L[j, j] = sp.sqrt(A[j, j] - sub)
        inner = f"{sp.latex(A[j, j])}" + (f"-{sp.latex(sub)}" if j else "")
        lines.append(f"l_{{{j + 1}{j + 1}}}=\\sqrt{{{inner}}}={sp.latex(L[j, j])}")
        for i in range(j + 1, n):
            sub = sum((L[i, k] * L[j, k] for k in range(j)), sp.Integer(0))
            L[i, j] = sp.simplify((A[i, j] - sub) / L[j, j])
            numerator = f"{sp.latex(A[i, j])}" + (f"-\\left({sp.latex(sub)}\\right)" if j else "")
            lines.append(f"l_{{{i + 1}{j + 1}}}=\\frac{{{numerator}}}{{{sp.latex(L[j, j])}}}={sp.latex(L[i, j])}")
        steps.append(step(f"Kolumna {j + 1} macierzy L", ",\\quad ".join(lines)))
    assert sp.simplify(L - expected) == sp.zeros(n, n)
    steps.append(step("Wynik (sprawdzenie: LL^T = A)", f"L={matrix_latex(L)}"))
    return steps



# --- Zadanie 14: Diagonalizacja lub forma Jordana (backend/tasks/task_14_diagonalizacja_jordan.py) ---

DEFINITION_14 = {
    "id": 14,
    "name": "Diagonalizacja lub forma Jordana",
    "area": "Wartości własne i diagonalizacja",
    "example": "Przykład: sprawdź diagonalizowalność A=[[2,1],[0,2]].",
    "description": "Dana jest macierz kwadratowa A. Trzeba sprawdzić, czy jest diagonalizowalna: jeśli tak - podać D i P (A=PDP⁻¹), a jeśli nie - formę Jordana J i macierz P.",
    "parameterHelp": "Ustaw rozmiar i granice elementów.",
    "examInstruction": "Zbadaj, czy macierz A jest diagonalizowalna. Jeśli tak, podaj D i P; w przeciwnym razie podaj formę Jordana J i macierz P.",
    "params": [
        {"key": "size", "label": "Rozmiar macierzy", "type": "number", "min": 2, "max": 4, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość elementu", "type": "number", "min": -12, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość elementu", "type": "number", "min": 0, "max": 12, "value": 6},
    ],
}


def generate_14(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(4, rint(config, "size", 3)))
    jordan = random.random() < 0.5
    A, D, P = random_diag_jordan_matrix(n, low, high, jordan)
    steps = diagonalization_steps(A, D, P, jordan)
    if not jordan:
        return {
            "task": f"Sprawdź diagonalizowalność macierzy A={mat_text(A)}.",
            "solution": f"Macierz diagonalizowalna. D={mat_text(D)}, P={mat_text(P)}.",
            "taskLatex": f"A={sp.latex(A)}",
            "solutionLatex": f"D={sp.latex(D)},\\ P={sp.latex(P)}",
            "steps": steps,
        }
    return {
        "task": f"Sprawdź diagonalizowalność macierzy A={mat_text(A)}; w przeciwnym razie podaj formę Jordana.",
        "solution": f"Macierz niediagonalizowalna. J={mat_text(D)}, P={mat_text(P)}.",
        "taskLatex": f"A={sp.latex(A)}",
        "solutionLatex": f"J={sp.latex(D)},\\ P={sp.latex(P)}",
        "steps": steps,
    }



# --- Zadanie 15: Pseudoodwrotna, lewa i prawa odwrotność (backend/tasks/task_15_pseudoodwrotna.py) ---

DEFINITION_15 = {
    "id": 15,
    "name": "Pseudoodwrotna, lewa i prawa odwrotność",
    "area": "Macierze",
    "example": "Przykład: policz A+ dla macierzy 2x3.",
    "description": "Dana jest macierz prostokątna A. Trzeba wyznaczyć jej pseudoodwrotność A⁺ i sprawdzić, czy istnieje lewa lub prawa odwrotność.",
    "parameterHelp": "Ustaw liczbę wierszy i kolumn.",
    "examInstruction": "Wyznacz pseudoodwrotność A⁺ oraz zbadaj istnienie lewej i prawej odwrotności macierzy A.",
    "params": [
        {"key": "rows", "label": "Liczba wierszy", "type": "number", "min": 2, "max": 2, "value": 2},
        {"key": "cols", "label": "Liczba kolumn", "type": "number", "min": 3, "max": 6, "value": 4},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość elementu", "type": "number", "min": -12, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość elementu", "type": "number", "min": 0, "max": 12, "value": 6},
    ],
}


def generate_15(config: dict[str, Any]) -> dict[str, Any]:
    rows = 2
    cols = max(3, rint(config, "cols", 4))
    c1, c2 = random.sample(range(cols), 2)
    data = [[0 for _ in range(cols)] for _ in range(rows)]
    data[0][c1] = 1
    data[1][c2] = -1
    A = sp.Matrix(data)
    A_plus = A.T
    AAt = A * A.T
    assert A.T * AAt.inv() == A.pinv() == A_plus
    steps = [
        step(
            f"rz(A)={A.rank()} = liczba wierszy, więc A ma pełny rząd wierszowy",
            f"AA^T={matrix_latex(AAt)},\\ \\left(AA^T\\right)^{{-1}}={matrix_latex(AAt.inv())}",
        ),
        step("Pseudoodwrotność dla pełnego rzędu wierszowego (jest też prawą odwrotnością: AA⁺ = I)",
             f"A^+=A^T\\left(AA^T\\right)^{{-1}}={matrix_latex(A_plus)}"),
        step(
            f"Lewa odwrotność wymagałaby rz(A) = liczba kolumn = {cols}, a rz(A) = {A.rank()} < {cols}",
            "\\text{lewa odwrotność nie istnieje}",
        ),
    ]
    return {
        "task": f"Dla macierzy A={mat_text(A)} wyznacz A+ oraz sprawdź istnienie lewej i prawej odwrotności.",
        "solution": f"A+ = {mat_text(A_plus)}. Istnieje prawa odwrotność, lewa nie istnieje.",
        "taskLatex": f"A={sp.latex(A)}",
        "solutionLatex": f"A^+={sp.latex(A_plus)}",
        "steps": steps,
    }



# --- Zadanie 16: Rozwiązanie kompletne układu Ax=b (backend/tasks/task_16_rozwiazanie_kompletne.py) ---

DEFINITION_16 = {
    "id": 16,
    "name": "Rozwiązanie kompletne układu Ax=b",
    "area": "Układy równań",
    "example": "Przykład: x = xp + Xh*t dla układu niedookreślonego.",
    "description": "Dany jest układ równań liniowych. Trzeba podać jego rozwiązanie kompletne x = xp + Xh·t: rozwiązanie szczególne plus wszystkie rozwiązania układu jednorodnego.",
    "parameterHelp": "Ustaw liczbę równań, niewiadomych i granice współczynników.",
    "examInstruction": "Znajdź rozwiązanie kompletne układu równań.",
    "params": [
        {"key": "equations", "label": "Liczba równań", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "variables", "label": "Liczba niewiadomych", "type": "number", "min": 2, "max": 6, "value": 4},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współczynnika", "type": "number", "min": -30, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współczynnika", "type": "number", "min": 0, "max": 30, "value": 6},
    ],
}


def integer_complete_system(m: int, n: int, low: int, high: int) -> tuple[sp.Matrix, sp.Matrix]:
    """Układ Ax=b, w którym xp (zmienne wolne = 0) i wektory bazy jądra mają współrzędne całkowite.

    A = M·R, gdzie R jest macierzą schodkową zredukowaną o całkowitych wpisach, a M unimodularna.
    """
    limit = max(abs(low), abs(high), 1)
    mix = max(1, min(3, limit // 2))
    rank = min(m, n) if m >= n else (m - 1 if m >= 3 and random.random() < 0.3 else m)
    for attempt in range(3000):
        pivots = sorted(random.sample(range(n), rank))
        free_cols = [j for j in range(n) if j not in pivots]
        R = sp.zeros(m, n)
        for i, p in enumerate(pivots):
            R[i, p] = 1
            for j in free_cols:
                if j > p:
                    R[i, j] = random.randint(low, high)
        if free_cols and all(all(R[i, j] == 0 for i in range(rank)) for j in free_cols):
            continue
        Lm = sp.eye(m)
        Um = sp.eye(m)
        for i in range(m):
            for j in range(i):
                Lm[i, j] = random.randint(-mix, mix)
                Um[j, i] = random.randint(-mix, mix)
        M = Lm * Um
        A = M * R
        if any(all(v == 0 for v in A.row(i)) for i in range(m)):
            continue
        factor = 2 + attempt // 600
        if attempt < 2500 and max(abs(v) for v in A) > factor * limit:
            continue
        xp = sp.Matrix([random.randint(low, high) for _ in range(n)])
        b = A * xp
        if attempt < 2500 and max(abs(v) for v in b) > 8 * factor * limit:
            continue
        return A, b
    raise ValueError("Nie udało się wylosować układu z całkowitym rozwiązaniem.")


def generate_16(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    m = max(2, rint(config, "equations", 3))
    n = min(max(2, rint(config, "variables", 4)), len(VARS))
    A, b = integer_complete_system(m, n, low, high)
    eq = [sp.Eq(sum(A[i, j] * VARS[j] for j in range(n)), b[i], evaluate=False) for i in range(m)]
    solution = next(iter(sp.linsolve((A, b))))
    free = sorted(set().union(*(expr.free_symbols for expr in solution)), key=lambda s: s.name)
    if free:
        xp_real = sp.Matrix([expr.subs({s: 0 for s in free}) for expr in solution])
        Xh = sp.Matrix.hstack(*[sp.Matrix([sp.diff(expr, s) for expr in solution]) for s in free])
    else:
        xp_real = sp.Matrix(solution)
        Xh = sp.zeros(n, 0)
    assert all(v.is_integer for v in xp_real) and all(v.is_integer for v in Xh)
    rank = A.rank()
    if Xh.cols:
        params = ", ".join(f"t{k + 1}" for k in range(Xh.cols))
        general = " + ".join(
            [vec_text(xp_real)] + [f"t{k + 1}*{vec_text(Xh[:, k])}" for k in range(Xh.cols)]
        )
        solution_text = (
            f"rz(A)=rz([A|b])={rank}<{n}, więc układ ma nieskończenie wiele rozwiązań ({n - rank} parametr(y): {params}). "
            f"xp={vec_text(xp_real)}; Xh={mat_text(Xh)}; x = xp + Xh*t = {general}, t∈R."
        )
        general_latex = "+".join(
            [sp.latex(xp_real)] + [f"t_{{{k + 1}}}{sp.latex(Xh[:, k])}" for k in range(Xh.cols)]
        )
        solution_latex = (
            f"\\operatorname{{rz}}(A)=\\operatorname{{rz}}([A|b])={rank}<{n}\\qquad "
            f"x_p={sp.latex(xp_real)},\\ X_h={sp.latex(Xh)}\\qquad x={general_latex},\\ t_i\\in\\mathbb{{R}}"
        )
    else:
        solution_text = f"rz(A)=rz([A|b])={rank}={n}, więc układ ma dokładnie jedno rozwiązanie: x={vec_text(xp_real)}."
        solution_latex = (
            f"\\operatorname{{rz}}(A)=\\operatorname{{rz}}([A|b])={rank}={n}\\qquad x={sp.latex(xp_real)}"
        )
    aug = A.row_join(b)
    reduced, steps, _ = elimination_steps(aug, reduced=True, split=n, pivot_cols=n)
    steps.insert(0, step("Macierz rozszerzona układu", f"[A|b]={matrix_latex(aug, n)}"))
    pivots = [next(c for c in range(n) if reduced[r, c] != 0) for r in range(m) if any(reduced[r, c] != 0 for c in range(n))]
    free_cols = [c for c in range(n) if c not in pivots]
    if free_cols:
        names = ", ".join(sp.latex(VARS[c]) for c in free_cols)
        steps.append(step(
            f"Zmienne wolne ({n - rank}): podstawiamy parametry t; x_p dla t = 0, kolumny X_h z rozwiązań jednorodnych",
            f"\\text{{zmienne wolne: }}{names},\\quad x_p={sp.latex(xp_real)},\\ X_h={sp.latex(Xh)}",
        ))
    return {
        "task": "Znajdź rozwiązanie kompletne układu równań:\n" + "\n".join(sp.sstr(e) for e in eq),
        "solution": solution_text,
        "taskLatex": "\\begin{cases}" + " \\\\ ".join(sp.latex(e) for e in eq) + "\\end{cases}",
        "solutionLatex": solution_latex,
        "steps": steps,
    }



# --- Zadanie 17: Równania zespolone w postaci wykładniczej (backend/tasks/task_17_rownania_wykladnicze.py) ---

DEFINITION_17 = {
    "id": 17,
    "name": "Równania zespolone w postaci wykładniczej",
    "area": "Liczby zespolone",
    "example": "Przykład: z^4 = (conj(z))^2.",
    "description": "Dane jest równanie zespolone z potęgami z, sprzężeniem lub modułem, np. z⁴ = (z̄)². Trzeba je rozwiązać, korzystając z postaci wykładniczej z = r·e^(iφ).",
    "parameterHelp": "Ustaw maksymalną potęgę z oraz maksymalny moduł współczynnika.",
    "examInstruction": "Rozwiąż w zbiorze liczb zespolonych równanie.",
    "params": [
        {"key": "maxPower", "label": "Maksymalna potęga z", "type": "number", "min": 2, "max": 6, "value": 4},
        {"key": "maxModulus", "label": "Maksymalny moduł współczynnika", "type": "number", "min": 1, "max": 5, "value": 3},
    ],
}


EXP_GAUSS_COEFFS = [
    1 + sp.I, 1 - sp.I, -1 + sp.I, -1 - sp.I,
    1 + sp.sqrt(3) * sp.I, 1 - sp.sqrt(3) * sp.I, -1 + sp.sqrt(3) * sp.I, -1 - sp.sqrt(3) * sp.I,
]
EXP_PYTHAGOREAN_MODULI = {5: (3, 4), 10: (6, 8), 13: (5, 12), 15: (9, 12), 17: (8, 15)}


def exp_principal_angle(phi: sp.Expr) -> sp.Expr:
    phi = sp.simplify(phi)
    while float(phi) > math.pi + 1e-9:
        phi = sp.simplify(phi - 2 * sp.pi)
    while float(phi) <= -math.pi + 1e-9:
        phi = sp.simplify(phi + 2 * sp.pi)
    return phi


def exp_root_expr(radius: sp.Expr, phi: sp.Expr) -> sp.Expr:
    return radius * sp.exp(sp.I * phi)


def exp_root_latex(radius: sp.Expr, phi: sp.Expr) -> str:
    if phi == 0:
        return sp.latex(radius)
    prefix = "" if radius == 1 else sp.latex(radius)
    sign = "-" if phi < 0 else ""
    return f"{prefix}e^{{{sign}i{sp.latex(abs(phi))}}}"


def exp_root_text(radius: sp.Expr, phi: sp.Expr) -> str:
    if phi == 0:
        return sp.sstr(radius)
    prefix = "" if radius == 1 else f"{sp.sstr(radius)}*"
    angle = f"({sp.sstr(phi)})" if phi < 0 else sp.sstr(phi)
    return f"{prefix}exp(i*{angle})"


def exp_disguised_real(value: sp.Expr) -> tuple[str, str]:
    """Zapisuje liczbę rzeczywistą jako Re(w), Im(w) lub |w| albo zwraca ją wprost."""
    sign = "-" if value < 0 else ""
    positive = abs(value)
    options = ["plain", "re", "im"]
    if positive.is_Integer and int(positive) in EXP_PYTHAGOREAN_MODULI:
        options.append("abs")
    kind = random.choice(options)
    pos_tex, pos_txt = sp.latex(positive), sp.sstr(positive)
    if kind == "plain":
        return sp.latex(value), sp.sstr(value)
    b = random.choice([-3, -2, -1, 1, 2, 3])
    if kind == "abs":
        u, v = EXP_PYTHAGOREAN_MODULI[int(positive)]
        return f"{sign}\\left|{u}+{v}i\\right|", f"{sign}|{u}+{v}*i|"
    if kind == "re":
        return (
            f"{sign}\\operatorname{{Re}}\\left({pos_tex}{imag_term(b)}\\right)",
            f"{sign}Re({pos_txt}{imag_term(b).replace('i', '') + '*i' if abs(b) != 1 else imag_term(b)})",
        )
    if positive == 1:
        return (
            f"{sign}\\operatorname{{Im}}\\left({b}+i\\right)",
            f"{sign}Im({b}+i)",
        )
    return (
        f"{sign}\\operatorname{{Im}}\\left({b}+{pos_tex}i\\right)",
        f"{sign}Im({b}+({pos_txt})*i)",
    )


def exp_coefficient_forms(a: sp.Expr) -> tuple[str, str]:
    """Zwraca zapis (LaTeX, tekst) współczynnika a stojącego przed sprzężeniem/modułem."""
    if a == 1:
        return "", ""
    if a == -1:
        return "-", "-"
    if a.is_real:
        if random.random() < 0.5:
            return exp_disguised_real(a)
        return sp.latex(a), sp.sstr(a)
    if a.is_imaginary:
        return sp.latex(a), sp.sstr(a).replace("I", "i")
    if random.random() < 0.3:
        w = sp.conjugate(a)
        return f"\\overline{{{sp.latex(w)}}}", f"conj({sp.sstr(w).replace('I', 'i')})"
    return f"\\left({sp.latex(a)}\\right)", f"({sp.sstr(a).replace('I', 'i')})"


def build_exponential_equation(max_power: int, max_radius: int) -> dict[str, Any]:
    """Losuje równanie z^n = a * conj(z)^m * |z|^k i rozwiązuje je w postaci wykładniczej.

    Dla z = r*e^{i*phi}: r^(n-m-k) = |a| oraz (n+m)*phi = arg(a) + 2*k*pi; dodatkowo z = 0.
    """
    max_power = max(2, min(6, max_power))
    max_radius = max(1, max_radius)
    for _ in range(500):
        kind = random.choice(["conj", "mod", "mixed"])
        n = random.randint(1, max_power)
        m = random.randint(1, max_power if kind == "conj" else 2) if kind in ("conj", "mixed") else 0
        k = random.randint(1, max_power if kind == "mod" else 2) if kind in ("mod", "mixed") else 0
        e = n - m - k
        count = n + m
        if e == 0 or count < 2 or count > 8:
            continue

        if random.random() < 0.7:
            radius = sp.Integer(random.randint(1, max_radius))
            a = sp.Integer(radius) ** e * random.choice([1, -1, sp.I, -sp.I])
        else:
            a = random.choice(EXP_GAUSS_COEFFS)
            radius = sp.simplify(sp.Abs(a) ** sp.Rational(1, e))
        alpha = sp.arg(a)

        angles = sorted(
            (exp_principal_angle((alpha + 2 * sp.pi * j) / count) for j in range(count)),
            key=lambda value: float(value),
        )
        roots = [sp.Integer(0)] + [exp_root_expr(radius, phi) for phi in angles]

        a_num = complex(sp.N(a))
        if not all(
            abs(complex(sp.N(root)) ** n - a_num * complex(sp.N(root)).conjugate() ** m * abs(complex(sp.N(root))) ** k) < 1e-9
            for root in roots
        ):
            continue

        coef_tex, coef_txt = exp_coefficient_forms(a)
        lhs_tex = "z" if n == 1 else f"z^{{{n}}}"
        lhs_txt = "z" if n == 1 else f"z^{n}"
        conj_tex = "" if m == 0 else ("\\overline{z}" if m == 1 else f"\\left(\\overline{{z}}\\right)^{{{m}}}")
        mod_tex = "" if k == 0 else ("\\left|z\\right|" if k == 1 else f"\\left|z\\right|^{{{k}}}")
        pieces_txt = []
        if m:
            pieces_txt.append("conj(z)" if m == 1 else f"conj(z)^{m}")
        if k:
            pieces_txt.append("|z|" if k == 1 else f"|z|^{k}")
        rhs_tex = f"{coef_tex}{conj_tex}{mod_tex}"
        rhs_txt = coef_txt + "*".join(pieces_txt) if coef_txt in ("", "-") else coef_txt + "*" + "*".join(pieces_txt)
        if random.random() < 0.25:
            equation_tex, equation_txt = f"{rhs_tex}={lhs_tex}", f"{rhs_txt} = {lhs_txt}"
        else:
            equation_tex, equation_txt = f"{lhs_tex}={rhs_tex}", f"{lhs_txt} = {rhs_txt}"

        a_modulus = sp.simplify(sp.Abs(a))
        alpha_tex = sp.latex(alpha)
        angle_sum_tex = f"{alpha_tex}+2l\\pi" if alpha != 0 else "2l\\pi"
        solution_tex = (
            "\\begin{aligned}"
            f"&z=re^{{i\\varphi}},\\quad a={sp.latex(a)}={sp.latex(a_modulus)}e^{{i\\left({alpha_tex}\\right)}}\\\\"
            f"&r^{{{e}}}={sp.latex(a_modulus)}\\ \\Rightarrow\\ r={sp.latex(radius)},\\qquad "
            f"{count}\\varphi={angle_sum_tex}\\ \\Rightarrow\\ "
            f"\\varphi=\\frac{{1}}{{{count}}}\\left({angle_sum_tex}\\right),\\ l=0,\\dots,{count - 1}\\\\"
            "&z\\in\\left\\{0,\\ " + ",\\ ".join(exp_root_latex(radius, phi) for phi in angles) + "\\right\\}"
            "\\end{aligned}"
        )
        solution_txt = "z = 0 lub z = " + ", ".join(exp_root_text(radius, phi) for phi in angles) + "."
        return {
            "equationLatex": equation_tex,
            "equationText": equation_txt,
            "roots": roots,
            "solutionLatex": solution_tex,
            "solutionText": solution_txt,
            "rootsLatex": ", ".join(["0"] + [exp_root_latex(radius, phi) for phi in angles]),
            "rootsText": ", ".join(["0"] + [exp_root_text(radius, phi) for phi in angles]),
        }
    raise ValueError("Nie udało się wygenerować równania.")


def generate_17(config: dict[str, Any]) -> dict[str, Any]:
    equation = build_exponential_equation(rint(config, "maxPower", 4), rint(config, "maxModulus", 3))
    return {
        "task": f"Rozwiąż w zbiorze liczb zespolonych równanie: {equation['equationText']}.",
        "solution": f"Rozwiązania: {equation['solutionText']}",
        "taskLatex": equation["equationLatex"],
        "solutionLatex": equation["solutionLatex"],
    }



# --- Zadanie 18: Kombinacja liniowa wektorów (x ∈ L(a,b)) (backend/tasks/task_18_kombinacja_liniowa.py) ---

DEFINITION_18 = {
    "id": 18,
    "name": "Kombinacja liniowa wektorów (x ∈ L(a,b))",
    "area": "Przestrzenie liniowe",
    "example": "Przykład: czy x=(-2,0,1) należy do L(a,b) dla a=(0,2,-1), b=(1,3,2)?",
    "description": "Dane są wektory rozpinające przestrzeń oraz wektor x. Trzeba sprawdzić, czy x jest ich kombinacją liniową, czyli czy x należy do ich powłoki liniowej.",
    "parameterHelp": "Ustaw wymiar, liczbę wektorów rozpinających, zakres współrzędnych i szansę, że x należy do L.",
    "examInstruction": "Sprawdź, czy wektor x jest kombinacją liniową podanych wektorów.",
    "params": [
        {"key": "dimension", "label": "Wymiar wektorów", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "vectors", "label": "Liczba wektorów rozpinających (a, b, ...)", "type": "number", "min": 1, "max": 4, "value": 2},
        {"key": "inSpanPercent", "label": "Szansa, że x należy do L (%)", "type": "number", "min": 0, "max": 100, "value": 50},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współrzędnej", "type": "number", "min": -9, "max": 0, "value": -3},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współrzędnej", "type": "number", "min": 0, "max": 9, "value": 3},
    ],
}


def generate_18(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(5, rint(config, "dimension", 3)))
    k = max(1, min(rint(config, "vectors", 2), n - 1, 4))
    percent = max(0, min(100, rint(config, "inSpanPercent", 50)))
    names = ["a", "b", "c", "d"][:k]
    symbols = [sp.Symbol(name) for name in names]
    want_inside = random.random() * 100 < percent

    def random_vector() -> sp.Matrix:
        return sp.Matrix([random.randint(low, high) for _ in range(n)])

    for _ in range(500):
        vectors = [random_vector() for _ in range(k)]
        base = sp.Matrix.hstack(*vectors)
        if base.rank() < k:
            continue
        if want_inside:
            coeffs = [rand_non_zero(-3, 3) for _ in range(k)]
            x = sum((c * v for c, v in zip(coeffs, vectors)), sp.zeros(n, 1))
        else:
            x = random_vector()
        if not any(x) or (sp.Matrix.hstack(base, x).rank() == k) != want_inside:
            continue
        if want_inside and max(abs(v) for v in x) > 3 * max(abs(low), abs(high)) + 3:
            continue
        break
    else:
        raise ValueError("Nie udało się wylosować zadania o kombinacji liniowej.")

    span = ", ".join(names)
    task_vectors = [("x", x)] + list(zip(names, vectors))
    task_text = (
        f"Sprawdź, czy x jest kombinacją liniową wektorów {span}, czyli czy x ∈ L({span}): "
        + ", ".join(f"{name}={vec_text(vec)}" for name, vec in task_vectors) + "."
    )
    task_latex = (
        ",\\ ".join(f"{name}={pmatrix_latex(vec)}" for name, vec in task_vectors)
        + f",\\quad x\\in L({span})?"
    )

    augmented = sp.Matrix.hstack(base, x)
    rank_text = f"rz[{span}] = {k}, rz[{span} | x] = {augmented.rank()}"
    rank_latex = (
        f"\\operatorname{{rz}}\\left[{span}\\right]={k},\\ "
        f"\\operatorname{{rz}}\\left[{span}\\mid x\\right]={augmented.rank()}"
    )
    if want_inside:
        solution = next(iter(sp.linsolve((base, x), *symbols)))
        combo = sum((c * s for c, s in zip(solution, symbols)), sp.Integer(0))
        combo_text = sp.sstr(combo).replace("*", "")
        return {
            "task": task_text,
            "solution": f"Tak: {rank_text}; x = {combo_text}, więc x ∈ L({span}).",
            "taskLatex": task_latex,
            "solutionLatex": (
                f"\\text{{Tak: }}{rank_latex},\\ x={sp.latex(combo)}\\Rightarrow x\\in L({span})"
            ),
        }
    return {
        "task": task_text,
        "solution": f"Nie: {rank_text}, układ jest sprzeczny, więc x ∉ L({span}).",
        "taskLatex": task_latex,
        "solutionLatex": f"\\text{{Nie: }}{rank_latex}\\Rightarrow x\\notin L({span})",
    }



# --- Zadanie 19: Liniowa niezależność wektorów (z definicji) (backend/tasks/task_19_niezaleznosc_z_definicji.py) ---

DEFINITION_19 = {
    "id": 19,
    "name": "Liniowa niezależność wektorów (z definicji)",
    "area": "Przestrzenie liniowe",
    "example": "Przykład: zbadaj z definicji niezależność (-1,2,3), (0,1,2), (2,1,0).",
    "description": "Dany jest układ wektorów. Trzeba zbadać jego liniową niezależność z definicji - rozwiązując równanie c₁v₁ + … + cₖvₖ = 0.",
    "parameterHelp": "Ustaw wymiar i liczbę wektorów, szansę na niezależność oraz czy współrzędne mogą być urojone (np. 2i).",
    "examInstruction": "Zbadaj z definicji liniową niezależność wektorów.",
    "params": [
        {"key": "dimension", "label": "Wymiar wektorów", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "vectors", "label": "Liczba wektorów", "type": "number", "min": 2, "max": 6, "value": 3},
        {"key": "independentPercent", "label": "Szansa na wektory niezależne (%)", "type": "number", "min": 0, "max": 100, "value": 50},
        {"key": "complexEntries", "label": "Liczby zespolone we współrzędnych (0 - nie, 1 - tak)", "type": "number", "min": 0, "max": 1, "value": 0},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość współrzędnej", "type": "number", "min": -9, "max": 0, "value": -3},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość współrzędnej", "type": "number", "min": 0, "max": 9, "value": 3},
    ],
}


def generate_19(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(5, rint(config, "dimension", 3)))
    k = max(2, min(6, rint(config, "vectors", 3)))
    percent = max(0, min(100, rint(config, "independentPercent", 50)))
    use_complex = rint(config, "complexEntries", 0) == 1
    want_independent = k <= n and random.random() * 100 < percent
    limit = 3 * max(abs(low), abs(high)) + 3

    def entry() -> sp.Expr:
        if use_complex and random.random() < 0.35:
            return rand_non_zero(-3, 3) * sp.I
        return sp.Integer(random.randint(low, high))

    def random_vector() -> sp.Matrix:
        return sp.Matrix([entry() for _ in range(n)])

    for _ in range(1000):
        vectors = [random_vector() for _ in range(k)]
        if not want_independent and k <= n:
            target = random.randrange(k)
            coeffs = [random.randint(-2, 2) for _ in range(k)]
            if not any(c for i, c in enumerate(coeffs) if i != target):
                continue
            vectors[target] = sum(
                (c * v for i, (c, v) in enumerate(zip(coeffs, vectors)) if i != target), sp.zeros(n, 1)
            )
        A = sp.Matrix.hstack(*vectors)
        rank = A.rank(simplify=True)
        if (rank == k) != want_independent and k <= n:
            continue
        if rank == k and k > n:
            continue
        if any(max(abs(sp.re(v)), abs(sp.im(v))) > limit for v in A):
            continue
        if use_complex and all(sp.im(v) == 0 for v in A):
            continue
        break
    else:
        raise ValueError("Nie udało się wylosować zadania o liniowej niezależności.")

    names = [f"v{i}" for i in range(1, k + 1)]
    field = " nad ciałem liczb zespolonych" if use_complex else ""
    equation_text = " + ".join(f"c{i}*{name}" for i, name in enumerate(names, start=1)) + " = 0"
    equation_latex = "+".join(f"c_{i}v_{i}" for i in range(1, k + 1)) + "=0"
    task_text = (
        "Zbadaj z definicji liniową niezależność wektorów: "
        + ", ".join(f"{name}={complex_vec_text(v)}" for name, v in zip(names, vectors)) + "."
    )
    task_latex = ",".join(pmatrix_latex(v) for v in vectors)

    head_text = f"Rozważamy {equation_text}, czyli układ jednorodny A c = 0, gdzie A=[{' '.join(names)}]{field}; rz(A) = {rank}"
    head_latex = (
        f"{equation_latex}\\iff A c=0,\\ A=\\left[{' '.join(f'v_{i}' for i in range(1, k + 1))}\\right],\\ "
        f"\\operatorname{{rz}}(A)={rank}"
    )
    if rank == k:
        return {
            "task": task_text,
            "solution": f"{head_text}, czyli równa liczbie wektorów ({k}), więc jedynym rozwiązaniem jest c = 0: wektory są liniowo niezależne.",
            "taskLatex": task_latex,
            "solutionLatex": (
                f"{head_latex}\\text{{ (liczba wektorów)}}\\Rightarrow c=0\\text{{ jest jedynym rozwiązaniem: wektory są liniowo niezależne}}"
            ),
        }
    c = clear_denominators(A.nullspace()[0])
    text_terms: list[tuple[str, str]] = []
    latex_terms: list[tuple[str, str]] = []
    for i, ci in enumerate(c, start=1):
        if ci == 0:
            continue
        if ci.is_Integer:
            sign = "-" if ci < 0 else "+"
            mag = abs(ci)
            text_terms.append((sign, f"v{i}" if mag == 1 else f"{mag}*v{i}"))
            latex_terms.append((sign, f"v_{i}" if mag == 1 else f"{mag}v_{i}"))
        else:
            coeff = complex_text(ci).replace(" ", "")
            text_terms.append(("+", f"({coeff})*v{i}"))
            latex_terms.append(("+", f"\\left({coeff}\\right)v_{i}"))

    def join_terms(terms: list[tuple[str, str]]) -> str:
        out = ("-" if terms[0][0] == "-" else "") + terms[0][1]
        for sign, body in terms[1:]:
            out += f" {sign} {body}"
        return out

    relation_text = join_terms(text_terms)
    relation_latex = join_terms(latex_terms)
    return {
        "task": task_text,
        "solution": (
            f"{head_text} < {k}, więc istnieje nietrywialne rozwiązanie c = {complex_vec_text(c)}: "
            f"{relation_text} = 0, czyli wektory są liniowo zależne."
        ),
        "taskLatex": task_latex,
        "solutionLatex": (
            f"{head_latex}<{k}\\Rightarrow c={pmatrix_latex(c)}:\\ {relation_latex}=0"
            "\\text{ - wektory są liniowo zależne}"
        ),
    }



# --- Zadanie 20: Baza przestrzeni L(v1,...,vk) (operacje elementarne) (backend/tasks/task_20_baza_operacje_elementarne.py) ---

DEFINITION_20 = {
    "id": 20,
    "name": "Baza przestrzeni L(v1,...,vk) (operacje elementarne)",
    "area": "Przestrzenie liniowe",
    "example": "Przykład: wyznacz bazę L((1,-2), (-2,4), (2,2)) operacjami elementarnymi.",
    "description": "Dany jest układ wektorów v₁, …, vₖ. Trzeba wyznaczyć bazę rozpinanej przez nie przestrzeni L(v₁, …, vₖ), korzystając z operacji elementarnych.",
    "parameterHelp": "Ustaw liczbę i rozmiar wektorów oraz czy współrzędne mogą być urojone (np. 2i). Zakres współrzędnych domyślnie od -5 do 5.",
    "examInstruction": "Wyznacz bazę przestrzeni liniowej, korzystając z operacji elementarnych.",
    "params": [
        {"key": "dimension", "label": "Rozmiar wektorów", "type": "number", "min": 2, "max": 5, "value": 2},
        {"key": "vectors", "label": "Liczba wektorów", "type": "number", "min": 2, "max": 6, "value": 3},
        {"key": "complexEntries", "label": "Liczby zespolone we współrzędnych (0 - nie, 1 - tak)", "type": "number", "min": 0, "max": 1, "value": 0},
        {"key": "coeffLower", "label": "Dolna wartość współrzędnej", "type": "number", "min": -9, "max": 0, "value": -5},
        {"key": "coeffUpper", "label": "Górna wartość współrzędnej", "type": "number", "min": 0, "max": 9, "value": 5},
    ],
}


def generate_20(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(5, rint(config, "dimension", 2)))
    k = max(2, min(6, rint(config, "vectors", 3)))
    use_complex = rint(config, "complexEntries", 0) == 1

    def entry() -> sp.Expr:
        if use_complex and random.random() < 0.35:
            return rand_non_zero(-3, 3) * sp.I
        return sp.Integer(random.randint(low, high))

    for _ in range(500):
        vectors = [sp.Matrix([entry() for _ in range(n)]) for _ in range(k)]
        if random.random() < 0.6:
            target = random.randrange(k)
            source = random.choice([i for i in range(k) if i != target])
            vectors[target] = random.choice([-2, -1, 2]) * vectors[source]
        if any(all(v == 0 for v in vec) for vec in vectors):
            continue
        if use_complex and all(sp.im(v) == 0 for vec in vectors for v in vec):
            continue
        if any(max(abs(sp.re(v)), abs(sp.im(v))) > 3 * max(abs(low), abs(high), 1) for vec in vectors for v in vec):
            continue
        break
    else:
        raise ValueError("Nie udało się wylosować zadania o bazie przestrzeni.")

    rows = [vec.T for vec in vectors]
    labels = list(range(k))
    steps: list[tuple[str, str, list[sp.Matrix]]] = []
    pivot_row = 0
    for col in range(n):
        if pivot_row >= k:
            break
        candidates = [r for r in range(pivot_row, k) if sp.simplify(rows[r][col]) != 0]
        if not candidates:
            continue
        ops_text: list[str] = []
        ops_latex: list[str] = []
        best = candidates[0]
        if best != pivot_row:
            rows[pivot_row], rows[best] = rows[best], rows[pivot_row]
            labels[pivot_row], labels[best] = labels[best], labels[pivot_row]
            ops_text.append(f"w{pivot_row + 1} <-> w{best + 1}")
            ops_latex.append(f"w_{pivot_row + 1}\\leftrightarrow w_{best + 1}")
        pivot = rows[pivot_row][col]
        for r in range(pivot_row + 1, k):
            a = sp.simplify(rows[r][col])
            if a == 0:
                continue
            ratio = sp.simplify(sp.expand(a / pivot))
            if is_gaussian_integer(ratio):
                terms = [(sp.Integer(1), f"w{r + 1}"), (-ratio, f"w{pivot_row + 1}")]
                rows[r] = (rows[r] - ratio * rows[pivot_row]).applyfunc(sp.simplify)
            else:
                terms = [(pivot, f"w{r + 1}"), (-a, f"w{pivot_row + 1}")]
                rows[r] = (pivot * rows[r] - a * rows[pivot_row]).applyfunc(lambda v: sp.simplify(sp.expand(v)))
            ops_text.append(f"w{r + 1} -> {row_combination(terms, False)}")
            ops_latex.append(
                f"w_{r + 1}\\to {row_combination([(c, nm.replace('w', 'w_')) for c, nm in terms], True)}"
            )
        pivot_row += 1
        if ops_text:
            steps.append((", ".join(ops_text), ",\\ ".join(ops_latex), [row.copy() for row in rows]))

    kept = sorted(labels[i] for i, row in enumerate(rows) if any(sp.simplify(v) != 0 for v in row))
    dropped = [i for i in range(k) if i not in kept]
    basis = [vectors[i] for i in kept]
    names = [f"v{i}" for i in range(1, k + 1)]
    kept_names = [names[i] for i in kept]
    task_text = (
        "Wyznacz bazę przestrzeni liniowej L(" + ", ".join(names) + ") korzystając z operacji elementarnych: "
        + ", ".join(f"{name}={complex_vec_text(v)}" for name, v in zip(names, vectors)) + "."
    )
    task_latex = "L\\left(" + ",".join(pmatrix_latex(v) for v in vectors) + "\\right)"

    solution_text = "Wektory zapisujemy jako wiersze macierzy i wykonujemy operacje elementarne na wierszach. "
    solution_latex = rows_matrix_latex([v.T for v in vectors])
    if steps:
        parts = []
        for op_text, op_latex, state in steps:
            parts.append(f"{op_text}: " + "; ".join(complex_vec_text(row.T) for row in state))
            solution_latex += f"\\xrightarrow{{{op_latex}}}" + rows_matrix_latex(state)
        solution_text += "Kolejne kroki: " + " | ".join(parts) + ". "
    else:
        solution_text += "Macierz ma już postać schodkową. "
    solution_text += (
        f"Niezerowe wiersze (liczba: {len(kept)}) pochodzą od wektorów {', '.join(kept_names)}, które są liniowo niezależne"
    )
    if len(dropped) == 1:
        solution_text += f", a wektor {names[dropped[0]]} jest kombinacją liniową pozostałych"
    elif dropped:
        solution_text += f", a wektory {', '.join(names[i] for i in dropped)} są kombinacjami liniowymi pozostałych"
    solution_text += (
        f". Baza (wektory początkowe): {{{', '.join(kept_names)}}} = {{{', '.join(complex_vec_text(b) for b in basis)}}}, dim L = {len(basis)}."
    )
    basis_latex = ",\\ ".join(pmatrix_latex(b) for b in basis)
    kept_latex = ",".join(f"v_{i + 1}" for i in kept)
    solution_latex += (
        f"\\qquad\\text{{Baza: }}\\left\\{{{kept_latex}\\right\\}}=\\left\\{{{basis_latex}\\right\\}},\\ \\dim L={len(basis)}"
    )
    return {
        "task": task_text,
        "solution": solution_text,
        "taskLatex": task_latex,
        "solutionLatex": solution_latex,
    }



# --- Zadanie 21: Baza i wymiar podprzestrzeni (układ równań) (backend/tasks/task_21_baza_podprzestrzeni.py) ---

DEFINITION_21 = {
    "id": 21,
    "name": "Baza i wymiar podprzestrzeni (układ równań)",
    "area": "Przestrzenie liniowe",
    "example": "Przykład: wyznacz bazę i wymiar X={(x1,x2)∈R²: 3x1-x2=0}.",
    "description": "Dana jest podprzestrzeń opisana układem równań jednorodnych. Trzeba wyznaczyć jej bazę i wymiar.",
    "parameterHelp": "Podprzestrzeń zadana jednorodnym układem równań. Wybierz ciało (rzeczywiste lub zespolone), liczbę zmiennych i równań.",
    "examInstruction": "Wyznacz bazę i wymiar podprzestrzeni.",
    "params": [
        {"key": "field", "label": "Ciało (0 - liczby rzeczywiste, 1 - liczby zespolone)", "type": "number", "min": 0, "max": 1, "value": 0},
        {"key": "dimension", "label": "Liczba zmiennych", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "equations", "label": "Liczba równań", "type": "number", "min": 1, "max": 4, "value": 1},
        {"key": "coeffLower", "label": "Dolna wartość współczynnika", "type": "number", "min": -9, "max": 0, "value": -3},
        {"key": "coeffUpper", "label": "Górna wartość współczynnika", "type": "number", "min": 0, "max": 9, "value": 3},
    ],
}


def generate_21(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    complex_field = rint(config, "field", 0) == 1
    n = max(2, min(5, rint(config, "dimension", 3)))
    m = max(1, min(rint(config, "equations", 1), n - 1, 4))

    def coefficient() -> sp.Expr:
        if complex_field and random.random() < 0.3:
            return rand_non_zero(-3, 3) * sp.I
        return sp.Integer(random.randint(low, high))

    for _ in range(500):
        A = sp.Matrix(m, n, lambda *_: coefficient())
        if any(all(v == 0 for v in A.row(r)) for r in range(m)):
            continue
        if A.rank(simplify=True) != m:
            continue
        if complex_field and all(sp.im(v) == 0 for v in A):
            continue
        break
    else:
        raise ValueError("Nie udało się wylosować układu równań.")

    xs_text = [f"x{j}" for j in range(1, n + 1)]
    xs_latex = [f"x_{j}" for j in range(1, n + 1)]
    ring_text, ring_latex = ("C", "\\mathbb{C}") if complex_field else ("R", "\\mathbb{R}")

    def equation(row: sp.Matrix, latex: bool) -> str:
        terms = [(row[j], (xs_latex if latex else xs_text)[j]) for j in range(n) if row[j] != 0]
        return row_combination(terms, latex) + "=0"

    eqs_text = "; ".join(equation(A.row(r), False) for r in range(m))
    eqs_latex_inline = "\\ \\wedge\\ ".join(equation(A.row(r), True) for r in range(m))
    vars_text = "(" + ", ".join(xs_text) + ")"
    vars_latex = "(" + ",".join(xs_latex) + ")"
    task_text = f"Wyznacz bazę i wymiar podprzestrzeni X={{{vars_text} ∈ {ring_text}^{n}: {eqs_text}}}."
    task_latex = f"X=\\{{{vars_latex}\\in{ring_latex}^{{{n}}}:\\ {eqs_latex_inline}\\}}"

    rref, pivots = A.rref(simplify=True)
    r = len(pivots)
    free = [j for j in range(n) if j not in pivots]
    d = len(free)
    t_text = [f"t{i}" for i in range(1, d + 1)]
    t_latex = [f"t_{i}" for i in range(1, d + 1)]

    expressions: dict[int, list[tuple[sp.Expr, int]]] = {}
    for row_index, p in enumerate(pivots):
        expressions[p] = [(-rref[row_index, f], idx) for idx, f in enumerate(free) if rref[row_index, f] != 0]

    def express(p: int, latex: bool) -> str:
        names = t_latex if latex else t_text
        terms = [(c, names[idx]) for c, idx in expressions[p]]
        return row_combination(terms, latex) if terms else "0"

    nullvectors = []
    for idx, f in enumerate(free):
        vec = sp.zeros(n, 1)
        vec[f] = 1
        for p in pivots:
            for c, i2 in expressions[p]:
                if i2 == idx:
                    vec[p] = c
        nullvectors.append(vec)
    basis = nullvectors

    param_text = ", ".join(f"x{f + 1}={t_text[idx]}" for idx, f in enumerate(free))
    dep_text = ", ".join(f"x{p + 1}={express(p, False)}" for p in pivots)
    combo_text = " + ".join(f"{t_text[i]}*{complex_vec_text(v)}" for i, v in enumerate(nullvectors))
    solution_text = (
        f"Układ jednorodny ma macierz A o rzędzie {r}, więc dim X = {n} - {r} = {d}. "
        f"Zmienne wolne: {param_text} (t∈{ring_text}); zmienne zależne: {dep_text}. "
        f"Zatem {vars_text} = {combo_text}. "
        f"Baza X: {{{', '.join(complex_vec_text(b) for b in basis)}}}, dim X = {d}."
    )

    dep_latex = ",\\ ".join(f"x_{p + 1}={express(p, True)}" for p in pivots)
    param_latex = ",\\ ".join(f"x_{f + 1}={t_latex[idx]}" for idx, f in enumerate(free))
    combo_latex = "+".join(f"{t_latex[i]}{pmatrix_latex(v)}" for i, v in enumerate(nullvectors))
    solution_latex = (
        f"{rows_matrix_latex([A.row(i) for i in range(m)])}\\sim{rows_matrix_latex([rref.row(i) for i in range(r)])},\\ "
        f"\\dim X={n}-{r}={d};\\ {param_latex},\\ {dep_latex};\\ "
        f"x={combo_latex},\\ t_i\\in{ring_latex};\\ "
        f"\\text{{baza: }}\\left\\{{{',\\ '.join(pmatrix_latex(b) for b in basis)}\\right\\}}"
    )
    return {
        "task": task_text,
        "solution": solution_text,
        "taskLatex": task_latex,
        "solutionLatex": solution_latex,
    }



# --- Zadanie 22: Działania na macierzach (A+B^T, A·B, (A+B)^T, ...) (backend/tasks/task_22_dzialania_na_macierzach.py) ---

DEFINITION_22 = {
    "id": 22,
    "name": "Działania na macierzach (A+B^T, A·B, (A+B)^T, ...)",
    "area": "Macierze",
    "example": "Przykład: oblicz A·B^T − C, gdzie A, B, C to losowe macierze o zgodnych wymiarach.",
    "description": "Dane są macierze A, B, … o zgodnych wymiarach i wyrażenie z nimi (sumy, iloczyny, transpozycje). Trzeba obliczyć wartość wyrażenia.",
    "parameterHelp": "Wymiary macierzy generator dobiera sam (nie muszą być kwadratowe). Wybierz liczbę macierzy, maksymalny wymiar i dozwolone działania.",
    "examInstruction": "",
    "params": [
        {"key": "matrices", "label": "Liczba macierzy w wyrażeniu", "type": "number", "min": 2, "max": 4, "value": 2},
        {"key": "maxSize", "label": "Maksymalny wymiar macierzy", "type": "number", "min": 2, "max": 4, "value": 3},
        {"key": "multiplication", "label": "Mnożenie macierzy (0 - nie, 1 - tak)", "type": "number", "min": 0, "max": 1, "value": 1},
        {"key": "subtraction", "label": "Odejmowanie (0 - nie, 1 - tak)", "type": "number", "min": 0, "max": 1, "value": 1},
        {"key": "transposition", "label": "Transpozycja (0 - nie, 1 - tak)", "type": "number", "min": 0, "max": 1, "value": 1},
        {"key": "coeffLower", "label": "Dolna wartość elementów", "type": "number", "min": -9, "max": 0, "value": -3},
        {"key": "coeffUpper", "label": "Górna wartość elementów", "type": "number", "min": 0, "max": 9, "value": 3},
    ],
}


def generate_22(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    count = max(2, min(4, rint(config, "matrices", 2)))
    max_size = max(2, min(4, rint(config, "maxSize", 3)))
    allow_mul = rint(config, "multiplication", 1) == 1
    allow_sub = rint(config, "subtraction", 1) == 1
    allow_transpose = rint(config, "transposition", 1) == 1

    def size() -> int:
        return random.randint(2, max_size)

    def build(rows: int, cols: int, leaves: int, can_transpose: bool = True) -> dict[str, Any]:
        if can_transpose and allow_transpose and random.random() < (0.25 if leaves == 1 else 0.15):
            return {"op": "T", "a": build(cols, rows, leaves, False)}
        if leaves == 1:
            return {"op": "leaf", "shape": (rows, cols)}
        left = random.randint(1, leaves - 1)
        ops = ["+"] + (["-"] if allow_sub else []) + (["*"] if allow_mul else [])
        op = random.choice(ops)
        if op == "*":
            inner = size()
            return {"op": op, "a": build(rows, inner, left), "b": build(inner, cols, leaves - left)}
        return {"op": op, "a": build(rows, cols, left), "b": build(rows, cols, leaves - left)}

    rows, cols = size(), size()
    for _ in range(20):
        if rows != cols or max_size == 2:
            break
        rows, cols = size(), size()
    tree = build(rows, cols, count)
    leaves_in_order: list[dict[str, Any]] = []

    def collect(node: dict[str, Any]) -> None:
        if node["op"] == "leaf":
            leaves_in_order.append(node)
            return
        collect(node["a"])
        if "b" in node:
            collect(node["b"])

    collect(tree)
    for index, leaf in enumerate(leaves_in_order):
        r, c = leaf["shape"]
        leaf["name"] = "ABCD"[index]
        leaf["value"] = sp.Matrix(r, c, lambda *_: random.randint(low, high))

    def render(node: dict[str, Any], latex: bool, wrap_sum: bool = False) -> str:
        op = node["op"]
        if op == "leaf":
            return node["name"]
        if op == "T":
            inner = render(node["a"], latex)
            if node["a"]["op"] != "leaf":
                inner = f"\\left({inner}\\right)" if latex else f"({inner})"
            return f"{inner}^T"
        sym = {"+": "+", "-": "-", "*": "\\cdot " if latex else "*"}[op]
        left = render(node["a"], latex)
        right = render(node["b"], latex)
        if op == "*":
            wrap_left = node["a"]["op"] in "+-"
            wrap_right = node["b"]["op"] in "+-"
        else:
            wrap_left = False
            wrap_right = op == "-" and node["b"]["op"] in "+-"
        if wrap_left:
            left = f"\\left({left}\\right)" if latex else f"({left})"
        if wrap_right:
            right = f"\\left({right}\\right)" if latex else f"({right})"
        return f"{left}{sym}{right}" if op == "*" else f"{left} {sym} {right}"

    def evaluate(node: dict[str, Any]) -> sp.Matrix:
        op = node["op"]
        if op == "leaf":
            return node["value"]
        if op == "T":
            return evaluate(node["a"]).T
        a, b = evaluate(node["a"]), evaluate(node["b"])
        return a + b if op == "+" else a - b if op == "-" else a * b

    steps: list[tuple[str, str, sp.Matrix, str]] = []

    def walk(node: dict[str, Any]) -> None:
        if node["op"] == "leaf":
            return
        walk(node["a"])
        if "b" in node:
            walk(node["b"])
        value = evaluate(node)
        note = ""
        if node["op"] == "*":
            ar, ac = evaluate(node["a"]).shape
            br, bc = evaluate(node["b"]).shape
            note = f"({ar}x{ac})*({br}x{bc}) -> {ar}x{bc}"
        steps.append((render(node, False), render(node, True), value, note))

    walk(tree)
    result = evaluate(tree)

    def rows_of(matrix: sp.Matrix) -> list[sp.Matrix]:
        return [matrix.row(i) for i in range(matrix.rows)]

    definitions_text = ", ".join(f"{leaf['name']}={mat_text(leaf['value'])}" for leaf in leaves_in_order)
    definitions_latex = ",\\ ".join(
        f"{leaf['name']}={rows_matrix_latex(rows_of(leaf['value']))}" for leaf in leaves_in_order
    )
    expr_text = render(tree, False)
    expr_latex = render(tree, True)
    task_text = f"Oblicz {expr_text}, gdzie {definitions_text}."
    task_latex = f"\\text{{Oblicz }}{expr_latex},\\ \\text{{gdzie }}{definitions_latex}"

    solution_text = "; ".join(
        f"{text} = {mat_text(value)}" + (f" [{note}]" if note else "") for text, _l, value, note in steps
    )
    solution_text += f". Wynik: {mat_text(result)} (wymiar {result.rows}x{result.cols})."
    solution_latex = ";\\ ".join(f"{latex}={rows_matrix_latex(rows_of(value))}" for _t, latex, value, _n in steps)
    return {
        "task": task_text,
        "solution": solution_text,
        "taskLatex": task_latex,
        "solutionLatex": solution_latex,
    }



# --- Zadanie 23: Macierz odwrotna (Gauss-Jordan lub dopełnienia algebraiczne) (backend/tasks/task_23_macierz_odwrotna.py) ---

DEFINITION_23 = {
    "id": 23,
    "name": "Macierz odwrotna (Gauss-Jordan lub dopełnienia algebraiczne)",
    "area": "Macierze",
    "example": "Przykład: oblicz macierz odwrotną macierzy A=[[1,2],[3,5]].",
    "description": "Dana jest macierz odwracalna A. Trzeba obliczyć macierz odwrotną A⁻¹ - metodą Gaussa-Jordana albo przez dopełnienia algebraiczne.",
    "parameterHelp": "Wybierz rozmiar macierzy kwadratowej, metodę rozwiązania oraz czy macierz odwrotna ma mieć same liczby całkowite (wyznacznik ±1).",
    "examInstruction": "",
    "params": [
        {"key": "dimension", "label": "Rozmiar macierzy (n x n)", "type": "number", "min": 2, "max": 4, "value": 3},
        {"key": "method", "label": "Metoda (0 - operacje elementarne [A|I], 1 - dopełnienia algebraiczne)", "type": "number", "min": 0, "max": 1, "value": 0},
        {"key": "integerInverse", "label": "Odwrotna o całkowitych elementach (0 - nie, 1 - tak)", "type": "number", "min": 0, "max": 1, "value": 0},
        {"key": "coeffLower", "label": "Dolna wartość elementów", "type": "number", "min": -9, "max": 0, "value": -3},
        {"key": "coeffUpper", "label": "Górna wartość elementów", "type": "number", "min": 0, "max": 9, "value": 3},
    ],
}


def generate_23(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(4, rint(config, "dimension", 3)))
    use_adjugate = rint(config, "method", 0) == 1
    integer_inverse = rint(config, "integerInverse", 0) == 1
    max_det = {2: 9, 3: 12, 4: 16}[n]
    limit = max(abs(low), abs(high), 1)

    A = None
    for attempt in range(3000):
        if integer_inverse:
            candidate = sp.eye(n)
            for _ in range(random.randint(n, 2 * n)):
                i, j = random.sample(range(n), 2)
                candidate[i, :] = candidate[i, :] + random.choice([-2, -1, 1, 2]) * candidate[j, :]
            if random.random() < 0.5:
                i, j = random.sample(range(n), 2)
                candidate[i, :], candidate[j, :] = candidate[j, :], candidate[i, :]
            in_range = all(low <= v <= high for v in candidate)
            if attempt < 2500 and not in_range:
                continue
            if sum(1 for v in candidate if v != 0) <= n + 1:
                continue
        else:
            candidate = sp.Matrix(n, n, lambda *_: random.randint(low, high))
            det = candidate.det()
            if det == 0 or abs(det) > max_det:
                continue
        A = candidate
        break
    if A is None:
        raise ValueError("Nie udało się wylosować macierzy odwracalnej.")

    det = A.det()
    inverse = A.inv()
    A_latex = rows_matrix_latex([A.row(i) for i in range(n)])
    inv_latex = rows_matrix_latex([inverse.row(i) for i in range(n)])
    task_text = f"Oblicz macierz odwrotną macierzy A={mat_text(A)}."
    task_latex = f"\\text{{Oblicz }}A^{{-1}},\\ \\text{{gdzie }}A={A_latex}"

    if use_adjugate:
        cofactors = A.cofactor_matrix()
        adjugate = cofactors.T
        cof_latex = rows_matrix_latex([cofactors.row(i) for i in range(n)])
        adj_latex = rows_matrix_latex([adjugate.row(i) for i in range(n)])
        factor = sp.Rational(1, 1) / det
        if abs(det) == 1:
            factor_text = "" if det == 1 else "-"
            factor_latex = factor_text
        else:
            factor_text = f"({factor})*"
            factor_latex = sp.latex(factor)
        solution_text = (
            f"det A = {det} ≠ 0, więc macierz jest odwracalna. "
            f"Macierz dopełnień algebraicznych C = [(-1)^(i+j)·M_ij] = {mat_text(cofactors)}. "
            f"A^(-1) = (1/det A)·C^T = {factor_text}{mat_text(adjugate)} = {mat_text(inverse)}."
        )
        solution_latex = (
            f"\\det A={det}\\neq 0;\\ C=\\left[(-1)^{{i+j}}M_{{ij}}\\right]={cof_latex};\\ "
            f"A^{{-1}}=\\frac{{1}}{{\\det A}}C^{{T}}={factor_latex}{adj_latex}={inv_latex}"
        )
        return {"task": task_text, "solution": solution_text, "taskLatex": task_latex, "solutionLatex": solution_latex}

    def aug_text(state: sp.Matrix) -> str:
        rows = []
        for r in range(n):
            left = ", ".join(str(state[r, c]) for c in range(n))
            right = ", ".join(str(state[r, c]) for c in range(n, 2 * n))
            rows.append(f"[{left} | {right}]")
        return "[" + "; ".join(rows) + "]"

    def aug_latex(state: sp.Matrix) -> str:
        body = "\\\\".join("&".join(entry_latex(state[r, c]) for c in range(2 * n)) for r in range(n))
        return "\\left(\\begin{array}{" + "c" * n + "|" + "c" * n + "}" + body + "\\end{array}\\right)"

    state = A.row_join(sp.eye(n))
    steps: list[tuple[str, str, sp.Matrix]] = []
    for col in range(n):
        candidates = [r for r in range(col, n) if state[r, col] != 0]
        best = min(candidates, key=lambda r: abs(state[r, col]))
        ops_text: list[str] = []
        ops_latex: list[str] = []
        if best != col:
            state[col, :], state[best, :] = state[best, :], state[col, :]
            ops_text.append(f"w{col + 1} <-> w{best + 1}")
            ops_latex.append(f"w_{col + 1}\\leftrightarrow w_{best + 1}")
        pivot = state[col, col]
        if pivot != 1:
            scale = sp.Integer(1) / pivot
            state[col, :] = (scale * state[col, :]).applyfunc(sp.nsimplify)
            terms = [(scale, f"w{col + 1}")]
            ops_text.append(f"w{col + 1} -> {row_combination(terms, False)}")
            ops_latex.append(f"w_{col + 1}\\to {row_combination([(scale, f'w_{col + 1}')], True)}")
        for r in range(n):
            if r == col or state[r, col] == 0:
                continue
            a = state[r, col]
            state[r, :] = (state[r, :] - a * state[col, :]).applyfunc(sp.nsimplify)
            terms = [(sp.Integer(1), f"w{r + 1}"), (-a, f"w{col + 1}")]
            ops_text.append(f"w{r + 1} -> {row_combination(terms, False)}")
            ops_latex.append(
                f"w_{r + 1}\\to {row_combination([(c, nm.replace('w', 'w_')) for c, nm in terms], True)}"
            )
        if ops_text:
            steps.append((", ".join(ops_text), ",\\ ".join(ops_latex), state.copy()))

    assert state[:, n:] == inverse
    start = A.row_join(sp.eye(n))
    solution_text = (
        f"det A = {det} ≠ 0, więc macierz jest odwracalna. "
        "Do macierzy A dopisujemy macierz jednostkową i operacjami elementarnymi na wierszach sprowadzamy [A | I] do postaci [I | A^(-1)]. "
        f"[A | I] = {aug_text(start)}. "
        + " ".join(f"Krok {i}: {op}: {aug_text(s)}." for i, (op, _l, s) in enumerate(steps, 1))
        + f" A^(-1) = {mat_text(inverse)}."
    )
    solution_latex = f"\\det A={det}\\neq 0;\\ \\left[A\\mid I\\right]={aug_latex(start)}"
    for _t, op_latex, s in steps:
        solution_latex += f"\\xrightarrow{{{op_latex}}}{aug_latex(s)}"
    solution_latex += f"\\qquad A^{{-1}}={inv_latex}"
    return {"task": task_text, "solution": solution_text, "taskLatex": task_latex, "solutionLatex": solution_latex}



# --- Zadanie 24: Liniowa niezależność wektorów (za pomocą rzędu) (backend/tasks/task_24_niezaleznosc_rzad.py) ---

DEFINITION_24 = {
    "id": 24,
    "name": "Liniowa niezależność wektorów (za pomocą rzędu)",
    "area": "Przestrzenie liniowe",
    "example": "Przykład: zbadaj za pomocą rzędu niezależność [2,-1,3], [0,4,-5].",
    "description": "Dany jest układ wektorów. Trzeba zbadać jego liniową niezależność, porównując rząd macierzy utworzonej z tych wektorów z ich liczbą.",
    "parameterHelp": "Ustaw wymiar i liczbę wektorów, szansę na niezależność oraz czy współrzędne mogą być urojone (np. 2i). Zakres współrzędnych domyślnie od -5 do 5.",
    "examInstruction": "Zbadaj za pomocą rzędu liniową niezależność wektorów.",
    "params": [
        {"key": "dimension", "label": "Wymiar wektorów", "type": "number", "min": 2, "max": 5, "value": 3},
        {"key": "vectors", "label": "Liczba wektorów", "type": "number", "min": 2, "max": 6, "value": 2},
        {"key": "independentPercent", "label": "Szansa na wektory niezależne (%)", "type": "number", "min": 0, "max": 100, "value": 50},
        {"key": "complexEntries", "label": "Liczby zespolone we współrzędnych (0 - nie, 1 - tak)", "type": "number", "min": 0, "max": 1, "value": 0},
        {"key": "coeffLower", "label": "Dolna wartość współrzędnej", "type": "number", "min": -9, "max": 0, "value": -5},
        {"key": "coeffUpper", "label": "Górna wartość współrzędnej", "type": "number", "min": 0, "max": 9, "value": 5},
    ],
}


def generate_24(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(5, rint(config, "dimension", 3)))
    k = max(2, min(6, rint(config, "vectors", 2)))
    percent = max(0, min(100, rint(config, "independentPercent", 50)))
    use_complex = rint(config, "complexEntries", 0) == 1
    want_independent = k <= n and random.random() * 100 < percent
    limit = 3 * max(abs(low), abs(high), 1) + 3

    def entry() -> sp.Expr:
        if use_complex and random.random() < 0.35:
            return rand_non_zero(-3, 3) * sp.I
        return sp.Integer(random.randint(low, high))

    for _ in range(1000):
        vectors = [sp.Matrix([entry() for _ in range(n)]) for _ in range(k)]
        if not want_independent and k <= n:
            target = random.randrange(k)
            coeffs = [random.randint(-2, 2) for _ in range(k)]
            if not any(c for i, c in enumerate(coeffs) if i != target):
                continue
            vectors[target] = sum(
                (c * v for i, (c, v) in enumerate(zip(coeffs, vectors)) if i != target), sp.zeros(n, 1)
            )
        if any(all(v == 0 for v in vec) for vec in vectors):
            continue
        rank = sp.Matrix.vstack(*[vec.T for vec in vectors]).rank(simplify=True)
        if k <= n and (rank == k) != want_independent:
            continue
        if k > n and rank == k:
            continue
        if any(max(abs(sp.re(v)), abs(sp.im(v))) > limit for vec in vectors for v in vec):
            continue
        if use_complex and all(sp.im(v) == 0 for vec in vectors for v in vec):
            continue
        break
    else:
        raise ValueError("Nie udało się wylosować zadania o niezależności za pomocą rzędu.")

    rows = [vec.T for vec in vectors]
    steps: list[tuple[str, str, list[sp.Matrix]]] = []
    pivot_row = 0
    for col in range(n):
        if pivot_row >= k:
            break
        candidates = [r for r in range(pivot_row, k) if sp.simplify(rows[r][col]) != 0]
        if not candidates:
            continue
        ops_text: list[str] = []
        ops_latex: list[str] = []
        best = candidates[0]
        if best != pivot_row:
            rows[pivot_row], rows[best] = rows[best], rows[pivot_row]
            ops_text.append(f"w{pivot_row + 1} <-> w{best + 1}")
            ops_latex.append(f"w_{pivot_row + 1}\\leftrightarrow w_{best + 1}")
        pivot = rows[pivot_row][col]
        for r in range(pivot_row + 1, k):
            a = sp.simplify(rows[r][col])
            if a == 0:
                continue
            ratio = sp.simplify(sp.expand(a / pivot))
            if is_gaussian_integer(ratio):
                terms = [(sp.Integer(1), f"w{r + 1}"), (-ratio, f"w{pivot_row + 1}")]
                rows[r] = (rows[r] - ratio * rows[pivot_row]).applyfunc(sp.simplify)
            else:
                scale_row, scale_pivot = pivot, a
                if pivot.is_Integer and a.is_Integer:
                    g = sp.igcd(int(pivot), int(a))
                    scale_row, scale_pivot = pivot / g, a / g
                    if scale_row < 0:
                        scale_row, scale_pivot = -scale_row, -scale_pivot
                terms = [(scale_row, f"w{r + 1}"), (-scale_pivot, f"w{pivot_row + 1}")]
                rows[r] = (scale_row * rows[r] - scale_pivot * rows[pivot_row]).applyfunc(
                    lambda v: sp.simplify(sp.expand(v))
                )
            ops_text.append(f"w{r + 1} -> {row_combination(terms, False)}")
            ops_latex.append(
                f"w_{r + 1}\\to {row_combination([(c, nm.replace('w', 'w_')) for c, nm in terms], True)}"
            )
        pivot_row += 1
        if ops_text:
            steps.append((", ".join(ops_text), ",\\ ".join(ops_latex), [row.copy() for row in rows]))

    nonzero = sum(1 for row in rows if any(sp.simplify(v) != 0 for v in row))
    assert nonzero == rank
    independent = rank == k

    def vec_row_text(vec: sp.Matrix) -> str:
        return "[" + ", ".join(complex_text(v) for v in vec) + "]"

    def vec_row_latex(vec: sp.Matrix) -> str:
        return "[" + ",".join(entry_latex(v) for v in vec) + "]"

    task_text = "Zbadaj za pomocą rzędu liniową niezależność układu wektorów: " + ", ".join(
        vec_row_text(v) for v in vectors
    ) + "."
    task_latex = ",\\ ".join(vec_row_latex(v) for v in vectors)

    solution_text = "Wektory zapisujemy jako wiersze macierzy A i sprowadzamy ją do postaci schodkowej operacjami elementarnymi na wierszach. "
    solution_latex = "A=" + rows_matrix_latex([v.T for v in vectors])
    if steps:
        parts = []
        for op_text, op_latex, state in steps:
            parts.append(f"{op_text}: " + "; ".join(vec_row_text(row.T) for row in state))
            solution_latex += f"\\xrightarrow{{{op_latex}}}" + rows_matrix_latex(state)
        solution_text += "Kolejne kroki: " + " | ".join(parts) + ". "
    else:
        solution_text += "Macierz ma już postać schodkową. "
    if independent:
        solution_text += f"Zatem rz(A) = {rank}, czyli tyle, ile wektorów ({k}), więc wektory są liniowo niezależne."
        verdict_latex = (
            f"\\qquad\\operatorname{{rz}}(A)={rank}\\text{{ (liczba wektorów: {k})}}"
            "\\Rightarrow\\text{ wektory są liniowo niezależne}"
        )
    else:
        solution_text += f"Zatem rz(A) = {rank} < {k} (liczba wektorów), więc wektory są liniowo zależne."
        verdict_latex = (
            f"\\qquad\\operatorname{{rz}}(A)={rank}<{k}\\Rightarrow\\text{{ wektory są liniowo zależne}}"
        )
    solution_latex += verdict_latex
    return {
        "task": task_text,
        "solution": solution_text,
        "taskLatex": task_latex,
        "solutionLatex": solution_latex,
    }



# --- Zadanie 25: Wartości i wektory własne macierzy (backend/tasks/task_25_wartosci_wlasne.py) ---

DEFINITION_25 = {
    "id": 25,
    "name": "Wartości i wektory własne macierzy",
    "area": "Wartości własne i diagonalizacja",
    "example": "Przykład: wyznacz wartości i wektory własne macierzy A=[[1,2],[2,1]] (wartości 3 i -1).",
    "description": "Dana jest macierz kwadratowa A. Trzeba wyznaczyć jej wartości własne i odpowiadające im wektory własne.",
    "parameterHelp": "Wartości własne są zawsze całkowite. Wybierz rozmiar macierzy, typ wartości własnych oraz ich zakres.",
    "examInstruction": "Wyznacz wartości własne i wektory własne macierzy A.",
    "params": [
        {"key": "dimension", "label": "Rozmiar macierzy (n x n)", "type": "number", "min": 2, "max": 4, "value": 2},
        {"key": "eigenType", "label": "Wartości własne (0 - różne, 1 - powtarzające się i diagonalizowalna, 2 - powtarzające się i niediagonalizowalna)", "type": "number", "min": 0, "max": 2, "value": 0},
        {"key": "coeffLower", "label": "Dolna wartość własna", "type": "number", "min": -9, "max": 0, "value": -3},
        {"key": "coeffUpper", "label": "Górna wartość własna", "type": "number", "min": 0, "max": 9, "value": 3},
    ],
}


def generate_25(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(4, rint(config, "dimension", 2)))
    eigen_type = max(0, min(2, rint(config, "eigenType", 0)))
    if n == 2 and eigen_type == 1:
        eigen_type = 0
    distinct_needed = n if eigen_type == 0 else n - 1
    pool = list(range(low, high + 1))
    extra = high + 1
    while len(pool) < distinct_needed:
        pool.append(extra)
        extra += 1

    lam = sp.Symbol("lambda")
    max_zeros = (n * n) // 5
    for _ in range(20000):
        values = random.sample(pool, distinct_needed)
        if eigen_type != 0:
            values.append(random.choice(values))
        values.sort()
        D = sp.diag(*values)
        if eigen_type == 2:
            first = next(i for i in range(n - 1) if values[i] == values[i + 1])
            D[first, first + 1] = 1
        P = sp.eye(n)
        for _ in range(random.randint(n, 2 * n)):
            i, j = random.sample(range(n), 2)
            P[i, :] = P[i, :] + random.choice([-1, 1]) * P[j, :]
        perm = list(range(n))
        random.shuffle(perm)
        P = P.extract(perm, list(range(n)))
        A = P * D * P.inv()
        if max(abs(v) for v in A) > 9:
            continue
        if sum(1 for v in A if v == 0) > max_zeros:
            continue
        if all(A[i, j] == 0 for i in range(n) for j in range(n) if i != j):
            continue
        break
    else:
        raise ValueError("Nie udało się wylosować macierzy o ładnych wartościach własnych.")

    A_latex = rows_matrix_latex([A.row(i) for i in range(n)])
    task_text = f"Wyznacz wartości własne i wektory własne macierzy A={mat_text(A)}."
    task_latex = f"A={A_latex}"

    poly = sp.expand((A - lam * sp.eye(n)).det())
    multiplicities: dict[int, int] = {}
    for value in values:
        multiplicities[value] = multiplicities.get(value, 0) + 1

    def factor_text(value: int, latex: bool) -> str:
        name = "\\lambda" if latex else "λ"
        if value == 0:
            return f"(-{name})"
        return f"({value}-{name})"

    def power(value: int, mult: int, latex: bool) -> str:
        base = factor_text(value, latex)
        return base if mult == 1 else (f"{base}^{{{mult}}}" if latex else f"{base}^{mult}")

    product_text = "".join(power(v, m, False) for v, m in multiplicities.items())
    product_latex = "".join(power(v, m, True) for v, m in multiplicities.items())
    poly_text = str(poly).replace("**", "^").replace("*", "").replace("lambda", "λ")
    eigenvalues_text = ", ".join(
        f"λ={v}" + (f" (krotność {m})" if m > 1 else "") for v, m in multiplicities.items()
    )
    eigenvalues_latex = ",\\ ".join(
        f"\\lambda={v}" + (f"\\ (\\text{{krotność }}{m})" if m > 1 else "") for v, m in multiplicities.items()
    )

    solution_text = (
        f"Wielomian charakterystyczny: det(A-λI) = {poly_text} = {product_text}. "
        f"Wartości własne: {eigenvalues_text}. "
    )
    solution_latex = (
        f"\\det(A-\\lambda I)={sp.latex(poly)}={product_latex}=0\\Rightarrow {eigenvalues_latex}"
    )
    for value, mult in multiplicities.items():
        M = A - value * sp.eye(n)
        rref, _pivots = M.rref()
        basis = []
        for vec in M.nullspace():
            scaled = clear_denominators(vec)
            basis.append(scaled)
        geometric = len(basis)
        names_text = [f"t{i}" for i in range(1, geometric + 1)]
        names_latex = [f"t_{i}" for i in range(1, geometric + 1)]
        if geometric == 1:
            names_text, names_latex = ["t"], ["t"]
        combo_text = " + ".join(f"{nt}*{vec_text(b)}" for nt, b in zip(names_text, basis))
        combo_latex = "+".join(f"{nl}{pmatrix_latex(b)}" for nl, b in zip(names_latex, basis))
        nonzero_text = "t ≠ 0" if geometric == 1 else f"({', '.join(names_text)}) ≠ ({', '.join('0' for _ in names_text)})"
        nonzero_latex = "t\\neq 0" if geometric == 1 else f"({','.join(names_latex)})\\neq 0"
        label = f"A{'-' if value > 0 else '+'}{abs(value)}I" if value != 0 else "A"
        solution_text += (
            f"Dla λ={value}: {label} = {mat_text(M)}, "
            f"rz({label}) = {n - geometric}, wymiar przestrzeni własnej: {geometric}. "
            f"Wektory własne: v = {combo_text}, {nonzero_text}. "
        )
        solution_latex += (
            f"\\qquad\\lambda={value}:\\ {label}={rows_matrix_latex([M.row(i) for i in range(n)])}"
            f"\\sim{rows_matrix_latex([rref.row(i) for i in range(n)])}"
            f"\\Rightarrow v={combo_latex},\\ {nonzero_latex}"
        )
    if any(len((A - v * sp.eye(n)).nullspace()) < m for v, m in multiplicities.items()):
        solution_text += "Macierz nie jest diagonalizowalna (dla wartości o krotności większej niż wymiar przestrzeni własnej)."
    return {
        "task": task_text,
        "solution": solution_text.strip(),
        "taskLatex": task_latex,
        "solutionLatex": solution_latex,
    }



# --- Zadanie 26: Równania macierzowe (AX=B, XA=B, AXB=C, AX+B=C, ...) (backend/tasks/task_26_rownania_macierzowe.py) ---

DEFINITION_26 = {
    "id": 26,
    "name": "Równania macierzowe (AX=B, XA=B, AXB=C, AX+B=C, ...)",
    "area": "Macierze",
    "example": "Przykład: rozwiąż równanie macierzowe AX=B dla macierzy odwracalnej A.",
    "description": "Dane jest równanie macierzowe typu AX=B, XA=B, AXB=C lub AX+B=C z podanymi macierzami. Trzeba wyznaczyć niewiadomą macierz X.",
    "parameterHelp": "Wybierz rozmiar macierzy i typ równania (0 - losowy). Szukana macierz X ma zawsze całkowite elementy.",
    "examInstruction": "Rozwiąż równanie macierzowe.",
    "params": [
        {"key": "dimension", "label": "Rozmiar macierzy (n x n)", "type": "number", "min": 2, "max": 3, "value": 2},
        {"key": "equationType", "label": "Typ równania (0 - losowy, 1 - AX=B, 2 - XA=B, 3 - AXB=C, 4 - AX±B=C, 5 - XA±B=C, 6 - AX±X=B)", "type": "number", "min": 0, "max": 6, "value": 0},
        {"key": "integerInverse", "label": "Odwracane macierze o całkowitych odwrotnych (0 - nie, 1 - tak)", "type": "number", "min": 0, "max": 1, "value": 0},
        {"key": "coeffLower", "label": "Dolna wartość elementów", "type": "number", "min": -9, "max": 0, "value": -3},
        {"key": "coeffUpper", "label": "Górna wartość elementów", "type": "number", "min": 0, "max": 9, "value": 3},
    ],
}


def generate_26(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(3, rint(config, "dimension", 2)))
    kind = rint(config, "equationType", 0)
    if kind not in range(1, 7):
        kind = random.randint(1, 6)
    unimodular = rint(config, "integerInverse", 0) == 1
    max_det = {2: 9, 3: 8}[n]
    identity = sp.eye(n)

    def random_matrix() -> sp.Matrix:
        while True:
            M = sp.Matrix(n, n, lambda *_: random.randint(low, high))
            if any(v != 0 for v in M):
                return M

    def invertible() -> sp.Matrix:
        for attempt in range(2000):
            if unimodular:
                M = sp.eye(n)
                for _ in range(random.randint(n, 2 * n)):
                    i, j = random.sample(range(n), 2)
                    M[i, :] = M[i, :] + random.choice([-2, -1, 1, 2]) * M[j, :]
                if attempt < 1500 and not all(low <= v <= high for v in M):
                    continue
                if sum(1 for v in M if v != 0) <= n + 1:
                    continue
                return M
            M = random_matrix()
            det = M.det()
            if det != 0 and abs(det) <= max_det:
                return M
        raise ValueError("Nie udało się wylosować macierzy odwracalnej.")

    sign = random.choice([1, -1])
    sign_char = "+" if sign == 1 else "-"
    X = random_matrix()
    left = right = None
    left_name = right_name = ""
    left_latex = right_latex = ""
    given: dict[str, sp.Matrix] = {}
    pre_text = pre_latex = ""
    rhs_name, rhs_latex_name = "B", "B"
    if kind == 1:
        A = invertible()
        B = A * X
        given = {"A": A, "B": B}
        left, left_name, left_latex = A, "A", "A"
        rhs = B
        eq_text = eq_latex = "AX=B"
        derivation_text = "Mnożymy równanie lewostronnie przez A^(-1)"
    elif kind == 2:
        A = invertible()
        B = X * A
        given = {"A": A, "B": B}
        right, right_name, right_latex = A, "A", "A"
        rhs = B
        eq_text = eq_latex = "XA=B"
        derivation_text = "Mnożymy równanie prawostronnie przez A^(-1)"
    elif kind == 3:
        A, B = invertible(), invertible()
        C = A * X * B
        given = {"A": A, "B": B, "C": C}
        left, left_name, left_latex = A, "A", "A"
        right, right_name, right_latex = B, "B", "B"
        rhs = C
        rhs_name = rhs_latex_name = "C"
        eq_text = eq_latex = "AXB=C"
        derivation_text = "Mnożymy równanie lewostronnie przez A^(-1) i prawostronnie przez B^(-1)"
    elif kind in (4, 5):
        A = invertible()
        B = random_matrix()
        C = A * X + sign * B if kind == 4 else X * A + sign * B
        given = {"A": A, "B": B, "C": C}
        rhs = C - sign * B
        rhs_name = f"(C{'-' if sign == 1 else '+'}B)"
        rhs_latex_name = f"\\left(C{'-' if sign == 1 else '+'}B\\right)"
        if kind == 4:
            left, left_name, left_latex = A, "A", "A"
            eq_text = eq_latex = f"AX{sign_char}B=C"
            moved = f"AX = C{'-' if sign == 1 else '+'}B"
            moved_latex = f"AX=C{'-' if sign == 1 else '+'}B"
            derivation_text = "Mnożymy lewostronnie przez A^(-1)"
        else:
            right, right_name, right_latex = A, "A", "A"
            eq_text = eq_latex = f"XA{sign_char}B=C"
            moved = f"XA = C{'-' if sign == 1 else '+'}B"
            moved_latex = f"XA=C{'-' if sign == 1 else '+'}B"
            derivation_text = "Mnożymy prawostronnie przez A^(-1)"
        pre_text = f"Przenosimy B na prawą stronę: {moved} = {mat_text(rhs)}. "
        pre_latex = f"{moved_latex}={rows_matrix_latex([rhs.row(i) for i in range(n)])}\\qquad "
    else:
        M = invertible()
        A = M - sign * identity
        B = M * X
        given = {"A": A, "B": B}
        name = f"A{sign_char}I"
        left, left_name, left_latex = M, f"({name})", f"\\left({name}\\right)"
        rhs = B
        eq_text = eq_latex = f"AX{sign_char}X=B"
        pre_text = f"Wyłączamy X przed nawias: ({name})X = B. A{sign_char}I = {mat_text(M)}. "
        pre_latex = (
            f"\\left({name}\\right)X=B,\\ {name}={rows_matrix_latex([M.row(i) for i in range(n)])}\\qquad "
        )
        derivation_text = f"Mnożymy lewostronnie przez ({name})^(-1)"

    def matrix_latex(M: sp.Matrix) -> str:
        return rows_matrix_latex([M.row(i) for i in range(M.rows)])

    definitions_text = ", ".join(f"{k}={mat_text(v)}" for k, v in given.items())
    definitions_latex = ",\\ ".join(f"{k}={matrix_latex(v)}" for k, v in given.items())
    task_text = f"Rozwiąż równanie macierzowe {eq_text}, gdzie {definitions_text}."
    task_latex = f"{eq_latex},\\ \\text{{gdzie }}{definitions_latex}"

    values: list[sp.Matrix] = []
    solution_text = pre_text + f"{derivation_text}: "
    solution_latex = pre_latex
    formula_text = (f"{left_name}^(-1)" if left is not None else "") + rhs_name + (
        f"{right_name}^(-1)" if right is not None else ""
    )
    formula_latex = (f"{left_latex}^{{-1}}" if left is not None else "") + rhs_latex_name + (
        f"{right_latex}^{{-1}}" if right is not None else ""
    )
    solution_text += f"X = {formula_text}. "
    inverse_latex_parts: list[str] = []
    for matrix, name_text, name_latex in ((left, left_name, left_latex), (right, right_name, right_latex)):
        if matrix is None:
            continue
        det = matrix.det()
        inv = matrix.inv()
        solution_text += f"det {name_text} = {det} ≠ 0, {name_text}^(-1) = {mat_text(inv)}. "
        inverse_latex_parts.append(
            f"\\det {name_latex}={det}\\neq 0,\\ {name_latex}^{{-1}}={matrix_latex(inv)}"
        )
        if matrix is left:
            values.insert(0, inv)
        else:
            values.append(inv)
    values.insert(1 if left is not None else 0, rhs)
    result = values[0]
    for value in values[1:]:
        result = result * value
    assert result == X
    solution_text += (
        "X = " + " * ".join(mat_text(v) for v in values) + f" = {mat_text(X)}."
    )
    product_latex = "\\cdot ".join(matrix_latex(v) for v in values)
    solution_latex += f"X={formula_latex}\\qquad " + "\\qquad ".join(inverse_latex_parts)
    solution_latex += f"\\qquad X={product_latex}={matrix_latex(X)}"
    return {
        "task": task_text,
        "solution": solution_text,
        "taskLatex": task_latex,
        "solutionLatex": solution_latex,
    }



# --- Zadanie 27: Diagonalizacja lub forma Jordana z potęgowaniem A^k (backend/tasks/task_27_potegowanie_diagonalizacja.py) ---

DEFINITION_27 = {
    "id": 27,
    "name": "Diagonalizacja lub forma Jordana z potęgowaniem A^k",
    "area": "Wartości własne i diagonalizacja",
    "example": "Przykład: zdiagonalizuj A=[[2,1],[0,3]] i oblicz A^4.",
    "description": "Dana jest macierz A i wykładnik k. Trzeba wyznaczyć jej postać diagonalną lub formę Jordana i na tej podstawie obliczyć potęgę Aᵏ.",
    "parameterHelp": "Ustaw rozmiar macierzy, granice elementów oraz zakres losowanego wykładnika potęgi.",
    "examInstruction": "Wyznacz postać diagonalną lub formę Jordana macierzy A, a następnie oblicz wskazaną potęgę.",
    "params": [
        {"key": "size", "label": "Rozmiar macierzy", "type": "number", "min": 2, "max": 4, "value": 3},
        {"key": "coeffLower", "label": "Maksymalna dolna wartość elementu", "type": "number", "min": -12, "max": 0, "value": -6},
        {"key": "coeffUpper", "label": "Maksymalna górna wartość elementu", "type": "number", "min": 0, "max": 12, "value": 6},
        {"key": "powerLower", "label": "Minimalny wykładnik potęgi", "type": "number", "min": 2, "max": 10, "value": 2},
        {"key": "powerUpper", "label": "Maksymalny wykładnik potęgi", "type": "number", "min": 2, "max": 10, "value": 5},
    ],
}


def generate_27(config: dict[str, Any]) -> dict[str, Any]:
    low, high = bounds(config)
    n = max(2, min(4, rint(config, "size", 3)))
    k_low = max(2, rint(config, "powerLower", 2))
    k_high = max(k_low, rint(config, "powerUpper", 5))
    k = random.randint(k_low, k_high)
    jordan = random.random() < 0.5
    A, D, P = random_diag_jordan_matrix(n, low, high, jordan)
    Ak = sp.expand(P * D**k * P.inv())
    form = "J" if jordan else "D"
    verdict = "Macierz niediagonalizowalna" if jordan else "Macierz diagonalizowalna"
    steps = diagonalization_steps(A, D, P, jordan)
    power_note = (
        "Potęga klatki Jordana: na przekątnej λ^k, nad przekątną k·λ^(k-1)"
        if jordan else "Potęga macierzy diagonalnej: potęgujemy elementy na przekątnej"
    )
    steps.append(step(power_note, f"{form}^{{{k}}}={matrix_latex(D**k)}"))
    steps.append(step(
        "Mnożymy",
        f"A^{{{k}}}=P{form}^{{{k}}}P^{{-1}}={matrix_latex(P)}{matrix_latex(D**k)}{matrix_latex(P.inv())}={matrix_latex(Ak)}",
    ))
    return {
        "task": (
            f"Wyznacz postać zdiagonalizowaną lub formę Jordana macierzy A={mat_text(A)}, "
            f"a następnie oblicz A^{k}."
        ),
        "solution": (
            f"{verdict}. {form}={mat_text(D)}, P={mat_text(P)}; "
            f"A^{k} = P*{form}^{k}*P^(-1) = {mat_text(Ak)}."
        ),
        "taskLatex": f"A={sp.latex(A)},\\ A^{{{k}}}=?",
        "solutionLatex": (
            f"{form}={sp.latex(D)},\\ P={sp.latex(P)},\\ "
            f"A^{{{k}}}=P{form}^{{{k}}}P^{{-1}}={sp.latex(Ak)}"
        ),
        "steps": steps,
    }



# Zadania w kolejności id - jak w rejestrze zadań backendu.
TASKS: dict[int, tuple[dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    definition["id"]: (definition, generate)
    for definition, generate in sorted(
        [
            (DEFINITION_01, generate_01),
            (DEFINITION_02, generate_02),
            (DEFINITION_03, generate_03),
            (DEFINITION_04, generate_04),
            (DEFINITION_05, generate_05),
            (DEFINITION_06, generate_06),
            (DEFINITION_07, generate_07),
            (DEFINITION_08, generate_08),
            (DEFINITION_09, generate_09),
            (DEFINITION_10, generate_10),
            (DEFINITION_11, generate_11),
            (DEFINITION_12, generate_12),
            (DEFINITION_13, generate_13),
            (DEFINITION_14, generate_14),
            (DEFINITION_15, generate_15),
            (DEFINITION_16, generate_16),
            (DEFINITION_17, generate_17),
            (DEFINITION_18, generate_18),
            (DEFINITION_19, generate_19),
            (DEFINITION_20, generate_20),
            (DEFINITION_21, generate_21),
            (DEFINITION_22, generate_22),
            (DEFINITION_23, generate_23),
            (DEFINITION_24, generate_24),
            (DEFINITION_25, generate_25),
            (DEFINITION_26, generate_26),
            (DEFINITION_27, generate_27),
        ],
        key=lambda task: task[0]["id"],
    )
}


main()
