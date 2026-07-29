# -*- coding: utf-8 -*-
"""storypack.json 직접 주입 CLI — 스튜디오를 거치지 않고 에피소드/배경을 반영한다.

사용법:
  python tools/inject_episode.py 대본.md [--order N] [--subtitle "부제"] [--cover asset_id]
  python tools/inject_episode.py --add-bg resource/bg/bg_A-1.png [--id-name bg_11]
  python tools/inject_episode.py --list
  python tools/inject_episode.py 대본.md --dry-run

동작:
  - md 첫 줄 "N편. 제목" 에서 제목을 읽는다 (전체 줄이 title이 됨).
  - 같은 title의 에피소드가 있으면 script/updatedAt만 갱신 (id 유지 → 방문자 진행도 보존).
  - 없으면 신규 추가. order 기본값은 (기존 최대 order + 1). "N편"의 N은 order로 쓰지 않는다
    (기존 팩이 1편 上/下를 order 1, 2로 나눠 쓰고 있어 편 번호와 order가 일치하지 않음).
  - 저장 전 storypack.json.bak 백업 생성, exportedAt 갱신 (플레이어가 이 값으로 변경을 감지함).
"""
import argparse
import base64
import copy
import json
import re
import secrets
import shutil
import string
import sys
from datetime import datetime, timezone
from pathlib import Path

PACK_PATH = Path(__file__).resolve().parent.parent / "data" / "storypack.json"

TITLE_RE = re.compile(r"^(\d+)편\s*[.:]\s*(.+)$")
BG_CMD_RE = re.compile(r"^@(?:배경|bg|집중|컷신|일러스트|focus|cutscene)\s+(\S+)\s*$", re.IGNORECASE)
# 파이프 없는 화자 줄로 흔히 쓰이는 표현 (스튜디오 문법은 "이름 | 소속"만 화자로 인식)
BARE_SPEAKER_RE = re.compile(r"^(일동|모두|전원)\s*$")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_BASELINE = None


def load_pack():
    global _BASELINE
    if not PACK_PATH.exists():
        sys.exit(f"storypack을 찾을 수 없습니다: {PACK_PATH}")
    with open(PACK_PATH, encoding="utf-8") as f:
        pack = json.load(f)
    # 파일에는 대본이 줄 배열로 저장된다 — 내부에서는 항상 문자열로 다룬다.
    for episode in pack.get("episodes", []):
        if isinstance(episode.get("script"), list):
            episode["script"] = "\n".join(episode["script"])
    _BASELINE = copy.deepcopy(pack)
    return pack


def save_if_changed(pack):
    """실제로 바뀐 게 있을 때만 저장한다.

    exportedAt만 바뀐 커밋은 방문자 전원에게 팩을 다시 내려받게 만들고
    diff도 지저분해지므로, 내용이 같으면 파일을 건드리지 않는다.
    """
    def content(p):
        return {k: v for k, v in p.items() if k != "exportedAt"}

    if _BASELINE is not None and content(pack) == content(_BASELINE):
        print("변경 사항이 없습니다 — storypack을 저장하지 않았습니다.")
        return False
    pack["exportedAt"] = now_iso()
    save_pack(pack)
    return True


def save_pack(pack):
    # 대본을 줄 배열로 풀어서 저장한다. 한 줄짜리 거대 JSON은 git/SourceTree가
    # 글자 단위 비교를 시도하며 멈추고, 무엇이 바뀌었는지도 읽을 수 없다.
    out = copy.deepcopy(pack)
    for episode in out.get("episodes", []):
        if isinstance(episode.get("script"), str):
            episode["script"] = episode["script"].split("\n")

    backup = PACK_PATH.with_suffix(".json.bak")
    shutil.copy2(PACK_PATH, backup)
    tmp = PACK_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(PACK_PATH)
    print(f"저장 완료: {PACK_PATH} (백업: {backup.name})")


