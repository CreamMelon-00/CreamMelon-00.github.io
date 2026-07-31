"""lore/ 의 md 문서를 읽어 단일 HTML 뷰어(wikipage/위키.html)를 만든다.

    python tools/build_wiki.py

서버도 localStorage도 필요 없다. 데이터가 HTML 안에 박히므로 더블클릭으로 열린다.
편집은 lore/*.md 에서 하고, 이 스크립트를 다시 돌려 뷰어를 갱신한다.
"""

import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
LORE = ROOT / "lore"
OUT = ROOT / "wikipage" / "위키.html"

FOLDER_ORDER = [
    "00_홈", "01_핵심 규칙", "02_지역", "03_세력", "04_인물",
    "05_사건", "06_기술과 병기", "07_연표", "08_서사", "09_검토", "미분류",
]

KEYS = {
    "id": "id", "분류": "type", "폴더": "folder", "상위문서": "parentTitle",
    "정렬순서": "sortOrder", "시대": "era", "정사": "canon",
    "상태": "status", "태그": "tags", "검토메모": "memo",
    "이미지": "image", "이미지설명": "imageCaption",
}


def unquote(v):
    """fm() 이 JSON.stringify 로 쓴 값을 되돌린다."""
    v = v.strip()
    if v.startswith('"'):
        try:
            return json.loads(v)
        except ValueError:
            return v.strip('"')
    return v


def parse(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"프론트매터가 없음: {path}")
    _, fm, body = text.split("---", 2)

    doc = {
        "title": path.stem, "id": "", "type": "메모", "folder": "미분류",
        "parentTitle": "", "sortOrder": 0, "era": "미정", "canon": "잠정",
        "status": "작성 중", "tags": [], "memo": "", "image": "",
        "imageCaption": "", "summary": [], "content": body.strip(),
    }

    lines = fm.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()

        # '개요:' 는 들여쓴 줄들을 순서 그대로 표의 행으로 받는다.
        if k == "개요":
            while i < len(lines) and lines[i].startswith((" ", "\t")):
                row = lines[i].strip()
                i += 1
                if ":" not in row:
                    continue
                rk, rv = row.split(":", 1)
                doc["summary"].append([rk.strip(), unquote(rv)])
            continue

        key = KEYS.get(k)
        if not key:
            continue
        if key == "tags":
            doc[key] = [t.strip() for t in v.split(",") if t.strip()]
        elif key == "sortOrder":
            doc[key] = int(v or 0)
        else:
            doc[key] = unquote(v)

    if not doc["id"]:
        doc["id"] = re.sub(r"\W+", "-", path.stem.lower())
    return doc


def load_templates():
    """lore/_틀/*.md → 탐색 틀. 여러 문서에서 ```틀 로 불러 쓴다.

    형식:
        제목: 미트로프 연방
        색조: 208
        ---
        통치: [[의회]] · [[통합파]]
        무력: [[헌병대]] · [[경찰청]]
    """
    out = {}
    box = LORE / "_틀"
    if not box.is_dir():
        return out
    for p in sorted(box.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        head, _, rows = text.partition("\n---\n")
        meta = {}
        for line in head.strip().split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        groups = []
        for line in rows.strip().split("\n"):
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            groups.append([k.strip(), v.strip()])
        out[p.stem] = {"title": meta.get("제목", p.stem),
                       "hue": meta.get("색조", ""), "groups": groups}
    return out


def build():
    if not LORE.is_dir():
        sys.exit(f"lore 폴더가 없습니다: {LORE}")

    # '_' 로 시작하는 파일과 폴더는 문서가 아니다 (_틀/ 등)
    def is_doc(p):
        return not p.name.startswith("_") and not any(
            part.startswith("_") for part in p.relative_to(LORE).parts[:-1])

    docs = [parse(p) for p in sorted(LORE.rglob("*.md")) if is_doc(p)]
    if not docs:
        sys.exit("lore/ 에 md 문서가 없습니다.")

    templates = load_templates()

    by_title = {d["title"]: d["id"] for d in docs}
    dangling = []
    for d in docs:
        parent = d.pop("parentTitle", "")
        d["parentId"] = by_title.get(parent) if parent else None
        if parent and not d["parentId"]:
            dangling.append((d["title"], parent))

    # 아직 문서가 없는 [[링크]] 를 모아 보고한다 — 앞으로 쓸 것의 목록이 된다.
    known = set(by_title)
    todo = {}
    for d in docs:
        for name in re.findall(r"\[\[([^\]|#]+)", d["content"]):
            name = name.strip()
            if name not in known:
                todo.setdefault(name, []).append(d["title"])

    payload = {"docs": docs, "folderOrder": FOLDER_ORDER, "templates": templates,
               "todo": {k: sorted(set(v)) for k, v in sorted(todo.items())}}

    template = (ROOT / "tools" / "wiki_template.html").read_text(encoding="utf-8")
    html = template.replace(
        "/*__DATA__*/null",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
    OUT.write_text(html, encoding="utf-8")

    print(f"문서 {len(docs)}밋 → {OUT.relative_to(ROOT)}  ({len(html) // 1024}KB)")
    if templates:
        print(f"틀 {len(templates)}개: {', '.join(templates)}")
    folders = {}
    for d in docs:
        folders[d["folder"]] = folders.get(d["folder"], 0) + 1
    for f in FOLDER_ORDER:
        if f in folders:
            print(f"  {f}: {folders[f]}")

    no_summary = [d["title"] for d in docs if not d["summary"] and d["type"] not in ("대문", "메모")]
    if no_summary:
        print(f"\n개요 표가 없는 문서 {len(no_summary)}밋: {', '.join(no_summary)}")

    missing_img = []
    for d in docs:
        if d["image"] and not (ROOT / d["image"]).exists():
            missing_img.append((d["title"], d["image"]))
    if missing_img:
        print(f"\n이미지 자리만 잡힌 문서 {len(missing_img)}밋 (파일을 넣으면 바로 보인다):")
        for t, src in missing_img:
            print(f"  {t}  →  {src}")
    if dangling:
        print("\n상위문서를 못 찾음:")
        for t, p in dangling:
            print(f"  {t} → '{p}'")
    if todo:
        print(f"\n아직 없는 문서를 가리키는 링크 {len(todo)}개:")
        for name, srcs in list(todo.items())[:20]:
            print(f"  {name}  ← {', '.join(srcs[:3])}")
        if len(todo) > 20:
            print(f"  … 그리고 {len(todo) - 20}개 더")


if __name__ == "__main__":
    build()