META_KEYS = {"부제": "subtitle", "설명": "description", "커버": "cover",
              "챕터": "chapter", "순서": "order", "역할": "role",
              "주역": "focal", "기호": "motif",
              "사건": "event", "날짜": "eventDate",
              # 미니게임 (역할: 미니게임) 전용
              "게임": "game", "주파수": "gameTarget",
              "전문": "gameMessage", "안내": "gameBrief"}
ROLE_MAP = {"프롤로그": "prologue", "에필로그": "epilogue",
            "미니게임": "minigame",
            "prologue": "prologue", "epilogue": "epilogue",
            "minigame": "minigame"}

# 본편에서 언급/회상되는 과거 사건. 한 화에 여러 줄 쓸 수 있다.
PAST_KEY = "과거사건"

# 과거 시점(역)의 배열 순서. 대본의 상대 표기를 그대로 쓰되 순서만 여기서 정한다.
# 앞에 있을수록 오래된 과거. 여기 없는 시점은 목록 맨 뒤(본편 직전)에 붙는다.
ERA_ORDER = [
    "먼 과거",
    "10년 전",
    "연말 위기",
    "과거 어느 날",
    "3년 전",
    "몇 달 전",
    "두 달 전",
    "몇 주 전",
]


def parse_md(md_path):
    """대본 md → (title, script, ep_num, meta).

    첫 비어있지 않은 줄을 제목으로 사용한다. "N편. 제목" 꼴이면 접두어를 떼어
    제목만 남기고 (기존 팩의 제목 관례), N은 order 기본값으로 쓴다.

    제목 바로 다음에 "부제: ...", "커버: ...", "챕터: ...", "순서: ...",
    "설명: ..." 메타 줄을 적을 수 있다 (선택). 본문이 시작되면 더 읽지 않는다.
    """
    text = Path(md_path).read_text(encoding="utf-8").replace("\r\n", "\n")
    lines = text.split("\n")

    title = None
    ep_num = None
    body_start = 0
    for i, line in enumerate(lines):
        if line.strip():
            title = line.strip().lstrip("#").strip()
            body_start = i + 1
            break
    if not title:
        sys.exit("대본이 비어 있습니다.")

    m = TITLE_RE.match(title)
    if m:
        ep_num = int(m.group(1))
        title = m.group(2).strip()

    # 제목 뒤 메타 줄 수집
    meta = {}
    past = []          # 과거사건: 시점 | 사건  (여러 줄 가능)
    i = body_start
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        key, sep, value = s.partition(":")
        key = key.strip()
        if sep and key == PAST_KEY:
            # 과거사건: 시점 | 사건 [| 주역]
            parts = [x.strip() for x in value.split("|")]
            era = parts[0] if parts else ""
            text = parts[1] if len(parts) > 1 else ""
            focal = parts[2] if len(parts) > 2 else ""
            if not era or not text:
                print(f"경고: 과거사건 형식은 '과거사건: 시점 | 사건 [| 주역]' 입니다 → {s}")
            else:
                entry = {"era": era, "text": text}
                if focal:
                    entry["focal"] = focal
                past.append(entry)
            i += 1
            continue
        if sep and key in META_KEYS:
            meta[META_KEYS[key]] = value.strip()
            i += 1
            continue
        break
    body_start = i
    if past:
        meta["pastEvents"] = past

    body = []
    for line in lines[body_start:]:
        stripped = line.strip()
        # 스튜디오는 "이름 | 소속" 형식만 화자로 인식 → 파이프 없는 화자 줄 보정
        if BARE_SPEAKER_RE.match(stripped):
            stripped = f"{stripped} |"
        body.append(stripped)

    script = "\n".join(body).strip("\n")
    return title, script, ep_num, meta


def check_assets(script, pack):
    """@배경/@집중 명령이 참조하는 asset id가 팩에 있는지 검사."""
    asset_ids = {a["id"] for a in pack.get("assets", [])}
    missing = []
    for line in script.split("\n"):
        m = BG_CMD_RE.match(line.strip())
        if m and m.group(1) not in asset_ids and m.group(1) != "0":
            missing.append(m.group(1))
    return sorted(set(missing))


def default_chapter(pack):
    chapters = pack.get("meta", {}).get("chapters") or []
    return chapters[0]["key"] if chapters else ""


def ensure_chapter(pack, chapter):
    """meta.chapters에 없는 챕터명이면 자동 등록한다."""
    if not chapter:
        return
    chapters = pack.setdefault("meta", {}).setdefault("chapters", [])
    if any(c.get("key") == chapter for c in chapters):
        return
    order = max((int(c.get("order") or 0) for c in chapters), default=0) + 1
    chapters.append({"key": chapter, "title": chapter, "description": "",
                     "coverAssetId": "", "order": order})
    print(f"새 챕터 등록: {chapter} (order {order})")


RELATION_FILE = "_인물관계.md"
# 라벨에 이 말이 들어가면 그 유형의 선으로 그린다 (앞에서부터 먼저 맞는 것)
REL_KINDS = [
    ("blood",   ("혈연", "형제", "남매", "가족", "부녀", "부자")),
    ("hostile", ("적대", "억압", "대립", "반목")),
    ("secret",  ("밀약", "포섭", "감시", "거래", "이용")),
]


def parse_relations(path):
    """_인물관계.md → {factions, people, relations}."""
    factions, people, relations = [], [], []
    for n, raw in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or key not in ("세력", "인물", "관계"):
            continue
        parts = [x.strip() for x in value.split("|")]

        def reveal(field):
            m = re.search(r"(\d+)", field or "")
            return int(m.group(1)) if m else None

        if key == "세력":
            if len(parts) < 1 or not parts[0]:
                print(f"경고: {n}행 세력 형식 오류 → {line}")
                continue
            factions.append({"name": parts[0],
                             "note": parts[1] if len(parts) > 1 else ""})
        elif key == "인물":
            if len(parts) < 4:
                print(f"경고: {n}행 인물 형식은 '이름 | 세력 | 직함 | 공개: N편' → {line}")
                continue
            rv = reveal(parts[3])
            if rv is None:
                print(f"경고: {n}행 공개 편을 읽을 수 없습니다 → {parts[3]}")
                continue
            people.append({"name": parts[0], "faction": parts[1],
                           "title": parts[2], "reveal": rv})
        else:
            if len(parts) < 4:
                print(f"경고: {n}행 관계 형식은 'A - B | 라벨 | 설명 | 공개: N편' → {line}")
                continue
            a, dash, b = parts[0].partition("-")
            rv = reveal(parts[3])
            if not dash or not a.strip() or not b.strip() or rv is None:
                print(f"경고: {n}행 관계 형식 오류 → {line}")
                continue
            label = parts[1]
            kind = "plain"
            if label.startswith("?"):
                kind, label = "unknown", label.lstrip("?").strip()
            else:
                for name, words in REL_KINDS:
                    if any(w in label for w in words):
                        kind = name
                        break
            relations.append({"from": a.strip(), "to": b.strip(), "label": label,
                              "kind": kind, "note": parts[2], "reveal": rv})
    return {"factions": factions, "people": people, "relations": relations}


def cmd_relations(pack, path, dry_run, save=True):
    data = parse_relations(path)
    known = {p["name"] for p in data["people"]}
    fnames = {f["name"] for f in data["factions"]}
    for p in data["people"]:
        if p["faction"] not in fnames:
            print(f"경고: 등록되지 않은 세력 '{p['faction']}' (인물 {p['name']})")
    for r in data["relations"]:
        for side in (r["from"], r["to"]):
            if side not in known:
                print(f"경고: 인물 목록에 없는 이름 '{side}' (관계 {r['from']}-{r['to']})")
    pack.setdefault("meta", {})["relationBoard"] = data
    print(f"관계도 반영: 세력 {len(data['factions'])} / 인물 {len(data['people'])} "
          f"/ 관계 {len(data['relations'])}")
    if dry_run:
        print("(dry-run: 저장하지 않음)")
        return
    if save:
        save_if_changed(pack)


QUOTE_FILE = "_어록.md"


def parse_quotes(path):
    """_어록.md → [{text, note}]  (note는 출처 메모, 화면에는 안 나온다)"""
    quotes = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or key.strip() != "어록":
            continue
        text, _, note = value.partition("|")
        text, note = text.strip(), note.strip()
        if not text:
            print(f"경고: {n}행 어록이 비어 있습니다 → {line}")
            continue
        quotes.append({"text": text, "note": note} if note else {"text": text})
    return quotes


def cmd_quotes(pack, path, dry_run, save=True):
    quotes = parse_quotes(path)
    seen = {}
    for q in quotes:
        if q["text"] in seen:
            print(f"경고: 중복된 어록 → {q['text'][:40]}")
        seen[q["text"]] = True
    pack.setdefault("meta", {})["quotes"] = quotes
    print(f"어록 반영: {len(quotes)}개")
    if dry_run:
        print("(dry-run: 저장하지 않음)")
        return
    if save:
        save_if_changed(pack)


GLOSSARY_FILE = "_용어사전.md"


def parse_glossary(path):
    """_용어사전.md → [{term, description}]"""
    entries = []
    for n, raw in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or key.strip() != "용어":
            continue
        term, bar, desc = value.partition("|")
        term, desc = term.strip(), desc.strip()
        if not term or not bar:
            print(f"경고: {n}행 형식은 '용어: 이름 | 설명' 입니다 → {line}")
            continue
        entries.append({"term": term, "description": desc})
    return entries


def cmd_glossary(pack, path, dry_run, save=True):
    entries = parse_glossary(path)

    seen = set()
    for e in entries:
        if e["term"] in seen:
            print(f"경고: 중복된 용어 → {e['term']}")
        seen.add(e["term"])

    # 용어가 다른 용어를 통째로 품으면 자동 링크가 짧은 쪽으로 잘릴 수 있다.
    # 플레이어는 긴 것부터 찾지만, 작성 단계에서 한 번 알려준다.
    for e in entries:
        inner = [o["term"] for o in entries
                 if o["term"] != e["term"] and o["term"] in e["term"]]
        if inner:
            print(f"참고: '{e['term']}' 안에 '{', '.join(inner)}'가 들어 있습니다 "
                  f"(긴 용어를 먼저 링크하므로 정상 동작합니다)")

    pack["glossary"] = entries
    print(f"용어사전 반영: {len(entries)}개")
    if dry_run:
        print("(dry-run: 저장하지 않음)")
        return
    if save:
        save_if_changed(pack)


def ensure_eras(pack, past_events):
    """과거 시점 목록(meta.eras)을 갱신한다 — 플레이어가 이 순서로 연표를 세운다."""
    if not past_events:
        return
    meta = pack.setdefault("meta", {})
    eras = meta.setdefault("eras", [])
    for item in past_events:
        era = item.get("era")
        if era and era not in eras:
            eras.append(era)
            print(f"새 시점 등록: {era}")
    # 알려진 시점은 정해진 연대순으로, 나머지는 등록순으로 뒤에 붙인다
    known = [e for e in ERA_ORDER if e in eras]
    rest = [e for e in eras if e not in ERA_ORDER]
    meta["eras"] = known + rest


def cmd_list(pack):
    print(f"작품: {pack.get('meta', {}).get('title', '?')}  (exportedAt: {pack.get('exportedAt')})")
    episodes = sorted(pack.get("episodes", []),
                      key=lambda e: (int(e.get("order") or 0), e.get("createdAt", "")))
    by_chapter = {}
    for ep in episodes:
        by_chapter.setdefault(ep.get("chapter") or "(미분류)", []).append(ep)
    print(f"\n에피소드 {len(episodes)}개:")
    for chapter, eps in by_chapter.items():
        print(f" 《{chapter}》")
        for ep in eps:
            label = {"prologue": "프롤로그", "epilogue": "에필로그",
                     "minigame": "미니게임"}.get(ep.get("role"), f"{ep.get('order')}")
            print(f"  [{label}] {ep.get('title')}  — {ep.get('subtitle') or '(부제 없음)'}"
                  f"  (id: {ep.get('id')}, script {len(ep.get('script', ''))}자)")
    print(f"\n배경 에셋 {len(pack.get('assets', []))}개:")
    for a in pack.get("assets", []):
        where = a.get("src") or "내장(base64)"
        print(f"  {a['id']}  ({a.get('fileName')}, {a.get('size', 0) / 1024 / 1024:.1f}MB, {where})")
    print(f"\n용어사전 {len(pack.get('glossary', []))}개")


BG_DIR = PACK_PATH.parent.parent / "resource" / "bg"
DRAFT_DIR = PACK_PATH.parent.parent / "script"
MIME_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def cmd_add_bg(pack, image_path, id_name, dry_run):
    """이미지를 WebP로 변환해 resource/bg/ 에 두고 storypack에는 경로(src)만 등록한다.

    배경 전체를 무손실 PNG로 두던 시절엔 49장에 122MB였다(2026-07-27, WebP
    전환으로 13.7MB). 같은 문제가 새 배경에서 반복되지 않도록 여기서 항상
    WebP로 저장한다.
    """
    img = Path(image_path)
    if not img.exists():
        sys.exit(f"이미지를 찾을 수 없습니다: {img}")

    suffix = img.suffix.lower().lstrip(".")
    if suffix not in ("png", "jpg", "jpeg", "webp"):
        sys.exit(f"지원하지 않는 이미지 형식입니다: {img.suffix}")

    name = id_name or img.stem
    # id 관례: 지역명 기반 슬러그 ("북방주 술집_저녁" → "북방주_술집_저녁"), 중복 시 랜덤 접미사
    asset_id = name.replace(" ", "_")
    if any(a["id"] == asset_id for a in pack.get("assets", [])):
        alphabet = string.ascii_lowercase + string.digits
        asset_id += "_" + "".join(secrets.choice(alphabet) for _ in range(5))

    target = BG_DIR / f"{img.stem}.webp"
    if target.exists():
        alphabet = string.ascii_lowercase + string.digits
        target = BG_DIR / f"{img.stem}_{''.join(secrets.choice(alphabet) for _ in range(5))}.webp"

    print(f"배경 추가: {asset_id}  → resource/bg/{target.name}")
    print(f"대본에서 사용법: @배경 {asset_id}")
    if dry_run:
        print("(dry-run: 저장하지 않음)")
        return

    BG_DIR.mkdir(parents=True, exist_ok=True)
    if suffix == "webp":
        shutil.copy2(img, target)
    else:
        try:
            from PIL import Image
        except ImportError:
            sys.exit("PNG/JPG를 WebP로 바꾸려면 Pillow가 필요합니다: pip install Pillow\n"
                     "  (이미 WebP 파일이면 Pillow 없이도 그대로 추가할 수 있습니다)")
        Image.open(img).convert("RGB").save(target, "WEBP", quality=85, method=6)

    size = target.stat().st_size
    asset = {
        "id": asset_id,
        "name": name,
        "fileName": target.name,
        "mimeType": "image/webp",
        "size": size,
        "createdAt": now_iso(),
        "src": f"resource/bg/{target.name}",
    }
    print(f"  {size / 1024 / 1024:.2f}MB")
    pack["assets"].append(asset)
    save_if_changed(pack)


def cmd_externalize(pack, dry_run):
    """base64로 내장된 배경들을 resource/bg/ 파일로 추출하고 src 참조로 바꾼다."""
    changed = 0
    for a in pack.get("assets", []):
        data_url = a.get("dataUrl")
        if not data_url:
            continue
        ext = MIME_EXT.get(a.get("mimeType"), ".png")
        data = base64.b64decode(data_url.partition(",")[2])
        file_name = a.get("fileName") or f"{a['id']}{ext}"
        target = BG_DIR / file_name
        if target.exists() and target.read_bytes() != data:
            target = BG_DIR / f"{a['id']}{ext}"
        print(f"  {a['id']}: {len(data) / 1024 / 1024:.1f}MB → resource/bg/{target.name}")
        if not dry_run:
            BG_DIR.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            a["src"] = f"resource/bg/{target.name}"
            a["fileName"] = target.name
            del a["dataUrl"]
        changed += 1
    if not changed:
        print("내장(base64) 배경이 없습니다 — 변환할 것이 없습니다.")
        return
    if dry_run:
        print(f"(dry-run: {changed}개 변환 예정, 저장하지 않음)")
        return
    save_if_changed(pack)
    print(f"완료: 배경 {changed}개 분리")


def cmd_inject(pack, md_path, order, subtitle, cover, description, chapter, dry_run,
               save=True):
    title, script, ep_num, md_meta = parse_md(md_path)

    # 우선순위: CLI 플래그 > md 메타 줄 > 기존 값 유지
    if subtitle is None:
        subtitle = md_meta.get("subtitle")
    if cover is None:
        cover = md_meta.get("cover")
    if description is None:
        description = md_meta.get("description")
    if chapter is None:
        chapter = md_meta.get("chapter")
    if order is None and "order" in md_meta:
        try:
            # 미니게임은 화 사이에 끼어들어야 하므로 3.5 같은 소수도 받는다.
            # 정수로 적힌 값은 정수로 남겨 기존 팩과 diff가 생기지 않게 한다.
            value = float(md_meta["order"])
            order = int(value) if value.is_integer() else value
        except ValueError:
            print(f"경고: 순서 값이 숫자가 아닙니다: {md_meta['order']}")

    role = None
    if "role" in md_meta:
        role = ROLE_MAP.get(md_meta["role"].strip())
        if role is None:
            print(f"경고: 알 수 없는 역할 값: {md_meta['role']}"
                  " (프롤로그/에필로그/미니게임만 가능)")
        elif role == "minigame":
            # 미니게임은 화 사이에 끼어들므로 순서를 직접 정해야 한다.
            # 3화와 4화 사이에 두려면 '순서: 3.5'.
            if order is None:
                print("경고: 미니게임에는 '순서:' 줄이 필요합니다."
                      " (예: 3화와 4화 사이 → 순서: 3.5)")
        elif order is None:
            # 역할 에피소드의 순서는 정렬용 관례값 (플레이어는 role로 정렬)
            order = 0 if role == "prologue" else 99

    left = [f"{n}행: {l.strip()[:60]}"
            for n, l in enumerate(script.split("\n"), 1) if "<<확인" in l]
    if left:
        print(f"경고: 손질하지 않은 확인 마커 {len(left)}곳이 남아 있습니다 "
              f"— 그대로 배포하면 화면에 노출됩니다.")
        for item in left[:5]:
            print(f"  {item}")
        if len(left) > 5:
            print(f"  … 외 {len(left) - 5}곳")

    missing = check_assets(script, pack)
    if missing:
        print(f"경고: 팩에 없는 배경 id 참조 {len(missing)}건 → {', '.join(missing)}")
        print("  (--add-bg 로 배경을 먼저 추가하거나, 대본의 @배경 id를 확인하세요. 주입은 계속 진행합니다.)")

    episodes = pack.setdefault("episodes", [])
    # 제목으로 찾는다. 파일을 어느 폴더로 옮겨도 제목이 같으면 진행도가 유지된다.
    # 단 '챕터:'가 적혀 있으면 그 챕터 안에서 먼저 찾는다 — 1부와 2부에 똑같이
    # '잡음'을 두는 경우, 제목만 보면 2부 것을 넣을 때 1부 것을 덮어쓴다.
    same_title = [e for e in episodes if e.get("title") == title]
    if chapter:
        key = chapter.strip()
        in_chapter = [e for e in same_title
                      if str(e.get("chapter") or "").strip() == key]
        existing = in_chapter[0] if in_chapter else None
        if existing is None and same_title:
            print(f"참고: 같은 제목 '{title}'이 다른 챕터에 {len(same_title)}개 있습니다."
                  f" 챕터 '{key}'의 새 항목으로 추가합니다.")
            print("  (챕터를 옮기려던 것이라면 storypack.json의 chapter를 직접 고칠 것)")
    else:
        existing = same_title[0] if same_title else None
    now = now_iso()

    if existing:
        action = "갱신"
        before = copy.deepcopy(existing)
        existing["script"] = script
        if order is not None:
            existing["order"] = order
        if subtitle is not None:
            existing["subtitle"] = subtitle
        if cover is not None:
            existing["coverAssetId"] = cover
        if description is not None:
            existing["description"] = description
        if chapter is not None:
            existing["chapter"] = chapter
        if role is not None:
            existing["role"] = role
        if "focal" in md_meta:
            existing["focal"] = md_meta["focal"]
        if "event" in md_meta:
            existing["event"] = md_meta["event"]
        if "eventDate" in md_meta:
            existing["eventDate"] = md_meta["eventDate"]
        for key in ("game", "gameTarget", "gameMessage", "gameBrief"):
            if key in md_meta:
                existing[key] = md_meta[key]
        # 과거사건 줄을 모두 지운 경우도 반영되도록 항상 덮어쓴다
        if md_meta.get("pastEvents"):
            existing["pastEvents"] = md_meta["pastEvents"]
        else:
            existing.pop("pastEvents", None)
        # 내용이 그대로면 updatedAt도 건드리지 않는다 (배포마다 diff가 지저분해지는 것 방지)
        if existing == before:
            action = "변경 없음"
        else:
            existing["updatedAt"] = now
        target = existing
    else:
        action = "신규 추가"
        if order is None:
            order = ep_num if ep_num else max((int(e.get("order") or 0) for e in episodes), default=0) + 1
        clash = next((e for e in episodes if int(e.get("order") or 0) == order), None)
        if clash:
            print(f"경고: order {order}는 이미 '{clash.get('title')}'가 사용 중입니다. "
                  f"같은 order는 등록순으로 정렬됩니다.")
        target = {
            "id": "episode_" + secrets.token_hex(6),
            "order": order,
            "chapter": chapter if chapter is not None else default_chapter(pack),
            **({"role": role} if role else {}),
            **({"focal": md_meta["focal"]} if "focal" in md_meta else {}),
            **({"event": md_meta["event"]} if "event" in md_meta else {}),
            **({"eventDate": md_meta["eventDate"]} if "eventDate" in md_meta else {}),
            **({"pastEvents": md_meta["pastEvents"]} if md_meta.get("pastEvents") else {}),
            **{key: md_meta[key] for key in
               ("game", "gameTarget", "gameMessage", "gameBrief") if key in md_meta},
            "title": title,
            "subtitle": subtitle or "",
            "description": description or "",
            "coverAssetId": cover or "",
            "script": script,
            "createdAt": now,
            "updatedAt": now,
        }
        episodes.append(target)

    ensure_chapter(pack, target.get("chapter"))
    ensure_eras(pack, target.get("pastEvents"))

    # 주역 인물의 기호를 팩 공용 매핑(meta.motifs)에 등록
    if "motif" in md_meta and target.get("focal"):
        motifs = pack.setdefault("meta", {}).setdefault("motifs", {})
        if motifs.get(target["focal"]) != md_meta["motif"]:
            motifs[target["focal"]] = md_meta["motif"]
            print(f"기호 등록: {target['focal']} → {md_meta['motif']}")

    speakers = sorted({line.split("|")[0].strip() for line in script.split("\n")
                       if "|" in line and not line.startswith(("(", "@", "-"))})
    print(f"{action}: [{target['order']}] {title}  (챕터: {target.get('chapter') or '미분류'})")
    print(f"  script {len(script)}자, 화자: {', '.join(speakers) or '(없음)'}")
    if dry_run:
        print("(dry-run: 저장하지 않음)")
        return
    if save:
        save_if_changed(pack)


def cmd_inject_all(pack, dry_run):
    """script 폴더(하위 폴더 포함)의 모든 md를 반영하고 한 번만 저장한다."""
    md_files = sorted(DRAFT_DIR.rglob("*.md"),
                      key=lambda p: (str(p.parent), len(p.stem.split("편")[0]), p.stem))
    if not md_files:
        sys.exit(f"드래프트 폴더에 md가 없습니다: {DRAFT_DIR}")
    # '_'로 시작하는 파일은 에피소드가 아니라 부속 데이터다
    for md in md_files:
        if md.name == RELATION_FILE:
            cmd_relations(pack, md, dry_run, save=False)
        elif md.name == QUOTE_FILE:
            cmd_quotes(pack, md, dry_run, save=False)
        elif md.name == GLOSSARY_FILE:
            cmd_glossary(pack, md, dry_run, save=False)
        elif md.name.startswith("_"):
            print(f"건너뜀(에피소드 아님): {md.name}")
        else:
            cmd_inject(pack, md, None, None, None, None, None, dry_run, save=False)
    if dry_run:
        print(f"\n(dry-run: {len(md_files)}개 파일 확인만 함)")
        return
    save_if_changed(pack)
    print(f"\n총 {len(md_files)}개 대본 반영 완료")


def main():
    ap = argparse.ArgumentParser(description="storypack.json 에피소드/배경 주입 CLI")
    ap.add_argument("md", nargs="?", help="주입할 대본 md 파일")
    ap.add_argument("--order", type=int, help="에피소드 순서 (기본: 기존 최대+1, 갱신 시 유지)")
    ap.add_argument("--chapter", help="챕터명 (신규 기본: 팩의 첫 챕터, 갱신 시 유지)")
    ap.add_argument("--subtitle", help="부제")
    ap.add_argument("--description", help="설명")
    ap.add_argument("--cover", help="커버 배경 asset id")
    ap.add_argument("--add-bg", metavar="IMAGE", help="배경 이미지를 에셋으로 추가 (resource/bg 경로 참조)")
    ap.add_argument("--id-name", help="--add-bg 시 에셋 이름 (기본: 파일명)")
    ap.add_argument("--all", action="store_true",
                    help="드래프트 폴더의 모든 md를 한 번에 반영")
    ap.add_argument("--externalize", action="store_true",
                    help="팩에 base64로 내장된 배경들을 resource/bg/ 파일로 분리")
    ap.add_argument("--list", action="store_true", help="현재 팩의 에피소드/에셋 목록 출력")
    ap.add_argument("--dry-run", action="store_true", help="변경 내용만 출력하고 저장하지 않음")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    pack = load_pack()

    if args.list:
        cmd_list(pack)
    elif args.all:
        cmd_inject_all(pack, args.dry_run)
    elif args.externalize:
        cmd_externalize(pack, args.dry_run)
    elif args.add_bg:
        cmd_add_bg(pack, args.add_bg, args.id_name, args.dry_run)
    elif args.md:
        if Path(args.md).name == QUOTE_FILE:
            cmd_quotes(pack, Path(args.md), args.dry_run)
        elif Path(args.md).name == GLOSSARY_FILE:
            cmd_glossary(pack, Path(args.md), args.dry_run)
        elif Path(args.md).name == RELATION_FILE:
            cmd_relations(pack, Path(args.md), args.dry_run)
        else:
            cmd_inject(pack, args.md, args.order, args.subtitle, args.cover, args.description,
                       args.chapter, args.dry_run)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
